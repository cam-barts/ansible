#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "cyclopts>=3.0",
#     "httpx>=0.27",
#     "pydantic>=2.6",
#     "pydantic-settings>=2.2",
#     # indexer.py's own imports, pulled in by `from indexer import chunk_body`.
#     "structlog>=24.1",
#     "pyyaml>=6.0",
#     "markdown-it-py>=3.0",
# ]
# ///
"""Mirror Karakeep bookmarks into SilverBullet's Meilisearch (`karakeep` index).

Karakeep keeps its own Meilisearch for its own UI; this is a second, read-only
copy so bookmarks show up in Fast Search and SearXNG next to notes, blog and
podcasts, with the same `nomic` embedder and the same chunker as the notes.

  karakeep_indexer.py index            sync pass (only changed bookmarks)
  karakeep_indexer.py index --force    rebuild every bookmark's chunks
  karakeep_indexer.py serve            sync on boot, then every interval
  karakeep_indexer.py search "query"   sanity check from the CLI

One document per chunk. Chunk ids are sha1(bookmarkId:n), so `distinct` on
`bookmarkId` (SearXNG) or `path` (Fast Search's federated query) collapses a
long article back to one result.

`path` is the bookmark's original URL (Karakeep preview page for text/asset
bookmarks with no URL). Fast Search's federation dedups and navigates on
`path`, and its navigateTo opens it in a new tab for this index. SearXNG links
`url` straight through and styles the result off its engine label.

Incremental: every pass lists bookmarks WITHOUT content (7 cheap requests for
~630 bookmarks) plus all highlights, fingerprints each bookmark, and fetches
full content only for those whose fingerprint differs from the one stored on
their indexed chunks. Bookmarks gone from Karakeep are deleted, behind the same
>25% guard podcast_indexer.py uses.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import cyclopts
import httpx
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

sys.path.insert(0, str(Path(__file__).parent))
from indexer import chunk_body  # noqa: E402  same chunker as the notes

# Bump when build_docs output changes shape, so the next pass rebuilds all.
SCHEMA = 2  # 2: dropped the searxUrl field
# ponytail: a few bookmarks carry multi-MB pages (a 5.8 MB HTML dump, a 1.8 MB
# PDF). Uncapped, one of them would be thousands of chunks and hours of
# embedding for a single search hit. 40k chars is ~45 chunks, which covers the
# 90th-percentile article whole; raise it if deep matches in long PDFs matter.
MAX_TEXT_CHARS = 40_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str
    # Host-published port via host-gateway, not https://hoarder.coder.cam: stays
    # on the box, so no BunkerWeb/Cloudflare dependency and multi-MB page
    # bodies never hairpin out through the proxy.
    karakeep_url: str = "http://host.docker.internal:8679"
    karakeep_api_key: str
    karakeep_public_url: str = "https://hoarder.coder.cam"
    karakeep_index: str = "karakeep"
    karakeep_index_interval: int = 900
    ollama_embed_url: str = "http://host.docker.internal:11434/api/embeddings"


class KarakeepDoc(BaseModel):
    id: str
    bookmarkId: str
    chunk: int = 0
    title: str
    body: str
    heading: str | None = None
    url: str | None = None
    karakeepUrl: str
    path: str
    # Hostname, or "karakeep" for text/asset bookmarks. Fills the `folder` slot
    # the Fast Search hit row renders under the title.
    folder: str
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    summary: str | None = None
    highlights: list[str] = Field(default_factory=list)
    type: str
    archived: bool = False
    favourited: bool = False
    createdAt: str
    createdAtTs: int
    fingerprint: str


# ---------------------------------------------------------------------------
# building documents
# ---------------------------------------------------------------------------

_BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "ul", "ol",
          "blockquote", "pre", "figure", "figcaption", "header", "footer", "li",
          "dd", "dt", "hr"}
_SKIP = {"script", "style", "noscript", "svg", "template", "head"}


class _Text(HTMLParser):
    """Karakeep's htmlContent is Readability output, so a flat walk that turns
    block boundaries into blank lines and headings into `#` lines is enough to
    hand chunk_body something paragraph-shaped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK or tag[:1] == "h" and tag[1:].isdigit():
            self.out.append("\n\n")

    def handle_data(self, data):
        if not self.skip:
            # Keep edge spaces: `want <em>your</em> answer` arrives as three
            # data events and stripping them glues the words together.
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    paras = (" ".join(x.split()) for x in "".join(p.out).split("\n\n"))
    return "\n\n".join(x for x in paras if x)


def bookmark_text(b: dict) -> str:
    c = b.get("content") or {}
    parts = [c.get("description") or ""]
    match c.get("type"):
        case "link":
            parts.append(html_to_text(c.get("htmlContent") or ""))
        case "text":
            parts.append(c.get("text") or "")
        case "asset":
            parts.append(c.get("content") or "")
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return text[:MAX_TEXT_CHARS]


def fingerprint(b: dict, highlights: list[str]) -> str:
    """Everything in the light (no-content) listing that changes the docs.
    Karakeep bumps modifiedAt on edits; crawledAt catches re-crawls, which
    rewrite the page text without touching the bookmark row."""
    c = b.get("content") or {}
    parts = [SCHEMA, b.get("modifiedAt"), b.get("title"), b.get("note"),
             b.get("summary"), b.get("archived"), b.get("favourited"),
             sorted(t["name"] for t in b.get("tags") or []),
             c.get("url"), c.get("sourceUrl"), c.get("title"), c.get("fileName"),
             c.get("description"), c.get("crawledAt"), highlights]
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def build_docs(b: dict, highlights: list[str], fp: str, public: str) -> list[KarakeepDoc]:
    c = b.get("content") or {}
    url = c.get("url") or c.get("sourceUrl")
    kk = f"{public}/dashboard/preview/{b['id']}"
    text = bookmark_text(b)
    title = (b.get("title") or c.get("title") or c.get("fileName")
             or (text.splitlines()[0][:120] if text else None) or url or "(untitled)")
    created = b.get("createdAt") or ""
    ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else 0
    pieces = chunk_body(text, 0) or [("", None, 0)]  # keep title/tag-only ones findable
    return [
        KarakeepDoc(
            id=hashlib.sha1(f"{b['id']}:{i}".encode()).hexdigest()[:24],
            bookmarkId=b["id"], chunk=i, title=title, body=body, heading=heading,
            url=url, karakeepUrl=kk, path=url or kk,
            folder=(urlsplit(url).hostname or "karakeep") if url else "karakeep",
            tags=sorted(t["name"] for t in b.get("tags") or []),
            note=b.get("note"), summary=b.get("summary"), highlights=highlights,
            type=c.get("type") or "unknown", archived=bool(b.get("archived")),
            favourited=bool(b.get("favourited")), createdAt=created, createdAtTs=ts,
            fingerprint=fp,
        )
        for i, (body, heading, _pos) in enumerate(pieces)
    ]


def plan(live: dict[str, str], indexed: dict[str, tuple[str, list[str]]],
         force: bool = False) -> tuple[list[str], list[str]]:
    """(bookmarks to rebuild, bookmarks to delete).

    `live` is bookmarkId -> fingerprint from Karakeep; `indexed` is bookmarkId
    -> (fingerprint, chunk ids) from Meilisearch. Deletion is refused when it
    would drop more than a quarter of the index: a truncated listing (Karakeep
    mid-restart, a pagination bug) must not wipe what took an hour to embed.
    """
    todo = [b for b, fp in live.items() if force or indexed.get(b, ("",))[0] != fp]
    gone = [b for b in indexed if b not in live]
    if indexed and len(gone) > len(indexed) * 0.25:
        print(f"  REFUSING to delete {len(gone)}/{len(indexed)} bookmarks: looks like "
              f"a bad listing, not real churn. Delete by hand if legitimate.", flush=True)
        gone = []
    return todo, gone


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

class Karakeep:
    def __init__(self, s: Settings):
        self.c = httpx.Client(base_url=s.karakeep_url.rstrip("/") + "/api/v1", timeout=120,
                              headers={"Authorization": f"Bearer {s.karakeep_api_key}"})

    def _paged(self, path: str, key: str, **params) -> list[dict]:
        out, cursor = [], None
        while True:
            r = self.c.get(path, params={"limit": 100, **params,
                                         **({"cursor": cursor} if cursor else {})})
            r.raise_for_status()
            d = r.json()
            out += d[key]
            cursor = d.get("nextCursor")
            if not cursor:
                return out

    def bookmarks(self) -> list[dict]:
        return self._paged("/bookmarks", "bookmarks", includeContent="false")

    def highlights(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for h in sorted(self._paged("/highlights", "highlights"),
                        key=lambda h: (h.get("startOffset") or 0, h["id"])):
            text = (h.get("text") or "").strip()
            if h.get("note"):
                text += f" (note: {h['note'].strip()})"
            if text:
                out.setdefault(h["bookmarkId"], []).append(text)
        return out

    def bookmark(self, bid: str) -> dict:
        r = self.c.get(f"/bookmarks/{bid}", params={"includeContent": "true"})
        r.raise_for_status()
        return r.json()


class Meili:
    def __init__(self, s: Settings):
        self.s, self.index = s, s.karakeep_index
        self.c = httpx.Client(base_url=s.meili_url, timeout=120,
                              headers={"Authorization": f"Bearer {s.meili_master_key}"})

    def wait(self, uid: int) -> dict:
        while True:
            t = self.c.get(f"/tasks/{uid}").json()
            if t["status"] in ("succeeded", "failed", "canceled"):
                if t["status"] != "succeeded":
                    print(f"  TASK {uid} {t['status']}: {t.get('error')}", flush=True)
                return t
            time.sleep(0.2)

    def ensure_index(self) -> None:
        if self.c.get(f"/indexes/{self.index}").status_code == 404:
            self.wait(self.c.post("/indexes", json={"uid": self.index, "primaryKey": "id"})
                      .raise_for_status().json()["taskUid"])
        r = self.c.patch(f"/indexes/{self.index}/settings", json={
            "searchableAttributes": ["title", "tags", "note", "highlights", "summary",
                                     "body", "url", "folder"],
            # bookmarkId for SearXNG's distinct, path for Fast Search's.
            "filterableAttributes": ["bookmarkId", "path", "tags", "folder", "type",
                                     "archived", "favourited", "createdAtTs"],
            "distinctAttribute": "bookmarkId",
            "sortableAttributes": ["createdAtTs"],
            "rankingRules": ["words", "typo", "proximity", "attribute", "sort",
                             "exactness", "createdAtTs:desc"],
            # No stop words, ever: see the note in podcast_indexer.py.
            "stopWords": [],
            "typoTolerance": {"enabled": True,
                              "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9},
                              "disableOnAttributes": ["bookmarkId", "url", "folder", "tags"]},
            # Same embedder as silverbullet/blog/podcasts. Template mirrors the
            # notes one; 4000 bytes covers a 1400-unit chunk plus title/tags/note.
            "embedders": {"nomic": {
                "source": "ollama", "model": "nomic-embed-text", "dimensions": 768,
                "url": self.s.ollama_embed_url,
                "documentTemplate": (
                    "{{ doc.title }}{% if doc.heading %} / {{ doc.heading }}{% endif %}. "
                    "Tags: {{ doc.tags }}. {% if doc.note %}Note: {{ doc.note }}. {% endif %}"
                    "{{ doc.body }}"),
                "documentTemplateMaxBytes": 4000,
            }},
        })
        r.raise_for_status()
        self.wait(r.json()["taskUid"])

    def indexed(self) -> dict[str, tuple[str, list[str]]]:
        out: dict[str, tuple[str, list[str]]] = {}
        offset = 0
        while True:
            r = self.c.post(f"/indexes/{self.index}/documents/fetch", json={
                "limit": 5000, "offset": offset, "fields": ["id", "bookmarkId", "fingerprint"]})
            r.raise_for_status()
            page = r.json()
            for d in page["results"]:
                out.setdefault(d["bookmarkId"], (d["fingerprint"], []))[1].append(d["id"])
            offset += len(page["results"])
            if not page["results"] or offset >= page["total"]:
                return out

    def push(self, docs: list[KarakeepDoc], batch: int = 40) -> None:
        """40 at a time with a wait per task: the EMBED_SAFE_BATCH rule from
        indexer.py (v1.21 dropped one vector per 41-doc sub-chunk). v1.56 has
        the fix, but the pattern is proven and this corpus is small."""
        for i in range(0, len(docs), batch):
            r = self.c.post(f"/indexes/{self.index}/documents",
                            json=[d.model_dump() for d in docs[i:i + batch]])
            r.raise_for_status()
            self.wait(r.json()["taskUid"])

    def delete(self, ids: list[str]) -> None:
        if ids:
            r = self.c.post(f"/indexes/{self.index}/documents/delete-batch", json=ids)
            r.raise_for_status()
            self.wait(r.json()["taskUid"])


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

app = cyclopts.App(help="Mirror Karakeep bookmarks into Meilisearch.")


def sync(force: bool = False) -> None:
    s = Settings()
    kk, meili = Karakeep(s), Meili(s)
    meili.ensure_index()
    light = kk.bookmarks()
    hl = kk.highlights()
    live = {b["id"]: fingerprint(b, hl.get(b["id"], [])) for b in light}
    indexed = meili.indexed()
    todo, gone = plan(live, indexed, force)
    print(f"{len(light)} bookmarks, {len(indexed)} indexed: "
          f"{len(todo)} to (re)build, {len(gone)} to delete", flush=True)
    stale: list[str] = []
    for n, bid in enumerate(todo, 1):
        try:
            docs = build_docs(kk.bookmark(bid), hl.get(bid, []), live[bid],
                              s.karakeep_public_url)
        except httpx.HTTPStatusError as exc:  # deleted between listing and fetch
            print(f"  skip {bid}: {exc.response.status_code}", flush=True)
            continue
        meili.push(docs)
        new = {d.id for d in docs}
        stale += [i for i in indexed.get(bid, ("", []))[1] if i not in new]
        if n % 50 == 0 or n == len(todo):
            print(f"  built {n}/{len(todo)}", flush=True)
    for bid in gone:
        stale += indexed[bid][1]
    meili.delete(stale)
    print(f"done: deleted {len(stale)} stale chunks", flush=True)


@app.command
def index(force: Annotated[bool, cyclopts.Parameter(help="Rebuild every bookmark.")] = False):
    """One sync pass."""
    sync(force)


@app.command
def serve():
    """Sync on boot, then every KARAKEEP_INDEX_INTERVAL seconds."""
    interval = Settings().karakeep_index_interval
    while True:
        started = time.time()
        try:
            sync()
        except Exception as exc:  # keep serving; the next pass retries
            print(f"sync failed: {exc!r}", flush=True)
        print(f"pass took {time.time() - started:.0f}s", flush=True)
        time.sleep(interval)


@app.command
def search(query: str, semantic: bool = False, limit: int = 8):
    """Sanity check: keyword (default) or pure-semantic search."""
    meili = Meili(Settings())
    body = {"q": query, "limit": limit, "showRankingScore": True}
    if semantic:
        body["hybrid"] = {"embedder": "nomic", "semanticRatio": 1.0}
    r = meili.c.post(f"/indexes/{meili.index}/search", json=body)
    r.raise_for_status()
    for h in r.json()["hits"]:
        print(f"  {h['_rankingScore']:.3f}  {h['title'][:70]}  <{h['path'][:70]}>")


if __name__ == "__main__":
    app()
