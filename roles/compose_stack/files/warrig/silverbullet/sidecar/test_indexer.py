# /// script
# dependencies = ["httpx", "pydantic", "pydantic-settings", "structlog", "pyyaml", "cyclopts", "markdown-it-py"]
# ///
"""Checks for the note chunker and the delete reconciliation.

Run: uv run --no-project sidecar/test_indexer.py

Two things here are worth a test and the rest of indexer.py is not:

1. The chunker must keep a paragraph together with the list that follows it.
   36% of the space has that pattern and a naive splitter keeps 0.8% of them.
2. The pass must not delete documents belonging to notes that still exist.
   Before chunking, deletions were a set difference over doc ids, which was
   correct only because ids were 1:1 with paths. Chunking breaks that, and the
   old expression would have wiped the whole index every 15 seconds.
"""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "ix", pathlib.Path(__file__).with_name("indexer.py"))
ix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ix)


# --------------------------------------------------------------------------
# chunker
# --------------------------------------------------------------------------

def test_paragraph_keeps_its_list():
    body = (
        "The auto-ban pipeline works like this:\n"
        "\n"
        "- BADBEHAVIOR triggers on 5 strikes\n"
        "- Grafana alert fires\n"
        "- Windmill job calls Cloudflare\n"
    )
    chunks = ix.chunk_body(body, 0)
    assert len(chunks) == 1, f"split into {len(chunks)}: {chunks}"
    text = chunks[0][0]
    assert "works like this" in text and "Cloudflare" in text


def test_tight_paragraph_list_pair():
    """No blank line between paragraph and list. 1,688 of the 3,577 real pairs
    look like this."""
    body = "Steps to reproduce:\n- start the container\n- watch it crash\n"
    chunks = ix.chunk_body(body, 0)
    assert len(chunks) == 1, chunks


def test_heading_recorded_and_scopes_chunks():
    body = (
        "## First Section\n\nAlpha text here.\n\n"
        "## Second Section\n\nBeta text here.\n"
    )
    chunks = ix.chunk_body(body, 0)
    heads = {h for _, h, _ in chunks}
    assert "First Section" in heads and "Second Section" in heads
    # A chunk must not span two headings.
    for text, head, _ in chunks:
        if "Alpha" in text:
            assert "Beta" not in text
        if head == "Second Section":
            assert "Alpha" not in text


def test_code_fence_not_split_and_attaches_up():
    body = "Run this:\n\n```sh\ndocker ps\ndocker logs x\n```\n"
    chunks = ix.chunk_body(body, 0)
    assert len(chunks) == 1, chunks
    assert "```" in chunks[0][0]


def test_oversized_block_is_hard_split():
    body = "".join(f"line {i} of a very long verbatim transcript\n" for i in range(400))
    chunks = ix.chunk_body(body, 0)
    assert len(chunks) > 1
    assert all(ix.utf16_len(c[0]) <= ix.CHUNK_HARD_SPLIT * 2 for c in chunks)


def test_positions_are_utf16_and_offset():
    """Non-BMP emoji count as 2 UTF-16 units. SilverBullet's `@pos` is what
    CodeMirror uses, so a Python-character offset would drift by one per emoji
    and land the reader in the wrong place."""
    body = "\U0001F600 emoji first\n\n## Later Heading\n\nbody text\n"
    chunks = ix.chunk_body(body, 100)
    assert chunks[0][2] == 100
    later = [c for c in chunks if c[1] == "Later Heading"]
    assert later, chunks
    prefix = body[: body.index("## Later Heading")]
    assert later[0][2] == 100 + ix.utf16_len(prefix)
    # and that really is bigger than the Python-char count, i.e. the emoji counted twice
    assert ix.utf16_len(prefix) == len(prefix) + 1


def test_empty_body_yields_nothing():
    assert ix.chunk_body("", 0) == []
    assert ix.chunk_body("   \n\n", 0) == []


# --------------------------------------------------------------------------
# parse_page_chunked
# --------------------------------------------------------------------------

def test_chunked_ids_are_unique_and_stable():
    raw = "---\ntitle: X\n---\n\nPara one.\n\n## H\n\nPara two.\n"
    docs = ix.parse_page_chunked("Notes/X.md", raw, 123, len(raw))
    ids = [d.id for d in docs]
    assert len(ids) == len(set(ids)), ids
    again = ix.parse_page_chunked("Notes/X.md", raw, 123, len(raw))
    assert [d.id for d in again] == ids, "ids must be deterministic"
    assert all(d.path == "Notes/X.md" for d in docs)
    assert [d.chunk for d in docs] == list(range(len(docs)))


def test_frontmatter_included_in_positions():
    """SB's editor shows frontmatter, so `@pos` counts it. A chunk position
    that ignored it would land every link too early."""
    raw = "---\ntitle: X\ntags: [a]\n---\nBody starts here.\n"
    docs = ix.parse_page_chunked("N.md", raw, 1, len(raw))
    assert docs[0].pos == ix.utf16_len("---\ntitle: X\ntags: [a]\n---\n")


def test_ref_prefers_heading_and_is_encoded():
    raw = "---\nt: x\n---\n\nIntro line.\n\n## Some Header / Bits\n\nUnder it.\n"
    docs = ix.parse_page_chunked("Home lab/My Note.md", raw, 1, len(raw))
    headed = [d for d in docs if d.heading]
    assert headed, docs
    r = headed[0].ref
    assert r.startswith("Home%20lab/My%20Note.md%23"), r
    assert " " not in r and "#" not in r, r
    # slashes survive in the path but are escaped inside the heading, so the
    # heading cannot be mistaken for another path segment
    assert r.count("/") == 1, r
    # chunk with no heading falls back to an offset ref
    headless = [d for d in docs if not d.heading]
    if headless:
        assert "@" in headless[0].ref or headless[0].ref.endswith(".md")


def test_blog_path_still_single_doc():
    """reindex_blog calls parse_page directly; that contract must not change."""
    raw = "---\ntitle: P\n---\n\nA.\n\n- one\n- two\n\n## H\n\nB.\n"
    doc = ix.parse_page("post.md", raw, 1, len(raw))
    assert isinstance(doc, ix.IndexDoc)
    assert doc.id == ix.stable_id("post.md")
    assert doc.chunk == 0 and doc.heading is None


# --------------------------------------------------------------------------
# the landmine: reconciliation must not evict live notes
# --------------------------------------------------------------------------

def _reconcile(known, fs_paths):
    """Mirror of the grouping in index_pass(). If this drifts from the real
    code the test is worthless, so keep them in step."""
    ids_by_path, mtime_by_path = {}, {}
    for doc_id, (p, lm) in known.items():
        ids_by_path.setdefault(p, []).append(doc_id)
        mtime_by_path[p] = max(mtime_by_path.get(p, 0), lm)
    to_delete = [i for p, ids in ids_by_path.items() if p not in fs_paths for i in ids]
    return ids_by_path, mtime_by_path, to_delete


def test_steady_state_deletes_nothing():
    """The regression that would have wiped the index every 15 seconds."""
    known = {ix.chunk_id("A.md", i): ("A.md", 10) for i in range(5)}
    known |= {ix.chunk_id("B.md", i): ("B.md", 20) for i in range(3)}
    _, _, to_delete = _reconcile(known, {"A.md", "B.md"})
    assert to_delete == [], f"would have deleted {len(to_delete)} live chunks"


def test_old_id_scheme_would_have_wiped_everything():
    """Pin the bug itself, so nobody reintroduces the id-based difference."""
    known = {ix.chunk_id("A.md", i): ("A.md", 10) for i in range(5)}
    fs_ids = {ix.stable_id("A.md")}                       # the OLD expression
    old_to_delete = [d for d in known if d not in fs_ids]
    assert len(old_to_delete) == 5, "sanity: the old scheme really did delete everything"
    _, _, new_to_delete = _reconcile(known, {"A.md"})
    assert new_to_delete == []


def test_deleted_note_evicts_all_its_chunks():
    known = {ix.chunk_id("A.md", i): ("A.md", 10) for i in range(4)}
    known |= {ix.chunk_id("Gone.md", i): ("Gone.md", 10) for i in range(7)}
    _, _, to_delete = _reconcile(known, {"A.md"})
    assert sorted(to_delete) == sorted(ix.chunk_id("Gone.md", i) for i in range(7))


def test_shrinking_note_evicts_only_the_tail():
    known = {ix.chunk_id("A.md", i): ("A.md", 10) for i in range(6)}
    ids_by_path, _, to_delete = _reconcile(known, {"A.md"})
    new_ids = {ix.chunk_id("A.md", i) for i in range(2)}   # rewritten, now 2 chunks
    to_delete += [i for i in ids_by_path["A.md"] if i not in new_ids]
    assert sorted(to_delete) == sorted(ix.chunk_id("A.md", i) for i in range(2, 6))
    assert not (set(to_delete) & new_ids), "must never delete a surviving chunk"


def test_mtime_is_max_across_chunks():
    known = {
        ix.chunk_id("A.md", 0): ("A.md", 10),
        ix.chunk_id("A.md", 1): ("A.md", 99),
    }
    _, mtime_by_path, _ = _reconcile(known, {"A.md"})
    assert mtime_by_path["A.md"] == 99


# --------------------------------------------------------------------------
# meta pages: skipped once, then left out of planning until they change
# --------------------------------------------------------------------------

class _Src:
    def __init__(self, pages):  # name -> (mtime, raw)
        self.pages, self.fetched = pages, []

    def list_files(self):
        return [ix.FsEntry(name=n, created=0, lastModified=m, contentType="text/markdown",
                           size=len(r)) for n, (m, r) in self.pages.items()]

    def fetch_page(self, name):
        self.fetched.append(name)
        return self.pages[name][1]


class _Meili:
    def __init__(self):
        self.upserted = []

    def upsert(self, docs):
        self.upserted += docs

    def delete_many(self, ids):
        pass


def test_meta_pages_are_not_refetched_until_they_change():
    ix.META_SKIP.clear()
    settings = type("S", (), {"exclude_prefixes": []})()
    src = _Src({"Lib/T.md": (100, "---\ntags: meta\n---\nTemplate body\n"),
                "Note.md": (100, "A real note.\n")})
    meili = _Meili()
    counts, known = ix.index_pass(settings, src, meili, force_full=False, known={})
    assert counts["skipped_meta"] == 1 and sorted(src.fetched) == ["Lib/T.md", "Note.md"]

    # Next poll, nothing changed: neither page is fetched again.
    src.fetched.clear()
    counts, known = ix.index_pass(settings, src, meili, force_full=False, known=known)
    assert src.fetched == [] and counts["skipped_meta"] == 0

    # Tag removed (file rewritten, new mtime): it is fetched and indexed.
    src.pages["Lib/T.md"] = (200, "Template body, now a real page.\n")
    counts, known = ix.index_pass(settings, src, meili, force_full=False, known=known)
    assert src.fetched == ["Lib/T.md"] and counts["indexed"] >= 1
    ix.META_SKIP.clear()


def main():
    tests =[v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"indexer: {len(tests)}/{len(tests)} ok")


if __name__ == "__main__":
    main()
