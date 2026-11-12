"""Regression tests for the search layer: source queries, de-duplication, relevance and ranking."""
import Articles_v2 as art
from conftest import REAL_SAFE_GET, FakeResponse, make_pdf


def _json_response(payload, status=200, headers=None):
    import json
    return FakeResponse(json.dumps(payload).encode(), status_code=status, headers=headers or {})


# ── de-duplication ────────────────────────────────────────────────────────────

def test_url_identity_keeps_the_part_that_identifies_the_paper():
    a = art.normalise_url("https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC111&blobtype=pdf")
    b = art.normalise_url("https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC222&blobtype=pdf")
    assert a != b
    assert art.normalise_url("https://X.org/p/?b=2&a=1&utm_source=mail#top") == art.normalise_url("https://x.org/p?a=1&b=2")
    assert art.normalise_url("https://x.org/paper/") == "https://x.org/paper"


def test_papers_sharing_one_pdf_endpoint_are_not_treated_as_duplicates():
    with art.lock_seen:
        art.seen_urls.clear(); art.seen_dois.clear(); art.seen_titles.clear()
    found = []
    for i in range(5):
        art.add_paper_candidate(found, art.Paper(
            url=f"https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC{1000 + i}&blobtype=pdf",
            title=f"Distinct paper number {i}"))
    assert len(found) == 5          # was 1: every paper after the first looked like a duplicate


# ── source queries ────────────────────────────────────────────────────────────

def test_crossref_pages_by_offset_in_relevance_order(monkeypatch):
    calls = []

    def fake_get(url, ctx=None, **kwargs):
        calls.append(kwargs["params"])
        items = [{"DOI": f"10.1/{kwargs['params']['offset'] + i}",
                  "title": [f"Climate change adaptation in agriculture study {kwargs['params']['offset'] + i}"]}
                 for i in range(100)]
        return _json_response({"message": {"items": items}})

    monkeypatch.setattr(art, "safe_get", fake_get)
    with art.lock_seen:
        art.seen_urls.clear(); art.seen_dois.clear(); art.seen_titles.clear()
    found = art.harvest_crossref("climate change adaptation agriculture", "2023", "2026", max_res=250)
    assert [c["offset"] for c in calls] == [0, 100]
    assert all("cursor" not in c and c["sort"] == "score" for c in calls)
    assert len(found) == 200


def test_pubmed_asks_for_relevance_order(monkeypatch):
    seen = []
    monkeypatch.setattr(art, "safe_get", lambda url, ctx=None, **k: seen.append(k["params"]) or _json_response(
        {"esearchresult": {"idlist": []}}))
    art.harvest_pubmed("crispr gene editing", "2023", "2026")
    assert seen[0]["sort"] == "relevance"


def test_openalex_key_is_sent_when_configured(monkeypatch):
    assert "api_key" not in art.openalex_params({"x": 1})
    monkeypatch.setattr(art, "OPENALEX_API_KEY", "k123")
    assert art.openalex_params({"x": 1}) == {"x": 1, "mailto": art.CONTACT_EMAIL, "api_key": "k123"}


def test_a_blocked_source_says_why_instead_of_harvested_zero(monkeypatch):
    logs = []
    monkeypatch.setattr(art, "_log", lambda msg, *a, **k: logs.append(msg))
    monkeypatch.setattr(art, "safe_get", lambda *a, **k: _json_response({"error": "budget"}, status=429))
    art._source_notes.clear()
    assert art.harvest_openalex("zinc air battery", "2023", "2026") == []
    assert "OPENALEX_API_KEY" in art._source_notes["OpenAlex"]
    assert any("OpenAlex unavailable" in m for m in logs)
    art._source_notes.clear()
    art.explain_failure("Crossref", None)
    art.explain_failure("DOAJ", FakeResponse(status_code=503))
    art.explain_failure("arXiv", FakeResponse(status_code=200))
    assert "network" in art._source_notes["Crossref"] and "503" in art._source_notes["DOAJ"]
    assert "arXiv" not in art._source_notes


def test_a_daily_quota_is_not_retried(monkeypatch):
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            return FakeResponse(status_code=429, headers={"Retry-After": "44583"})

    monkeypatch.setattr(art, "_api_session", lambda: Session())
    monkeypatch.setattr(art, "sleep_check_cancel", lambda *a, **k: False)
    r = REAL_SAFE_GET("https://api.openalex.org/works", quiet=True)
    assert r.status_code == 429 and len(calls) == 1
    calls.clear()

    class Busy(Session):
        def get(self, url, **kwargs):
            calls.append(url)
            return FakeResponse(status_code=429, headers={"Retry-After": "2"})

    monkeypatch.setattr(art, "_api_session", lambda: Busy())
    REAL_SAFE_GET("https://api.crossref.org/works", quiet=True)
    assert len(calls) == 3                       # a short wait is still retried


# ── relevance ─────────────────────────────────────────────────────────────────

def test_health_and_ai_queries_keep_clinical_papers():
    for query, title in (
        ("AI in healthcare", "Artificial intelligence (AI) for patients in clinical healthcare"),
        ("vitamin C deficiency", "Vitamin C deficiency in hospital patients"),
        ("machine learning education", "Machine learning in classroom teaching and education"),
    ):
        ok, _score, why = art.check_paper_relevance(title, "", query)
        assert ok, (query, why)


def test_battery_queries_still_reject_medical_acronym_collisions():
    ok, _s, why = art.check_paper_relevance("LATP biopsy under local anaesthetic in prostate patients", "", "LATP solid electrolyte")
    assert not ok and "collision" in why
    assert art.check_paper_relevance("LATP solid electrolyte for lithium batteries", "", "LATP solid electrolyte")[0]


def test_numbered_meeting_abstracts_are_filtered():
    assert not art.check_paper_relevance("P001 Severe vitamin C deficiency presenting as a mimic", "", "vitamin C deficiency")[0]
    assert not art.check_paper_relevance("MP30-18 Zinc air battery outcomes", "", "zinc air battery")[0]
    assert art.check_paper_relevance("Severe vitamin C deficiency in adults", "", "vitamin C deficiency")[0]
    assert art.check_paper_relevance("2D materials for zinc air battery cathodes", "", "zinc air battery")[0]
    for real_title in ("2024 Roadmap on zinc air battery research", "C60 Fullerene zinc air battery cathode",
                       "P53 and the zinc air battery", "IL17 Signaling near a zinc air battery"):
        assert art.check_paper_relevance(real_title, "", "zinc air battery")[0], real_title


def test_full_matches_rank_before_partial_matches_within_a_quartile():
    papers = [
        art.Paper(title="Vitamin D deficiency", quartile="Q1", citations=500, relevance_score=0.66),
        art.Paper(title="Vitamin C deficiency", quartile="Q1", citations=10, relevance_score=1.0),
        art.Paper(title="Vitamin C deficiency review", quartile="Q1", citations=90, relevance_score=1.0),
        art.Paper(title="Vitamin C deficiency in Q2", quartile="Q2", citations=900, relevance_score=1.0),
    ]
    order = [p.title for p in art.rank_papers(papers)]
    assert order == ["Vitamin C deficiency review", "Vitamin C deficiency", "Vitamin D deficiency",
                     "Vitamin C deficiency in Q2"]


# ── downloading & DOI mode ────────────────────────────────────────────────────

def test_an_open_pubmed_central_copy_found_by_any_route_is_downloaded(tmp_path, monkeypatch):
    fetched = []

    def fake_fetch(url, profile, ctx):
        fetched.append(url)
        if "ptpmcrender.fcgi?accid=PMC7792607" in url:
            return FakeResponse(make_pdf(), headers={"Content-Type": "application/pdf"}, url=url)
        return FakeResponse(b"<html>landing page</html>", headers={"Content-Type": "text/html"}, url=url)

    monkeypatch.setattr(art, "_fetch_document", fake_fetch)
    ctx = art.DownloadContext(1, 1, tmp_path)
    res = art.download_article(("https://doi.org/10.3390/x", "Paywalled site, open copy", "", tmp_path, ctx, None,
                                ["http://europepmc.org/pmc/articles/PMC7792607"]))
    assert res["success"], res
    assert fetched[0].startswith("https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC7792607")


ARXIV_ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Attention Is
 All You Need</title><published>2017-06-12T17:57:34Z</published><summary> We propose the Transformer. </summary>
<author><name>Ashish Vaswani</name></author><author><name>Noam Shazeer</name></author></entry></feed>"""


def test_arxiv_dois_get_real_metadata(monkeypatch):
    def fake_get(url, ctx=None, **kwargs):
        if "arxiv.org" in url:
            assert kwargs["params"]["id_list"] == "1706.03762"
            return FakeResponse(ARXIV_ATOM)
        return None                                           # OpenAlex and Crossref know nothing

    monkeypatch.setattr(art, "safe_get", fake_get)
    papers = art.fetch_papers_by_doi(["10.48550/arXiv.1706.03762", "10.9999/unknown"])
    arxiv, unknown = papers
    assert (arxiv.title, arxiv.year, arxiv.authors[0]) == ("Attention Is All You Need", "2017", "Ashish Vaswani")
    assert arxiv.url == "https://arxiv.org/pdf/1706.03762" and arxiv.quartile == "Preprint"
    assert unknown.title == "DOI 10.9999/unknown"             # never "Untitled"
