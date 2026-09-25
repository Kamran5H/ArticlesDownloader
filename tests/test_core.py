"""Offline unit tests for parsing, relevance, ranking and citation formatting."""
import json

import pytest

import Articles_v2 as art
from conftest import FakeResponse


# ── Authors & citations ──────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("Kamran Ashraf", ("Ashraf", "K.")),
    ("Ashraf, Kamran", ("Ashraf", "K.")),
    ("Ashraf, K.", ("Ashraf", "K.")),
    ("John Michael Doe", ("Doe", "J. M.")),
    ("Doe, John Michael", ("Doe", "J. M.")),
    ("A. B. Smith", ("Smith", "A. B.")),
    ("SingleName", ("SingleName", "")),
    ("", ("", "")),
])
def test_parse_author_name(raw, expected):
    assert art._parse_author_name(raw) == expected


def test_authors_apa_rules():
    assert art._authors_apa(["Ashraf, Kamran", "John Michael Doe", "Smith, A. B."]) == \
        "Ashraf, K., Doe, J. M., & Smith, A. B."
    many = [f"Author{i}, A." for i in range(25)]
    out = art._authors_apa(many)
    assert out.count(",") > 19 and "... Author24, A." in out and "&" not in out


def _paper(**kw):
    base = dict(title="Advanced Catalysts for Zn–Air Batteries", doi="10.1016/j.jpowsour.2024.1",
                authors=["Ashraf, Kamran", "Doe, John"], year="2024", journal="Journal of Power Sources",
                citations=5, quartile="Q1", relevance_score=0.9, url="https://doi.org/10.1016/j.jpowsour.2024.1")
    base.update(kw)
    return art.Paper(**base)


def test_bibtex_keys_unique_and_escaped():
    p1, p2 = _paper(title="Heat & Mass_Transfer 100%"), _paper()
    text = art.format_citations([p1, p2], "bib")
    assert "@article{ashraf2024," in text and "@article{ashraf2024a," in text
    assert r"Heat \& Mass\_Transfer 100\%" in text


def test_bibtex_preprint_is_misc():
    assert art.format_bibtex(_paper(journal="arXiv preprint", quartile="Preprint")).startswith("@misc{")


def test_apa_punctuation_and_fallbacks():
    assert art.format_apa(_paper(title="Does it work?")).count("?.") == 0
    no_meta = art.format_apa(art.Paper(title="T", url="https://x.org/a"))
    assert no_meta.startswith("Unknown author (n.d.). T.") and no_meta.endswith("https://x.org/a")


def test_ris_record_fields():
    ris = art.format_ris(_paper(abstract="Line one\nline two", issns=["0378-7753"]))
    assert ris.startswith("TY  - JOUR") and ris.rstrip().endswith("ER  -")
    assert "AU  - Ashraf, K." in ris and "SN  - 0378-7753" in ris and "AB  - Line one line two" in ris
    assert art.format_ris(_paper(journal="arXiv preprint")).startswith("TY  - UNPB")


def test_format_citations_rejects_unknown():
    with pytest.raises(ValueError):
        art.format_citations([_paper()], "docx")


def test_write_bibliography_files(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    p = _paper(pdf_path=str(pdf), relevance_score=0.92, issns=["0378-7753"])
    art.write_bibliography([p], tmp_path)
    assert "relevance: 92%" in (tmp_path / "references.bib").read_text(encoding="utf-8")
    assert "92%" in (tmp_path / "results.csv").read_text(encoding="utf-8-sig")
    corpus = json.loads((tmp_path / "corpus_metadata.json").read_text(encoding="utf-8"))
    assert corpus[0]["relevance_score"] == 0.92 and corpus[0]["quartile"] == "Q1"
    assert (tmp_path / "references.ris").exists() and (tmp_path / "references_APA.txt").exists()


# ── Text cleaning ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("<i>In situ</i> Raman of &amp;beta;-MnO<sub>2</sub> for Zn&ndash;air batteries",
     "In situ Raman of β-MnO2 for Zn–air batteries"),
    ("Advanced &quot;Smart&quot; Catalysts: A &lt;Review&gt;", 'Advanced "Smart" Catalysts: A <Review>'),
    ("  Multiple   spaces\nand\ttabs  ", "Multiple spaces and tabs"),
    ("", "Untitled"),
])
def test_clean_title(raw, expected):
    assert art.clean_title(raw) == expected


def test_reconstruct_abstract():
    assert art._reconstruct_abstract({"world": [1], "hello": [0]}) == "hello world"
    assert art._reconstruct_abstract(None) == ""


def test_parse_doi_list_dedupes_and_strips():
    text = "https://doi.org/10.1038/s41586-020-2649-2, 10.3390/ma14010001.\n10.1038/S41586-020-2649-2 junk"
    assert art.parse_doi_list(text) == ["10.1038/s41586-020-2649-2", "10.3390/ma14010001"]


def test_normalize_query_is_shared_key():
    assert art.normalize_query("Zinc Air battery", "battery DFT") == "zinc air battery dft"
    assert art.normalize_query("LATP", "") == "latp"


# ── Scimago ──────────────────────────────────────────────────────────────────
def test_norm_issn_rejects_blanks():
    assert art._norm_issn("") == "" and art._norm_issn("-") == "" and art._norm_issn("0000-0000") == ""
    assert art._norm_issn("0378-7753") == "03787753" and art._norm_issn("1234-567x") == "1234567X"


def test_scimago_lookup():
    issn_map, title_map = art.load_scimago_quartiles()
    assert len(issn_map) > 10000 and len(title_map) > 10000
    assert "00000000" not in issn_map
    assert art.quartile_for(["00079235"]) == "Q1"
    assert art.quartile_for([], "Journal of Power Sources") == "Q1"
    assert art.quartile_for([], "Adv. Energy Mater.") == "Q1"
    assert art.quartile_for([], "ACS Appl. Mater. Interfaces") == "Q1"
    assert art.quartile_for([""], "") == ""


# ── Relevance engine ─────────────────────────────────────────────────────────
def test_relevance_threshold():
    kw = "zinc air battery electrocatalyst"
    assert art.check_paper_relevance("Recent advances in Zn-air battery electrocatalysts", "", kw)[0]
    assert art.check_paper_relevance("Rechargeable zinc-air batteries with high stability",
                                     "We explore oxygen electrocatalyst reactions.", kw)[0]
    assert not art.check_paper_relevance("Sodium-ion battery degradation under fast charging", "", kw)[0]
    assert not art.check_paper_relevance("Deep learning for autonomous vehicle path planning", "", kw)[0]


def test_collision_guard_and_errata():
    ok, _, _ = art.check_paper_relevance(
        "Influence of LiBF4 sintering aid on the ionic conductivity of LATP",
        "LATP solid electrolyte for lithium metal battery", "LATP", "recent")
    assert ok
    for bad in ["Local anaesthetic transperineal (LATP) prostate biopsy using a reusable guide",
                "LATPS, a novel prognostic signature based on tumor microenvironment",
                "Correction: Conformal LATP surface engineering for Ni-rich cathodes",
                "MP30-18 ANTIBIOTIC FREE LOCAL ANAESTHETIC TRANSPERINEAL BIOPSY"]:
        assert not art.check_paper_relevance(bad, "", "LATP")[0], bad


def test_extract_keywords_splits_long_titles():
    parts = art.extract_keywords("Recent advances in zinc air battery electrocatalysts and LATP solid electrolytes")
    assert len(parts) >= 2 and any("zinc air battery" in p.lower() for p in parts)
    assert art.extract_keywords("zinc air battery") == ["zinc air battery"]


# ── Sources, ranking, scraping ───────────────────────────────────────────────
def test_resolve_sources():
    assert art.resolve_sources(None) == art.ALL_SOURCES
    assert art.resolve_sources("arxiv, Semantic Scholar,bogus") == ["arxiv", "semanticscholar"]
    assert art.resolve_sources("arxiv openalex") == ["arxiv", "openalex"]
    assert art.resolve_sources(["nothing"]) == art.ALL_SOURCES


def test_rank_papers_strategies():
    a = _paper(title="a", quartile="Q2", citations=50, year="2020", relevance_score=0.7)
    b = _paper(title="b", quartile="Q1", citations=10, year="2024", relevance_score=0.9)
    c = _paper(title="c", quartile="Unranked", citations=99, year="2022", relevance_score=0.6)
    assert [p.title for p in art.rank_papers([a, b, c], "quartile_cits")] == ["b", "a", "c"]
    assert [p.title for p in art.rank_papers([a, b, c], "citations")] == ["c", "a", "b"]
    assert [p.title for p in art.rank_papers([a, b, c], "newest")] == ["b", "c", "a"]
    assert [p.title for p in art.rank_papers([a, b, c], "relevance")] == ["b", "a", "c"]


def test_scraper_does_not_reject_hosts_containing_ris():
    html = '<html><head><meta name="citation_pdf_url" content="https://hal.univ-paris.fr/doc/file.pdf"></head></html>'
    assert art._scrape_pdf_from_html(html, "https://hal.univ-paris.fr/doc") == "https://hal.univ-paris.fr/doc/file.pdf"
    html2 = '<a href="/export/citation.ris">RIS</a><a href="/files/paper.pdf">PDF</a>'
    assert art._scrape_pdf_from_html(html2, "https://x.org/a") == "https://x.org/files/paper.pdf"


def test_synthesized_publisher_urls():
    urls = art._synthesize_direct_pdf_urls("https://www.mdpi.com/1996-1944/14/1/1", "10.3390/ma14010001")
    assert "https://www.mdpi.com/10.3390/ma14010001/pdf" in urls
    assert art._synthesize_direct_pdf_urls("", "10.1007/s10008-020-1")[0].endswith("s10008-020-1.pdf")


def test_looks_like_pdf_and_tls_detection():
    assert art._looks_like_pdf(b"\xef\xbb\xbf%PDF-1.5 ...") and not art._looks_like_pdf(b"<html>")
    assert art._is_tls_error(Exception("SSL: CERTIFICATE_VERIFY_FAILED"))
    assert not art._is_tls_error(Exception("HTTP 404"))


# ── Harvester request shaping (no network: safe_get is captured) ─────────────
def test_arxiv_query_uses_and_and_dates(monkeypatch):
    seen = {}

    def fake_get(url, ctx=None, **kw):
        seen.update(kw.get("params", {}))
        return FakeResponse(b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')

    monkeypatch.setattr(art, "safe_get", fake_get)
    art.harvest_arxiv("zinc air battery", "2022", "2024")
    q = seen["search_query"]
    assert "all:zinc AND all:air AND all:battery" in q and "submittedDate:[202201010000 TO 202412312359]" in q


def test_pubmed_reads_doi_from_articleids(monkeypatch):
    def fake_get(url, ctx=None, **kw):
        if "esearch" in url:
            return FakeResponse(json.dumps({"esearchresult": {"idlist": ["123"]}}).encode())
        return FakeResponse(json.dumps({"result": {"123": {
            "title": "Zinc air battery electrocatalyst review", "authors": [{"name": "Doe J"}],
            "source": "J Power Sources", "pubdate": "2024 Jan",
            "articleids": [{"idtype": "pmcid", "value": "PMC123"}, {"idtype": "doi", "value": "10.1016/abc"}],
        }}}).encode())

    monkeypatch.setattr(art, "safe_get", fake_get)
    papers = art.harvest_pubmed("zinc air battery electrocatalyst", "2020", "2025")
    assert len(papers) == 1 and papers[0].doi == "10.1016/abc" and papers[0].pmcid == "PMC123"


def test_harvesters_survive_html_error_pages(monkeypatch):
    monkeypatch.setattr(art, "safe_get", lambda *a, **k: FakeResponse(b"<html>oops</html>"))
    for fn in (art.harvest_openalex, art.harvest_crossref, art.harvest_europepmc, art.harvest_doaj,
               art.harvest_semantic_scholar, art.harvest_core, art.harvest_base):
        assert fn("zinc air battery", "2020", "2024") == []


def test_cli_parser_defaults_and_aliases():
    a = art.parse_args(["--query", "LATP", "--doi", "10.1/x", "--doi", "10.1/y", "--sources", "arxiv"])
    assert a.keywords == "LATP" and a.doi == ["10.1/x", "10.1/y"] and not a.allow_scihub
    assert a.end_year == str(art.CURRENT_YEAR)
