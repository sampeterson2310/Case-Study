from pathlib import Path

import pytest

import pipeline
from letter_validator import audit_run, validate_paragraph

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = pipeline.load_config(FIXTURES / "sample_campaign.yaml")
CY = CONFIG.campaign_year


def build_letter(config=CONFIG, **row_overrides):
    base = {
        "donor_name": "Elizabeth Warren",
        "tier": "Gold",
        "region": "Northeast",
        "gifts": "2019: $10,000, 2021: $12,500, 2023: $15,000",
        "largest_gift": "$15,000",
        "lifetime_total": "$37,500",
        "last_gift_year": "2023",
        "volunteer": "No",
    }
    base.update(row_overrides)
    record = pipeline.parse_row(1, base, campaign_year=CY)
    cls = pipeline.classify(record, CY)
    ask, _, _trace = pipeline.compute_ask(record, cls, CY, CONFIG.campaign.type)
    env = pipeline.jinja_env()
    ctx = pipeline.build_letter_context(
        record, cls, ask, config, pipeline.load_copy(), env, "August 1, 2024"
    )
    html = pipeline.render_letter(env, ctx)
    return html, record, cls.treatment, ask


def problems_for(html, record, treatment, ask, config=CONFIG):
    return pipeline.validate_rendered_letter(html, record, treatment, ask, config)


def test_clean_templated_letter_passes():
    html, record, treatment, ask = build_letter()
    assert problems_for(html, record, treatment, ask) == []


@pytest.mark.parametrize(
    "tamper, expected_fragment",
    [
        (lambda h: h.replace("consider a gift", "consider a gift of [ASK_AMOUNT] or"),
         "unresolved placeholder"),
        (lambda h: h.replace("$4,150", "$9,999"), "not a fact of this donor's record"),
        (lambda h: h.replace("</p>", " Your gift will be matched dollar-for-dollar!</p>", 1),
         "match is not confirmed"),
        (lambda h: h.replace("Dear Elizabeth Warren,", "Hi Elizabeth,"),
         "salutation does not match rules"),
        (lambda h: h.replace("</body>", '<a href="https://evil.example.com">click</a></body>'),
         "exactly one link"),
        (lambda h: h.replace("Sam Peterson", "A Friend"), "signer name missing"),
    ],
)
def test_tampered_letters_are_rejected(tamper, expected_fragment):
    html, record, treatment, ask = build_letter()
    assert pipeline.format_money(ask) == "$4,150"  # 15000*.25 -> *1.10 -> 4125 -> 4150
    problems = problems_for(tamper(html), record, treatment, ask)
    assert any(expected_fragment in p for p in problems), problems


def test_missing_ask_is_rejected():
    html, record, treatment, ask = build_letter()
    tampered = html.replace("$4,150", "$4,000")  # $4,000 is not in the record either
    problems = problems_for(tampered, record, treatment, ask)
    assert any("missing from letter" in p for p in problems)


def test_too_short_letter_is_rejected():
    html, record, treatment, ask = build_letter()
    gutted = html.split("<p>")[0] + "<p>Dear Elizabeth Warren,</p><p>$4,150</p></body></html>"
    problems = problems_for(gutted, record, treatment, ask)
    assert any("words outside" in p for p in problems)


def test_match_line_is_allowed_when_confirmed():
    confirmed = CONFIG.model_copy(deep=True)
    confirmed.match_confirmed = True
    confirmed.match_sponsor = "The Bergstrom Family Foundation"
    record = pipeline.parse_row(1, {
        "donor_name": "Elizabeth Warren", "tier": "Gold",
        "gifts": "2021: $12,500, 2023: $15,000", "largest_gift": "$15,000",
        "lifetime_total": "$37,500", "last_gift_year": "2023", "volunteer": "No",
    }, campaign_year=CY)
    cls = pipeline.classify(record, CY)
    ask, _, _trace = pipeline.compute_ask(record, cls, CY, confirmed.campaign.type)
    env = pipeline.jinja_env()
    ctx = pipeline.build_letter_context(
        record, cls, ask, confirmed, pipeline.load_copy(), env, "August 1, 2024"
    )
    html = pipeline.render_letter(env, ctx)
    assert "matched by The Bergstrom Family Foundation" in html
    assert pipeline.validate_rendered_letter(
        html, record, cls.treatment, ask, confirmed
    ) == []


def test_donation_url_with_query_string_passes():
    # Jinja escapes "&" to "&amp;" in the href; that is the same link, not a stray one.
    tracked = CONFIG.model_copy(deep=True)
    tracked.donation_url = "https://secure.aspca.org/donate?utm_source=mail&utm_campaign=fy24"
    html, record, treatment, ask = build_letter(config=tracked)
    assert "&amp;utm_campaign" in html
    assert problems_for(html, record, treatment, ask, tracked) == []


def test_config_owned_text_is_not_scanned_as_letter_claims():
    # A quid-pro-quo disclosure has to name a dollar value and deductibility, and a
    # bracketed nickname is part of a name; neither is the letter inventing a fact.
    disclosed = CONFIG.model_copy(deep=True)
    disclosed.compliance_footer = (
        "Your gift is tax-deductible to the extent it exceeds the $25 value of any "
        "goods received. " + "This disclosure is required by law. " * 30
    )
    disclosed.regional_notes = {"Northeast": "Our [Boston] shelter reopens in May."}
    html, record, treatment, ask = build_letter(config=disclosed, donor_name="Robert [Bob] Smith")
    assert problems_for(html, record, treatment, ask, disclosed) == []
    # The exemption covers only the approved text, never the body around it.
    tampered = html.replace("consider a gift", "consider a tax-deductible gift")
    assert any("tax-advice" in p for p in problems_for(tampered, record, treatment, ask, disclosed))


@pytest.mark.parametrize(
    "largest, lifetime, single",
    [
        ("$90,000", "$275,000", False),  # many gifts, none itemized: no invented gift
        ("$5,000", "$5,000", True),      # lifetime equals largest: one gift, known
    ],
)
def test_opening_without_gift_history_states_only_known_facts(largest, lifetime, single):
    html, *_ = build_letter(gifts="", largest_gift=largest, lifetime_total=lifetime,
                            last_gift_year="2022")
    assert ("2022 gift of" in html) is single
    assert (f"{lifetime} in total" in html) is not single


# --------------------------------------------------------------------- paragraphs
#
# Three brief profiles so both failure directions are measured: a validator that
# rejected everything would look perfect against a block-only corpus while
# degrading every letter to templated copy. The must-pass set is the over-block
# metric; the must-block set is the missed-fabrication metric.

BRIEF_GAPPED = {  # gap-year donor, not a volunteer
    "ask_amount": "$4,150", "lifetime_total": "$37,500", "first_name": "Elizabeth",
    "region": "Northeast", "gift_years": [2019, 2021, 2023], "gave_every_year": False,
    "volunteer": False, "campaign_year": 2024, "last_gift_year": 2023,
}
BRIEF_LOYAL = {  # unbroken-streak volunteer
    "ask_amount": "$36,100", "lifetime_total": "$275,000", "first_name": "Earl",
    "region": "Southeast", "gift_years": [2019, 2020, 2021, 2022, 2023],
    "gave_every_year": True, "volunteer": True, "campaign_year": 2024,
    "last_gift_year": 2023,
}
BRIEF_FIRST_TIME = {  # one large gift: reaches Tier A, has no giving *pattern*
    "ask_amount": "$6,250", "lifetime_total": "$25,000", "first_name": "Nadia",
    "region": "West", "gift_years": [2023], "gave_every_year": False,
    "volunteer": False, "campaign_year": 2024, "last_gift_year": 2023,
}

GOOD = ("Your steady support since 2019 has helped keep our shelters ready for the "
        "animals who need them most. Thank you for being part of that safety net.")


@pytest.mark.parametrize(
    "text, brief",
    [
        (GOOD, BRIEF_GAPPED),
        ("Across $37,500 of giving you have been a constant friend. Thank you.",
         BRIEF_GAPPED),
        ("Animals across the region have a safer place to land because of friends "
         "like you. Thank you for standing with this work.", BRIEF_GAPPED),
        ("Your support has meant a great deal to the Northeast rescue community we "
         "serve. Thank you for being part of it.", BRIEF_GAPPED),
        ("As 2024 brings new challenges for animals in crisis, your friendship "
         "matters more than ever. Thank you.", BRIEF_GAPPED),
        ("Your 2023 gift arrived exactly when it was needed most. Thank you for "
         "staying close to this work.", BRIEF_GAPPED),
        ("Thank you for being a steady friend to animals who needed one.",
         BRIEF_GAPPED),
        # A figure followed by punctuation is ordinary prose, and a live run put
        # exactly this in front of the validator. The dollar pattern used to
        # swallow the trailing comma, so "$37,500," never matched the brief's
        # "$37,500" and an honest paragraph fell back to templated copy.
        ("Your gifts brought your lifetime support to $37,500, and that steadiness "
         "is what lets us plan past the next crisis.", BRIEF_GAPPED),
        ("For this campaign, would you consider a gift of $4,150? Either way, thank "
         "you for standing with this work.", BRIEF_GAPPED),
        ("From your first gift in 2019 to your most recent in 2023, you have helped "
         "animals find safety. Thank you.", BRIEF_GAPPED),
        ("You have given every single year since 2019, and that consistency is the "
         "backbone of this work. Thank you.", BRIEF_LOYAL),
        ("Each year, you have been there for animals who had no one else. Thank you "
         "for that steadiness.", BRIEF_LOYAL),
        ("Beyond your giving, the hours you volunteer with us have meant just as "
         "much. We would be glad to see you at the shelter again soon.", BRIEF_LOYAL),
        ("We hope you will consider the $36,100 invitation in this letter as a "
         "continuation of everything you have built with us. Either way, thank you.",
         BRIEF_LOYAL),
        # A first-time donor can still be written to warmly: the rule removes the
        # false pattern claim, not the personalization.
        ("Your 2023 gift was a generous way to begin, and it went to work for animals "
         "who needed it. Thank you for starting here.", BRIEF_FIRST_TIME),
        ("Welcome: your first gift arrived at a moment when the West Coast shelters "
         "we serve needed it badly. Thank you.", BRIEF_FIRST_TIME),
    ],
)
def test_grounded_paragraphs_must_pass(text, brief):
    assert validate_paragraph(text, brief) == []  # over-blocking degrades letters


@pytest.mark.parametrize(
    "text, brief, expected_fragment",
    [
        ("One. Two. Three sentences is too many.", BRIEF_GAPPED, "sentences exceeds"),
        ("A generous friend pledged $9,999 toward this.", BRIEF_GAPPED,
         "not in the donor's brief"),
        # Refused in a paragraph whatever the config says, and the message says so
        # rather than sending the reader to check a config that may be correct.
        ("Give today and your gift will be matched.", BRIEF_GAPPED,
         "belongs to the config-gated match_line"),
        ("We guarantee your gift changes lives.", BRIEF_GAPPED, "guarantee/promise"),
        ("Your gift is tax-deductible, of course.", BRIEF_GAPPED, "tax-advice"),
        ("See https://example.com for details.", BRIEF_GAPPED, "URL in paragraph"),
        ("Dear Ms. Warren, thank you again.", BRIEF_GAPPED, "honorific in paragraph"),
        ("Fill in [DONOR_STORY] here.", BRIEF_GAPPED, "template token"),
        ("", BRIEF_GAPPED, "empty"),
        (None, BRIEF_GAPPED, "empty"),
        ("x" * 400 + ".", BRIEF_GAPPED, "exceeds 350"),
        ("Since 2015, you have stood with us at every turn. Thank you.",
         BRIEF_GAPPED, "2015 is not in this donor's giving history"),
        ("You have given every single year, without fail. Thank you.",
         BRIEF_GAPPED, "giving has gaps"),
        ("Your volunteering means the world to this team. Thank you.",
         BRIEF_GAPPED, "not recorded as a volunteer"),
        # A first-time major donor is gapless only because there is nothing to gap.
        # "Every year" is false for someone who has given once, so the brief reports
        # gave_every_year=False and these must block just as a gap-year donor's do.
        ("You have given every single year, and it shows. Thank you.",
         BRIEF_FIRST_TIME, "giving has gaps"),
        ("Year after year you have stood with these animals. Thank you.",
         BRIEF_FIRST_TIME, "giving has gaps"),
        ("You have never missed a year, and that matters more than you know.",
         BRIEF_FIRST_TIME, "giving has gaps"),
    ],
)
def test_fabricated_paragraphs_must_block(text, brief, expected_fragment):
    problems = validate_paragraph(text, brief)
    assert any(expected_fragment in p for p in problems), problems


# ------------------------------------------------------------------------- audit

def test_full_sample_run_passes_independent_audit(tmp_path):
    rc = pipeline.main([
        "generate",
        "--csv", str(FIXTURES / "sample_donors.csv"),
        "--config", str(FIXTURES / "sample_campaign.yaml"),
        "--out", str(tmp_path), "--run-id", "audit-test",
    ])
    assert rc == 0
    run_dir = tmp_path / "run-audit-test"
    assert audit_run(run_dir) == 0

    # A hand-edited letter no longer matches the bytes its manifest row
    # authorized, and the audit's hash check must catch even a one-space edit
    # that every content rule would miss.
    victim = next((run_dir / "letters").glob("*.html"))
    victim.write_bytes(victim.read_bytes() + b" ")
    assert audit_run(run_dir) == 1
