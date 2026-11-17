# /// script
# dependencies = ["httpx"]
# ///
"""Check prune()'s and the diff's decision logic. Run: uv run --no-project sidecar/test_podcast_indexer.py

prune() deletes, runs unattended every hour, and its inputs come from a SQLite
file another container writes to. That combination is the only thing in this
file worth a test; everything else is read-only.
"""
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import types

spec = importlib.util.spec_from_file_location(
    "pi", pathlib.Path(__file__).with_name("podcast_indexer.py"))
pi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pi)


class FakeClient:
    """Stands in for the Meilisearch httpx client: records deletes."""

    def __init__(self, ids):
        self.ids, self.deleted = ids, None

    def post(self, path, json):
        self.deleted = json
        return types.SimpleNamespace(raise_for_status=lambda: None)


class FetchClient:
    """Serves documents/fetch pages (honouring an episodeId IN [...] filter)."""

    def __init__(self, docs):
        self.docs, self.bodies = docs, []

    def post(self, path, json):
        assert path.endswith("/documents/fetch"), path
        self.bodies.append(json)
        docs = self.docs
        if "filter" in json:
            want = json["filter"].split("[", 1)[1].rstrip("]")
            docs = [d for d in docs if f'"{d["episodeId"]}"' in want]
        page = [{k: d[k] for k in json["fields"] if k in d}
                for d in docs[json["offset"]:json["offset"] + json["limit"]]]
        body = {"results": page, "total": len(docs)}
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


def main():
    # Normal churn: a handful of re-chunked orphans go.
    c = FakeClient([f"d{i}" for i in range(100)])
    pi.prune(c, c.ids, {f"d{i}" for i in range(95)})
    assert c.deleted == [f"d{i}" for i in range(95, 100)], c.deleted

    # Steady state: no delete-batch call at all.
    c = FakeClient([f"d{i}" for i in range(100)])
    pi.prune(c, c.ids, {f"d{i}" for i in range(100)})
    assert c.deleted is None

    # Bad read of podcast.db makes 90% look stale. The guard must hold: losing
    # the index costs hours of re-embedding, orphans cost nothing.
    c = FakeClient([f"d{i}" for i in range(100)])
    pi.prune(c, c.ids, {f"d{i}" for i in range(10)})
    assert c.deleted is None, "guard let a mass delete through"

    # Exactly 25% is allowed; the guard trips on *more* than 25%.
    c = FakeClient([f"d{i}" for i in range(100)])
    pi.prune(c, c.ids, {f"d{i}" for i in range(75)})
    assert c.deleted is not None and len(c.deleted) == 25

    # The real index is >5000 docs, so the paging loop has to actually page.
    c = FakeClient([f"d{i}" for i in range(12000)])
    pi.prune(c, c.ids, {f"d{i}" for i in range(11999)})
    assert c.deleted == ["d11999"], c.deleted

    print("prune: 5/5 ok")


def diff_tests():
    """A pass must push only what Meilisearch lacks or holds differently."""
    docs = pi.build_docs([row(eid="e1", seg=SEGS), row(eid="e2", seg=SEGS)])
    assert len(docs) == 4

    # Steady state: Meili holds every chunk as built -> nothing to push. This is
    # the restart case that used to re-push the whole corpus.
    held = [dict(d) for d in docs]
    for d in held:  # Meili handing floats back as ints must not count as a change
        d["start"] = int(d["start"]) if d["start"] == int(d["start"]) else d["start"]
    assert pi.changed(docs, pi.fetch_indexed(FetchClient(held))) == []

    # One edited chunk and one missing chunk: exactly those two go.
    held = [dict(d) for d in docs[1:]]
    held[0]["text"] = "stale text"
    todo = pi.changed(docs, pi.fetch_indexed(FetchClient(held)))
    assert [d["id"] for d in todo] == [docs[0]["id"], docs[1]["id"]], todo

    # Empty index (wiped Meili): everything goes, so it self-heals.
    assert len(pi.changed(docs, pi.fetch_indexed(FetchClient([])))) == 4

    # Paging: more than one 5000-doc page is read in full.
    big = [{**docs[0], "id": f"x{i}"} for i in range(12001)]
    c = FetchClient(big)
    assert len(pi.fetch_indexed(c)) == 12001 and len(c.bodies) == 3

    # Incremental: only the touched episodes are fetched, via the filter.
    c = FetchClient([dict(d) for d in docs])
    got = pi.fetch_indexed(c, ["e2"])
    assert set(got) == {d["id"] for d in docs if d["episodeId"] == "e2"}
    assert 'episodeId IN ["e2"]' == c.bodies[0]["filter"]
    assert pi.fetch_indexed(FetchClient(docs), []) == {}

    print("diff: 7/7 ok")


def row(slug="s", eid="e1", title="T", published="2024-05-05", seg=None,
        text=None, ads=None, podcast="P"):
    """A load_episodes()-shaped row. sqlite3.Row is read-only; a dict indexes
    the same way, which is all build_docs and export_vault need."""
    return {"slug": slug, "podcast": podcast, "episode_id": eid, "title": title,
            "published_at": published,
            "final_segments_json": seg, "original_segments_json": None,
            "transcript_text": text, "original_transcript_text": None,
            "ad_markers_json": ads}


SEGS = json.dumps([{"start": 0.0, "end": 50.0, "text": "hello there"},
                   {"start": 50.0, "end": 95.0, "text": "second chunk"}])


def vault_tests():
    with tempfile.TemporaryDirectory() as d:
        pi.VAULT = pathlib.Path(d) / "transcripts.db"
        pi.DB = pathlib.Path(d) / "nonexistent.db"  # no live DB: forget skips nothing

        # Export writes once, then recognises unchanged rows and does nothing.
        rows = [row(eid="e1", seg=SEGS), row(eid="e2", seg=SEGS)]
        assert pi.export_vault(rows) == 2
        assert pi.export_vault(rows) == 0, "unchanged rows must not churn"

        # A normalize-style rewrite changes the fingerprint, so it re-exports.
        rows[0]["final_segments_json"] = SEGS.replace("hello there", "hello, there")
        assert pi.export_vault(rows) == 1

        # Live rows win: a vault episode MinusPod still has is not re-indexed.
        assert len(pi.load_vault({("s", "e1"), ("s", "e2")})) == 0
        orphans = pi.load_vault({("s", "e1")})
        assert [r["episode_id"] for r in orphans] == ["e2"]

        # Round trip: a vault row must chunk to exactly what the live row did.
        live_docs = pi.build_docs([row(eid="e2", seg=SEGS)])
        vault_docs = pi.build_docs(orphans, retired=True)
        assert [d["text"] for d in vault_docs] == [d["text"] for d in live_docs]
        assert [d["id"] for d in vault_docs] == [d["id"] for d in live_docs], \
            "ids must be stable across sources or prune would churn every pass"
        assert all(d["retired"] for d in vault_docs)
        assert not any(d["retired"] for d in live_docs)

        # A transcript with no segment JSON still survives the round trip.
        plain = [row(eid="e3", text="[00:00:00.000 --> 00:00:30.000] plain text")]
        pi.export_vault(plain)
        got = [r for r in pi.load_vault(set()) if r["episode_id"] == "e3"]
        assert pi.build_docs(got, retired=True)[0]["text"] == "plain text"

        print("vault: 8/8 ok")


def forget_tests():
    with tempfile.TemporaryDirectory() as d:
        pi.VAULT = pathlib.Path(d) / "transcripts.db"
        pi.DB = pathlib.Path(d) / "nonexistent.db"
        pi.export_vault([
            row(slug="a", eid="old", published="2020-01-01", seg=SEGS),
            row(slug="a", eid="new", published="2026-01-01", seg=SEGS),
            row(slug="b", eid="keep", published="2026-01-01", seg=SEGS),
        ])

        def ids():
            return sorted(r["episode_id"] for r in pi.load_vault(set()))

        # No filter at all must never be read as "everything".
        try:
            pi.cmd_forget(["--yes"])
            raise AssertionError("forget with no filter should refuse")
        except SystemExit:
            pass

        # Dry run is the default: it reports and changes nothing.
        pi.cmd_forget(["--slug", "a"])
        assert ids() == ["keep", "new", "old"], "dry run deleted rows"

        # --before is strict, and scoped to the slug it was given.
        pi.cmd_forget(["--slug", "a", "--before", "2021-01-01", "--yes"])
        assert ids() == ["keep", "new"]

        # Whole-show delete leaves other shows alone.
        pi.cmd_forget(["--slug", "a", "--yes"])
        assert ids() == ["keep"]

        print("forget: 4/4 ok")


def live_row_refusal():
    """An episode MinusPod still holds must not be deleted from the vault: the
    next pass would re-export it, so a 'successful' delete would silently undo."""
    with tempfile.TemporaryDirectory() as d:
        pi.VAULT = pathlib.Path(d) / "transcripts.db"
        pi.DB = pathlib.Path(d) / "podcast.db"
        mp = sqlite3.connect(pi.DB)
        mp.executescript(
            "CREATE TABLE podcasts (id INTEGER PRIMARY KEY, slug TEXT);"
            "CREATE TABLE episodes (id INTEGER PRIMARY KEY, podcast_id INTEGER,"
            " episode_id TEXT);"
            "CREATE TABLE episode_details (episode_id INTEGER);"
            "INSERT INTO podcasts VALUES (1, 'a');"
            "INSERT INTO episodes VALUES (7, 1, 'live');"
            "INSERT INTO episode_details VALUES (7);")
        mp.commit()
        mp.close()

        pi.export_vault([row(slug="a", eid="live", seg=SEGS),
                         row(slug="a", eid="gone", seg=SEGS)])
        pi.cmd_forget(["--slug", "a", "--yes"])
        left = sorted(r["episode_id"] for r in pi.load_vault(set()))
        assert left == ["live"], f"live episode should have been skipped, got {left}"

        print("live-row refusal: 1/1 ok")


if __name__ == "__main__":
    main()
    diff_tests()
    vault_tests()
    forget_tests()
    live_row_refusal()
