"""
Unit & Integration Test Suite for Articles_v2.py (v10 Ultra Pro)
Tests:
  1. Author parsing & APA 7 generation
  2. HTML tag stripping & Title cleaning
  3. Scimago Dual Index (ISSN + Journal Title + Abbreviation Expansion)
  4. 60% Hybrid Title + Abstract Relevance Matching
  5. Disciplinary Collision Guard (blocks urology/prostate/pedagogy on battery queries)
  6. Non-Research & Errata Filter (blocks corrections, corrigenda, meeting abstracts)
  7. Chemical & Acronym Concept Expansion (LATP, ZAB, Li, Zn)
  8. Bibliography & Corpus Export (BibTeX, RIS, APA 7, CSV with Relevance %, JSON)
  9. Headless CLI workflow execution
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

    authors = ["Ashraf, Kamran", "John Michael Doe", "Smith, A. B."]
    apa = art._authors_apa(authors)
    expected_apa = "Ashraf, K., Doe, J. M., & Smith, A. B."
    assert apa == expected_apa, f"Expected '{expected_apa}', got '{apa}'"
    print(f"  ✓ Multi-author APA 7: {apa}")

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
        assert cleaned == expected, f"Expected '{expected}', got '{cleaned}'"
        print(f"  ✓ Cleaned: {cleaned}")

def test_scimago_abbreviation_index():
    print("\n--- TEST 3: Scimago Dual Index + Abbreviation Expansion ---")
    issn_map, title_map = art.load_scimago_quartiles()
    assert len(issn_map) > 10000, f"Expected > 10,000 ISSNs, got {len(issn_map)}"
    assert len(title_map) > 10000, f"Expected > 10,000 Titles, got {len(title_map)}"

    # 1. Test by ISSN
    q_issn = art.quartile_for(["00079235"], "")
    assert q_issn == "Q1", f"Expected Q1, got {q_issn}"
    print(f"  ✓ ISSN 0007-9235 -> {q_issn}")

    # 2. Test by exact title
    q_title = art.quartile_for([], "Journal of Power Sources")
    assert q_title == "Q1", f"Expected Q1, got {q_title}"
    print(f"  ✓ Exact Title 'Journal of Power Sources' -> {q_title}")

    # 3. Test abbreviation expansion (e.g. "Adv. Energy Mater." -> "Advanced Energy Materials")
    q_abbrev = art.quartile_for([], "Adv. Energy Mater.")
    assert q_abbrev == "Q1", f"Expected Q1, got {q_abbrev}"
    print(f"  ✓ Abbreviation 'Adv. Energy Mater.' -> {q_abbrev}")

    # 4. Test "ACS Appl. Mater. Interfaces"
    q_acs = art.quartile_for([], "ACS Appl. Mater. Interfaces")
    assert q_acs == "Q1", f"Expected Q1, got {q_acs}"
    print(f"  ✓ Abbreviation 'ACS Appl. Mater. Interfaces' -> {q_acs}")

def test_60_percent_relevance_matching():
    print("\n--- TEST 4: 60% Minimum Relevance Matching ---")
    kw = "zinc air battery electrocatalyst"

    # Paper with 100% keyword coverage (zinc, air, battery, electrocatalyst)
    t1 = "Recent advances in Zn-air battery electrocatalysts for energy storage"
    rel1, score1, _ = art.check_paper_relevance(t1, "", kw, "", min_ratio=0.60)
    assert rel1 is True and score1 >= 0.60, f"Expected >= 60%, got {score1}"
    print(f"  ✓ PASS (Score: {score1*100:.0f}%): {t1}")

    # Paper with 75% coverage (zinc, air, battery - missing electrocatalyst, has catalyst in abstract)
    t2 = "Rechargeable zinc-air batteries with high stability"
    a2 = "We explore oxygen electrocatalyst reactions in aqueous systems."
    rel2, score2, _ = art.check_paper_relevance(t2, a2, kw, "", min_ratio=0.60)
    assert rel2 is True and score2 >= 0.60, f"Expected >= 60%, got {score2}"
    print(f"  ✓ PASS (Score: {score2*100:.0f}%): {t2}")

    # Paper with only 25% coverage (only 'battery', no zinc/air/electrocatalyst)
    t3 = "Sodium-ion battery degradation mechanisms under fast charging"
    rel3, score3, _ = art.check_paper_relevance(t3, "", kw, "", min_ratio=0.60)
    assert rel3 is False and score3 < 0.60, f"Expected < 60%, got {score3}"
    print(f"  ✓ BLOCKED (Score: {score3*100:.0f}% < 60%): {t3}")

    # Totally off-topic paper
    t4 = "Deep learning for autonomous vehicle path planning"
    rel4, score4, _ = art.check_paper_relevance(t4, "", kw, "", min_ratio=0.60)
    assert rel4 is False and score4 < 0.60, f"Expected < 60%, got {score4}"
    print(f"  ✓ BLOCKED (Score: {score4*100:.0f}%): {t4}")

def test_disciplinary_collision_guard():
    print("\n--- TEST 5: Disciplinary Collision Guard ---")
    kw = "LATP"

    # 1. Valid battery paper with acronym LATP
    t_valid = "Influence of LiBF4 sintering aid on the microstructure and ionic conductivity of LATP"
    a_valid = "LATP solid electrolyte for lithium metal battery"
    rel_v, score_v, r_v = art.check_paper_relevance(t_valid, a_valid, kw, "recent", min_ratio=0.60)
    assert rel_v is True, f"Expected True for battery LATP, got {rel_v} ({r_v})"
    print(f"  ✓ ACCEPTED Technical Paper: {t_valid[:60]}... ({score_v*100:.0f}%)")

    # 2. Medical paper (Local Anaesthetic Transperineal Prostate Biopsy)
    t_med1 = "Local anaesthetic transperineal (LATP) prostate biopsy using a reusable guide"
    rel_m1, score_m1, r_m1 = art.check_paper_relevance(t_med1, "", kw, "recent", min_ratio=0.60)
    assert rel_m1 is False, f"Expected False for medical prostate LATP, got {rel_m1}"
    print(f"  ✓ BLOCKED Medical Collision: {t_med1[:60]}... ({r_m1})")

    # 3. Tumor immunology paper
    t_med2 = "LATPS, a novel prognostic signature based on tumor microenvironment"
    rel_m2, _, r_m2 = art.check_paper_relevance(t_med2, "", kw, "", min_ratio=0.60)
    assert rel_m2 is False, f"Expected False for tumor LATPS, got {rel_m2}"
    print(f"  ✓ BLOCKED Oncology Collision: {t_med2[:60]}... ({r_m2})")

    # 4. Educational / linguistics paper
    t_ped = "Perceptions of English Language Instructors on the Effect of Lexical Grammar"
    rel_ped, _, r_ped = art.check_paper_relevance(t_ped, "", kw, "", min_ratio=0.60)
    assert rel_ped is False, f"Expected False for language instructors, got {rel_ped}"
    print(f"  ✓ BLOCKED Non-STEM Collision: {t_ped[:60]}... ({r_ped})")

def test_non_research_errata_filter():
    print("\n--- TEST 6: Substantive Paper vs Errata/Abstract Filter ---")
    kw = "LATP"

    errata = [
        "Correction: Conformal LATP surface engineering for Ni-rich cathodes",
        "Correction to “Co‐Engineering of In Situ Lithium Compensation”",
        "Corrigendum to “Behaviors of lithium ions around LiCoO2 positive electrode”",
        "Erratum: Preparation of B-doped LATP solid electrolyte",
        "P040 Diagnostic value, safety, and patient-reported outcomes",
        "MP30-18 ANTIBIOTIC FREE LOCAL ANAESTHETIC TRANSPERINEAL BIOPSY",
        "A0140 When mpMRI falls short: Diagnostic insights",
    ]
    for title in errata:
        is_rel, score, reason = art.check_paper_relevance(title, "", kw, "", min_ratio=0.60)
        assert is_rel is False, f"Expected blocked for erratum/abstract: {title}"
        print(f"  ✓ BLOCKED Erratum/Notice: {title[:55]}... ({reason})")

def test_bibliography_export_with_relevance():
    print("\n--- TEST 7: Bibliography & Corpus Export with Relevance % ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        folder = Path(tmpdir)
        pdf_file = folder / "test_paper.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 dummy content %%EOF")

        p = art.Paper(
            url="https://doi.org/10.1016/j.jpowsour.2024.123456",
            title="Advanced Heteroatom-Doped Catalysts for Zn–Air Batteries",
            doi="10.1016/j.jpowsour.2024.123456",
            authors=["Ashraf, Kamran", "Doe, John Michael"],
            year="2024",
            journal="Journal of Power Sources",
            issns=["0378-7753"],
            citations=45,
            quartile="Q1",
            relevance_score=0.92,
            source="OpenAlex",
            pdf_path=str(pdf_file),
            abstract="Progress in zinc-air battery electrocatalysis.",
        )
        art.write_bibliography([p], folder)

        # Check BibTeX
        bib = (folder / "references.bib").read_text(encoding="utf-8")
        assert "@article{ashraf2024," in bib
        assert "relevance: 92%" in bib
        print(f"  ✓ references.bib written with relevance: 92%")

        # Check CSV
        csv_text = (folder / "results.csv").read_text(encoding="utf-8-sig")
        assert "92%" in csv_text
        print(f"  ✓ results.csv written with Relevance column")

        # Check JSON Corpus
        corpus_json = json.loads((folder / "corpus_metadata.json").read_text(encoding="utf-8"))
        assert len(corpus_json) == 1
        assert corpus_json[0]["relevance_score"] == 0.92
        assert corpus_json[0]["quartile"] == "Q1"
        print(f"  ✓ corpus_metadata.json written successfully with relevance_score=0.92")

def test_cli_live_harvest():
    print("\n--- TEST 8: Live Harvester & Cascading Resolver ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_dir = Path(tmpdir)
        ctx = art.DownloadContext(run_id=999, target_downloads=2, save_folder=test_dir)
        downloaded = art.execute_research_workflow(
            keywords="LATP",
            focus="solid electrolyte",
            year_start="2023",
            year_end="2026",
            max_articles=2,
            save_folder=test_dir,
            quartile_filter="all_ranked",
            mode="fresh",
            min_relevance=0.60,
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
    test_scimago_abbreviation_index()
    test_60_percent_relevance_matching()
    test_disciplinary_collision_guard()
    test_non_research_errata_filter()
    test_bibliography_export_with_relevance()
    test_cli_live_harvest()
    print("\n🎉 ALL 8 TEST SUITES PASSED FLAWLESSLY! 🎉\n")
