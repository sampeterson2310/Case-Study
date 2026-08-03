import re
from pathlib import Path

import yaml

from letter_validator import _banned_content

COPY = yaml.safe_load(
    (Path(__file__).parents[1] / "charity-donor-outreach" / "assets" / "copy.yaml")
    .read_text(encoding="utf-8")
)


def walk_strings(node, path="copy"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk_strings(value, f"{path}.{key}")
    elif isinstance(node, str):
        yield path, node


def test_no_banned_language_anywhere_in_approved_copy():
    failures = []
    for path, text in walk_strings(COPY):
        allowed = path == "copy.match_line"  # the one config-gated exception
        for problem in _banned_content(text, match_language_allowed=allowed):
            failures.append(f"{path}: {problem}")
    assert failures == [], failures


def test_match_line_exists_and_is_the_only_match_language():
    assert "match" in COPY["match_line"].lower()
    non_match_strings = [t for p, t in walk_strings(COPY) if p != "copy.match_line"]
    assert all(
        _banned_content(t, match_language_allowed=False) == [] for t in non_match_strings
    )


def test_emergency_lapsed_variant_exists_for_gentler_reengagement():
    assert "emergency_lapsed" in COPY["campaign_paragraphs"]


# Campaign paragraphs are selected by campaign TYPE alone (build_letter_context),
# so unlike `openings` they are not gated on the donor's giving history. Any claim
# about a giving *pattern* therefore reaches donors it is false of: a donor with one
# gift ever, or one who lapsed years ago. This is the same unsupported-continuity
# class the validator blocks in AI-drafted paragraphs; the org's own template is
# held to it too.
CONTINUITY_CLAIMS = [
    r"year after year",
    r"your (?:annual|yearly|monthly|continued|ongoing|faithful|loyal|steady|regular|repeat)"
    r" (?:gift|gifts|support|generosity|giving)",
    r"(?:continued|ongoing|faithful|loyal|steady|regular) (?:support|giving) from"
    r" (?:you|friends like you|donors like you|supporters like you)",
    r"as you have (?:done )?(?:every|each) year",
    r"every year (?:you|since)",
]


def test_campaign_paragraphs_claim_no_giving_pattern():
    failures = []
    for key, text in COPY["campaign_paragraphs"].items():
        for pattern in CONTINUITY_CLAIMS:
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                failures.append(
                    f"campaign_paragraphs.{key} asserts a giving pattern "
                    f"({found.group(0)!r}) but is sent to one-time and lapsed "
                    f"donors too; describe the campaign, not the donor"
                )
    assert failures == [], failures


def test_only_openings_speak_to_donor_history():
    """The three openings are the history-gated slot, and must stay distinct."""
    openings = COPY["openings"]
    assert set(openings) == {"active", "single_gift", "lapsed"}
    assert len({v.strip() for v in openings.values()}) == 3
