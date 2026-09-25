"""End-to-end pipeline tests with in-memory sources and a fake PDF server."""
import Articles_v2 as art


def test_full_download_workflow(tmp_path, fake_sources):
    out = tmp_path / "out"
    papers_seen = []
    phases = []
    got = art.execute_research_workflow(
        keywords="zinc air battery electrocatalyst", max_articles=5, save_folder=out,
        quartile_filter="all", paper_callback=lambda p, r: papers_seen.append(p.title),
        phase_callback=phases.append,
    )
    titles = sorted(p.title for p in got)
    # za1 + za2 + za3 are downloadable (za1 appears twice but is de-duplicated); za4 is paywalled
    assert len(got) == 3 and len(set(titles)) == 3
    assert phases[:3] == ["harvesting", "ranking", "downloading"] and "citations" in phases
    assert len(papers_seen) == 3
    for f in ("references.bib", "references.ris", "references_APA.txt", "results.csv", "corpus_metadata.json"):
        assert (out / f).exists(), f
    q1_files = list((out / "Q1_Q2").glob("*.pdf"))
    assert len(q1_files) == 2                        # Power Sources + Adv. Energy Mater. are Q1
    assert list((out / "Q3_Q4").glob("*.pdf"))       # the preprint
    assert art.query_seen_count(art.normalize_query("zinc air battery electrocatalyst")) == 3


def test_quartile_filter_excludes_unranked(tmp_path, fake_sources):
    got = art.execute_research_workflow(keywords="zinc air battery electrocatalyst", max_articles=5,
                                        save_folder=tmp_path, quartile_filter="q1_q2")
    assert got and all(p.quartile in ("Q1", "Q2") for p in got)


def test_preview_downloads_nothing(tmp_path, fake_sources):
    ranked = art.execute_research_workflow(keywords="zinc air battery electrocatalyst", max_articles=5,
                                           save_folder=tmp_path, quartile_filter="all", download=False)
    assert len(ranked) == 4 and not fake_sources["fetched"]
    assert ranked[0].quartile == "Q1" and ranked[0].citations == 120   # quartile, then citations
    assert not list(tmp_path.rglob("*.pdf"))


def test_incremental_mode_skips_known_papers(tmp_path, fake_sources):
    kw = "zinc air battery electrocatalyst"
    first = art.execute_research_workflow(keywords=kw, max_articles=5, save_folder=tmp_path / "a", quartile_filter="all")
    again = art.execute_research_workflow(keywords=kw, max_articles=5, save_folder=tmp_path / "b",
                                          quartile_filter="all", mode="incremental")
    assert len(first) == 3 and again == []


def test_target_is_respected(tmp_path, fake_sources):
    got = art.execute_research_workflow(keywords="zinc air battery electrocatalyst", max_articles=1,
                                        save_folder=tmp_path, quartile_filter="all")
    assert len(got) == 1 and len(list(tmp_path.rglob("*.pdf"))) == 1


def test_cancel_before_start_returns_nothing(tmp_path, fake_sources):
    ctx = art.DownloadContext(7, 5, tmp_path)
    ctx.cancellation_event.set()
    assert art.execute_research_workflow(keywords="zinc air battery electrocatalyst", ctx=ctx,
                                         save_folder=tmp_path, quartile_filter="all") == []


def test_scihub_is_opt_in(tmp_path, fake_sources, monkeypatch):
    calls = []
    monkeypatch.setattr(art, "_fetch_scihub_mirrors", lambda doi: calls.append(doi) or [])
    art.execute_research_workflow(keywords="zinc air battery electrocatalyst", max_articles=5,
                                  save_folder=tmp_path, quartile_filter="all")
    assert calls == []                                   # off by default
    ctx = art.DownloadContext(2, 5, tmp_path / "s", allow_scihub=True)
    art.execute_research_workflow(keywords="zinc air battery electrocatalyst", max_articles=5, ctx=ctx,
                                  save_folder=tmp_path / "s", quartile_filter="all")
    assert "10.1000/za4" in calls                        # only the paywalled paper needed it


def test_doi_workflow_with_crossref_fallback(tmp_path, fake_sources, monkeypatch):
    import json
    from conftest import FakeResponse

    def fake_get(url, ctx=None, **kw):
        if "openalex" in url:
            return FakeResponse(json.dumps({"results": []}).encode())
        if "crossref" in url:
            return FakeResponse(json.dumps({"message": {
                "title": ["Known paper"], "author": [{"family": "Doe", "given": "Jane"}],
                "container-title": ["Journal of Power Sources"], "ISSN": ["0378-7753"],
                "published": {"date-parts": [[2023]]},
                "link": [{"URL": "https://repo.example.org/ok/known.pdf"}]}}).encode())
        return None

    monkeypatch.setattr(art, "safe_get", fake_get)
    got = art.execute_doi_workflow("see https://doi.org/10.5555/known here", tmp_path)
    assert len(got) == 1 and got[0].title == "Known paper" and got[0].quartile == "Q1"
    assert art.history_topics()[0]["query"] == "doi list"


def test_history_helpers(tmp_path, fake_sources):
    art.execute_research_workflow(keywords="zinc air battery electrocatalyst", max_articles=5,
                                  save_folder=tmp_path, quartile_filter="all")
    topics = art.history_topics()
    assert topics[0]["count"] == 3 and topics[0]["high_impact"] == 2
    assert art.history_topics("cobalt")                 # searches paper titles too
    rows = art.history_papers(topics[0]["query"])
    assert len(rows) == 3 and all(r["exists"] for r in rows) and rows[0]["authors"]
    assert tmp_path.resolve() / "Q1_Q2" in art.history_download_roots()
    assert art.delete_history(topics[0]["query"]) == 3 and art.history_topics() == []
