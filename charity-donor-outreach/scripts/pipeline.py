#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "jinja2>=3.1", "markupsafe>=2", "pyyaml>=6"]
# ///
"""Donor outreach pipeline: the deterministic engine behind the charity-donor-outreach skill.

Subcommands:
  generate               CSV + campaign config -> letters/, run_summary.csv,
                         exceptions.csv, briefs.json (Tier A), all under a fresh
                         output/run-<id>/ directory. Zero LLM involvement.
  apply-personalization  Merge Claude-drafted paragraphs.json into Tier-A letters,
                         validating every paragraph; missing/rejected paragraphs
                         fall back to the templated version (never blocks the batch).

Every rule implemented here is specified in references/business-rules.md; if they
disagree, this file has the bug.

Exit codes: 0 completed (exceptions, if any, are recorded per-row) · 2 invalid config
· 3 CSV unreadable/structurally invalid · 4 max_rows exceeded · 1 unexpected crash.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup
from pydantic import BaseModel, ValidationError, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import letter_validator  # noqa: E402  (sibling module; scripts/ ships as a unit)

# Exit 1 is deliberately absent: it is what an uncaught exception already returns,
# and a named constant nothing raises would only read as if something did.
EXIT_OK, EXIT_CONFIG, EXIT_CSV, EXIT_VOLUME = 0, 2, 3, 4

SKILL_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = SKILL_DIR / "assets"

SUMMARY_COLUMNS = [
    "donor_id", "donor_name", "region", "provided_tier", "derived_tier", "recency",
    "treatment", "review_tier", "review_status", "generation_mode", "ask_amount",
    "ask_trace", "largest_gift", "lifetime_total", "last_gift_year", "volunteer",
    "flags", "letter_file", "letter_sha256", "validator_status",
]


# --------------------------------------------------------------------------- errors

class ConfigError(Exception):
    """Campaign config missing/invalid -> exit 2."""


class CsvError(Exception):
    """An input file (donor CSV, paragraphs.json, run state) unreadable or
    structurally invalid -> exit 3."""


class VolumeError(Exception):
    """Row count exceeds limits.max_rows -> exit 4."""


class RowError(Exception):
    """One donor row cannot be processed safely -> exceptions.csv, batch continues."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- config

class CampaignInfo(BaseModel):
    type: str
    name: Optional[str] = None

    @model_validator(mode="after")
    def _known_type(self) -> "CampaignInfo":
        allowed = {"emergency", "annual", "capital", "event"}
        if self.type not in allowed:
            raise ValueError(
                f"campaign.type must be one of {sorted(allowed)} (got {self.type!r}); "
                "there is deliberately no default"
            )
        return self


class Signer(BaseModel):
    name: str
    title: str


class EventInfo(BaseModel):
    registered_count: Optional[int] = None


class Offers(BaseModel):
    naming_opportunity: bool = False
    reengagement_gift: Optional[str] = None


class Limits(BaseModel):
    max_rows: int = 500
    max_personalized: int = 25


class CampaignConfig(BaseModel):
    organization: str
    campaign: CampaignInfo
    campaign_year: int
    letter_date: Optional[str] = None
    signer: Signer
    donation_url: str
    match_confirmed: bool = False
    match_sponsor: Optional[str] = None
    event: EventInfo = EventInfo()
    offers: Offers = Offers()
    regional_notes: dict[str, str] = {}
    compliance_footer: Optional[str] = None
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _cross_checks(self) -> "CampaignConfig":
        if not 1900 <= self.campaign_year <= 2100:
            raise ValueError(f"campaign_year {self.campaign_year} is not plausible")
        if not self.donation_url.startswith("https://"):
            raise ValueError("donation_url must be an https:// URL")
        if self.match_confirmed and not self.match_sponsor:
            raise ValueError("match_confirmed is true but match_sponsor is not named")
        return self


UNFILLED_MARKER = "[REPLACE"


def unfilled_fields(node, path: str = "") -> list[str]:
    """Config keys still holding an example-file [REPLACE] marker."""
    if isinstance(node, dict):
        return [
            field
            for key, value in node.items()
            for field in unfilled_fields(value, f"{path}.{key}".lstrip("."))
        ]
    return [path] if isinstance(node, str) and UNFILLED_MARKER in node else []


def load_config(path: Path) -> CampaignConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read campaign config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"campaign config {path} is not a YAML mapping")

    # Caught here rather than downstream: an unfilled marker survives schema
    # validation happily, then fails every letter as "internal validation
    # failure", which the docs define as a skill defect. A config nobody finished
    # filling in is a config error, and telling the user their skill is broken
    # when they simply have not answered Gate 0 yet sends them somewhere useless.
    unfilled = unfilled_fields(raw)
    if unfilled:
        raise ConfigError(
            f"campaign config {path} still has unfilled {UNFILLED_MARKER}] markers in: "
            f"{', '.join(unfilled)}. Fill them in with the user; the run refuses "
            "rather than putting a placeholder in front of a donor."
        )

    try:
        return CampaignConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid campaign config {path}:\n{exc}") from exc


# --------------------------------------------------------------------------- parsing

MONEY_STRIP_RE = re.compile(r"[$,\s]")
# Thousands groups are explicit and the amount must end at a separator, so a
# missing space after a comma cannot glue the next year onto the amount.
GIFT_PAIR_RE = re.compile(
    r"(\d{4})\s*:\s*\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?=\s*(?:[,;]|$))"
)
TRUTHY = {"yes", "true", "1", "y"}
FALSY = {"no", "false", "0", "n"}

# Consent & suppression column families (both polarities). A truthy suppress
# column or a falsy consent column means: no letter, ever, for this row.
SUPPRESS_COLUMNS = {"do_not_contact", "opt_out", "do_not_email", "no_contact", "deceased"}
CONSENT_COLUMNS = {"consent_to_contact", "mail_ok", "opt_in", "solicit"}


def resolve_contactability(row: dict[str, str], columns: set[str]) -> tuple[str, str]:
    """Return (verdict, reason): verdict in {'contact', 'suppress', 'unreadable'}.

    Strictest verdict wins across every consent/suppression column present.

    Blank cells are read according to what the column actually encodes, because the
    two families are not symmetrical:

    * A **suppression** column records a flag someone set. Blank means no flag was
      set (the ordinary state of most rows in a CRM export), so blank is `contact`.
      Holding it would refuse essentially every real export.
    * A **consent** column records a permission someone gave. Blank means no
      permission is on record, which is never the same as permission, so blank is
      `unreadable` and the row is held.

    An *unrecognized* token ('MAYBE', 'N/A') is `unreadable` in either family: a
    value somebody deliberately wrote is never guessed at.
    """
    verdict, reason = "contact", ""
    rank = {"contact": 0, "unreadable": 1, "suppress": 2}
    for column in sorted(columns & (SUPPRESS_COLUMNS | CONSENT_COLUMNS)):
        raw = (row.get(column) or "").strip()
        token = raw.lower()
        if column in SUPPRESS_COLUMNS:
            this = ("contact" if not token
                    else "suppress" if token in TRUTHY
                    else "contact" if token in FALSY else "unreadable")
        else:
            this = ("contact" if token in TRUTHY
                    else "suppress" if token in FALSY else "unreadable")
        if this == "suppress":
            this_reason = f"{column}={raw}"
        elif this == "unreadable":
            this_reason = (
                f"consent unreadable: {column} is blank; a blank consent column "
                "is never treated as permission"
                if not raw else
                f"consent unreadable: {column} is unrecognized ({raw!r}); a value "
                "somebody wrote is never guessed at"
            )
        else:
            this_reason = ""
        if rank[this] > rank[verdict]:
            verdict, reason = this, this_reason
    return verdict, reason

CAPACITY_LABELS = {"platinum": "Platinum", "gold": "Gold", "silver": "Silver", "bronze": "Bronze"}

HEADER_ALIASES = {
    "name": "donor_name",
    "donor": "donor_name",
    "gifts_year_amount": "gifts",
    "giving_history": "gifts",
    "largest": "largest_gift",
    "lifetime": "lifetime_total",
    "lifetime_giving": "lifetime_total",
    "is_volunteer": "volunteer",
}


def normalize_header(header: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
    return HEADER_ALIASES.get(key, key)


def parse_money(text: str) -> Decimal:
    cleaned = MONEY_STRIP_RE.sub("", text.strip())
    if not cleaned:
        raise ValueError("empty amount")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {text!r}") from exc
    # Decimal accepts "NaN" and "Infinity"; NaN raises on comparison and infinity
    # overflows format_money, so both would crash the batch instead of one row.
    if not value.is_finite():
        raise ValueError(f"unparseable amount {text!r}")
    if value < 0:
        raise ValueError(f"negative amount {text!r}")
    return value


def parse_gifts(text: str) -> list[tuple[int, Decimal]]:
    # Every character must belong to a "YYYY: $amount" pair or a separator. Scanning
    # for pairs alone skipped what it could not read: "2020: $5k" became $5, a
    # negative gift vanished, and "$25,000,2013: $30,000" read as $250,002,013.
    text = text or ""
    gifts, position = [], 0
    for match in GIFT_PAIR_RE.finditer(text):
        if text[position:match.start()].strip(" \t,;"):
            break
        gifts.append((int(match.group(1)), parse_money(match.group(2))))
        position = match.end()
    if text[position:].strip(" \t,;"):
        raise ValueError(f"unreadable entry near {text[position:].strip(' ,;')[:30]!r}")
    return gifts


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "donor"


def make_donor_id(row_number: int, name: str) -> str:
    return f"{row_number:03d}-{slugify(name)}"


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    return parts[0], " ".join(parts[1:])


class DonorRecord(BaseModel):
    row_number: int
    donor_id: str
    donor_name: str
    first_name: str
    last_name: str  # empty for mononyms
    title: Optional[str] = None
    provided_tier: Optional[str] = None  # canonical capacity label, "Lapsed", or None
    region: Optional[str] = None
    gifts: list[tuple[int, Decimal]] = []
    largest_gift: Decimal
    lifetime_total: Decimal
    last_gift_year: int
    volunteer: bool = False
    flags: list[str] = []


def parse_row(row_number: int, raw: dict[str, str], campaign_year: int) -> DonorRecord:
    def get(key: str) -> str:
        return (raw.get(key) or "").strip()

    name = get("donor_name")
    if not name:
        raise RowError("missing donor_name")

    try:
        gifts = parse_gifts(get("gifts"))
    except ValueError as exc:
        raise RowError(f"gifts history: {exc}") from exc

    flags: list[str] = []

    def field_value(column: str, derive, label: str) -> Decimal | int:
        text = get(column)
        derived = derive() if gifts else None
        if text:
            try:
                provided = int(text) if label == "year" else parse_money(text)
            except ValueError as exc:
                raise RowError(f"{column}: {exc}") from exc
            if derived is not None and provided != derived:
                flags.append(f"data_inconsistency({column})")
            return provided  # provided values are authoritative (spec §3.3)
        if derived is None:
            raise RowError(f"{column} missing and not derivable from gifts history")
        return derived

    largest = field_value("largest_gift", lambda: max(a for _, a in gifts), "money")
    lifetime = field_value("lifetime_total", lambda: sum(a for _, a in gifts), "money")
    last_year = field_value("last_gift_year", lambda: max(y for y, _ in gifts), "year")

    if last_year > campaign_year or any(y > campaign_year for y, _ in gifts):
        raise RowError(f"future-dated gift year (campaign_year is {campaign_year})")

    tier_text = get("tier").lower()
    if tier_text in CAPACITY_LABELS:
        provided_tier = CAPACITY_LABELS[tier_text]
    elif tier_text == "lapsed":
        provided_tier = "Lapsed"
    else:
        provided_tier = None  # unknown/blank labels carry no claim

    first, last = split_name(name)
    return DonorRecord(
        row_number=row_number,
        donor_id=make_donor_id(row_number, name),
        donor_name=name,
        first_name=get("first_name") or first,
        last_name=get("last_name") or last,
        title=get("title") or None,
        provided_tier=provided_tier,
        region=get("region") or None,
        gifts=gifts,
        largest_gift=largest,
        lifetime_total=lifetime,
        last_gift_year=last_year,
        volunteer=get("volunteer").lower() in TRUTHY,
        flags=flags,
    )


# --------------------------------------------------------------------------- rules

class Treatment(str, Enum):
    PLATINUM = "platinum"
    GOLD = "gold"
    SILVER = "silver"
    BRONZE = "bronze"
    LAPSED = "lapsed"
    LAPSED_MAJOR = "lapsed_major"


TIER_A = {Treatment.PLATINUM, Treatment.GOLD, Treatment.LAPSED_MAJOR}
ASK_PERCENTAGE = {"Platinum": Decimal("0.40"), "Gold": Decimal("0.25"), "Silver": Decimal("0.15")}
FLAT_ASK = {Treatment.BRONZE: Decimal(150), Treatment.LAPSED: Decimal(50)}
LAPSED_MAJOR_THRESHOLD = Decimal(10_000)


def derive_capacity_tier(lifetime_total: Decimal) -> str:
    if lifetime_total >= 50_000:
        return "Platinum"  # exactly $50,000 is Platinum (the original left it in a gap)
    if lifetime_total >= 10_000:
        return "Gold"
    if lifetime_total >= 1_000:
        return "Silver"
    return "Bronze"


def is_lapsed(last_gift_year: int, campaign_year: int) -> bool:
    return campaign_year - last_gift_year > 3  # strict: exactly 3 years ago is active


@dataclass
class Classification:
    capacity_tier: str
    lapsed: bool
    treatment: Treatment
    review_tier: str  # "A" | "B"
    flags: list[str] = field(default_factory=list)


def classify(record: DonorRecord, campaign_year: int) -> Classification:
    capacity = derive_capacity_tier(record.lifetime_total)
    lapsed = is_lapsed(record.last_gift_year, campaign_year)
    flags: list[str] = []

    if record.provided_tier in CAPACITY_LABELS.values() and record.provided_tier != capacity:
        flags.append(f"tier_mismatch(provided={record.provided_tier},derived={capacity})")

    if lapsed and record.lifetime_total >= LAPSED_MAJOR_THRESHOLD:
        treatment = Treatment.LAPSED_MAJOR
        flags.append("lapsed_major_hold")
    elif lapsed:
        treatment = Treatment.LAPSED
    else:
        treatment = Treatment(capacity.lower())

    review_tier = "A" if treatment in TIER_A else "B"
    return Classification(capacity, lapsed, treatment, review_tier, flags)


def round_to_nearest_50(amount: Decimal) -> Decimal:
    rounded = (amount / 50).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 50
    return max(rounded, Decimal(50))


def compute_ask(
    record: DonorRecord,
    classification: Classification,
    campaign_year: int,
    campaign_type: str,
) -> tuple[Decimal, list[str], str]:
    """Return (ask, flags, human-readable trace).

    The trace lands in run_summary.csv so a reviewer can check any ask from the
    spreadsheet without reopening the rules document or a letter."""
    if classification.treatment in FLAT_ASK:
        ask = FLAT_ASK[classification.treatment]  # flat means flat: no modifiers
        return ask, [], f"flat {classification.treatment.value} ask -> {format_money(ask)}"

    percentage = ASK_PERCENTAGE[classification.capacity_tier]
    ask = record.largest_gift * percentage
    trace = [
        f"base = largest_gift {format_money(record.largest_gift)} "
        f"x {percentage * 100:.0f}% = {format_money(ask)}"
    ]
    if record.last_gift_year in (campaign_year, campaign_year - 1):
        ask *= Decimal("1.10")
        trace.append(f"loyalty uplift +10% -> {format_money(ask)}")
    if record.volunteer:
        ask += 100
        trace.append(f"volunteer +$100 -> {format_money(ask)}")
    if campaign_type == "emergency":
        ask *= Decimal("1.20")
        trace.append(f"emergency x1.20 -> {format_money(ask)}")
    ask = round_to_nearest_50(ask)
    trace.append(f"rounded to nearest $50 -> {format_money(ask)}")

    flags = ["ask_exceeds_largest_gift"] if ask > record.largest_gift else []
    return ask, flags, " | ".join(trace)


def build_salutation(record: DonorRecord, treatment: Treatment) -> str:
    # Treatment comparisons throughout use ==, never `is`: when this file runs as
    # __main__ (uv run scripts/pipeline.py) and another module imports `pipeline`,
    # two enum instances exist and identity checks fail silently. Found in a
    # real end-to-end run; regression-tested via a subprocess test.
    if treatment in TIER_A:
        if record.title and record.last_name:
            return f"Dear {record.title} {record.last_name},"
        if record.last_name:
            return f"Dear {record.first_name} {record.last_name},"
        return f"Dear {record.first_name},"  # mononym: never guess a title
    # Standard lapsed uses the same informal greeting as Silver/Bronze. The
    # original's "We've missed you, {first}!" asserts the organization's own
    # conduct, and a greeting is not entitled to that claim (business-rules §7/§12).
    return f"Hi {record.first_name},"


def format_money(amount: Decimal) -> str:
    if amount == amount.to_integral_value():
        return f"${int(amount):,}"
    return f"${amount:,.2f}"


def validate_rendered_letter(
    html: str,
    record: DonorRecord,
    treatment: Treatment,
    ask: Decimal,
    config: CampaignConfig,
) -> list[str]:
    """Assemble this donor's expectations and run the pure letter validator.

    letter_validator deliberately knows nothing about donor records or Treatment;
    expectations are computed here, in the same module instance that rendered the
    letter, so dual-module enum instances can never disagree with themselves."""
    allowed = {
        format_money(ask),
        format_money(record.lifetime_total),
        format_money(record.largest_gift),
        *(format_money(amount) for _, amount in record.gifts),
    }
    return letter_validator.validate_letter(
        html,
        expected_salutation=build_salutation(record, treatment),
        ask_display=format_money(ask),
        allowed_amounts=allowed,
        config=config,
    )


def money_cell(amount: Decimal) -> str:
    return str(int(amount)) if amount == amount.to_integral_value() else str(amount)


FORMULA_PREFIXES = ("=", "+", "-", "@")


def csv_safe(value):
    """Neutralize spreadsheet formula injection: review happens in Excel/Sheets,
    where a cell beginning =, +, - or @ executes. Donor-controlled text (names)
    flows into these files."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return f"'{value}"
    return value


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings, whatever the platform prefers.

    Both halves matter. `letter_sha256` hashes the string, so a
    platform that translated LF to CRLF on the way to disk would make every
    letter fail its own audit. And donor names arrive as UTF-8 (§1.1), so encoding
    them with the machine's locale codepage turns a Polish surname into a
    crashed run on one reviewer's laptop and a correct one on another's.

    Written through open() rather than Path.write_text(newline=...) because the
    latter's newline argument is not available on every version this skill
    supports, and this is the wrong place to depend on that."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{k: csv_safe(v) for k, v in row.items()} for row in rows])


# --------------------------------------------------------------------------- rendering

def jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(ASSETS_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def load_copy() -> dict:
    return yaml.safe_load((ASSETS_DIR / "copy.yaml").read_text(encoding="utf-8"))


def render_fragment(env: Environment, text: str, ctx: dict) -> Markup:
    # Fragments escape their variables here; Markup prevents double-escaping when
    # the assembled fragment is inserted into the letter template.
    return Markup(env.from_string(text).render(**ctx))


def build_letter_context(
    record: DonorRecord,
    classification: Classification,
    ask: Decimal,
    config: CampaignConfig,
    copy_blocks: dict,
    env: Environment,
    letter_date: str,
    campaign_paragraph: Optional[str] = None,
) -> dict:
    facts = {
        "organization": config.organization,
        "lifetime_total": format_money(record.lifetime_total),
        "largest_gift": format_money(record.largest_gift),
        "last_gift_year": record.last_gift_year,
        "last_gift_amount": format_money(
            next((a for y, a in sorted(record.gifts) if y == record.last_gift_year),
                 record.largest_gift)
        ),
        "ask_amount": format_money(ask),
        "match_sponsor": config.match_sponsor,
        "reengagement_gift": config.offers.reengagement_gift,
        "registered_count": config.event.registered_count,
    }

    # With no gifts column, one gift is known only when lifetime equals largest;
    # otherwise "your 2022 gift of $90,000" would state a gift nobody recorded.
    single_gift = (
        len(record.gifts) == 1 if record.gifts
        else record.lifetime_total == record.largest_gift
    )
    treatment = classification.treatment
    if treatment in (Treatment.LAPSED, Treatment.LAPSED_MAJOR):
        opening_key = "lapsed"
    elif single_gift:
        opening_key = "single_gift"
    else:
        opening_key = "active"

    if campaign_paragraph is None:
        paragraph_key = config.campaign.type
        if treatment in (Treatment.LAPSED, Treatment.LAPSED_MAJOR):
            lapsed_key = f"{config.campaign.type}_lapsed"
            if lapsed_key in copy_blocks["campaign_paragraphs"]:
                paragraph_key = lapsed_key  # no urgency pressure on re-engagement asks
        paragraph = render_fragment(
            env, copy_blocks["campaign_paragraphs"][paragraph_key], facts
        )
    else:
        paragraph = Markup.escape(campaign_paragraph)  # AI text is data, not markup

    if treatment == Treatment.PLATINUM and config.offers.naming_opportunity:
        ask_line_key = "platinum_with_naming"
    elif treatment == Treatment.LAPSED and config.offers.reengagement_gift:
        ask_line_key = "lapsed_with_gift"
    else:
        ask_line_key = treatment.value

    regional_note = config.regional_notes.get(record.region or "")

    return {
        "letter_date": letter_date,
        "salutation": build_salutation(record, treatment),
        "opening": render_fragment(env, copy_blocks["openings"][opening_key], facts),
        "campaign_paragraph": paragraph,
        "match_line": (
            render_fragment(env, copy_blocks["match_line"], facts)
            if config.match_confirmed else None
        ),
        "ask_sentence": render_fragment(env, copy_blocks["ask_sentence"], facts),
        "ask_line": render_fragment(env, copy_blocks["ask_lines"][ask_line_key], facts),
        "donation_url": config.donation_url,
        "signer_name": config.signer.name,
        "signer_title": config.signer.title,
        "organization": config.organization,
        "regional_ps": (
            render_fragment(env, copy_blocks["regional_ps"], {"regional_note": regional_note})
            if regional_note else None
        ),
        "compliance_footer": config.compliance_footer,
    }


def render_letter(env: Environment, ctx: dict) -> str:
    return env.get_template("letter_template.html.j2").render(**ctx)


# --------------------------------------------------------------------------- briefs

BRIEF_TONES = {
    Treatment.PLATINUM: "formal and gracious; a valued leadership donor",
    Treatment.GOLD: "warm and professional",
    Treatment.LAPSED_MAJOR: (
        "warm, respectful re-engagement; acknowledge the time that has passed "
        "without guilt or pressure"
    ),
}

# These are the rules the validator actually enforces. State every one of them here:
# a drafter judged against a rule it was never given produces rejected paragraphs and
# silent templated fallbacks, and policy is never to retry.
BRIEF_INSTRUCTIONS = (
    "Draft the campaign paragraph for each donor in `briefs`. Rules: 1-2 sentences, "
    "maximum 350 characters, written in the donor's stated tone for this campaign "
    "type. Use only facts present in that donor's brief plus the shared `campaign` "
    "block (organization, campaign type/name), which is the whitelist. You may echo "
    "the brief's ask_amount or lifetime_total, but introduce no other dollar "
    "amounts, and no URLs, program names, statistics, honorifics, or commitments "
    "(matching, premiums, naming) that are not in the brief. "
    "Three further rules are checked mechanically, so a paragraph that breaks one is "
    "rejected and the donor falls back to templated copy: (1) name a year only if it "
    "appears in that donor's `gift_years`, or is the `campaign_year`; (2) use "
    "continuity language ('every year', 'year after year', 'never missed') only when "
    "the brief's `gave_every_year` is true; (3) mention volunteering only when the "
    "brief's `volunteer` is true. "
    "Write the result as a single JSON object mapping donor_id to paragraph string, "
    "saved as paragraphs.json in the run directory."
)


def build_brief(
    record: DonorRecord,
    classification: Classification,
    ask: Decimal,
    campaign_year: int,
) -> dict:
    years = sorted(y for y, _ in record.gifts)
    brief = {
        "first_name": record.first_name,
        "treatment": classification.treatment.value,
        "capacity_tier": classification.capacity_tier,
        "region": record.region,
        "gift_count": len(record.gifts),
        "years_supported": f"{years[0]}-{years[-1]}" if years else str(record.last_gift_year),
        "gift_years": years,
        # Continuity language ("every year") is only true when every calendar year
        # between first and last gift is covered, and the validator gates on it. A single
        # gift is trivially gapless but is not a pattern, so it requires two or more
        # years: "you have given every year" is false for a donor who gave once.
        "gave_every_year": len(years) > 1 and set(range(years[0], years[-1] + 1)) <= set(years),
        "campaign_year": campaign_year,
        "last_gift_year": record.last_gift_year,
        "lifetime_total": format_money(record.lifetime_total),
        "volunteer": record.volunteer,
        "ask_amount": format_money(ask),
        "tone": BRIEF_TONES[classification.treatment],
    }
    if classification.treatment == Treatment.LAPSED_MAJOR:
        brief["note"] = (
            f"lapsed major donor: no gift since {record.last_gift_year}; this draft is "
            "held for gift-officer review"
        )
    return brief


# --------------------------------------------------------------------------- generate

def read_donor_rows(csv_path: Path) -> tuple[list[dict[str, str]], set[str]]:
    """Return (rows, normalized column names present in the file)."""
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise CsvError(f"{csv_path}: no header row")

            # Two columns that collapse onto one field would mean silently picking
            # a value, and the pipeline never silently picks. Refuse with names.
            raw_names = [n for n in reader.fieldnames if n is not None]
            if len(set(raw_names)) != len(raw_names):
                dupes = sorted({n for n in raw_names if raw_names.count(n) > 1})
                raise CsvError(
                    f"{csv_path}: duplicate column headers {dupes}; rename them "
                    "so every column is unambiguous"
                )
            keymap = {name: normalize_header(name) for name in raw_names}
            colliding: dict[str, list[str]] = {}
            for name, key in keymap.items():
                colliding.setdefault(key, []).append(name)
            collisions = {k: v for k, v in colliding.items() if len(v) > 1}
            if collisions:
                detail = "; ".join(
                    f"{sorted(names)} all normalize to '{key}'"
                    for key, names in sorted(collisions.items())
                )
                raise CsvError(
                    f"{csv_path}: column collision: {detail}. Rename the columns; "
                    "the pipeline never silently picks one over another."
                )

            if "donor_name" not in keymap.values():
                raise CsvError(
                    f"{csv_path}: no donor_name column (headers: {reader.fieldnames})"
                )
            rows = [
                {keymap[k]: (v or "") for k, v in row.items() if k is not None}
                for row in reader
            ]
            return rows, set(keymap.values())
    except OSError as exc:
        raise CsvError(f"cannot read {csv_path}: {exc}") from exc
    except csv.Error as exc:
        raise CsvError(f"{csv_path}: malformed CSV: {exc}") from exc
    except UnicodeDecodeError as exc:
        # No encoding-guess fallback chain: latin-1 never fails, so a chain would
        # silently accept mojibake donor names into letters. Refuse and say how
        # to fix it instead, because guessing contradicts this skill's never-guess rule.
        raise CsvError(
            f"{csv_path}: not valid UTF-8 ({exc.reason} at byte {exc.start}). "
            "Re-export the file as UTF-8 (in Excel: Save As -> 'CSV UTF-8')."
        ) from exc


def generate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    rows, columns = read_donor_rows(Path(args.csv))
    consent_columns = sorted(columns & (SUPPRESS_COLUMNS | CONSENT_COLUMNS))

    if len(rows) > config.limits.max_rows:
        raise VolumeError(
            f"{len(rows)} rows exceeds limits.max_rows={config.limits.max_rows}. "
            "Split the file into smaller batches and run each one."
        )

    run_id = args.run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) / f"run-{run_id}"
    if run_dir.exists():
        raise ConfigError(
            f"{run_dir} already exists; refusing to overwrite a run that may be "
            "under review. Pass a different --run-id."
        )
    letters_dir = run_dir / "letters"
    letters_dir.mkdir(parents=True)

    env = jinja_env()
    copy_blocks = load_copy()
    # "%-d" is glibc-only and "%#d" is Windows-only, so build the unpadded day by hand.
    today = datetime.date.today()
    letter_date = config.letter_date or f"{today:%B} {today.day}, {today.year}"

    summary_rows: list[dict[str, str]] = []
    exceptions: list[dict[str, str]] = []
    suppressed: list[dict[str, str]] = []
    donor_state: dict[str, dict] = {}
    candidates: list[tuple[Decimal, int, str]] = []  # (lifetime, row, donor_id)

    for index, raw in enumerate(rows, start=1):
        # Consent comes before parsing: a suppressed row is not processed at all,
        # and an unreadable consent value holds the row (blank is not permission).
        if consent_columns:
            verdict, reason = resolve_contactability(raw, columns)
            if verdict == "suppress":
                suppressed.append({
                    "row_number": str(index),
                    "donor_name": (raw.get("donor_name") or "").strip(),
                    "reason": reason,
                })
                continue
            if verdict == "unreadable":
                exceptions.append({
                    "row_number": str(index),
                    "donor_name": (raw.get("donor_name") or "").strip(),
                    "reason": reason,
                })
                continue
        try:
            record = parse_row(index, raw, config.campaign_year)
            classification = classify(record, config.campaign_year)
            ask, ask_flags, ask_trace = compute_ask(
                record, classification, config.campaign_year, config.campaign.type
            )
            ctx = build_letter_context(
                record, classification, ask, config, copy_blocks, env, letter_date
            )
            html = render_letter(env, ctx)
            problems = validate_rendered_letter(
                html, record, classification.treatment, ask, config
            )
            if problems:
                # A templated letter failing validation is a pipeline defect, not a
                # data problem, and the reason prefix tells downstream docs/audit so.
                raise RowError(f"internal validation failure: {'; '.join(problems)}")
        except RowError as exc:
            exceptions.append({
                "row_number": str(index),
                "donor_name": (raw.get("donor_name") or "").strip(),
                "reason": exc.reason,
            })
            continue

        letter_file = f"letters/{record.donor_id}.html"
        write_text(run_dir / letter_file, html)

        flags = record.flags + classification.flags + ask_flags
        summary_rows.append({
            "donor_id": record.donor_id,
            "donor_name": record.donor_name,
            "region": record.region or "",
            "provided_tier": record.provided_tier or "",
            "derived_tier": classification.capacity_tier,
            "recency": "lapsed" if classification.lapsed else "active",
            "treatment": classification.treatment.value,
            "review_tier": classification.review_tier,
            "review_status": "needs_review" if classification.review_tier == "A" else "spot_check",
            "generation_mode": "templated",
            "ask_amount": money_cell(ask),
            "ask_trace": ask_trace,
            "largest_gift": money_cell(record.largest_gift),
            "lifetime_total": money_cell(record.lifetime_total),
            "last_gift_year": str(record.last_gift_year),
            "volunteer": "Yes" if record.volunteer else "No",
            "flags": ";".join(flags),
            "letter_file": letter_file,
            "letter_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "validator_status": "passed",
        })

        donor_state[record.donor_id] = {
            "record": json.loads(record.model_dump_json()),
            "classification": {
                "capacity_tier": classification.capacity_tier,
                "lapsed": classification.lapsed,
                "treatment": classification.treatment.value,
                "review_tier": classification.review_tier,
            },
            "ask": str(ask),
        }
        if classification.review_tier == "A":
            candidates.append((record.lifetime_total, index, record.donor_id))
            donor_state[record.donor_id]["brief"] = build_brief(
                record, classification, ask, config.campaign_year
            )

    # Duplicate people: row-prefixed IDs make filename collisions impossible,
    # which also makes real duplicates invisible, so flag every row whose
    # normalized name appears more than once. Letters still generate; deciding
    # which record is canonical is the organization's call, not the pipeline's.
    name_groups: dict[str, list[str]] = {}
    for row in summary_rows:
        key = " ".join(row["donor_name"].split()).casefold()
        name_groups.setdefault(key, []).append(row["donor_id"])
    duplicate_ids = {d for ids in name_groups.values() if len(ids) > 1 for d in ids}
    for row in summary_rows:
        if row["donor_id"] in duplicate_ids:
            row["flags"] = ";".join(filter(None, [row["flags"], "duplicate_donor"]))

    # Highest-capacity donors get the capped personalization slots.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    briefed_ids = [donor_id for _, _, donor_id in candidates[: config.limits.max_personalized]]
    over_cap = {donor_id for _, _, donor_id in candidates[config.limits.max_personalized:]}
    for row in summary_rows:
        if row["donor_id"] in over_cap:
            row["flags"] = ";".join(filter(None, [row["flags"], "over_personalization_cap"]))

    write_outputs(run_dir, config, letter_date, summary_rows, exceptions,
                  suppressed, briefed_ids, donor_state,
                  input_rows=len(rows), consent_columns=consent_columns)
    print_run_summary(run_dir, summary_rows, exceptions, suppressed,
                      briefed_ids, config, consent_columns)
    return EXIT_OK


def write_outputs(
    run_dir: Path,
    config: CampaignConfig,
    letter_date: str,
    summary_rows: list[dict],
    exceptions: list[dict],
    suppressed: list[dict],
    briefed_ids: list[str],
    donor_state: dict[str, dict],
    *,
    input_rows: int,
    consent_columns: list[str],
) -> None:
    write_csv(run_dir / "run_summary.csv", SUMMARY_COLUMNS, summary_rows)
    write_csv(run_dir / "exceptions.csv", ["row_number", "donor_name", "reason"], exceptions)
    write_csv(run_dir / "suppressed.csv", ["row_number", "donor_name", "reason"], suppressed)

    briefs = {
        "instructions": BRIEF_INSTRUCTIONS,
        "campaign": {
            "organization": config.organization,
            "type": config.campaign.type,
            "name": config.campaign.name,
            "match_confirmed": config.match_confirmed,
        },
        "briefs": {donor_id: donor_state[donor_id]["brief"] for donor_id in briefed_ids},
    }
    write_text(
        run_dir / "briefs.json",
        json.dumps(briefs, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )

    state = {
        "schema": 2,
        "letter_date": letter_date,
        "input_rows": input_rows,
        "consent": {
            "columns": consent_columns,
            "warning": (
                None if consent_columns else
                "no consent/suppression column in this file; every row was "
                "treated as contactable; confirm that is true of the source system"
            ),
        },
        "config": json.loads(config.model_dump_json()),
        "donors": donor_state,
        "briefed_ids": briefed_ids,
        "summary_rows": summary_rows,
    }
    write_text(
        run_dir / "_state.json",
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
    )


def print_run_summary(
    run_dir: Path,
    summary_rows: list[dict],
    exceptions: list[dict],
    suppressed: list[dict],
    briefed_ids: list[str],
    config: CampaignConfig,
    consent_columns: list[str],
) -> None:
    tier_a = [r for r in summary_rows if r["review_tier"] == "A"]
    tier_b = [r for r in summary_rows if r["review_tier"] == "B"]
    # Count by flag name, so tier_mismatch(provided=…) rows tally as "tier_mismatch".
    flag_counts: Counter[str] = Counter(
        flag.split("(")[0]
        for row in summary_rows
        for flag in filter(None, row["flags"].split(";"))
    )

    print(f"Run directory: {run_dir}")
    print(f"Letters generated: {len(summary_rows)} "
          f"(Tier A needs_review: {len(tier_a)}, Tier B spot_check: {len(tier_b)})")
    print(f"Exceptions (no letter): {len(exceptions)} -> exceptions.csv")
    print(f"Suppressed (no letter, by donor consent): {len(suppressed)} -> suppressed.csv")
    if consent_columns:
        print(f"Consent columns honored: {', '.join(consent_columns)}")
    else:
        print("WARNING: no consent/suppression column in this file; every row was "
              "treated as contactable; confirm that is true of the source system.")
    if flag_counts:
        rendered = " ".join(f"{k}={v}" for k, v in sorted(flag_counts.items()))
        print(f"Flags for human attention: {rendered}")
    print(f"AI personalization briefs: {len(briefed_ids)} "
          f"(cap {config.limits.max_personalized}) -> briefs.json")


# ----------------------------------------------------------- apply-personalization

def _load_json_object(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise CsvError(f"cannot read {what} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CsvError(f"{what} {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CsvError(f"{what} {path} must be a JSON object")
    return data


def _classification_from_state(entry: dict) -> Classification:
    stored = entry["classification"]
    return Classification(
        capacity_tier=stored["capacity_tier"],
        lapsed=stored["lapsed"],
        treatment=Treatment(stored["treatment"]),
        review_tier=stored["review_tier"],
    )


def apply_personalization(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    state_path = run_dir / "_state.json"
    if not state_path.exists():
        raise CsvError(f"{run_dir} contains no _state.json; not a generate run directory")
    state = _load_json_object(state_path, "run state")
    paragraphs = _load_json_object(Path(args.paragraphs), "paragraphs file")

    config = CampaignConfig.model_validate(state["config"])
    env = jinja_env()
    copy_blocks = load_copy()
    letter_date = state["letter_date"]
    briefed: list[str] = state["briefed_ids"]
    summary_by_id = {row["donor_id"]: row for row in state["summary_rows"]}

    ignored_ids = sorted(set(paragraphs) - set(briefed))
    applied: list[str] = []
    fallbacks: dict[str, str] = {}

    for donor_id in briefed:
        entry = state["donors"][donor_id]
        record = DonorRecord.model_validate(entry["record"])
        classification = _classification_from_state(entry)
        ask = Decimal(entry["ask"])
        row = summary_by_id[donor_id]

        text = paragraphs.get(donor_id)
        problems: list[str] = []
        if text is None:
            fallbacks[donor_id] = "paragraph_missing"
        else:
            problems = letter_validator.validate_paragraph(text, entry["brief"])
            if not problems:
                ctx = build_letter_context(
                    record, classification, ask, config, copy_blocks, env,
                    letter_date, campaign_paragraph=str(text).strip(),
                )
                html = render_letter(env, ctx)
                problems = validate_rendered_letter(
                    html, record, classification.treatment, ask, config
                )
                if not problems:
                    write_text(run_dir / row["letter_file"], html)
                    row["letter_sha256"] = hashlib.sha256(html.encode()).hexdigest()
                    applied.append(donor_id)
            if problems:
                # The flags column is ';'-separated, so the reason cannot carry one.
                reason = problems[0].replace(";", ",")
                fallbacks[donor_id] = f"paragraph_rejected({reason})"

        if donor_id in fallbacks:
            # Restore the templated letter (a prior apply may have personalized it,
            # including when this paragraphs file simply omits the donor).
            ctx = build_letter_context(
                record, classification, ask, config, copy_blocks, env, letter_date
            )
            restored = render_letter(env, ctx)
            write_text(run_dir / row["letter_file"], restored)
            row["letter_sha256"] = hashlib.sha256(restored.encode()).hexdigest()

        base_flags = [f for f in row["flags"].split(";") if f and not f.startswith("paragraph_")]
        if donor_id in fallbacks:
            row["generation_mode"] = "templated_fallback"
            base_flags.append(fallbacks[donor_id])
        else:
            row["generation_mode"] = "ai_personalized"
        row["flags"] = ";".join(base_flags)

    write_csv(run_dir / "run_summary.csv", SUMMARY_COLUMNS, state["summary_rows"])

    state["personalization"] = {
        "applied": applied,
        "fallbacks": fallbacks,
        "ignored_unknown_ids": ignored_ids,
        "paragraphs_used": {donor_id: paragraphs[donor_id] for donor_id in applied},
    }
    write_text(
        state_path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
    )

    missing = sum(1 for r in fallbacks.values() if r == "paragraph_missing")
    rejected = len(fallbacks) - missing
    print(f"Personalization applied: {len(applied)} of {len(briefed)} briefed donors")
    print(f"Templated fallback: {len(fallbacks)} (missing paragraph: {missing}, "
          f"rejected by validator: {rejected}); letters remain complete and valid")
    if ignored_ids:
        print(f"Ignored {len(ignored_ids)} paragraph key(s) matching no briefed donor: "
              f"{', '.join(ignored_ids[:5])}{'…' if len(ignored_ids) > 5 else ''}")
    print("Every Tier-A letter still requires individual human review before use.")
    return EXIT_OK


# --------------------------------------------------------------------------- cli

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="deterministic pass 1: CSV + config -> letters")
    gen.add_argument("--csv", required=True, help="donor CSV path")
    gen.add_argument("--config", required=True, help="campaign_config.yaml path")
    gen.add_argument("--out", default="output", help="output base directory")
    gen.add_argument("--run-id", default=None, help="run directory suffix (default: timestamp)")
    gen.set_defaults(func=generate)

    apply = sub.add_parser(
        "apply-personalization",
        help="merge drafted paragraphs.json into Tier-A letters (validated, with fallback)",
    )
    apply.add_argument("--run-dir", required=True, help="the output/run-<id> directory")
    apply.add_argument("--paragraphs", required=True, help="paragraphs.json path")
    apply.set_defaults(func=apply_personalization)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except CsvError as exc:
        print(f"CSV ERROR: {exc}", file=sys.stderr)
        return EXIT_CSV
    except VolumeError as exc:
        print(f"VOLUME LIMIT: {exc}", file=sys.stderr)
        return EXIT_VOLUME


if __name__ == "__main__":
    sys.exit(main())
