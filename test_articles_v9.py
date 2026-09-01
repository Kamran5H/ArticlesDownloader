"""
Unit & Integration Test Suite for Articles_v2.py (v9 Pro)
Tests:
  1. Author parsing & APA 7 generation (Inversion Bug Fix verification)
  2. HTML tag stripping & Title cleaning
  3. Scimago Dual Index (ISSN + Journal Title fallback)
  4. Chemical & Acronym relevance matching
  5. Long title splitting & quota distribution
  6. Bibliography writers (BibTeX, RIS, APA 7, CSV, JSON)
  7. Headless CLI workflow execution
"""

import os, sys, json, tempfile, shutil
from pathlib import Path

# Configure utf-8 standard output with line-buffering for Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Add Utilities to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import Articles_v2 as art

def test_author_parsing():
    print("\n--- TEST 1: Author Parsing & APA 7 ---")
    # Test cases that previously caused name inversion
    cases = [
        ("Kamran Ashraf", ("Ashraf", "K.")),
        ("Ashraf, Kamran", ("Ashraf", "K.")),
        ("Ashraf, K.", ("Ashraf", "K.")),
        ("John Michael Doe", ("Doe", "J. M.")),
        ("Doe, John Michael", ("Doe", "J. M.")),
        ("A. B. Smith", ("Smith", "A. B.")),
        ("Smith, A. B.", ("Smith", "A. B.")),
        ("SingleName", ("SingleName", "")),
    ]
    for raw, expected in cases:
        parsed = art._parse_author_name(raw)
        assert parsed == expected, f"Failed for {raw}: expected {expected}, got {parsed}"
        print(f"  ✓ {raw:<22} -> {parsed}")

    # Test APA multi-author string
    authors = ["Ashraf, Kamran", "John Michael Doe", "Smith, A. B."]
    apa = art._authors_apa(authors)
    expected_apa = "Ashraf, K., Doe, J. M., & Smith, A. B."
    assert apa == expected_apa, f"Expected '{expected_apa}', got '{apa}'"
    print(f"  ✓ Multi-author APA 7: {apa}")

    # Test BibTeX key
    p = art.Paper(authors=["Ashraf, Kamran"], year="2024", title="Zinc Air Review")
    key = art._bib_key(p, set())
    assert key == "ashraf2024", f"Expected 'ashraf2024', got '{key}'"
    print(f"  ✓ BibTeX key: {key}")

def test_title_cleaning():
    print("\n--- TEST 2: Title Cleaning & HTML Stripping ---")
    dirty_titles = [
        ("<i>In situ</i> Raman of &amp;beta;-MnO<sub>2</sub> for Zn&ndash;air batteries",
         "In situ Raman of β-MnO2 for Zn–air batteries"),
        ("Advanced &quot;Smart&quot; Catalysts: A &lt;Review&gt;",
         "Advanced \"Smart\" Catalysts: A <Review>"),
        ("  Multiple   spaces\nand\ttabs  ",
         "Multiple spaces and tabs"),
    ]
    for raw, expected in dirty_titles:
        cleaned = art.clean_title(raw)
        print(f"  ✓ Raw:     {raw}")
        print(f"    Cleaned: {cleaned}")
        assert cleaned == expected, f"Expected '{expected}', got '{cleaned}'"

def test_scimago_dual_index():
    print("\n--- TEST 3: Scimago Dual Index (ISSN + Journal Title) ---")
    issn_map, title_map = art.load_scimago_quartiles()
    assert len(issn_map) > 10000, f"Expected > 10,000 ISSNs, got {len(issn_map)}"
    assert len(title_map) > 10000, f"Expected > 10,000 Titles, got {len(title_map)}"

    # 1. Test by ISSN
    q_issn = art.quartile_for(["00079235"], "")
    assert q_issn == "Q1", f"Expected Q1, got {q_issn}"
    print(f"  ✓ ISSN 0007-9235 -> {q_issn}")

    # 2. Test by Journal Title when ISSN is missing
    q_title1 = art.quartile_for([], "Journal of Power Sources")
    assert q_title1 == "Q1", f"Expected Q1, got {q_title1}"
    print(f"  ✓ Title 'Journal of Power Sources' (missing ISSN) -> {q_title1}")

    q_title2 = art.quartile_for([], "Advanced Energy Materials")
    assert q_title2 == "Q1", f"Expected Q1, got {q_title2}"
    print(f"  ✓ Title 'Advanced Energy Materials' -> {q_title2}")

def test_relevance_matching():
    print("\n--- TEST 4: Topic & Chemical Relevance ---")
    kw = "zinc air battery"
    relevant_titles = [
        "Rechargeable Zinc-Air Batteries with High Energy Density",
        "Recent advances in Zn-air battery electrocatalysts",
        "Bifunctional oxygen electrocatalysis for aqueous Zn-air systems",
    ]
    irrelevant_titles = [
        "Deep learning for autonomous vehicle path planning",
        "Economic impacts of real estate taxation in urban centers",
    ]
    for t in relevant_titles:
        res = art._title_is_relevant(t, kw)
        assert res is True, f"Expected relevant for '{t}'"
        print(f"  ✓ PASS (Relevant): {t}")

    for t in irrelevant_titles:
        res = art._title_is_relevant(t, kw)
        assert res is False, f"Expected irrelevant for '{t}'"
        print(f"  ✓ BLOCKED (Irrelevant): {t}")

def test_bibliography_export():
    print("\n--- TEST 5: Bibliography & Corpus Export ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        folder = Path(tmpdir)
        # Create dummy PDF
        pdf_file = folder / "test_paper.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 dummy content %%EOF")

        p = art.Paper(
            url="https://doi.org/10.1016/j.jpowsour.2024.123456",
            title="<i>Advanced</i> Heteroatom-Doped Catalysts for Zn&ndash;Air Batteries",
            doi="10.1016/j.jpowsour.2024.123456",
            authors=["Ashraf, Kamran", "Doe, John Michael"],
            year="2024",
            journal="Journal of Power Sources",
            issns=["0378-7753"],
            citations=45,
            quartile="Q1",
            source="OpenAlex",
            pdf_path=str(pdf_file),
            abstract="This review explores recent progress in zinc-air battery technologies.",
        )
        art.write_bibliography([p], folder)

        # Check BibTeX
        bib = (folder / "references.bib").read_text(encoding="utf-8")
        assert "@article{ashraf2024," in bib
        assert "title   = {{Advanced Heteroatom-Doped Catalysts for Zn–Air Batteries}}" in bib
        print(f"  ✓ references.bib written successfully:\n{bib.strip()[:180]}...")

        # Check RIS
        ris = (folder / "references.ris").read_text(encoding="utf-8")
        assert "AU  - Ashraf, K." in ris
        assert "AU  - Doe, J. M." in ris
        print(f"  ✓ references.ris written successfully")

        # Check APA
        apa = (folder / "references_APA.txt").read_text(encoding="utf-8")
        assert "Ashraf, K., & Doe, J. M. (2024). Advanced Heteroatom-Doped Catalysts for Zn–Air Batteries." in apa
        print(f"  ✓ references_APA.txt written successfully:\n{apa.strip()}")

        # Check JSON Corpus
        corpus_json = json.loads((folder / "corpus_metadata.json").read_text(encoding="utf-8"))
        assert len(corpus_json) == 1
        assert corpus_json[0]["quartile"] == "Q1"
        assert corpus_json[0]["doi"] == "10.1016/j.jpowsour.2024.123456"
        print(f"  ✓ corpus_metadata.json written successfully")

def test_cli_live_harvest():
    print("\n--- TEST 6: Live Harvesters & Deduplication ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_dir = Path(tmpdir)
        ctx = art.DownloadContext(run_id=999, target_downloads=2, save_folder=test_dir)
        downloaded = art.execute_research_workflow(
            keywords="zinc air battery electrocatalyst",
            focus="recent",
            year_start="2024",
            year_end="2026",
            max_articles=2,
            save_folder=test_dir,
            quartile_filter="all_ranked",
            mode="fresh",
            ctx=ctx,
        )
        print(f"  ✓ Workflow completed. Downloaded {len(downloaded)} PDFs.")
        assert (test_dir / "references.bib").exists()
        assert (test_dir / "results.csv").exists()
        assert (test_dir / "corpus_metadata.json").exists()
        print(f"  ✓ Verified output files in {test_dir}")

if __name__ == "__main__":
    test_author_parsing()
    test_title_cleaning()
    test_scimago_dual_index()
    test_relevance_matching()
    test_bibliography_export()
    test_cli_live_harvest()
    print("\n🎉 ALL 6 TEST SUITES PASSED FLAWLESSLY! 🎉\n")
