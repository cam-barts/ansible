#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "cyclopts>=3.0",
#     "httpx>=0.27",
#     "pydantic>=2.6",
#     "pydantic-settings>=2.2",
#     "structlog>=24.1",
#     "pyyaml>=6.0",
#     "markdown-it-py>=3.0",
# ]
# ///
"""Indexer sidecar: read SilverBullet space pages, push to Meilisearch.

Two commands:
    indexer.py reindex   # full pass, then exit
    indexer.py serve     # full pass, then poll forever

Two page sources, picked at runtime by `make_source()`:
    * SpaceReader (default in the consolidated SB compose) — reads markdown
      straight off the bind-mounted space/ dir (SIDECAR_SPACE_DIR). No HTTP
      round-trip; the poll loop is a filesystem mtime scan.
    * SbClient (fallback) — talks to SilverBullet's `/.fs` endpoint over HTTP,
      for running the sidecar detached from the space dir.

Either way SB owns the write path, so we never see partial-write or
rename-race states the way an inotify watcher would.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import cyclopts
import httpx
import structlog
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str
    sidecar_index_name: str = "silverbullet"
    sidecar_poll_interval: int = 15
    sidecar_exclude_prefixes: str = ""

    # Filesystem source — when set, read pages from this dir (the bind-mounted
    # space/) instead of pulling over HTTP. Empty = fall back to HTTP (SbClient).
    sidecar_space_dir: str = ""

    # HTTP source creds — only required when sidecar_space_dir is unset. Default
    # empty so filesystem-only mode needs no SB token.
    sb_url: str = ""
    sb_auth_token: str = ""
    sb_user: str = "nux"

    # Secondary blog corpus — mounted into the container at SIDECAR_BLOG_DIR,
    # indexed into SIDECAR_BLOG_INDEX_NAME by the `reindex-blog` command.
    sidecar_blog_dir: str = ""
    sidecar_blog_index_name: str = "blog"
    sidecar_blog_exclude_prefixes: str = ""

    @property
    def exclude_prefixes(self) -> list[str]:
        return [p.strip() for p in self.sidecar_exclude_prefixes.split(",") if p.strip()]

    @property
    def blog_exclude_prefixes(self) -> list[str]:
        return [p.strip() for p in self.sidecar_blog_exclude_prefixes.split(",") if p.strip()]


class FsEntry(BaseModel):
    name: str
    created: int
    last_modified: int = Field(alias="lastModified")
    content_type: str = Field(alias="contentType")
    size: int
    perm: str | None = None


class IndexDoc(BaseModel):
    id: str
    path: str
    title: str
    body: str
    # --- chunking (notes only; the blog corpus still indexes one doc per file)
    # Which chunk of the note this is. 0 for an unchunked doc.
    chunk: int = 0
    # Enclosing markdown heading text, no leading '#'. Used to build the
    # result deep link: bullet.coder.cam/<path>%23<heading>.
    heading: str | None = None
    # Character offset of this chunk into the RAW file, frontmatter included,
    # in UTF-16 code units. SilverBullet's `@<pos>` ref counts the document as
    # the editor sees it, and CodeMirror indexes in UTF-16, so a note with a
    # non-BMP emoji above the chunk would be off by one per emoji if we used
    # Python character offsets. Fallback link target when `heading` is None.
    pos: int = 0
    # Pre-built SilverBullet ref, already percent-encoded, e.g.
    # `Homelab/SearXNG.md%23Current%20engine%20lineup`. Consumers append it to
    # https://bullet.coder.cam/ . Built here because SearXNG's json_engine can
    # read only ONE field into the result URL and cannot concatenate
    # path + heading. Same reason podcast_indexer.py stores a whole `url`.
    ref: str = ""
    tags: list[str] = Field(default_factory=list)
    # Note aliases lifted from frontmatter (`aliases:` list or `alias:` string).
    # Indexed as a searchable attribute so a note titled "Confirmation Bias"
    # with alias "Myside Bias" surfaces for searches against either name.
    aliases: list[str] = Field(default_factory=list)
    state: str | None = None
    extension: str
    folder: str
    last_modified: int = Field(alias="lastModified")
    size: int

    model_config = {"populate_by_name": True}


log = structlog.get_logger()

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]*)")


def stable_id(path: str) -> str:
    """Meilisearch doc IDs only allow [a-zA-Z0-9_-]. Hash the path."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:24]


def chunk_id(path: str, chunk: int) -> str:
    """Doc id for chunk `chunk` of `path`. Same scheme podcast_indexer.py uses.

    NOTE: ids are no longer a 1:1 function of path. `index_pass()` reconciles
    deletions by PATH for exactly this reason; see the comment there.
    """
    return hashlib.sha1(f"{path}:{chunk}".encode("utf-8")).hexdigest()[:24]


CHUNK_CAP = 900
CHUNK_HARD_SPLIT = 1400

# Block types that belong to the paragraph immediately above them. This is the
# whole point of the chunker: 36% of Cam's notes have a paragraph introducing a
# list, and a naive blank-line splitter keeps the pair together in 0.8% of
# cases. Gluing these to the preceding paragraph keeps 100%.
_ATTACHES_UP = {"bullet_list", "ordered_list", "table", "fence", "code_block", "blockquote"}


def utf16_len(s: str) -> int:
    """Length in UTF-16 code units, which is how CodeMirror (and therefore
    SilverBullet's `@<pos>` ref) counts positions. Differs from len() only for
    non-BMP characters, i.e. emoji."""
    return len(s.encode("utf-16-le")) // 2


def _blocks(md_text: str) -> list[tuple[int, int, str]]:
    """Top-level markdown blocks as (start_line, end_line, type).

    markdown-it gives us real block boundaries, which matters because a regex
    approach swallows indented fences and produces absurd 2 MB "list" blocks on
    the Zotero imports.
    """
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").parse(md_text)
    out: list[tuple[int, int, str]] = []
    for t in tokens:
        if t.level != 0 or t.map is None:
            continue
        if t.nesting == -1:  # closing token of a container we already recorded
            continue
        out.append((t.map[0], t.map[1], t.type.removesuffix("_open")))
    return out


def chunk_body(body: str, body_offset_u16: int) -> list[tuple[str, str | None, int]]:
    """Split a note body into (text, heading, utf16_pos) chunks.

    `body_offset_u16` is where `body` starts inside the raw file, so the
    returned positions address the whole document (frontmatter included), which
    is what SilverBullet's editor sees.

    Strategy: group each paragraph with any list/table/fence that follows it,
    never split a group, pack groups up to CHUNK_CAP, hard-split anything
    atomic that exceeds CHUNK_HARD_SPLIT.
    """
    lines = body.splitlines(keepends=True)
    if not lines:
        return []

    # utf16 offset of the start of each line, so a block maps to a position.
    line_pos: list[int] = []
    acc = body_offset_u16
    for ln in lines:
        line_pos.append(acc)
        acc += utf16_len(ln)
    line_pos.append(acc)

    blocks = _blocks(body)
    if not blocks:
        text = body.strip()
        return [(text, None, body_offset_u16)] if text else []

    # Group: a block that attaches upward merges into the previous group.
    groups: list[tuple[int, int, str | None]] = []  # (start_line, end_line, heading)
    heading: str | None = None
    for start, end, typ in blocks:
        if typ == "heading":
            heading = "".join(lines[start:end]).strip().lstrip("#").strip()
            groups.append((start, end, heading))
            continue
        # Attach upward across intervening BLANK lines, not just when the
        # blocks are line-adjacent. markdown-it's `map` excludes the blank line
        # between a paragraph and the list under it, so an exact
        # `prev_end == start` test only ever caught the tight, no-blank-line
        # form. That silently halved the whole point of the chunker: measured
        # 31.4% of paragraph/list pairs kept together instead of 100%.
        if (
            groups
            and typ in _ATTACHES_UP
            and all(not lines[i].strip() for i in range(groups[-1][1], start))
        ):
            ps, _, ph = groups[-1]
            groups[-1] = (ps, end, ph)
        else:
            groups.append((start, end, heading))

    # Pack groups toward the cap, never splitting one, never crossing a heading.
    chunks: list[tuple[str, str | None, int]] = []
    cur_start: int | None = None
    cur_end = 0
    cur_head: str | None = None

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_head
        if cur_start is None:
            return
        text = "".join(lines[cur_start:cur_end]).strip()
        if text:
            chunks.append((text, cur_head, line_pos[cur_start]))
        cur_start = None

    for start, end, head in groups:
        raw_len = utf16_len("".join(lines[start:end]))
        if raw_len > CHUNK_HARD_SPLIT:
            # Atomic but oversized (a 2 MB verbatim transcript, a giant table).
            # Split it on line boundaries; there is no structure left to respect.
            flush()
            seg_start = start
            seg_len = 0
            for i in range(start, end):
                line_u16 = utf16_len(lines[i])
                if line_u16 > CHUNK_HARD_SPLIT:
                    # A single line longer than the cap: a wrapped-off prose
                    # paragraph, a base64 blob, a one-line table. Line
                    # boundaries cannot help, so cut inside the line. Without
                    # this a 37,606-char chunk got through a 1,400 cap, which
                    # would blow past documentTemplateMaxBytes and silently
                    # truncate the embedding again.
                    if seg_start < i:
                        text = "".join(lines[seg_start:i]).strip()
                        if text:
                            chunks.append((text, head, line_pos[seg_start]))
                    ln = lines[i]
                    base = line_pos[i]
                    for off in range(0, len(ln), CHUNK_CAP):
                        piece = ln[off:off + CHUNK_CAP].strip()
                        if piece:
                            chunks.append((piece, head, base + utf16_len(ln[:off])))
                    seg_start = i + 1
                    seg_len = 0
                    continue
                seg_len += line_u16
                if seg_len >= CHUNK_CAP:
                    text = "".join(lines[seg_start:i + 1]).strip()
                    if text:
                        chunks.append((text, head, line_pos[seg_start]))
                    seg_start = i + 1
                    seg_len = 0
            if seg_start < end:
                text = "".join(lines[seg_start:end]).strip()
                if text:
                    chunks.append((text, head, line_pos[seg_start]))
            continue
        if cur_start is not None and (
            head != cur_head
            or utf16_len("".join(lines[cur_start:end])) > CHUNK_CAP
        ):
            flush()
        if cur_start is None:
            cur_start, cur_head = start, head
        cur_end = end
    flush()
    return chunks


def parse_page(path: str, raw: str, last_modified: int, size: int) -> IndexDoc:
    body = raw
    frontmatter: dict = {}
    if m := FRONTMATTER_RE.match(raw):
        try:
            fm = yaml.safe_load(m.group(1)) or {}
            if isinstance(fm, dict):
                frontmatter = fm
        except yaml.YAMLError:
            frontmatter = {}
        body = raw[m.end() :]

    tags: list[str] = []
    fm_tags = frontmatter.get("tags")
    if isinstance(fm_tags, str):
        tags.extend(t.strip() for t in fm_tags.split(",") if t.strip())
    elif isinstance(fm_tags, list):
        tags.extend(str(t) for t in fm_tags)
    tags.extend(TAG_RE.findall(body))
    tags = sorted(set(tags))

    # Aliases — SB convention is a YAML list under `aliases:`; some pages
    # use the singular `alias:` form with a single string. Handle both.
    aliases: list[str] = []
    for key in ("aliases", "alias"):
        v = frontmatter.get(key)
        if isinstance(v, str):
            aliases.append(v.strip())
        elif isinstance(v, list):
            aliases.extend(str(a).strip() for a in v if str(a).strip())
    aliases = sorted({a for a in aliases if a})

    state_val = frontmatter.get("state")
    state = str(state_val) if state_val is not None else None

    folder = "/".join(path.split("/")[:-1]) or "/"
    title = path.rsplit("/", 1)[-1]
    if title.lower().endswith(".md"):
        title = title[:-3]
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""

    return IndexDoc(
        id=stable_id(path),
        path=path,
        title=title,
        body=body,
        tags=tags,
        aliases=aliases,
        state=state,
        extension=ext,
        folder=folder,
        last_modified=last_modified,
        size=size,
    )


def parse_page_chunked(path: str, raw: str, last_modified: int, size: int) -> list[IndexDoc]:
    """Notes path: one IndexDoc per chunk, sharing the note's metadata.

    Deliberately a wrapper rather than a change to `parse_page`, because
    `reindex_blog()` calls `parse_page()` directly and the blog corpus (23
    files) has no reason to be chunked. Keeping the single-doc contract intact
    is what gates chunking to the notes index.
    """
    base = parse_page(path, raw, last_modified, size)
    m = FRONTMATTER_RE.match(raw)
    body_offset = utf16_len(raw[: m.end()]) if m else 0

    pieces = chunk_body(base.body, body_offset)
    if not pieces:
        # Empty or frontmatter-only note. Keep one doc so the note is still
        # findable by title/tags rather than vanishing from the index.
        pieces = [("", None, body_offset)]

    docs: list[IndexDoc] = []
    quoted_path = quote(path, safe="/")
    for i, (text, heading, pos) in enumerate(pieces):
        # Prefer the heading ref. A stale heading fails loudly (SB shows
        # `Could not find header "..."` and leaves you at the top); a stale
        # offset is silently clamped to the document length, dumping the reader
        # at the end of the note with no indication anything went wrong.
        # `%23` rather than a bare `#` because that is what SB itself emits.
        if heading:
            ref = f"{quoted_path}%23{quote(heading, safe='')}"
        else:
            ref = f"{quoted_path}@{pos}" if pos else quoted_path
        docs.append(
            base.model_copy(
                update={
                    "id": chunk_id(path, i),
                    "body": text,
                    "chunk": i,
                    "heading": heading,
                    "pos": pos,
                    "ref": ref,
                }
            )
        )
    return docs


class SbClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.sb_url,
            headers={
                "Authorization": f"Bearer {settings.sb_auth_token}",
                # SB serves the SPA on /.fs unless this header is set, in
                # which case it returns the file-listing JSON (and raw page
                # content on /.fs/{path}). Discovered by reading the
                # silverbulletmd/silverbullet server source.
                "X-Sync-Mode": "true",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    def list_files(self) -> list[FsEntry]:
        r = self.client.get("/.fs")
        r.raise_for_status()
        return [FsEntry.model_validate(e) for e in r.json()]

    def fetch_page(self, path: str) -> str:
        from urllib.parse import quote
        r = self.client.get(f"/.fs/{quote(path)}")
        r.raise_for_status()
        return r.text


class SpaceReader:
    """Filesystem-backed page source — the bind-mounted SilverBullet space/ dir.

    Walks SIDECAR_SPACE_DIR the way SbClient walks SB's /.fs, returning the same
    FsEntry shape so index_pass() stays source-agnostic. Reading files straight
    off disk (no HTTP round-trip through SilverBullet) is the whole reason the
    sidecar now lives in the SB compose: the space/ dir is already on the host.

    Two fidelity rules keep this byte-for-byte compatible with the HTTP path —
    verified by diffing this walk against a live /.fs listing:

      * Skip any path with a dot-prefixed component (`.git/`, `.planning/`,
        `.chrome-data/`, `.foo.tmp.md`). SB's /.fs omits dotfiles/dot-dirs; a
        naive rglob would otherwise inject ~65 planning/junk pages.
      * Report lastModified in MILLISECONDS (st_mtime * 1000). SB's /.fs and the
        stored Meilisearch docs both use ms; the incremental mtime compare in
        index_pass() only works if both sides agree on the unit. (Seconds here
        would make every poll a no-op and never pick up edits.)
    """

    def __init__(self, settings: Settings):
        self.root = Path(settings.sidecar_space_dir)

    @staticmethod
    def _has_dot_component(rel: str) -> bool:
        return any(part.startswith(".") for part in rel.split("/"))

    def list_files(self) -> list[FsEntry]:
        out: list[FsEntry] = []
        for p in self.root.rglob("*.md"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if self._has_dot_component(rel):
                continue
            st = p.stat()
            out.append(FsEntry(
                name=rel,
                created=int(st.st_ctime * 1000),
                lastModified=int(st.st_mtime * 1000),
                contentType="text/markdown",
                size=st.st_size,
            ))
        return out

    def fetch_page(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8", errors="replace")


def make_source(settings: Settings) -> SbClient | SpaceReader:
    """Pick the page source. Filesystem (SpaceReader) when SIDECAR_SPACE_DIR is
    set — the default in the consolidated SB compose; HTTP (SbClient) otherwise,
    kept as a fallback for running the sidecar detached from the space dir."""
    if settings.sidecar_space_dir:
        log.info("source_filesystem", space_dir=settings.sidecar_space_dir)
        return SpaceReader(settings)
    log.info("source_http", sb_url=settings.sb_url)
    return SbClient(settings)


class MeiliClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.meili_url,
            headers={
                "Authorization": f"Bearer {settings.meili_master_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
        self.index = settings.sidecar_index_name

    def wait_healthy(self, timeout_s: int = 60) -> None:
        deadline = time.time() + timeout_s
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                r = self.client.get("/health")
                if r.status_code == 200 and r.json().get("status") == "available":
                    return
            except httpx.HTTPError as e:
                last_err = e
            time.sleep(1)
        raise RuntimeError(f"Meilisearch never became healthy: {last_err}")

    def ensure_index(self) -> None:
        """Idempotently configure the index. Safe to run on every boot — Meilisearch
        diffs settings and only enqueues a re-vectorization if the embedder
        template actually changed."""
        r = self.client.get(f"/indexes/{self.index}")
        if r.status_code == 404:
            self.client.post(
                "/indexes", json={"uid": self.index, "primaryKey": "id"}
            ).raise_for_status()

        # Core search settings — attributes, sort, ranking, display
        self.client.patch(
            f"/indexes/{self.index}/settings",
            json={
                # Order matters — earlier attributes rank higher under the
                # `attribute` ranking rule. Aliases sit right after title so
                # an alias match scores comparably to the canonical name.
                "searchableAttributes": [
                    "title", "aliases", "tags", "body", "path", "folder",
                ],
                # `path` is filterable so search-time `distinct=path` works:
                # without it a query matching one long note would fill the
                # whole result page with chunks of that single note.
                "filterableAttributes": [
                    "tags", "state", "folder", "extension", "lastModified", "size",
                    "path",
                ],
                # Document field is `lastModified` (camelCase, by_alias=True
                # on IndexDoc.model_dump). Sortable attrs and ranking rule
                # tiebreaker must match that exact name — earlier versions
                # had `last_modified` (snake_case) here which silently no-op'd
                # because Meilisearch didn't find that field on the docs.
                "sortableAttributes": ["lastModified", "size"],
                "rankingRules": [
                    "words",
                    "typo",
                    "proximity",
                    "attribute",
                    "sort",
                    "exactness",
                    "lastModified:desc",
                ],
                "displayedAttributes": [
                    "id", "path", "title", "body", "tags", "aliases", "state",
                    "folder", "extension", "last_modified", "size",
                    # Consumers build the deep link from these; `ref` is the
                    # ready-made one that SearXNG's json_engine consumes.
                    "chunk", "heading", "pos", "ref",
                ],
            },
        ).raise_for_status()

        # Typo tolerance — disable on exact-match attrs (tags, paths, etc.)
        self.client.patch(
            f"/indexes/{self.index}/settings/typo-tolerance",
            json={
                "enabled": True,
                "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9},
                "disableOnAttributes": [
                    "tags", "extension", "folder", "path", "state",
                ],
            },
        ).raise_for_status()

        # Synonyms — Cam's personal shorthand + common abbreviations
        self.client.put(
            f"/indexes/{self.index}/settings/synonyms",
            json={
                "kb": ["knowledge base", "knowledgebase"],
                "knowledge base": ["kb", "knowledgebase"],
                "zk": ["zettelkasten"],
                "zettelkasten": ["zk"],
                "sb": ["silverbullet"],
                "silverbullet": ["sb"],
                "llm": ["large language model", "language model"],
                "large language model": ["llm"],
                "vm": ["virtual machine"],
                "ci": ["continuous integration"],
                "cd": ["continuous deployment"],
                "k8s": ["kubernetes"],
                "kubernetes": ["k8s"],
                "auth": ["authentication", "authorization"],
                "doc": ["document"],
                "docs": ["documentation"],
                "config": ["configuration"],
                "env": ["environment"],
                "repo": ["repository"],
                "wmill": ["windmill"],
                "windmill": ["wmill"],
            },
        ).raise_for_status()

        # Embedder — nomic-embed-text via Ollama. host.docker.internal resolves to
        # the warrig docker0 gateway; Ollama listens on 11434 on the host. Picked
        # nomic over bge-m3 because the latter emits NaN under concurrent embed
        # load on Ollama 0.24 (flash attention compat). Re-evaluate when Ollama
        # ships a fix.
        self.client.patch(
            f"/indexes/{self.index}/settings/embedders",
            json={
                "nomic": {
                    "source": "ollama",
                    "url": "http://host.docker.internal:11434/api/embeddings",
                    "model": "nomic-embed-text",
                    "dimensions": 768,
                    # Embed the chunk WHOLE. The old template was
                    # `{{ doc.body | truncatewords: 80 }}` and, worse, relied
                    # on Meilisearch's default documentTemplateMaxBytes of 400
                    # — the byte cap binds long before the word cap, so every
                    # note was embedded from roughly its first 400 bytes, ~16%
                    # of a median note. Measured recall@1 was 0.233. Chunks cap
                    # at CHUNK_HARD_SPLIT (1400 UTF-16 units), so 4000 bytes
                    # covers the worst case even with multibyte expansion.
                    "documentTemplate": (
                        "{{ doc.title }}"
                        "{% if doc.heading %} / {{ doc.heading }}{% endif %}. "
                        "Tags: {{ doc.tags }}. {{ doc.body }}"
                    ),
                    "documentTemplateMaxBytes": 4000,
                },
            },
        ).raise_for_status()

        # One-time-ish cleanup: evict any meta-tagged pages that were indexed
        # before the meta-skip rule landed. Idempotent — if there are none,
        # this enqueues a no-op deletion. Cam's convention: `tags = meta` (and
        # `meta/*` subtags like `meta/template/page`, `meta/library`) marks
        # pages that should be hidden from the picker AND from search.
        try:
            self.delete_by_filter(
                'tags = "meta" OR tags = "meta/template/page" '
                'OR tags = "meta/template/slash" OR tags = "meta/library" '
                'OR tags = "meta/api"'
            )
        except httpx.HTTPStatusError as exc:
            # If filterableAttributes hasn't propagated yet (first-ever
            # start), the filter rejects — fine to swallow on first boot.
            log.warning("meta_purge_skipped", error=str(exc))

    # Meilisearch v1.21 silently loses the vector of the LAST document in every
    # complete 41-document sub-chunk of each 100-document embedding group. It is
    # not a flaky embedder: Ollama answered 1,860 probe requests at concurrency
    # 1..64 with zero failures, and the loss is bit-for-bit reproducible. Measured
    # on a throwaway index with production's embedder config (2026-10-13):
    #
    #   docs pushed in one task | 40 | 41 | 82 | 100 | 141 | 200
    #   documents left unvectorised |  0 |  1 |  2 |   2 |   3 |   4
    #
    # and 954 docs pushed 41-at-a-time lost exactly 23 (= 954 // 41). Three
    # identical repeats of the 200-doc arm lost the same 19 ids every time.
    #
    # So: stay under the 41 boundary. BOTH halves are needed. Pushing 40 at a
    # time but NOT waiting still lost 18 of 954, because Meilisearch auto-batches
    # consecutive enqueued addDocuments tasks and merges them back into groups
    # over 41 — which is also why the loss looked random in production, where
    # batch grouping differs run to run. Waiting for each task keeps the group
    # exactly as pushed; 954 docs at 40-with-wait lost 0, twice.
    #
    # COST, measured rather than estimated (an earlier note here said ~1.1 s per
    # task and ~20 min for a full reindex; both were extrapolated from the first
    # 75 seconds and were wrong). Waiting per task defeats Meilisearch's
    # auto-batching, which exists to merge queued tasks into ONE LMDB commit, so
    # we now pay a full commit per 40 documents and the cost grows with index
    # size: ~1.3 s/task early, 5.34 s/task at 21k documents in, settling around
    # 7.5 docs/s overall, with ~24 GB written for a 3.8 GB index (~6x write
    # amplification). A 48k-document rebuild is therefore ~1.8 h, not 20 min.
    # Meilisearch sat at 1.16% CPU throughout, so this is IO, not compute.
    #
    # Incremental passes touch a handful of documents and pay almost nothing,
    # which is why this is still the right default. If full-rebuild time ever
    # matters, the thing to test is waiting only until the task leaves
    # "enqueued" (batches look immutable once formed, so that may keep groups at
    # 40 while letting commits pipeline) rather than waiting for "succeeded".
    EMBED_SAFE_BATCH = 40

    def upsert(self, docs: list[IndexDoc]) -> dict:
        """Upsert documents, never letting Meilisearch see a group over the
        41-document embedding boundary. Splits here rather than at the call
        sites because callers batch by page and a single page can contribute
        dozens of chunks, so their batch length overshoots any cap they set.
        Returns the last sub-task, which is the only one still interesting
        once every sub-task has been waited on."""
        task: dict = {}
        for i in range(0, len(docs), self.EMBED_SAFE_BATCH):
            payload = [
                d.model_dump(by_alias=True)
                for d in docs[i : i + self.EMBED_SAFE_BATCH]
            ]
            r = self.client.post(f"/indexes/{self.index}/documents", json=payload)
            r.raise_for_status()
            task = r.json()
            done = self.wait_for_task(task["taskUid"], poll_s=0.2)
            if done.get("status") != "succeeded":
                log.error(
                    "upsert_task_failed",
                    task_uid=task["taskUid"],
                    status=done.get("status"),
                    error=done.get("error"),
                )
        return task

    def delete_by_filter(self, filter_expr: str) -> dict:
        """Delete all docs matching the Meilisearch filter expression. Used
        to evict meta-tagged pages from prior indexes after the meta-skip
        rule landed."""
        r = self.client.post(
            f"/indexes/{self.index}/documents/delete",
            json={"filter": filter_expr},
        )
        r.raise_for_status()
        return r.json()

    def delete_many(self, ids: list[str]) -> dict | None:
        if not ids:
            return None
        r = self.client.post(f"/indexes/{self.index}/documents/delete-batch", json=ids)
        r.raise_for_status()
        return r.json()

    def known_doc_index(self) -> dict[str, tuple[str, int]]:
        """Return {id: (path, last_modified)} for everything currently indexed.

        `path` is fetched because documents are chunks now and ids are no
        longer 1:1 with paths, so reconciliation has to group by path.
        """
        known: dict[str, tuple[str, int]] = {}
        offset = 0
        limit = 1000
        while True:
            r = self.client.get(
                f"/indexes/{self.index}/documents",
                params={
                    "limit": limit,
                    "offset": offset,
                    "fields": "id,path,lastModified",
                },
            )
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            for doc in results:
                # Meilisearch returns the by_alias key (`lastModified`); we
                # serialized that way to keep the SB-side JSON readable.
                known[doc["id"]] = (doc.get("path", ""), doc.get("lastModified", 0))
            if len(results) < limit:
                break
            offset += limit
        return known

    def stats(self) -> dict:
        r = self.client.get(f"/indexes/{self.index}/stats")
        r.raise_for_status()
        return r.json()

    def swap_with(self, other_index: str) -> dict:
        """Atomically swap `self.index` and `other_index`. Settings, docs, task
        history all rotate in one go (no downtime window for queries). Returns
        the enqueued task payload."""
        r = self.client.post(
            "/swap-indexes",
            json=[{"indexes": [self.index, other_index]}],
        )
        r.raise_for_status()
        return r.json()

    def delete_index(self, index_name: str) -> dict:
        """Drop an index entirely. Used to clean up the post-swap stale index."""
        r = self.client.delete(f"/indexes/{index_name}")
        r.raise_for_status()
        return r.json()

    def wait_for_task(
        self, task_uid: int, timeout_s: int = 600, poll_s: float = 1.0
    ) -> dict:
        """Block until a Meilisearch task hits a terminal state."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self.client.get(f"/tasks/{task_uid}")
            r.raise_for_status()
            data = r.json()
            status = data.get("status")
            if status in ("succeeded", "failed", "canceled"):
                return data
            time.sleep(poll_s)
        raise RuntimeError(f"Task {task_uid} timed out after {timeout_s}s")

    def wait_index_idle(self, timeout_s: int = 600, poll_s: float = 1.0) -> None:
        """Block until isIndexing flips false. Covers async embedder work that
        doesn't surface a single taskUid (re-vectorization happens implicitly
        when documentTemplate changes)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            s = self.stats()
            if not s.get("isIndexing"):
                return
            time.sleep(poll_s)
        raise RuntimeError(f"Index {self.index} never went idle after {timeout_s}s")

    def wait_embeddings_settled(
        self, timeout_s: int = 900, poll_s: float = 5.0, stable_polls: int = 3
    ) -> dict:
        """Block until numberOfEmbeddedDocuments stops climbing.

        `wait_index_idle` is NOT sufficient before an index swap. Meilisearch
        finishes the documentAdditionOrUpdate task and reports isIndexing false
        while the embedder is still vectorising in the background, so swapping on
        idle alone rotates in a half-vectorised index and then deletes the better
        one. That is exactly what happened on 2026-10-12: a swap taken on idle
        moved the embedding gap from 773 to 953 and it did not self-heal.

        Returns when the embedded count has been unchanged for `stable_polls`
        consecutive polls, or when every document is embedded. Does not raise on
        timeout: a persistent shortfall is real information for the caller, not a
        reason to abandon a finished rebuild.
        """
        deadline = time.time() + timeout_s
        last = -1
        stable = 0
        s: dict = self.stats()
        while time.time() < deadline:
            s = self.stats()
            docs = s.get("numberOfDocuments", 0)
            emb = s.get("numberOfEmbeddedDocuments", 0)
            if docs and emb >= docs:
                return s
            stable = stable + 1 if emb == last else 0
            last = emb
            if stable >= stable_polls:
                log.warning(
                    "embeddings_plateaued", docs=docs, embedded=emb, gap=docs - emb
                )
                return s
            time.sleep(poll_s)
        log.warning("embeddings_wait_timeout", timeout_s=timeout_s)
        return s


def is_indexable(entry: FsEntry, exclude_prefixes: list[str]) -> bool:
    if not entry.name.lower().endswith(".md"):
        return False
    if any(entry.name.startswith(p) for p in exclude_prefixes):
        return False
    return True


# ---------------------------------------------------------------------
# Blog source — filesystem walker. The blog corpus (Cam's Quartz site) lives
# on disk under SIDECAR_BLOG_DIR; we walk it directly rather than going
# through SB's /.fs because it isn't part of the SilverBullet space.

import os
from pathlib import Path


def walk_blog(root: str, exclude_prefixes: list[str]) -> list[FsEntry]:
    """Walk the blog directory and return FsEntry-shaped records for every
    indexable markdown file. Path is relative to `root` so it stays portable
    when the host mount changes."""
    out: list[FsEntry] = []
    root_path = Path(root)
    for p in root_path.rglob("*.md"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root_path))
        if any(rel.startswith(ex) for ex in exclude_prefixes):
            continue
        st = p.stat()
        out.append(FsEntry(
            name=rel,
            created=int(st.st_ctime),
            lastModified=int(st.st_mtime),
            contentType="text/markdown",
            size=st.st_size,
        ))
    return out


def read_blog_file(root: str, rel_path: str) -> str:
    """Read a markdown file from the blog mount."""
    path = Path(root) / rel_path
    return path.read_text(encoding="utf-8", errors="replace")


def is_meta_page(doc: IndexDoc) -> bool:
    """True when a page is `meta`-tagged or `meta/*`-tagged. Cam uses `meta`
    deliberately to hide pages from the SB page picker; search should respect
    the same convention so settings/library/template pages don't pollute hits."""
    for t in doc.tags:
        t_lower = t.lower()
        if t_lower == "meta" or t_lower.startswith("meta/"):
            return True
    return False


# path -> lastModified of pages last seen tagged `meta`. index_pass() leaves
# them out of planning until the file changes (e.g. the tag is removed), so the
# poll stops fetching and re-skipping the same ~43 Library/template pages every
# 15 seconds. In memory only: after a restart they are detected once again.
META_SKIP: dict[str, int] = {}


def index_pass(
    settings: Settings,
    sb: SbClient | SpaceReader,
    meili: MeiliClient,
    *,
    force_full: bool,
    known: dict[str, tuple[str, int]] | None = None,
) -> tuple[dict, dict[str, tuple[str, int]]]:
    """One full or incremental pass. Returns (counts, known_doc_index).

    `known`, when passed in, is trusted as the current Meilisearch state
    instead of re-paginating `GET /indexes/{index}/documents` — that
    pagination is what turned every 15s poll into ~48 HTTP round-trips once
    the index passed 47k docs, even when nothing in the space had changed.
    serve() carries the returned map forward across polls and only passes
    `known=None` (forcing a real fetch) on its first pass and on its
    periodic safety-net refresh. One-shot commands (reindex, reindex_swap)
    don't pass `known` at all, so they behave exactly as before.
    """
    fs_entries = sb.list_files()
    indexable = [e for e in fs_entries if is_indexable(e, settings.exclude_prefixes)]
    log.info("listed_space", total=len(fs_entries), indexable=len(indexable))

    if force_full:
        known = {}
    elif known is None:
        known = meili.known_doc_index()
    log.info("known_indexed", count=len(known))

    # RECONCILE BY PATH, NOT BY DOC ID.
    #
    # This used to be a set difference over ids, which worked only while
    # `stable_id(path)` made ids a 1:1 function of the path. Chunking broke
    # that: a note now owns N ids. Comparing "one id per file on disk" against
    # "N ids per file in the index" yields an empty intersection, so the old
    # expression would have evaluated `to_delete` to the ENTIRE INDEX on every
    # poll, every 15 seconds. Group by path instead.
    ids_by_path: dict[str, list[str]] = {}
    mtime_by_path: dict[str, int] = {}
    for doc_id, (p, lm) in known.items():
        ids_by_path.setdefault(p, []).append(doc_id)
        mtime_by_path[p] = max(mtime_by_path.get(p, 0), lm)

    fs_paths = {e.name for e in indexable}
    to_delete = [
        doc_id
        for p, ids in ids_by_path.items()
        if p not in fs_paths
        for doc_id in ids
    ]

    to_index: list[FsEntry] = []
    for e in indexable:
        # A meta page is never indexed, so the index never holds an mtime for
        # it and the compare below would re-plan it on every poll forever.
        if not force_full and META_SKIP.get(e.name) == e.last_modified:
            continue
        if force_full or mtime_by_path.get(e.name, 0) < e.last_modified:
            to_index.append(e)

    log.info("pass_plan", to_index=len(to_index), to_delete=len(to_delete))

    indexed = 0
    failed = 0
    skipped_meta = 0
    batch: list[IndexDoc] = []
    BATCH = 200
    for e in to_index:
        try:
            raw = sb.fetch_page(e.name)
            docs = parse_page_chunked(e.name, raw, e.last_modified, e.size)
        except httpx.HTTPError as exc:
            log.warning("fetch_failed", path=e.name, error=str(exc))
            failed += 1
            continue
        # Cam's convention: tags = meta hides a page from the picker AND from
        # search. Library/Personal/*, templates, PLUG.md files, and config
        # pages typically carry this tag; they shouldn't pollute results.
        if is_meta_page(docs[0]):
            skipped_meta += 1
            META_SKIP[e.name] = e.last_modified
            # If we previously indexed this page (before the meta rule landed,
            # or before Cam tagged it), make sure every chunk gets evicted now.
            to_delete.extend(ids_by_path.get(e.name, []))
            continue
        # A rewritten note can produce FEWER chunks than last time. Ids are
        # deterministic, so surviving chunks are overwritten by the upsert;
        # only the tail needs deleting. Taking the set difference (rather than
        # deleting all old ids) keeps this correct regardless of whether the
        # delete lands before or after the upsert, which matters because the
        # delete is issued once at the end of the pass.
        new_ids = {d.id for d in docs}
        to_delete.extend(i for i in ids_by_path.get(e.name, []) if i not in new_ids)
        for d in docs:
            known[d.id] = (d.path, d.last_modified)
        batch.extend(docs)
        if len(batch) >= BATCH:
            meili.upsert(batch)
            indexed += len(batch)
            log.info("batch_pushed", indexed=indexed)
            batch = []
    if batch:
        meili.upsert(batch)
        indexed += len(batch)

    if to_delete:
        meili.delete_many(to_delete)
        for doc_id in to_delete:
            known.pop(doc_id, None)

    counts = {
        "indexed": indexed,
        "deleted": len(to_delete),
        "failed": failed,
        "skipped_meta": skipped_meta,
    }
    return counts, known


app = cyclopts.App(name="indexer", help="SilverBullet → Meilisearch sidecar indexer.")


@app.command
def reindex(
    *,
    force: Annotated[bool, cyclopts.Parameter(help="Ignore Meilisearch state, push every page.")] = False,
) -> None:
    """One-shot full or incremental pass, then exit."""
    settings = Settings()
    sb = make_source(settings)
    meili = MeiliClient(settings)
    meili.wait_healthy()
    meili.ensure_index()
    started = time.time()
    counts, _ = index_pass(settings, sb, meili, force_full=force)
    elapsed = time.time() - started
    log.info("reindex_complete", elapsed_s=round(elapsed, 2), **counts)
    print(meili.stats())


@app.command
def reindex_swap(
    *,
    keep_stale: Annotated[
        bool,
        cyclopts.Parameter(help="Skip the final delete of the post-swap stale index."),
    ] = False,
) -> None:
    """Zero-downtime full reindex via the staging-index swap pattern.

    1. Build a fresh `{index}_staging` index with the same settings.
    2. Push every page into staging (forces full re-vectorization through the
       embedder). The live `{index}` keeps serving queries throughout.
    3. Wait for staging to finish embedding.
    4. POST /swap-indexes to atomically rotate `{index}` ↔ `{index}_staging`.
       Settings, docs, task history all swap in one operation.
    5. Delete the now-stale staging (which used to be the live index).

    Use when you change the embedder model, the documentTemplate, the
    document schema, or anything else that requires a full re-vectorization
    or would otherwise leave the live index inconsistent during rebuild.
    """
    settings = Settings()
    sb = make_source(settings)
    canonical = MeiliClient(settings)
    canonical.wait_healthy()

    staging_name = f"{settings.sidecar_index_name}_staging"
    staging = MeiliClient(settings)
    staging.index = staging_name

    started = time.time()
    log.info("swap_begin", live=canonical.index, staging=staging_name)

    # Step 1: clean any prior staging leftover, ensure_index on fresh staging.
    try:
        staging.delete_index(staging_name)
        log.info("staging_cleared", name=staging_name)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    staging.ensure_index()
    log.info("staging_ready", name=staging_name)

    # Step 2: push every page into staging.
    counts, _ = index_pass(settings, sb, staging, force_full=True)
    log.info("staging_pushed", **counts)

    # Step 3: wait for embedder work to drain (re-vectorization can outlast the
    # last upsert task).
    log.info("waiting_for_embeddings")
    staging.wait_index_idle(timeout_s=900)
    # Idle is not enough: the embedder keeps working after the task completes.
    s = staging.wait_embeddings_settled(timeout_s=1800)
    docs = s.get("numberOfDocuments", 0)
    emb = s.get("numberOfEmbeddedDocuments", 0)
    log.info("staging_idle", docs=docs, embedded=emb, gap=docs - emb)
    staging_gap = docs - emb

    # Never rotate in an index that is worse than the one being replaced. A
    # 2026-10-12 run swapped a half-vectorised staging index over a better live
    # one and then deleted the better one, moving the gap 773 -> 953. The swap
    # is the irreversible step, so the comparison belongs here.
    live = canonical.stats()
    live_gap = live.get("numberOfDocuments", 0) - live.get(
        "numberOfEmbeddedDocuments", 0
    )
    if docs and staging_gap > live_gap:
        log.error(
            "swap_aborted_worse_than_live",
            staging_gap=staging_gap,
            live_gap=live_gap,
            staging_index=staging_name,
        )
        raise SystemExit(
            f"Refusing to swap: staging is missing {staging_gap} embeddings vs "
            f"{live_gap} live. Staging kept as {staging_name} for inspection."
        )
    if staging_gap:
        log.warning(
            "swapping_with_embedding_shortfall",
            docs=docs,
            embedded=emb,
            gap=staging_gap,
            live_gap=live_gap,
        )

    # Step 4: atomic swap.
    swap_task = canonical.swap_with(staging_name)
    log.info("swap_enqueued", task=swap_task)
    canonical.wait_for_task(swap_task["taskUid"])
    log.info("swap_complete")

    # Step 5: drop the post-swap stale index unless told to keep it.
    if not keep_stale:
        canonical.delete_index(staging_name)
        log.info("stale_deleted", name=staging_name)

    elapsed = time.time() - started
    log.info("reindex_swap_complete", elapsed_s=round(elapsed, 2))
    print(canonical.stats())


# Safety-net cadence for a real `known_doc_index()` refetch from Meilisearch,
# even though the in-memory cache is kept correct by index_pass() mutating it
# after every upsert/delete this process makes. The only way it drifts is
# something *else* writing to the index (a manual `reindex`/`reindex_swap`
# run against the same index, a hand-edited doc via the Meilisearch API).
# ponytail: time-based cache invalidation, not event-based — fine because the
# only writer in normal operation is this same serve() loop; if an external
# writer becomes common, invalidate on that event instead of waiting an hour.
KNOWN_REFRESH_INTERVAL_S = 3600


@app.command
def serve() -> None:
    """Run an initial pass, then poll on an interval forever."""
    settings = Settings()
    sb = make_source(settings)
    meili = MeiliClient(settings)
    meili.wait_healthy()
    meili.ensure_index()
    log.info("serve_start", poll_interval=settings.sidecar_poll_interval)
    # First pass: incremental against whatever Meilisearch already has. This
    # is the only poll that pays for the full known_doc_index() pagination
    # (~48 GETs at 47.5k docs) — index_pass() hands back its view of the
    # index afterward, and every later pass reuses + updates that view in
    # memory instead of re-paginating Meilisearch on a no-change poll.
    counts, known = index_pass(settings, sb, meili, force_full=False)
    log.info("initial_pass", **counts)
    last_refresh = time.monotonic()
    while True:
        time.sleep(settings.sidecar_poll_interval)
        try:
            refresh = time.monotonic() - last_refresh >= KNOWN_REFRESH_INTERVAL_S
            counts, known = index_pass(
                settings, sb, meili, force_full=False,
                known=None if refresh else known,
            )
            if refresh:
                last_refresh = time.monotonic()
            if counts["indexed"] or counts["deleted"] or counts["failed"]:
                log.info("poll_pass", **counts)
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("poll_pass_failed", error=str(exc))


@app.command
def reindex_blog() -> None:
    """One-shot reindex of the blog corpus (filesystem walk).

    Reads markdown files under SIDECAR_BLOG_DIR, pushes them to
    SIDECAR_BLOG_INDEX_NAME with the same schema/settings as the SB index.
    The Fast Search plug discovers both indexes and federates queries across
    them via /multi-search.

    Re-run manually whenever the blog content changes — the blog isn't
    polled continuously like the SB space (it changes much less often, and
    the source directory isn't part of SilverBullet's write path).
    """
    settings = Settings()
    if not settings.sidecar_blog_dir:
        raise RuntimeError(
            "SIDECAR_BLOG_DIR is empty — set it to the mount path of the blog "
            "content tree, e.g. /blog (with the host dir mounted via docker-compose)."
        )

    # Build a Meilisearch client pointed at the blog index. Reuse all the
    # ensure_index work — same searchable/filterable/sortable/embedder shape
    # makes federation behave consistently.
    meili = MeiliClient(settings)
    meili.index = settings.sidecar_blog_index_name
    meili.wait_healthy()
    meili.ensure_index()

    started = time.time()
    log.info(
        "blog_reindex_begin",
        index=meili.index,
        root=settings.sidecar_blog_dir,
    )

    entries = walk_blog(settings.sidecar_blog_dir, settings.blog_exclude_prefixes)
    log.info("blog_listed", indexable=len(entries))

    indexed = 0
    failed = 0
    batch: list[IndexDoc] = []
    BATCH = 200
    for e in entries:
        try:
            raw = read_blog_file(settings.sidecar_blog_dir, e.name)
            doc = parse_page(e.name, raw, e.last_modified, e.size)
            batch.append(doc)
        except OSError as exc:
            log.warning("blog_read_failed", path=e.name, error=str(exc))
            failed += 1
            continue
        if len(batch) >= BATCH:
            meili.upsert(batch)
            indexed += len(batch)
            log.info("blog_batch_pushed", indexed=indexed)
            batch = []
    if batch:
        meili.upsert(batch)
        indexed += len(batch)

    # Reconcile deletes — any doc in the index whose stable_id no longer
    # appears in the filesystem walk gets removed.
    fs_ids = {stable_id(e.name) for e in entries}
    known = meili.known_doc_index()
    to_delete = [doc_id for doc_id in known if doc_id not in fs_ids]
    if to_delete:
        meili.delete_many(to_delete)

    elapsed = time.time() - started
    log.info(
        "blog_reindex_complete",
        elapsed_s=round(elapsed, 2),
        indexed=indexed,
        failed=failed,
        deleted=len(to_delete),
    )
    print(meili.stats())


@app.command
def stats() -> None:
    """Print current Meilisearch index stats."""
    settings = Settings()
    meili = MeiliClient(settings)
    print(meili.stats())


@app.command
def search(query: str, limit: int = 5) -> None:
    """Quick query for ad-hoc testing."""
    settings = Settings()
    meili = MeiliClient(settings)
    r = meili.client.post(
        f"/indexes/{meili.index}/search",
        json={"q": query, "limit": limit, "attributesToHighlight": ["title", "body"]},
    )
    r.raise_for_status()
    data = r.json()
    print(f"hits: {len(data.get('hits', []))}/{data.get('estimatedTotalHits', '?')} "
          f"({data.get('processingTimeMs', '?')}ms)")
    for h in data.get("hits", []):
        print(f"  {h.get('path')}  [{', '.join(h.get('tags', [])[:5])}]")


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
