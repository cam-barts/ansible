#!/usr/bin/env python3
"""Index MinusPod podcast transcripts into Meilisearch for PKM search.

One document per ~45 second chunk rather than per episode. A whole-episode
document would match a search and then leave you scrubbing a 90 minute file
looking for the sentence; a chunk carries its own start timestamp, so a hit
deep-links to the moment.

Reads MinusPod's SQLite directly (read-only URI). The API would work too but
paginates, and this runs on the same host.

  podcast_indexer.py index          full pass (pushes only new/changed chunks)
  podcast_indexer.py index --force  full pass that re-pushes every chunk
  podcast_indexer.py index --since 3600   only episodes touched in the last hour
  podcast_indexer.py search "query"       sanity-check from the CLI
  podcast_indexer.py forget --slug x      drop a show from the vault (see cmd_forget)

`forget` writes, and the vault file is owned by root because this container
runs as root, so run it inside the container rather than from the host:
  docker exec silverbullet-podcast-indexer-1 \
    uv run --no-project --with httpx /sidecar/podcast_indexer.py forget --slug x

MinusPod is not the system of record for transcripts. Its retention sweep
(`delete_episodes` -> `batch_clear_episode_details`) deletes episode_details
along with the audio, so a transcript only lives as long as its mp3 --
`retention_days` is 90 by default. Every full pass therefore copies each
transcript into a separate SQLite vault, and the vault is indexed alongside the
live DB. Episodes that exist only in the vault are flagged `retired: true`:
searchable text, no audio behind the URL.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

# Container mounts MinusPod's data dir at /minuspod-data; on the host it is
# under ~/docker_services. Both paths are tried so the same file runs either way.
DB = Path(os.environ.get("MINUSPOD_DB", "/minuspod-data/podcast.db"))
if not DB.exists():
    DB = Path.home() / "docker_services/minuspod/data/podcast.db"
# Deliberately NOT a table inside podcast.db: an archive stored in the thing it
# protects dies with it. Restoring a podcast-pre-upgrade-*.db would silently
# roll the vault back too, and writing there would mean giving this container a
# write handle on MinusPod's live DB (its mount is :ro, and stays that way).
VAULT = Path(os.environ.get("PODCAST_VAULT", "/vault/transcripts.db"))
if not VAULT.parent.exists():
    VAULT = Path.home() / "docker_services/minuspod/vault/transcripts.db"
MEILI = os.environ.get("MEILI_URL", "http://localhost:7701")
INDEX = os.environ.get("PODCAST_INDEX", "podcasts")
BASE_URL = os.environ.get("MINUSPOD_BASE", "https://minuspod.coder.cam")
# MinusPod gates /episodes/ with feedAuth, so a bare enclosure URL 401s. The key
# rides in the indexed URL because SearXNG renders result links client-side and
# has no way to inject one. Blast radius matches the Meili search key already
# sitting in settings.yml: the `!pod` engine is token-gated. Rotating the
# MinusPod key means a full reindex (`podcast_indexer.py index`, no --since).
FEED_KEY = os.environ.get("MINUSPOD_FEED_KEY", "")
FEED_Q = f"?key={FEED_KEY}" if FEED_KEY else ""
CHUNK_SECONDS = 45.0
CHUNK_MAX_CHARS = 900

# DO NOT ADD STOP WORDS HERE. A 143-word list lived here for a few hours on
# 2026-09-12 and was reverted, because Meilisearch strips stop words from the
# INDEX, which silently destroys exact phrase search:
#
#   "each other"   ->  1.0 against a chunk reading "We are taking a brief
#                      hiatus for a few weeks as..." (contains neither word)
#
# The quotes do not error, they just stop constraining anything, so a user
# quoting a remembered line gets confident unrelated hits. Cam hit this with
# `!pod "we are all just walking each other home"`, which reduced to
# `walking home`.
#
# The list was added to stop `how to train a dog` scoring 0.99 on an unrelated
# episode via "how/to/a". It did not actually do that: before/after scores were
# borg backup 0.43/0.43, best pizza recipe 0.58/0.58, python async 1.0/1.0,
# how to train a dog 0.99/0.98. The separation that makes rankingScoreThreshold
# work was already there without them. Negligible gain, phrase search broken.
#
# If filler words ever genuinely need suppressing, do it in a way that leaves
# the index intact (query-side handling, or `matchingStrategy`), never by
# removing terms from the index.


def meili_key() -> str:
    if os.environ.get("MEILI_MASTER_KEY"):
        return os.environ["MEILI_MASTER_KEY"]
    for cand in (Path("/sidecar/../.env"), Path.home()/"docker_services/silverbullet/.env"):
        if cand.exists():
            env = cand
            break
    else:
        raise SystemExit("no .env found and MEILI_MASTER_KEY unset")
    for line in env.read_text().splitlines():
        if line.startswith("MEILI_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("MEILI_MASTER_KEY not found")


def client() -> httpx.Client:
    return httpx.Client(base_url=MEILI, timeout=120,
                        headers={"Authorization": f"Bearer {meili_key()}"})


def ensure_index(c: httpx.Client) -> None:
    if c.get(f"/indexes/{INDEX}").status_code == 404:
        c.post("/indexes", json={"uid": INDEX, "primaryKey": "id"}).raise_for_status()
        time.sleep(1)
    c.patch(f"/indexes/{INDEX}/settings", json={
        # Order matters: earlier attributes rank higher under `attribute`.
        "searchableAttributes": ["text", "episodeTitle", "podcast"],
        "filterableAttributes": ["podcast", "slug", "episodeId", "isAd", "published",
                                 "retired"],
        "sortableAttributes": ["published", "start"],
        "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
        # Explicitly empty, not merely absent: this resets the index if a stop
        # word list was ever applied. See the block near the top for why.
        "stopWords": [],
        "displayedAttributes": ["id", "podcast", "slug", "episodeId", "episodeTitle",
                                "text", "start", "end", "published", "isAd", "url",
                                "retired"],
    }).raise_for_status()
    # Same embedder the silverbullet and blog indexes use, so semantic and
    # hybrid search work here identically. nomic-embed-text is the one model
    # deliberately left in Ollama.
    c.patch(f"/indexes/{INDEX}/settings/embedders", json={
        "nomic": {
            "source": "ollama",
            "model": "nomic-embed-text",
            "dimensions": 768,
            "documentTemplate": "{{ doc.podcast }}. {{ doc.episodeTitle }}. {{ doc.text }}",
            "documentTemplateMaxBytes": 900,
            "url": os.environ.get("OLLAMA_EMBED_URL",
                                  "http://host.docker.internal:11434/api/embeddings"),
        }
    }).raise_for_status()

    c.patch(f"/indexes/{INDEX}/settings/typo-tolerance", json={
        "enabled": True,
        "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9},
        "disableOnAttributes": ["slug", "episodeId", "podcast"],
    }).raise_for_status()


# transcript_text uses "[HH:MM:SS.mmm --> HH:MM:SS.mmm] text"; the JSON
# segment columns are preferred because they carry real float seconds.
SEG_RE = re.compile(r"\[(\d+):(\d+):([\d.]+)\s*-->\s*(\d+):(\d+):([\d.]+)\]\s*(.*)")


def parse_segments(raw: str) -> list[tuple[float, float, str]]:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [(float(s["start"]), float(s["end"]), (s.get("text") or "").strip())
                    for s in data if s.get("text")]
    except Exception:
        pass
    out = []
    for line in (raw or "").splitlines():
        m = SEG_RE.match(line.strip())
        if m:
            a = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            b = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
            if m.group(7).strip():
                out.append((a, b, m.group(7).strip()))
    return out


def chunk(segs, ad_spans):
    """Group segments into ~CHUNK_SECONDS blocks, flagging those inside ads."""
    cur, out = [], []
    for s, e, t in segs:
        if not t:
            continue
        cur.append((s, e, t))
        span = cur[-1][1] - cur[0][0]
        chars = sum(len(x[2]) for x in cur)
        if span >= CHUNK_SECONDS or chars >= CHUNK_MAX_CHARS:
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    docs = []
    for g in out:
        start, end = g[0][0], g[-1][1]
        mid = (start + end) / 2
        docs.append({
            "start": round(start, 1), "end": round(end, 1),
            "text": " ".join(x[2] for x in g),
            "isAd": any(a <= mid <= b for a, b in ad_spans),
        })
    return docs


def load_episodes(since: int | None):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # NOTE: episode_details.episode_id is an INTEGER FK to episodes.id, not the
    # string episode id on episodes.episode_id. Joining on the string yields
    # zero rows silently.
    q = """SELECT p.slug, p.title AS podcast, e.episode_id, e.title, e.published_at,
                  d.final_segments_json, d.original_segments_json,
                  d.transcript_text, d.original_transcript_text, d.ad_markers_json
           FROM episodes e
           JOIN podcasts p ON p.id = e.podcast_id
           JOIN episode_details d ON d.episode_id = e.id
           WHERE COALESCE(d.final_segments_json, d.original_segments_json,
                          d.transcript_text, d.original_transcript_text) IS NOT NULL"""
    params: tuple = ()
    if since is not None:
        # `since` was accepted and then IGNORED here until 2026-10-13, so
        # `index --since N` silently did a full pass and the only thing the flag
        # ever changed was that it suppressed prune and the vault export. That
        # did not matter while push() was 2000-at-a-time fire-and-forget; it
        # matters now that push() commits per 40 documents (see push()).
        #
        # Filter on episodes.updated_at, which tracks processing (it equals
        # processed_at on every sampled row). Build the cutoff in the SAME
        # format the column uses -- 'YYYY-MM-DDTHH:MM:SSZ' -- because
        # datetime('now') yields 'YYYY-MM-DD HH:MM:SS' and a lexicographic
        # comparison between the two is wrong on both the 'T' and the 'Z'.
        #
        # Caveat: a rewrite that touches only episode_details and leaves the
        # episodes row alone would be missed, since episode_details has a
        # created_at but no updated_at. The periodic full pass is what catches
        # that, which is one of the reasons it still exists.
        q += " AND e.updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)"
        params = (f"-{since} seconds",)
    return con.execute(q, params).fetchall()


def raw_transcript(r):
    """The payload build_docs parses: best available of the four columns."""
    return (r["final_segments_json"] or r["original_segments_json"]
            or r["transcript_text"] or r["original_transcript_text"])


# Everything that changes the resulting documents, so the monthly `normalize`
# rewrites and any retitling are picked up, and nothing else churns the vault.
def _fingerprint(r) -> str:
    parts = [r["podcast"], r["title"], r["published_at"],
             r["ad_markers_json"], raw_transcript(r)]
    h = hashlib.sha1()
    for p in parts:
        h.update(b"\x00" + (p or "").encode())
    return h.hexdigest()


VAULT_DDL = """CREATE TABLE IF NOT EXISTS transcripts (
    slug TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    podcast TEXT,
    title TEXT,
    published TEXT,
    -- The coalesced payload, i.e. whichever of MinusPod's four transcript
    -- columns build_docs would have used. Usually the segment JSON, so
    -- per-segment timestamps survive and re-chunking stays possible.
    segments_json TEXT,
    ad_markers_json TEXT,
    fingerprint TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (slug, episode_id)
)"""


def vault_conn() -> sqlite3.Connection:
    VAULT.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(VAULT)
    con.row_factory = sqlite3.Row
    con.execute(VAULT_DDL)
    return con


def export_vault(rows) -> int:
    """Copy live transcripts into the vault, skipping unchanged ones."""
    con = vault_conn()
    try:
        have = {(r["slug"], r["episode_id"]): r["fingerprint"]
                for r in con.execute("SELECT slug, episode_id, fingerprint FROM transcripts")}
        pending = []
        for r in rows:
            fp = _fingerprint(r)
            if have.get((r["slug"], r["episode_id"])) == fp:
                continue
            pending.append((r["slug"], r["episode_id"], r["podcast"], r["title"],
                            r["published_at"], raw_transcript(r), r["ad_markers_json"],
                            fp, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        with con:
            con.executemany(
                "INSERT INTO transcripts (slug, episode_id, podcast, title, published,"
                " segments_json, ad_markers_json, fingerprint, archived_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(slug, episode_id) DO UPDATE SET"
                " podcast=excluded.podcast, title=excluded.title,"
                " published=excluded.published, segments_json=excluded.segments_json,"
                " ad_markers_json=excluded.ad_markers_json,"
                " fingerprint=excluded.fingerprint, archived_at=excluded.archived_at",
                pending)
        return len(pending)
    finally:
        con.close()


# Aliased to the live query's column names so build_docs takes either source
# without caring which it got.
VAULT_SELECT = """SELECT slug, podcast, episode_id, title, published AS published_at,
                         segments_json AS final_segments_json,
                         NULL AS original_segments_json,
                         NULL AS transcript_text,
                         NULL AS original_transcript_text,
                         ad_markers_json
                  FROM transcripts"""


def load_vault(live: set[tuple[str, str]]):
    """Vault rows with no live DB row. The live DB always wins: it is fresher."""
    con = vault_conn()
    try:
        return [r for r in con.execute(VAULT_SELECT)
                if (r["slug"], r["episode_id"]) not in live]
    finally:
        con.close()


def build_docs(rows, retired: bool = False):
    docs = []
    for r in rows:
        raw = raw_transcript(r)
        segs = parse_segments(raw or "")
        if not segs:
            continue
        ad_spans = []
        if r["ad_markers_json"]:
            try:
                ad_spans = [(float(m["start"]), float(m["end"]))
                            for m in json.loads(r["ad_markers_json"])]
            except Exception:
                pass
        pub = (r["published_at"] or "")[:10]
        for i, ch in enumerate(chunk(segs, ad_spans)):
            key = f"{r['slug']}:{r['episode_id']}:{i}"
            docs.append({
                "id": hashlib.sha1(key.encode()).hexdigest()[:24],
                "podcast": r["podcast"], "slug": r["slug"],
                "episodeId": r["episode_id"], "episodeTitle": r["title"],
                "published": pub, "retired": retired,
                "url": f"{BASE_URL}/episodes/{r['slug']}/{r['episode_id']}.mp3{FEED_Q}#t={int(ch['start'])}",
                **ch,
            })
    return docs


def push(c: httpx.Client, docs, batch=40):
    """Push in sub-41 groups, waiting for each task.

    Meilisearch v1.21 silently drops the vector of the last document in every
    complete 41-document sub-chunk of a 100-document embedding group, so a
    2000-document task left ~40 chunks unvectorised with no error anywhere.
    See the EMBED_SAFE_BATCH note in indexer.py for the measurements. The wait
    is not optional: without it Meilisearch auto-batches consecutive tasks back
    into groups over 41 and the loss returns.
    """
    for i in range(0, len(docs), batch):
        r = c.post(f"/indexes/{INDEX}/documents", json=docs[i:i + batch])
        r.raise_for_status()
        uid = r.json()["taskUid"]
        while True:
            t = c.get(f"/tasks/{uid}").json()
            if t["status"] in ("succeeded", "failed", "canceled"):
                break
            time.sleep(0.2)
        if t["status"] != "succeeded":
            print(f"  TASK {uid} {t['status']}: {t.get('error')}", flush=True)
        if i % (batch * 25) == 0 or i + batch >= len(docs):
            print(f"  pushed {min(i+batch, len(docs))}/{len(docs)}", flush=True)


# What a chunk looks like to the diff. Every field build_docs writes, all of them
# in displayedAttributes, so documents/fetch returns exactly these.
DOC_FIELDS = ("id", "podcast", "slug", "episodeId", "episodeTitle", "published",
              "retired", "url", "start", "end", "text", "isAd")


def doc_key(d) -> str:
    """Comparable form of a chunk, the same whether built here or read back."""
    vals = []
    for k in DOC_FIELDS:
        v = d.get(k)
        if k in ("start", "end") and v is not None:
            v = round(float(v), 1)  # Meili may hand 12.0 back as 12
        vals.append(v)
    return json.dumps(vals, ensure_ascii=False)


def fetch_indexed(c: httpx.Client, episode_ids=None) -> dict[str, str]:
    """id -> doc_key for what Meilisearch holds: the whole index, or only the
    given episodes (incremental passes)."""
    flt = None
    if episode_ids is not None:
        if not episode_ids:
            return {}
        flt = "episodeId IN [" + ", ".join(json.dumps(e) for e in episode_ids) + "]"
    out, offset = {}, 0
    while True:
        body = {"limit": 5000, "offset": offset, "fields": list(DOC_FIELDS)}
        if flt:
            body["filter"] = flt
        r = c.post(f"/indexes/{INDEX}/documents/fetch", json=body)
        r.raise_for_status()
        page = r.json()
        for d in page["results"]:
            out[d["id"]] = doc_key(d)
        offset += len(page["results"])
        if not page["results"] or offset >= page["total"]:
            return out


def changed(docs, indexed: dict[str, str]):
    """Chunks Meilisearch lacks or holds in a different form. Unchanged ones are
    skipped entirely: re-pushing them costs an LMDB commit per 40 even when
    nothing re-embeds, which is what made every restart a ~12h rewrite."""
    return [d for d in docs if indexed.get(d["id"]) != doc_key(d)]


def cmd_index(argv):
    since = None
    if "--since" in argv:
        since = int(argv[argv.index("--since") + 1])
    force = "--force" in argv
    rows = load_episodes(since)
    print(f"{len(rows)} episodes with transcripts")
    docs = build_docs(rows)
    # Vault work is full-pass only, for the same reason prune is: a --since
    # slice is not the whole corpus, and exporting from one would be fine but
    # indexing the vault alongside it would double-count nothing useful.
    if since is None:
        print(f"vault: {export_vault(rows)} transcripts written")
        live = {(r["slug"], r["episode_id"]) for r in rows}
        retired = build_docs(load_vault(live), retired=True)
        if retired:
            print(f"vault: {len(retired)} chunks from retired episodes")
        docs += retired
    ads = sum(1 for d in docs if d["isAd"])
    print(f"{len(docs)} chunks ({ads} inside known ad spans)")
    with client() as c:
        ensure_index(c)
        if since is None:
            indexed = fetch_indexed(c)
        else:
            indexed = fetch_indexed(c, sorted({d["episodeId"] for d in docs}))
        todo = docs if force else changed(docs, indexed)
        print(f"{len(todo)} new or changed, {len(docs) - len(todo)} already current"
              + (" (--force: pushing all)" if force else ""), flush=True)
        push(c, todo)
        if since is None:
            prune(c, list(indexed), {d["id"] for d in docs})
    print("done")


def cmd_forget(argv):
    """Delete transcripts from the vault. Dry run unless --yes.

      forget --slug the-daily-stoic
      forget --slug x --before 2024-01-01      (published strictly before)
      forget --slug x --after 2024-01-01       (published strictly after)
      forget --episode a886c7099a70

    Meilisearch catches up on its own: the next full pass stops writing those
    ids and prune() drops them. Nothing here talks to Meili.

    At least one filter is required -- there is no "forget everything", because
    a typo that wipes the vault is unrecoverable without a restore.
    """
    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else None

    where, params = [], []
    for flag, sql in (("--slug", "slug = ?"), ("--episode", "episode_id = ?"),
                      ("--before", "published < ?"), ("--after", "published > ?")):
        if (v := opt(flag)) is not None:
            where.append(sql)
            params.append(v)
    if not where:
        raise SystemExit("forget: need at least one of "
                         "--slug / --episode / --before / --after")

    clause = " AND ".join(where)
    con = vault_conn()
    try:
        rows = con.execute(
            f"SELECT slug, episode_id, title, published FROM transcripts WHERE {clause}"  # noqa: S608
            " ORDER BY published", params).fetchall()
        if not rows:
            print("no vault rows match")
            return

        # An episode MinusPod still holds gets re-exported on the next pass, so
        # deleting it here just churns. Say so instead of pretending it worked.
        live = set()
        if DB.exists():
            mp = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            try:
                live = {(a, b) for a, b in mp.execute(
                    "SELECT p.slug, e.episode_id FROM episodes e"
                    " JOIN podcasts p ON p.id = e.podcast_id"
                    " JOIN episode_details d ON d.episode_id = e.id")}
            finally:
                mp.close()

        doomed = [r for r in rows if (r["slug"], r["episode_id"]) not in live]
        skipped = len(rows) - len(doomed)
        for r in doomed[:10]:
            print(f"  {r['published']}  {r['slug']}  {(r['title'] or '')[:60]}")
        if len(doomed) > 10:
            print(f"  ... and {len(doomed) - 10} more")
        if skipped:
            print(f"  skipping {skipped} still live in MinusPod "
                  f"(delete them there first, or they come back next pass)")
        if not doomed:
            return
        if "--yes" not in argv:
            print(f"would delete {len(doomed)} transcripts. Re-run with --yes.")
            return
        with con:
            con.executemany("DELETE FROM transcripts WHERE slug = ? AND episode_id = ?",
                            [(r["slug"], r["episode_id"]) for r in doomed])
        print(f"deleted {len(doomed)} transcripts from the vault")
    finally:
        con.close()


def prune(c: httpx.Client, seen: list[str], live_ids: set[str]) -> None:
    """Drop documents this full pass did not write.

    Chunk ids are sha1(slug:episode_id:index), so re-chunking an episode (or
    dropping it from MinusPod) orphans its old chunks: `push` overwrites the
    ids that still exist and silently leaves the rest. They keep answering
    searches with whatever URL was correct when they were written, which is how
    5813 documents survived with dead http://localhost:8077 links.

    Only safe on a full pass. With --since, `docs` covers a slice of the corpus
    and every untouched id would look stale. `seen` is every id in the index,
    as read by fetch_indexed() at the start of the pass.
    """
    stale = [i for i in seen if i not in live_ids]
    if not stale:
        return
    # Guard, because this runs unattended every hour and deletes. A half-read
    # podcast.db (mid-write, or a failed join returning few rows) would make
    # most of the index look stale, and re-embedding 86k chunks through Ollama
    # to recover is hours. Orphans are cosmetic; a wiped index is not.
    if seen and len(stale) > len(seen) * 0.25:
        print(f"  REFUSING to prune {len(stale)}/{len(seen)} chunks: that is >25% "
              f"of the index and looks like a bad read, not real churn. "
              f"Run `podcast_indexer.py index` by hand if this is legitimate.")
        return
    c.post(f"/indexes/{INDEX}/documents/delete-batch", json=stale).raise_for_status()
    print(f"  pruned {len(stale)} orphaned chunks")


def cmd_search(argv):
    q = argv[0]
    with client() as c:
        r = c.post(f"/indexes/{INDEX}/search",
                   json={"q": q, "limit": 8, "filter": "isAd = false",
                         "attributesToHighlight": ["text"]})
        r.raise_for_status()
        for h in r.json().get("hits", []):
            mm = int(h["start"] // 60)
            tag = " [retired]" if h.get("retired") else ""
            print(f"\n  {h['podcast']} — {h['episodeTitle'][:52]}"
                  f"  @{mm}:{int(h['start']%60):02d}{tag}")
            print(f"    {h['text'][:190]}")
            print(f"    {h['url']}")


def cmd_serve(argv):
    """Full pass on boot, incremental on the interval, full every Nth pass.

    This used to run a FULL pass every hour, pushing all ~114k chunks. The old
    docstring's reasoning was sound at the time: Meilisearch skips re-embedding
    unchanged documents, so a repeat pass was cheap and never re-hit Ollama.
    That stopped being true when push() had to drop to 40-document batches with
    a wait per task to dodge the v1.21 embedding-drop bug (see push()). Cheap
    embedding does not make the pass cheap any more, because 114k chunks is now
    ~2,850 sequential LMDB commits regardless of whether anything changed.

    So the interval pass is incremental. The `--since` window is deliberately
    several times the interval: episodes only enter the DB when processing
    finishes, and overlapping windows is much cheaper than missing an episode
    that landed just after a pass started.

    Full passes still have to happen, because `prune` (dropping chunks for
    deleted episodes) and the transcript vault export both only run when
    `since is None`. Every 24th pass by default, so daily at the 1h interval.

    Measured 2026-10-13: a full pass over 114,148 already-embedded chunks took
    2081s (~35 min) at ~55 docs/s, against ~4-9 docs/s when the same documents
    were being embedded for the first time. That gap is the cost of the commits
    alone, and it is the number to watch if the interval is ever shortened.

    2026-11-16: every pass now diffs against what Meilisearch already holds
    (fetch_indexed + changed) and pushes only new or changed chunks. A restart
    recreated the stack and the boot FULL pass re-pushed all 119k chunks: ~11h
    of LMDB commits at ~1,000 small writes/s on warrig's spinning disk, for a
    corpus where almost nothing had changed. The boot pass is still FULL, but a
    FULL pass now costs one paged read of the index plus whatever really
    changed. `podcast_indexer.py index --force` pushes everything regardless.
    """
    interval = int(os.environ.get("PODCAST_INDEX_INTERVAL", "3600"))
    full_every = int(os.environ.get("PODCAST_FULL_EVERY", "24"))
    passes = 0
    while True:
        full = passes % full_every == 0
        try:
            started = time.time()
            if full:
                print(f"pass {passes}: FULL (prune + vault)", flush=True)
                cmd_index([])
            else:
                window = interval * 3
                print(f"pass {passes}: incremental, since {window}s", flush=True)
                cmd_index(["--since", str(window)])
            print(f"pass {passes} took {time.time() - started:.0f}s", flush=True)
        except Exception as exc:
            print(f"index pass failed: {exc!r}", flush=True)
        passes += 1
        time.sleep(interval)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "index"
    {"search": cmd_search, "serve": cmd_serve,
     "forget": cmd_forget}.get(cmd, cmd_index)(sys.argv[2:])
