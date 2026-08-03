import csv
import shutil
from pathlib import Path

import pytest

import pipeline

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"
CONFIG = FIXTURES / "sample_campaign.yaml"
LETTER_SAMPLES = [
    "001-robert-svensson.html",   # lapsed-major: formal, capacity ask, no tote bag
    "041-brenda-kowalski.html",   # bronze single-gift: casual, flat $150
    "050-arthur-mwangi.html",     # mismatch row: Silver treatment despite Bronze label
]


@pytest.fixture()
def fresh_run(tmp_path):
    rc = pipeline.main([
        "generate",
        "--csv", str(FIXTURES / "sample_donors.csv"),
        "--config", str(CONFIG),
        "--out", str(tmp_path), "--run-id", "golden",
    ])
    assert rc == 0
    return tmp_path / "run-golden"


def test_summary_and_exceptions_match_golden_bytes(fresh_run):
    for name in ("run_summary.csv", "exceptions.csv"):
        assert (fresh_run / name).read_bytes() == (GOLDEN / name).read_bytes(), (
            f"{name} drifted from the committed golden snapshot"
        )


@pytest.mark.parametrize("letter", LETTER_SAMPLES)
def test_representative_letters_match_golden_bytes(fresh_run, letter):
    assert (fresh_run / "letters" / letter).read_bytes() == (GOLDEN / letter).read_bytes()


def test_golden_aggregates_are_the_anomaly_inventory():
    with open(GOLDEN / "run_summary.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 50
    assert sum(r["review_tier"] == "A" for r in rows) == 20
    assert sum(r["review_tier"] == "B" for r in rows) == 30
    assert sum("tier_mismatch" in r["flags"] for r in rows) == 4
    assert sum("lapsed_major_hold" in r["flags"] for r in rows) == 2
    assert all(r["validator_status"] == "passed" for r in rows)
    boundary_ids = {"005-victor-ambrosius", "009-angela-petersen",
                    "016-nancy-okafor", "017-vera-johansson"}
    for row in rows:
        if row["donor_id"] in boundary_ids:
            assert row["recency"] == "active", "exactly-3-years donors must not be lapsed"


def regenerate():
    """Maintenance helper, not a test: refresh the committed golden snapshots
    after a deliberate copy or rule change. From the repository root:

        python -c "import sys; sys.path[:0] = ['tests', 'charity-donor-outreach/scripts']; import test_golden; test_golden.regenerate()"
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        rc = pipeline.main([
            "generate",
            "--csv", str(FIXTURES / "sample_donors.csv"),
            "--config", str(CONFIG),
            "--out", tmp, "--run-id", "golden",
        ])
        assert rc == 0
        run = Path(tmp) / "run-golden"
        GOLDEN.mkdir(exist_ok=True)
        for name in ("run_summary.csv", "exceptions.csv"):
            shutil.copy2(run / name, GOLDEN / name)
        for letter in LETTER_SAMPLES:
            shutil.copy2(run / "letters" / letter, GOLDEN / letter)
        print(f"golden refreshed under {GOLDEN}")
