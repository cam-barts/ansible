# /// script
# dependencies = ["httpx", "pydantic", "pydantic-settings", "structlog", "pyyaml", "cyclopts", "markdown-it-py"]
# ///
"""Checks for the Karakeep mirror's decision logic.

Run: uv run --no-project sidecar/test_karakeep_indexer.py

plan() deletes unattended, and its input is an HTTP listing that can come back
short. The doc builder decides where every result click lands. Those are the
two things worth a test; the rest is plumbing.
"""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "ki", pathlib.Path(__file__).with_name("karakeep_indexer.py"))
ki = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ki)


def bm(bid="b1", url="https://example.com/a", typ="link", html="<p>hello</p>", **kw):
    content = {"type": typ, "url": url, "title": "Page", "description": "desc",
               "htmlContent": html, "crawledAt": "2026-01-01T00:00:00.000Z"}
    if typ != "link":
        content = {"type": typ, "text": "a text note", "sourceUrl": None}
    return {"id": bid, "createdAt": "2026-01-02T03:04:05.000Z", "modifiedAt": "x",
            "title": None, "note": None, "summary": None, "archived": False,
            "favourited": False, "tags": [{"name": "b"}, {"name": "a"}],
            "content": content, **kw}


def plan_tests():
    idx = {f"b{i}": ("fp", [f"c{i}"]) for i in range(100)}
    live = {f"b{i}": "fp" for i in range(100)}

    # Steady state: nothing to do.
    assert ki.plan(live, idx) == ([], [])

    # A changed fingerprint and a new bookmark are rebuilt; a removed one goes.
    live2 = dict(live, b0="new", b100="fp")
    del live2["b99"]
    todo, gone = ki.plan(live2, idx)
    assert sorted(todo) == ["b0", "b100"] and gone == ["b99"], (todo, gone)

    # A short listing (Karakeep half-up) must not wipe the index.
    todo, gone = ki.plan({f"b{i}": "fp" for i in range(10)}, idx)
    assert gone == [], "guard let a mass delete through"
    # Exactly 25% is allowed.
    assert len(ki.plan({f"b{i}": "fp" for i in range(75)}, idx)[1]) == 25

    # --force rebuilds everything that is live; empty index rebuilds all too.
    assert len(ki.plan(live, idx, force=True)[0]) == 100
    assert len(ki.plan(live, {})[0]) == 100
    print("plan: 6/6 ok")


def doc_tests():
    pub = "https://hoarder.coder.cam"
    html = "<h2>Intro</h2><p>" + "word " * 400 + "</p><script>evil()</script><p>tail</p>"
    docs = ki.build_docs(bm(html=html), ["a highlight"], "fp", pub)
    assert len(docs) >= 2, "long page should chunk"
    assert len({d.id for d in docs}) == len(docs)
    assert docs[0].id == ki.build_docs(bm(html=html), [], "fp", pub)[0].id, "ids must be stable"
    assert all("evil" not in d.body for d in docs), "script text leaked"
    assert ki.html_to_text("<p>want <em>your</em> answer</p>") == "want your answer"
    d = docs[0]
    assert d.path == d.url == "https://example.com/a"
    assert d.karakeepUrl == f"{pub}/dashboard/preview/b1"
    assert d.title == "Page" and d.folder == "example.com" and d.tags == ["a", "b"]
    assert d.createdAtTs == 1767323045 and d.highlights == ["a highlight"]

    # Text bookmark: no URL, so everything points at Karakeep's preview page.
    t = ki.build_docs(bm(typ="text"), [], "fp", pub)[0]
    assert t.url is None and t.path == f"{pub}/dashboard/preview/b1"
    assert t.folder == "karakeep" and t.title == "a text note"

    # Empty page still yields one findable doc.
    assert len(ki.build_docs(bm(html=""), [], "fp", pub)) == 1

    # Oversized pages are capped, not chunked into thousands.
    huge = ki.build_docs(bm(html="<p>" + "x " * 200_000 + "</p>"), [], "fp", pub)
    assert sum(len(x.body) for x in huge) <= ki.MAX_TEXT_CHARS
    print("docs: 10/10 ok")


def fingerprint_tests():
    base = ki.fingerprint(bm(), [])
    assert base == ki.fingerprint(bm(), [])
    assert base != ki.fingerprint(bm(), ["new highlight"])
    assert base != ki.fingerprint(bm(tags=[{"name": "c"}]), [])
    # Tag order from the API must not churn the index.
    assert base == ki.fingerprint(bm(tags=[{"name": "a"}, {"name": "b"}]), [])
    print("fingerprint: 4/4 ok")


if __name__ == "__main__":
    plan_tests()
    doc_tests()
    fingerprint_tests()
