from decimal import Decimal

import pytest

from pipeline import (
    RowError,
    Treatment,
    build_brief,
    build_salutation,
    classify,
    compute_ask,
    derive_capacity_tier,
    is_lapsed,
    make_donor_id,
    parse_money,
    parse_row,
    round_to_nearest_50,
)

CY = 2024  # campaign_year used throughout; anchors all recency math


def row(**overrides):
    """A parseable donor row with sensible defaults; override per test."""
    base = {
        "donor_name": "Test Donor",
        "tier": "Silver",
        "region": "Northeast",
        "gifts": "2022: $1,000, 2023: $1,400",
        "largest_gift": "$1,400",
        "lifetime_total": "$2,400",
        "last_gift_year": "2023",
        "volunteer": "No",
    }
    base.update(overrides)
    return base


def record(**overrides):
    return parse_row(1, row(**overrides), campaign_year=CY)


# --- §4.1 capacity tier boundaries (incl. the original's $50,000 gap) ---

@pytest.mark.parametrize(
    "lifetime, tier",
    [
        (Decimal(50_000), "Platinum"),  # the gap: exactly $50,000 is Platinum
        (Decimal(49_999), "Gold"),
        (Decimal(10_000), "Gold"),
        (Decimal(9_999), "Silver"),
        (Decimal(1_000), "Silver"),
        (Decimal(999), "Bronze"),
        (Decimal(0), "Bronze"),
    ],
)
def test_capacity_tier_boundaries(lifetime, tier):
    assert derive_capacity_tier(lifetime) == tier


# --- §4.2 lapsed boundary is strict ---

def test_exactly_three_years_is_not_lapsed():
    assert is_lapsed(2021, CY) is False  # Ambrosius/Petersen/Okafor/Johansson
    assert is_lapsed(2020, CY) is True   # Svensson/Adeyemi


# --- §6 rounding: final, HALF_UP, floor $50 ---

@pytest.mark.parametrize(
    "raw, rounded",
    [
        (Decimal("225"), Decimal(250)),      # banker's round() would give 200
        (Decimal("75"), Decimal(100)),       # 1.5 units -> HALF_UP -> 2
        (Decimal("331"), Decimal(350)),
        (Decimal("314.5"), Decimal(300)),
        (Decimal("43320"), Decimal(43300)),
        (Decimal("20"), Decimal(50)),        # floor
        (Decimal("0"), Decimal(50)),         # floor
    ],
)
def test_round_to_nearest_50_half_up(raw, rounded):
    assert round_to_nearest_50(raw) == rounded


# --- §6 worked examples from the spec (annual campaign unless noted) ---

def ask_for(rec, campaign_type="annual"):
    cls = classify(rec, CY)
    ask, _flags, _trace = compute_ask(rec, cls, CY, campaign_type)
    return ask


def test_ask_harold_mensah_silver_loyal_volunteer():
    rec = record(donor_name="Harold Mensah", largest_gift="$1,400",
                 lifetime_total="$3,600", last_gift_year="2023", volunteer="Yes")
    assert ask_for(rec) == Decimal(350)  # 210 -> 231 -> 331 -> 350


def test_ask_susan_nakamura_current_year_gift_gets_uplift():
    rec = record(donor_name="Susan Nakamura", gifts="2023: $1,250, 2024: $1,500",
                 largest_gift="$1,500", lifetime_total="$5,000",
                 last_gift_year="2024", volunteer="Yes")
    assert ask_for(rec) == Decimal(350)  # 225 -> 247.50 -> 347.50 -> 350


def test_ask_svensson_lapsed_major_uses_capacity_formula():
    rec = record(donor_name="Robert Svensson", tier="Platinum",
                 gifts="2016: $40,000, 2020: $50,000", largest_gift="$50,000",
                 lifetime_total="$145,000", last_gift_year="2020")
    assert ask_for(rec) == Decimal(20_000)  # 40% of largest, no modifiers apply


def test_ask_torres_standard_lapsed_flat_50():
    rec = record(donor_name="Michael Torres", tier="Lapsed",
                 gifts="2018: $600, 2019: $700", largest_gift="$700",
                 lifetime_total="$1,800", last_gift_year="2019")
    assert ask_for(rec) == Decimal(50)


def test_ask_fontaine_emergency_multiplier_then_final_round():
    rec = record(donor_name="Earl Fontaine", tier="Platinum",
                 gifts="2018: $75,000, 2022: $90,000", largest_gift="$90,000",
                 lifetime_total="$275,000", last_gift_year="2022", volunteer="Yes")
    assert ask_for(rec, "emergency") == Decimal(43_300)  # 36000 -> 36100 -> 43320 -> 43300


def test_ask_alcott_loyalty_only():
    rec = record(donor_name="Margaret Alcott", largest_gift="$1,500",
                 lifetime_total="$4,950", last_gift_year="2023")
    assert ask_for(rec) == Decimal(250)  # 225 -> 247.50 -> 250


# --- §6 flat asks are exempt from every modifier ---

def test_bronze_flat_ask_ignores_all_modifiers():
    rec = record(tier="Bronze", gifts="2023: $250", largest_gift="$250",
                 lifetime_total="$250", last_gift_year="2023", volunteer="Yes")
    assert ask_for(rec, "emergency") == Decimal(150)


def test_lapsed_flat_ask_ignores_all_modifiers():
    rec = record(tier="Lapsed", gifts="2019: $300, 2020: $300", largest_gift="$300",
                 lifetime_total="$600", last_gift_year="2020", volunteer="Yes")
    assert ask_for(rec, "emergency") == Decimal(50)  # original's ordering: $180


# --- §6.6 sanity flag ---

def test_ask_exceeding_largest_gift_is_flagged_not_blocked():
    rec = record(gifts="2022: $1,000, 2023: $60", largest_gift="$60",
                 lifetime_total="$1,060", last_gift_year="2023", volunteer="Yes")
    cls = classify(rec, CY)
    ask, flags, _trace = compute_ask(rec, cls, CY, "emergency")
    assert ask == Decimal(150)  # 9 -> 9.90 -> 109.90 -> 131.88 -> 150
    assert "ask_exceeds_largest_gift" in flags


# --- §5 treatment matrix & §9 review tiers ---

def test_svensson_is_lapsed_major_draft_and_hold():
    rec = record(donor_name="Robert Svensson", tier="Platinum",
                 gifts="2016: $40,000, 2020: $50,000", largest_gift="$50,000",
                 lifetime_total="$145,000", last_gift_year="2020")
    cls = classify(rec, CY)
    assert cls.treatment is Treatment.LAPSED_MAJOR
    assert cls.review_tier == "A"
    assert "lapsed_major_hold" in cls.flags


def test_active_silver_is_tier_b():
    cls = classify(record(), CY)
    assert cls.treatment is Treatment.SILVER
    assert cls.review_tier == "B"
    assert cls.flags == []


# --- §4.3 provided tier is a claim: flag, use derived, never silently correct ---

def test_mwangi_bronze_label_derives_silver_with_flag():
    rec = record(donor_name="Arthur Mwangi", tier="Bronze",
                 gifts="2022: $700, 2023: $800", largest_gift="$800",
                 lifetime_total="$2,600", last_gift_year="2023", volunteer="Yes")
    cls = classify(rec, CY)
    assert cls.treatment is Treatment.SILVER
    assert "tier_mismatch(provided=Bronze,derived=Silver)" in cls.flags


def test_andersen_silver_label_derives_gold_and_upgrades_to_tier_a():
    rec = record(donor_name="Ruth Andersen", tier="Silver",
                 gifts="2022: $6,000, 2023: $7,000", largest_gift="$7,000",
                 lifetime_total="$25,000", last_gift_year="2023", volunteer="Yes")
    cls = classify(rec, CY)
    assert cls.treatment is Treatment.GOLD
    assert cls.review_tier == "A"
    assert "tier_mismatch(provided=Silver,derived=Gold)" in cls.flags


def test_lapsed_label_is_a_recency_claim_not_a_mismatch():
    rec = record(donor_name="Michael Torres", tier="Lapsed",
                 gifts="2018: $600, 2019: $700", largest_gift="$700",
                 lifetime_total="$1,800", last_gift_year="2019")
    cls = classify(rec, CY)
    assert not any(f.startswith("tier_mismatch") for f in cls.flags)


def test_unknown_and_blank_tier_labels_derive_without_mismatch():
    for label in ("Unknown", ""):
        cls = classify(record(tier=label), CY)
        assert cls.treatment is Treatment.SILVER
        assert not any(f.startswith("tier_mismatch") for f in cls.flags)


# --- §1.1/§3 parsing: derive-or-exception, never assume ---

def test_missing_largest_gift_derived_from_gifts():
    rec = record(largest_gift="")
    assert rec.largest_gift == Decimal(1_400)


def test_missing_everything_with_no_gifts_is_an_exception():
    with pytest.raises(RowError):
        parse_row(1, row(gifts="", largest_gift="", lifetime_total=""), campaign_year=CY)


def test_future_dated_gift_year_is_an_exception():
    with pytest.raises(RowError):
        record(gifts="2022: $1,000, 2025: $1,400", last_gift_year="2025")


def test_negative_amount_is_an_exception():
    with pytest.raises(RowError):
        record(largest_gift="-$500")


def test_inconsistent_history_flagged_but_provided_values_win():
    rec = record(largest_gift="$2,000")  # gifts max is $1,400
    assert rec.largest_gift == Decimal(2_000)
    assert any(f.startswith("data_inconsistency") for f in rec.flags)


def test_parse_money_handles_currency_strings():
    assert parse_money("$1,200") == Decimal(1_200)
    assert parse_money("1200.00") == Decimal(1_200)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-inf"])
def test_non_finite_amount_is_a_row_exception_not_a_crash(text):
    with pytest.raises(RowError):
        record(largest_gift=text)


def test_gift_history_without_space_after_comma_parses_as_two_gifts():
    rec = record(gifts="2022: $1,000,2023: $1,400")
    assert rec.gifts == [(2022, Decimal(1_000)), (2023, Decimal(1_400))]


@pytest.mark.parametrize(
    "gifts",
    ["2022: $1,000, 2023: $1.4k", "2022: $1,000, 2023: -$1,400", "2022: $1,000, about $1,400"],
)
def test_unreadable_gift_history_is_an_exception_not_a_partial_read(gifts):
    with pytest.raises(RowError):
        record(gifts=gifts)


# --- §7 salutations: no guessing, no formality downgrade ---

def salutation_for(rec):
    return build_salutation(rec, classify(rec, CY).treatment)


def test_platinum_with_title_uses_title():
    rec = record(tier="Platinum", title="Mr.", gifts="2020: $50,000",
                 largest_gift="$50,000", lifetime_total="$145,000",
                 last_gift_year="2023", donor_name="Robert Svensson")
    assert salutation_for(rec) == "Dear Mr. Svensson,"


@pytest.mark.parametrize("gifts, expected, why", [
    ("2021: $5,000, 2022: $6,000, 2023: $7,000", True, "three consecutive years"),
    ("2019: $5,000, 2021: $6,000, 2023: $7,000", False, "gap years"),
    ("2023: $25,000", False, "one gift is gapless but is not a pattern"),
    ("2022: $10,000, 2023: $15,000", True, "two consecutive years is a pattern"),
])
def test_gave_every_year_requires_an_actual_pattern(gifts, expected, why):
    """The validator can only be as right as the fact it gates on.

    A single-gift donor is trivially 'unbroken', so a naive check would let an
    AI paragraph tell a first-time donor they have given every year, the same
    unsupported-continuity class the templated copy is held to.
    """
    # largest_gift/lifetime_total left blank so they derive from the gifts string,
    # which keeps each row internally consistent (no incidental data_inconsistency flag).
    rec = record(gifts=gifts, tier="Gold", largest_gift="", lifetime_total="")
    brief = build_brief(rec, classify(rec, CY), Decimal("6250"), CY)
    assert brief["gave_every_year"] is expected, why


def test_gold_without_title_keeps_formal_full_name():
    # The tempting assertion here is "Hi Elizabeth,", which would encode the
    # original's honorific guess as correct behavior. Missing title must not
    # downgrade formality: fall back to the full name, never to the first name.
    rec = record(donor_name="Elizabeth Warren", tier="Gold", gifts="2023: $12,000",
                 largest_gift="$12,000", lifetime_total="$35,000", last_gift_year="2023")
    assert salutation_for(rec) == "Dear Elizabeth Warren,"


def test_silver_informal():
    assert salutation_for(record(donor_name="Margaret Alcott")) == "Hi Margaret,"


def test_lapsed_gets_standard_informal_greeting_not_a_conduct_claim():
    # The original spec's "We've missed you, Michael!" asserts the org's own
    # conduct, dropped per business-rules §7/§12 row 17.
    rec = record(donor_name="Michael Torres", gifts="2019: $700", largest_gift="$700",
                 lifetime_total="$1,800", last_gift_year="2019")
    assert salutation_for(rec) == "Hi Michael,"


def test_mononym_formal_uses_the_name_alone():
    rec = record(donor_name="Cher", tier="Gold", gifts="2023: $12,000",
                 largest_gift="$12,000", lifetime_total="$35,000", last_gift_year="2023")
    assert salutation_for(rec) == "Dear Cher,"


# --- §2 identity: slug safety and collision-proofing ---

def test_donor_id_is_filesystem_safe_against_hostile_names():
    hostile = "../../etc/passwd'); DROP TABLE donors;--"
    did = make_donor_id(7, hostile)
    assert did.startswith("007-")
    assert all(c.isalnum() or c == "-" for c in did)
    assert ".." not in did and "/" not in did


def test_near_collision_names_get_distinct_ids():
    assert make_donor_id(9, "Frank Watanabe") != make_donor_id(37, "Frank Watanabe")
    assert make_donor_id(1, "Helen Magnusson") != make_donor_id(2, "Shirley Magnusdottir")
