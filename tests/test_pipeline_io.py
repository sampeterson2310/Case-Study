import csv
import json
import subprocess
import sys
from pathlib import Path

import yaml

import pipeline
from letter_validator import audit_run

SKILL = Path(__file__).parents[1] / "charity-donor-outreach"
FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = str(FIXTURES / "sample_campaign.yaml")


def run_generate(tmp_path, csv_path, run_id, config=CONFIG):
    rc = pipeline.main([
        "generate", "--csv", str(csv_path), "--config", config,
        "--out", str(tmp_path), "--run-id", run_id,
    ])
    return rc, tmp_path / f"run-{run_id}"


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_injection_name_is_neutralized(tmp_path):
    rc, run_dir = run_generate(tmp_path, FIXTURES / "injection.csv", "inj")
    assert rc == 0
    rows = read_rows(run_dir / "run_summary.csv")
    assert len(rows) == 1
    donor_id = rows[0]["donor_id"]
    assert donor_id.startswith("001-") and all(c.isalnum() or c == "-" for c in donor_id)

    letter_path = (run_dir / rows[0]["letter_file"]).resolve()
    assert letter_path.is_relative_to(run_dir.resolve() / "letters")
    html = letter_path.read_text(encoding="utf-8")
    assert "<script>" not in html            # autoescape neutralized the payload
    assert "&lt;script&gt;" in html          # ...but the name is preserved as data


def test_malformed_rows_become_exceptions_and_batch_continues(tmp_path):
    rc, run_dir = run_generate(tmp_path, FIXTURES / "malformed.csv", "mal")
    assert rc == 0
    exceptions = read_rows(run_dir / "exceptions.csv")
    reasons = {e["donor_name"]: e["reason"] for e in exceptions}
    assert "not derivable" in reasons["No Data Donor"]
    assert "unparseable" in reasons["Bad Money Donor"]

    letters = read_rows(run_dir / "run_summary.csv")
    assert len(letters) == 2  # duplicate rows are distinct donors, distinct files
    assert letters[0]["donor_id"] != letters[1]["donor_id"]
    # ...but two rows for the same person are flagged for a human to resolve
    assert all("duplicate_donor" in row["flags"] for row in letters)
    # contract: every input row is a letter XOR an exception
    assert len(letters) + len(exceptions) == 4


def test_negative_amount_is_an_exception_not_a_letter(tmp_path):
    rc, run_dir = run_generate(tmp_path, FIXTURES / "negative.csv", "neg")
    assert rc == 0
    assert read_rows(run_dir / "run_summary.csv") == []
    exceptions = read_rows(run_dir / "exceptions.csv")
    assert len(exceptions) == 1 and "negative" in exceptions[0]["reason"]


def test_duplicate_raw_headers_refuse_with_exit_3(tmp_path):
    bad = tmp_path / "dupheader.csv"
    bad.write_text("donor_name,tier,tier\nAnn Smith,Silver,Gold\n")
    rc, run_dir = run_generate(tmp_path, bad, "dupraw")
    assert rc == pipeline.EXIT_CSV
    assert not run_dir.exists()


def test_colliding_normalized_headers_refuse_with_exit_3(tmp_path, capsys):
    # 'Largest' and 'Largest Gift' both normalize to largest_gift; silently
    # letting one win is the failure mode this check exists to prevent.
    bad = tmp_path / "collide.csv"
    bad.write_text(
        "donor_name,Largest,Largest Gift,lifetime_total,last_gift_year\n"
        'Ann Smith,"$1,000","$2,000","$3,000",2023\n'
    )
    rc, run_dir = run_generate(tmp_path, bad, "collide")
    assert rc == pipeline.EXIT_CSV
    assert not run_dir.exists()
    err = capsys.readouterr().err
    assert "Largest" in err and "largest_gift" in err


def test_non_utf8_export_refused_with_guidance_not_a_crash(tmp_path, capsys):
    bad = tmp_path / "cp1252.csv"
    header = "donor_name,tier,gifts,largest_gift,lifetime_total,last_gift_year,volunteer\n"
    row = "Renée Dupré,Silver,2023: $1,400,$1,400,$2,400,2023,No\n"
    bad.write_bytes((header + row).encode("cp1252"))
    rc, run_dir = run_generate(tmp_path, bad, "cp1252")
    assert rc == pipeline.EXIT_CSV  # clean exit 3, not a UnicodeDecodeError traceback
    assert not run_dir.exists()
    assert "UTF-8" in capsys.readouterr().err


def test_outputs_are_utf8_with_lf_regardless_of_platform_locale(tmp_path):
    """Every file the pipeline writes is UTF-8 with LF, not whatever the OS prefers.

    Two failures hide here, and both need a name the machine's locale codepage
    cannot encode to surface. On a cp1252 Windows box the run used to die with a
    UnicodeEncodeError writing the letter, and text-mode newline translation
    turned the letters into CRLF on disk while `letter_sha256` hashed the LF
    string, so every letter failed the audit's byte check on the platform where
    reviewers are most likely to open them.
    """
    unicode_csv = tmp_path / "unicode.csv"
    unicode_csv.write_bytes(
        "donor_name,tier,gifts,largest_gift,lifetime_total,last_gift_year,volunteer\n"
        'Małgorzata Wiśniewska,Silver,"2022: $1,000, 2023: $1,400","$1,400","$2,400",2023,No\n'
        .encode("utf-8")
    )
    rc, run_dir = run_generate(tmp_path, unicode_csv, "utf8")
    assert rc == 0

    letter = next((run_dir / "letters").glob("*.html"))
    assert "Małgorzata" in letter.read_text(encoding="utf-8")
    for path in (letter, run_dir / "run_summary.csv", run_dir / "_state.json"):
        assert b"\r\n" not in path.read_bytes(), f"{path.name} was written with CRLF"

    assert audit_run(run_dir) == 0  # the sha256 in the manifest must match the bytes


def test_formula_injection_is_neutralized_in_csv_outputs(tmp_path):
    hostile = tmp_path / "formula.csv"
    hostile.write_text(
        "donor_name,tier,region,gifts,largest_gift,lifetime_total,last_gift_year,volunteer\n"
        '"=SUM(A1:A9) Jones",Silver,West,"2022: $1,000, 2023: $1,400","$1,400","$2,400",2023,No\n'
    )
    rc, run_dir = run_generate(tmp_path, hostile, "formula")
    assert rc == 0
    raw = (run_dir / "run_summary.csv").read_text(encoding="utf-8")
    assert "'=SUM(A1:A9) Jones" in raw      # neutralized for spreadsheet review
    assert "\n=SUM" not in raw and ",=SUM" not in raw  # never a live formula cell


def test_missing_donor_name_column_refuses_with_exit_3(tmp_path):
    rc, run_dir = run_generate(tmp_path, FIXTURES / "no_name_column.csv", "badcsv")
    assert rc == pipeline.EXIT_CSV
    assert not run_dir.exists()


def test_over_volume_cap_refuses_with_exit_4_and_writes_nothing(tmp_path):
    config = yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8"))
    config["limits"] = {"max_rows": 5}
    tiny = tmp_path / "tiny.yaml"
    tiny.write_text(yaml.safe_dump(config))
    sample = FIXTURES / "sample_donors.csv"
    rc, run_dir = run_generate(tmp_path, sample, "cap", config=str(tiny))
    assert rc == pipeline.EXIT_VOLUME
    assert not run_dir.exists()


def test_invalid_config_refuses_with_exit_2(tmp_path):
    config = yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8"))
    config["match_confirmed"] = True  # ...without naming a sponsor
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(config))
    rc, run_dir = run_generate(
        tmp_path, FIXTURES / "injection.csv", "badcfg", config=str(bad)
    )
    assert rc == pipeline.EXIT_CONFIG
    assert not run_dir.exists()


def test_unfilled_config_refuses_with_exit_2_naming_the_fields(tmp_path, capsys):
    """The example config, uploaded unfinished, must stop the run at Gate 0.

    It used to pass schema validation, generate zero letters, and report one
    `internal validation failure` per row, which the docs define as a skill
    defect. A user who simply had not filled in the config was told the skill was
    broken. It is a config error, and the message has to name what to fill in.
    """
    example = SKILL / "assets" / "campaign_config.example.yaml"
    unfilled = tmp_path / "unfilled.yaml"
    unfilled.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    rc, run_dir = run_generate(
        tmp_path, FIXTURES / "sample_donors.csv", "unfilled", config=str(unfilled)
    )
    assert rc == pipeline.EXIT_CONFIG
    assert not run_dir.exists()  # nothing written, not even an empty run
    err = capsys.readouterr().err
    assert "signer.name" in err and "signer.title" in err


def test_non_utf8_config_refuses_with_exit_2_not_a_traceback(tmp_path):
    # Every file the pipeline reads is read as UTF-8, so every one of them needs a
    # defined answer for "it wasn't": the CSV has one (exit 3), and so does this.
    bad = tmp_path / "cp1252.yaml"
    bad.write_bytes(Path(CONFIG).read_bytes().replace(b"ASPCA", "Société".encode("cp1252")))
    rc, run_dir = run_generate(
        tmp_path, FIXTURES / "injection.csv", "cp1252cfg", config=str(bad)
    )
    assert rc == pipeline.EXIT_CONFIG
    assert not run_dir.exists()


def test_header_only_csv_completes_with_zero_letters(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("donor_name,tier,largest_gift,lifetime_total,last_gift_year\n")
    rc, run_dir = run_generate(tmp_path, empty, "empty")
    assert rc == 0
    assert read_rows(run_dir / "run_summary.csv") == []
    assert read_rows(run_dir / "exceptions.csv") == []


def test_cli_subprocess_run_handles_every_treatment(tmp_path):
    """Regression for the dual-module enum bug found in a real end-to-end run: when
    pipeline.py runs as __main__ (the documented `uv run` path), letter_validator
    re-imports pipeline as a second module instance; enum identity checks then
    failed and every standard-lapsed letter became a false-positive exception.
    pytest imports pipeline exactly once, so only a real subprocess sees this."""
    result = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "pipeline.py"), "generate",
         "--csv", str(FIXTURES / "sample_donors.csv"),
         "--config", CONFIG,
         "--out", str(tmp_path), "--run-id", "subproc"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    run_dir = tmp_path / "run-subproc"
    assert read_rows(run_dir / "exceptions.csv") == []  # lapsed donors included
    rows = read_rows(run_dir / "run_summary.csv")
    assert len(rows) == 50
    assert sum(r["treatment"] == "lapsed" for r in rows) == 10


# --- consent & suppression (business-rules §1.3): fail closed, never guess ---

def consent_run(tmp_path):
    rc, run_dir = run_generate(tmp_path, FIXTURES / "donors_consent.csv", "consent")
    assert rc == 0
    return run_dir


def test_consent_dispositions_reconcile(tmp_path):
    run_dir = consent_run(tmp_path)
    letters = read_rows(run_dir / "run_summary.csv")
    exceptions = read_rows(run_dir / "exceptions.csv")
    suppressed = read_rows(run_dir / "suppressed.csv")
    assert [r["donor_name"] for r in letters] == ["Unflagged Bill", "Clean Clara"]
    assert {r["donor_name"] for r in suppressed} == {
        "Opted Out Olive", "No Consent Nina", "Deceased Dan"
    }
    assert {r["donor_name"] for r in exceptions} == {"Ambiguous Andy", "Unrecorded Ursula"}
    # contract: letters + exceptions + suppressed = input rows
    assert len(letters) + len(exceptions) + len(suppressed) == 7
    # suppressed donors must have no letter file
    assert not any("olive" in p.name for p in (run_dir / "letters").glob("*.html"))


def test_strictest_verdict_wins_over_explicit_consent(tmp_path):
    # Olive says consent_to_contact=Yes AND opt_out=Yes -> suppressed
    run_dir = consent_run(tmp_path)
    olive = [r for r in read_rows(run_dir / "suppressed.csv")
             if r["donor_name"] == "Opted Out Olive"][0]
    assert "opt_out=Yes" in olive["reason"]


def test_blank_consent_column_is_never_permission(tmp_path):
    """A consent column records a permission. Blank means none is on record."""
    run_dir = consent_run(tmp_path)
    ursula = [r for r in read_rows(run_dir / "exceptions.csv")
              if r["donor_name"] == "Unrecorded Ursula"][0]
    assert "consent_to_contact is blank" in ursula["reason"]
    assert "never treated as permission" in ursula["reason"]


def test_blank_suppression_column_means_no_flag_was_set(tmp_path):
    """A suppression column records a flag somebody set; blank means nobody did.

    The asymmetry is deliberate. Holding on a blank suppression cell would refuse
    nearly every row of a typical CRM export, where only flagged donors carry a
    value.
    """
    run_dir = consent_run(tmp_path)
    letters = read_rows(run_dir / "run_summary.csv")
    assert "Unflagged Bill" in {r["donor_name"] for r in letters}
    held = {r["donor_name"] for r in read_rows(run_dir / "exceptions.csv")}
    assert "Unflagged Bill" not in held


def test_unrecognized_consent_token_holds_not_guesses(tmp_path):
    """Unrecognized cuts across both families: blank is absence, 'N/A' is a value."""
    run_dir = consent_run(tmp_path)
    andy = [r for r in read_rows(run_dir / "exceptions.csv")
            if r["donor_name"] == "Ambiguous Andy"][0]
    assert "N/A" in andy["reason"] and "never guessed at" in andy["reason"]


def test_deceased_gets_its_own_reason(tmp_path):
    run_dir = consent_run(tmp_path)
    dan = [r for r in read_rows(run_dir / "suppressed.csv")
           if r["donor_name"] == "Deceased Dan"][0]
    assert dan["reason"].startswith("deceased=")


def test_missing_suppression_column_warns_loudly(tmp_path, capsys):
    rc, run_dir = run_generate(tmp_path, FIXTURES / "injection.csv", "nowarncol")
    assert rc == 0
    assert "no consent/suppression column" in capsys.readouterr().out
    state = json.loads((run_dir / "_state.json").read_text(encoding="utf-8"))
    assert state["consent"]["warning"] is not None


def test_consent_run_passes_audit_reconciliation(tmp_path):
    run_dir = consent_run(tmp_path)
    assert audit_run(run_dir) == 0


def test_refuses_to_overwrite_an_existing_run(tmp_path):
    rc, _ = run_generate(tmp_path, FIXTURES / "injection.csv", "dup")
    assert rc == 0
    rc_again, _ = run_generate(tmp_path, FIXTURES / "injection.csv", "dup")
    assert rc_again == pipeline.EXIT_CONFIG  # never clobber a run under review
