import json
from pathlib import Path

import pytest

import pipeline
from letter_validator import audit_run

FIXTURES = Path(__file__).parent / "fixtures"

GOOD_SVENSSON = (
    "Your generosity from 2010 through 2020 helped animals in the Northeast find "
    "safety when they needed it most. We would be honored to welcome you back into "
    "that work."
)
BANNED_FONTAINE = "Give today and your gift will be doubled by our partners."


@pytest.fixture()
def run_dir(tmp_path):
    rc = pipeline.main([
        "generate",
        "--csv", str(FIXTURES / "sample_donors.csv"),
        "--config", str(FIXTURES / "sample_campaign.yaml"),
        "--out", str(tmp_path), "--run-id", "p2",
    ])
    assert rc == 0
    return tmp_path / "run-p2"


def apply(run_dir, paragraphs, filename="paragraphs.json"):
    path = run_dir / filename
    path.write_text(json.dumps(paragraphs) if isinstance(paragraphs, dict) else paragraphs)
    return pipeline.main([
        "apply-personalization", "--run-dir", str(run_dir), "--paragraphs", str(path),
    ])


def summary(run_dir):
    import csv
    with open(run_dir / "run_summary.csv", newline="") as handle:
        return {row["donor_id"]: row for row in csv.DictReader(handle)}


def test_good_paragraph_is_applied_and_letter_revalidated(run_dir):
    assert apply(run_dir, {"001-robert-svensson": GOOD_SVENSSON}) == 0
    rows = summary(run_dir)
    assert rows["001-robert-svensson"]["generation_mode"] == "ai_personalized"
    html = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    assert "welcome you back into" in html
    assert audit_run(run_dir) == 0  # post-apply, the whole run still audits clean


def test_banned_claim_falls_back_to_templated_and_flags(run_dir):
    templated = (run_dir / "letters/002-earl-fontaine.html").read_text(encoding="utf-8")
    assert apply(run_dir, {
        "001-robert-svensson": GOOD_SVENSSON,
        "002-earl-fontaine": BANNED_FONTAINE,
    }) == 0
    rows = summary(run_dir)
    fontaine = rows["002-earl-fontaine"]
    assert fontaine["generation_mode"] == "templated_fallback"
    assert "paragraph_rejected" in fontaine["flags"]
    assert (run_dir / "letters/002-earl-fontaine.html").read_text(encoding="utf-8") == templated
    assert audit_run(run_dir) == 0


def test_missing_paragraphs_fall_back_without_blocking(run_dir):
    assert apply(run_dir, {"001-robert-svensson": GOOD_SVENSSON}) == 0
    rows = summary(run_dir)
    briefed = json.loads((run_dir / "briefs.json").read_text(encoding="utf-8"))["briefs"]
    missing = [d for d in briefed if d != "001-robert-svensson"]
    assert missing and all(
        rows[d]["generation_mode"] == "templated_fallback"
        and "paragraph_missing" in rows[d]["flags"]
        for d in missing
    )


def test_hallucinated_donor_ids_are_ignored_not_fatal(run_dir):
    assert apply(run_dir, {
        "001-robert-svensson": GOOD_SVENSSON,
        "999-nobody-real": "A lovely paragraph for a donor who does not exist.",
        "018-margaret-alcott": "Tier B donors are never AI-personalized.",
    }) == 0
    rows = summary(run_dir)
    assert rows["001-robert-svensson"]["generation_mode"] == "ai_personalized"
    assert rows["018-margaret-alcott"]["generation_mode"] == "templated"  # untouched
    state = json.loads((run_dir / "_state.json").read_text(encoding="utf-8"))
    assert set(state["personalization"]["ignored_unknown_ids"]) == {
        "999-nobody-real", "018-margaret-alcott"
    }


def test_malformed_paragraphs_json_exits_3_and_touches_nothing(run_dir):
    before = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    assert apply(run_dir, '{"broken": ', filename="broken.json") == pipeline.EXIT_CSV
    assert (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8") == before
    assert summary(run_dir)["001-robert-svensson"]["generation_mode"] == "templated"


def test_non_utf8_paragraphs_json_exits_3_and_touches_nothing(run_dir):
    before = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    path = run_dir / "cp1252.json"
    path.write_bytes('{"001-robert-svensson": "Merci, Renée."}'.encode("cp1252"))
    rc = pipeline.main([
        "apply-personalization", "--run-dir", str(run_dir), "--paragraphs", str(path),
    ])
    assert rc == pipeline.EXIT_CSV
    assert (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8") == before


def test_non_string_paragraph_value_falls_back(run_dir):
    assert apply(run_dir, {"001-robert-svensson": ["not", "a", "string"]}) == 0
    row = summary(run_dir)["001-robert-svensson"]
    assert row["generation_mode"] == "templated_fallback"
    assert "paragraph_rejected" in row["flags"]


def test_reapply_is_idempotent_and_replaces_cleanly(run_dir):
    assert apply(run_dir, {"001-robert-svensson": GOOD_SVENSSON}) == 0
    first = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    assert apply(run_dir, {"001-robert-svensson": GOOD_SVENSSON}) == 0
    assert (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8") == first
    # a rejected re-apply restores the templated letter
    assert apply(run_dir, {"001-robert-svensson": BANNED_FONTAINE}) == 0
    restored = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    assert "welcome you back into" not in restored
    assert audit_run(run_dir) == 0


def test_reapply_omitting_a_donor_restores_their_templated_letter(run_dir):
    templated = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    assert apply(run_dir, {"001-robert-svensson": GOOD_SVENSSON}) == 0
    assert apply(run_dir, {}) == 0
    letter = (run_dir / "letters/001-robert-svensson.html").read_text(encoding="utf-8")
    assert letter == templated  # the summary says templated, so the file must be
    assert summary(run_dir)["001-robert-svensson"]["generation_mode"] == "templated_fallback"
    assert audit_run(run_dir) == 0
