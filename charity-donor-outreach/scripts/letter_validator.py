#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "jinja2>=3.1", "markupsafe>=2", "pyyaml>=6"]
# ///
"""Deterministic letter validation: the enforcement layer for business-rules.md §11.

Every letter passes through validate_letter() before it is written, regardless of
how its campaign paragraph was produced (templated or AI-drafted). AI paragraphs
additionally pass validate_paragraph() before they are accepted into a letter.

The banned-content scan is a backstop, not the guarantee: matching language can
only enter a letter via the config-gated match_line, AI briefs instruct against
it, this scan catches drift, and every Tier-A letter still gets human review.

Standalone audit of a completed run (re-derives expectations from _state.json):
    python letter_validator.py audit --run-dir output/run-<id>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from decimal import Decimal
from html import unescape
from pathlib import Path

from markupsafe import escape

UNRESOLVED_TOKEN_RE = re.compile(r"\[|\]|\{\{|\}\}")
# Thousands groups are matched explicitly rather than as a run of [\d,]: the loose
# form swallows the comma *after* a figure, so "support to $25,000, and" captured
# "$25,000," and never matched the brief's "$25,000". A comma after an amount is
# ordinary prose, so this rejected honest paragraphs and sent them to templated
# fallback for a punctuation mark.
DOLLAR_RE = re.compile(r"\$\s?\d+(?:,\d{3})*(?:\.\d{1,2})?")
URL_RE = re.compile(r"""https?://[^\s"'<>]+""")
HREF_RE = re.compile(r'href="([^"]*)"')
TAG_RE = re.compile(r"<[^>]+>")
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")
HONORIFIC_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Mx|Dr)\.?\s", re.IGNORECASE)

MATCH_LANGUAGE_RE = re.compile(r"\bmatch(?:ed|ing)?\b|\bdoubl(?:e[ds]?|ing)\b|\b[23]x\b|\btripl", re.IGNORECASE)
GUARANTEE_RE = re.compile(r"\bguarantee[ds]?\b|\bpromise[ds]?\b", re.IGNORECASE)
TAX_ADVICE_RE = re.compile(r"\btax[- ]deduct|\btax advice\b|\bwrite[- ]off\b", re.IGNORECASE)
YEAR_TOKEN_RE = re.compile(r"\b(?:19|20)\d{2}\b")
CONTINUITY_RE = re.compile(
    r"\bevery (?:single )?year\b|\beach year\b|\byear after year\b"
    r"|\bnever missed\b|\bwithout fail\b|\bunbroken\b",
    re.IGNORECASE,
)
VOLUNTEER_LANGUAGE_RE = re.compile(r"\bvolunteer(?:s|ed|ing)?\b", re.IGNORECASE)

WORD_MIN, WORD_MAX = 80, 320
PARAGRAPH_MAX_CHARS = 350
PARAGRAPH_MAX_SENTENCES = 2


# Match language is refused for two different reasons, and saying the wrong one
# sends the reader to check a config that is already correct: a letter may not
# claim a match nobody confirmed, and a personalized paragraph may not claim one
# at all, confirmed or not; that sentence is the templated match_line's job.
MATCH_UNCONFIRMED = "matching-gift language present but match is not confirmed in config"
MATCH_NOT_IN_PARAGRAPH = (
    "matching-gift language in a personalized paragraph: the match statement "
    "belongs to the config-gated match_line, whether or not a match is confirmed"
)


def _banned_content(
    text: str, match_language_allowed: bool, match_problem: str = MATCH_UNCONFIRMED
) -> list[str]:
    problems = []
    if not match_language_allowed and MATCH_LANGUAGE_RE.search(text):
        problems.append(match_problem)
    if GUARANTEE_RE.search(text):
        problems.append("guarantee/promise language")
    if TAX_ADVICE_RE.search(text):
        problems.append("tax-advice language")
    return problems


def validate_letter(
    html: str,
    *,
    expected_salutation: str,
    ask_display: str,
    allowed_amounts: set[str],
    config,
) -> list[str]:
    """Return a list of problems; an empty list means the letter is acceptable.

    Deliberately pure: expectations (salutation, formatted amounts) are computed by
    the caller. This module never imports pipeline for them, so it cannot disagree
    with the renderer about enum instances or formatting (the dual-module enum
    bug found in a real end-to-end run)."""
    problems: list[str] = []
    # Unescaped, so a donation URL with "&" compares equal to its config value.
    visible = unescape(TAG_RE.sub(" ", html))
    # Text the organization approved in config, and the donor's own name, are not
    # the letter's claims: a compliance footer must be able to mention a $25 premium
    # or tax deductibility, and "Robert [Bob] Smith" is a name, not a placeholder.
    scanned = visible
    for owned in (config.compliance_footer, *config.regional_notes.values(),
                  expected_salutation):
        if owned:
            scanned = scanned.replace(owned, " ")

    if UNRESOLVED_TOKEN_RE.search(scanned):
        problems.append("unresolved placeholder token ('[', ']' or '{{') in letter text")

    # Dollar amounts: the ask must appear; every amount must be a fact of this record.
    found = {m.replace("$ ", "$") for m in DOLLAR_RE.findall(scanned)}
    if ask_display not in found:
        problems.append(f"computed ask {ask_display} missing from letter")
    for amount in sorted(found - allowed_amounts):
        problems.append(f"dollar amount {amount} is not a fact of this donor's record")

    expected_escaped = str(escape(expected_salutation))
    if expected_escaped not in html:
        problems.append(f"salutation does not match rules (expected {expected_escaped!r})")

    problems.extend(_banned_content(scanned, match_language_allowed=config.match_confirmed))

    hrefs = [unescape(href) for href in HREF_RE.findall(html)]
    if hrefs != [config.donation_url]:
        problems.append(f"expected exactly one link to {config.donation_url}, found {hrefs}")
    stray = {url.rstrip(".") for url in URL_RE.findall(visible)} - {config.donation_url}
    if stray:
        problems.append(f"unexpected URLs in letter text: {sorted(stray)}")

    if str(escape(config.signer.name)) not in html:
        problems.append("configured signer name missing")

    # The compliance footer is legal boilerplate the organization controls, so a
    # long disclosure does not count against the letter's length band.
    body = visible.replace(config.compliance_footer, " ") if config.compliance_footer else visible
    words = len(body.split())
    if not WORD_MIN <= words <= WORD_MAX:
        problems.append(f"letter length {words} words outside {WORD_MIN}-{WORD_MAX}")

    return problems


def validate_paragraph(text, brief: dict) -> list[str]:
    """AI-drafted campaign paragraphs: stricter rules than the letter as a whole.

    Matching language is never acceptable here even when a match is confirmed:
    the match statement belongs to the config-gated template line, not the
    personalized paragraph."""
    problems: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return ["paragraph is empty or not a string"]
    text = text.strip()

    if len(text) > PARAGRAPH_MAX_CHARS:
        problems.append(f"paragraph {len(text)} chars exceeds {PARAGRAPH_MAX_CHARS}")

    sentences = [s for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) > PARAGRAPH_MAX_SENTENCES:
        problems.append(f"{len(sentences)} sentences exceeds {PARAGRAPH_MAX_SENTENCES}")

    allowed = {brief.get("ask_amount"), brief.get("lifetime_total")}
    for amount in {m.replace("$ ", "$") for m in DOLLAR_RE.findall(text)} - allowed:
        problems.append(f"dollar amount {amount} not in the donor's brief")

    if URL_RE.search(text):
        problems.append("URL in paragraph")
    if UNRESOLVED_TOKEN_RE.search(text):
        problems.append("bracket/template token in paragraph")
    if HONORIFIC_RE.search(text):
        problems.append("honorific in paragraph (salutation rules own titles)")

    # Temporal grounding: every year the paragraph names must be a year this
    # donor actually gave (or the campaign year). "Since 2015" for a donor whose
    # first gift was 2019 is the fabrication class reviewers probe first.
    allowed_years = {str(y) for y in brief.get("gift_years", [])}
    if brief.get("campaign_year") is not None:
        allowed_years.add(str(brief["campaign_year"]))
    if allowed_years:  # briefs without gift_years (older runs) skip the check
        for year in set(YEAR_TOKEN_RE.findall(text)) - allowed_years:
            problems.append(f"year {year} is not in this donor's giving history")

    # Continuity and volunteer language are boolean-gated on brief facts.
    if "gave_every_year" in brief and not brief["gave_every_year"]:
        if CONTINUITY_RE.search(text):
            problems.append(
                "continuity language ('every year' and similar) but this donor's "
                "giving has gaps"
            )
    if not brief.get("volunteer") and VOLUNTEER_LANGUAGE_RE.search(text):
        problems.append("volunteer language but the donor is not recorded as a volunteer")

    problems.extend(
        _banned_content(
            text,
            match_language_allowed=False,
            match_problem=MATCH_NOT_IN_PARAGRAPH,
        )
    )
    return problems


# --------------------------------------------------------------------------- audit

def audit_run(run_dir: Path) -> int:
    """Independent re-validation of a completed run from its recorded state."""
    import pipeline  # local: pipeline imports this module, so a top-level import would cycle

    state = json.loads((run_dir / "_state.json").read_text(encoding="utf-8"))
    config = pipeline.CampaignConfig.model_validate(state["config"])
    failures = 0

    summary_rows = state["summary_rows"]
    letter_files = {row["letter_file"] for row in summary_rows}
    on_disk = {f"letters/{p.name}" for p in (run_dir / "letters").glob("*.html")}
    for missing in sorted(letter_files - on_disk):
        print(f"FAIL {missing}: listed in run_summary but missing on disk")
        failures += 1
    for orphan in sorted(on_disk - letter_files):
        print(f"FAIL {orphan}: on disk but not in run_summary")
        failures += 1

    for row in summary_rows:
        entry = state["donors"][row["donor_id"]]
        record = pipeline.DonorRecord.model_validate(entry["record"])
        treatment = pipeline.Treatment(entry["classification"]["treatment"])
        ask = Decimal(entry["ask"])
        html_path = run_dir / row["letter_file"]
        if not html_path.exists():
            continue
        problems = pipeline.validate_rendered_letter(
            html_path.read_text(encoding="utf-8"), record, treatment, ask, config
        )
        for problem in problems:
            print(f"FAIL {row['donor_id']}: {problem}")
        failures += len(problems)

        # The manifest row authorized specific bytes; the file must still be them.
        expected_hash = row.get("letter_sha256")
        if expected_hash:
            actual_hash = hashlib.sha256(html_path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                print(f"FAIL {row['donor_id']}: letter bytes differ from the hash "
                      "the manifest authorized (edited after generation?)")
                failures += 1

    # Exceptions caused by the pipeline's own validation are defects by definition
    # (§9.3 "should not occur"), and an audit that ignores them certifies broken runs.
    with open(run_dir / "exceptions.csv", newline="", encoding="utf-8") as handle:
        exception_rows = list(csv.DictReader(handle))
    for exc_row in exception_rows:
        if exc_row["reason"].startswith("internal validation failure"):
            print(f"FAIL row {exc_row['row_number']} ({exc_row['donor_name']}): "
                  f"pipeline defect: {exc_row['reason']}")
            failures += 1

    suppressed_path = run_dir / "suppressed.csv"
    suppressed_rows = []
    if suppressed_path.exists():
        with open(suppressed_path, newline="", encoding="utf-8") as handle:
            suppressed_rows = list(csv.DictReader(handle))

    # Reconciliation: every input row is exactly one of letter/exception/suppressed.
    input_rows = state.get("input_rows")
    if input_rows is not None:
        accounted = len(summary_rows) + len(exception_rows) + len(suppressed_rows)
        if accounted != input_rows:
            print(f"FAIL reconciliation: {len(summary_rows)} letters + "
                  f"{len(exception_rows)} exceptions + {len(suppressed_rows)} suppressed "
                  f"= {accounted}, but the run ingested {input_rows} rows")
            failures += 1

    print(
        f"Audited {len(summary_rows)} letters, {len(exception_rows)} exceptions, "
        f"{len(suppressed_rows)} suppressed: "
        + ("all checks passed" if failures == 0 else f"{failures} FAILURES")
    )
    return 0 if failures == 0 else 1


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="re-validate every letter in a completed run")
    audit.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    return audit_run(Path(args.run_dir))


if __name__ == "__main__":
    sys.exit(main())
