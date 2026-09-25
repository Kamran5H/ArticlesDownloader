"""Shared fixtures: import the app module, isolate history/downloads, block the network."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import Articles_v2 as art  # noqa: E402


class FakeResponse:
    def __init__(self, content=b"", status_code=200, headers=None, url="https://example.org/x"):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.text = content.decode("utf-8", "ignore") if isinstance(content, bytes) else str(content)

    def json(self):
        import json
        return json.loads(self.text)


def make_pdf(size=20_000) -> bytes:
    body = b"%PDF-1.7\n" + b"0" * size + b"\n%%EOF\n"
    return body


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own history DB and default download folder, and no real HTTP."""
    monkeypatch.setattr(art, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(art, "get_default_save_folder", lambda: tmp_path / "downloads")

    def _no_network(*_a, **_k):
        raise AssertionError("Real network access attempted in a unit test")

    monkeypatch.setattr(art, "safe_get", lambda *a, **k: None)
    monkeypatch.setattr(art.requests, "get", _no_network)
    monkeypatch.setattr(art, "_fetch_unpaywall_mirrors", lambda doi: [])
    monkeypatch.setattr(art, "_fetch_scihub_mirrors", lambda doi: [])
    monkeypatch.setattr(art, "jitter", lambda *a, **k: None)
    art.cancellation_event.clear()
    yield tmp_path


@pytest.fixture
def fake_sources(monkeypatch):
    """Replace all harvesters with two deterministic in-memory sources and PDF fetching
    with a stub that serves a valid PDF for every URL containing '/ok/'."""
    def mk(title, doi, journal, issn, cits, year="2024", ok=True, abstract=""):
        return art.Paper(
            url=f"https://repo.example.org/{'ok' if ok else 'bad'}/{doi.split('/')[-1]}.pdf",
            title=title, doi=doi, authors=["Ashraf, Kamran", "Doe, John"], year=year,
            journal=journal, issns=[issn] if issn else [], citations=cits, abstract=abstract,
            relevance_score=0.0, source="Fake",
        )

    corpus_a = [
        mk("Zinc air battery electrocatalyst with cobalt nitrogen carbon", "10.1000/za1",
           "Journal of Power Sources", "0378-7753", 120),
        mk("Bifunctional electrocatalyst for rechargeable zinc air battery", "10.1000/za2",
           "Advanced Energy Materials", "", 80),
        mk("Zinc air battery electrocatalyst preprint", "10.1000/za3", "arXiv preprint", "", 3, ok=True),
    ]
    corpus_b = [
        mk("Zinc air battery electrocatalyst stability study", "10.1000/za4",
           "Journal of Power Sources", "0378-7753", 40, ok=False),
        mk("Zinc air battery electrocatalyst with cobalt nitrogen carbon", "10.1000/za1",
           "Journal of Power Sources", "0378-7753", 120),   # duplicate of corpus_a[0]
    ]

    def harvester(corpus):
        def fn(keywords, y1, y2, max_res=100, ctx=None, focus="", min_relevance=0.6):
            out = []
            for p in corpus:
                ok, score, _ = art.check_paper_relevance(p.title, p.abstract, keywords, focus, min_relevance)
                if ok:
                    q = art.Paper(**{**p.__dict__, "relevance_score": score,
                                     "authors": list(p.authors), "issns": list(p.issns),
                                     "candidate_urls": [], "concepts": []})
                    art.add_paper_candidate(out, q)
            return out
        return fn

    registry = {
        "openalex": ("OpenAlex", harvester(corpus_a), 200),
        "crossref": ("Crossref", harvester(corpus_b), 200),
    }
    monkeypatch.setattr(art, "HARVESTER_REGISTRY", registry)
    monkeypatch.setattr(art, "ALL_SOURCES", list(registry))
    monkeypatch.setattr(art, "enrich_with_openalex", lambda papers, ctx=None: papers)

    fetched = []

    def fake_fetch(url, profile, ctx):
        fetched.append(url)
        if "/ok/" in url:
            return FakeResponse(make_pdf(), headers={"Content-Type": "application/pdf"}, url=url)
        return FakeResponse(b"<html>paywall</html>", status_code=403, headers={"Content-Type": "text/html"}, url=url)

    monkeypatch.setattr(art, "_fetch_document", fake_fetch)
    return {"fetched": fetched}
