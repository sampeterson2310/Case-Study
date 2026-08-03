# Review Workflow: gates, schemas, and drafting rules

Read this before running the gates or drafting paragraphs. The pipeline enforces
everything enforceable; this file covers the parts where you interact with the
user and produce text.

## The three gates

**Gate 0, config confirmation (before anything runs).** Check for a
`campaign_config.yaml` the user already has before building one from scratch; a
config they approved for an earlier campaign is the normal starting point, and
the fields most likely to be stale are `campaign_year`, `letter_date`,
`campaign.type` and anything under `offers`. If there is no config, copy
`assets/campaign_config.example.yaml` and fill it in with them. A config still
holding `[REPLACE]` markers is refused at load with exit 2 naming the fields, so
an unfinished one cannot reach a donor; fill those fields and re-run.

Either way, show the user the filled `campaign_config.yaml` and confirm: campaign type + year, signer, donation URL,
match status (only mark `match_confirmed: true` if the user affirms a real,
named sponsor commitment, which gates all matching language), offers, limits.
Ask explicitly whether a compliance footer (state-solicitation disclosure /
unsubscribe or premium-disclosure text) is required; leaving it null should be
the user's decision, not a default they never saw. An unconfirmed config does
not get run.

**Gate 1, after `generate`, before personalization.** Report, concretely:

- letters generated, Tier A vs Tier B counts (from the script's stdout)
- the consent picture: every `suppressed.csv` row (donor, reason; these donors asked not
  to be contacted, or are deceased; no letter exists and none should be created), and if
  the script warned that the file has **no** consent/suppression column, relay that warning
  verbatim and ask the user to confirm the source system really has no such data
- every `exceptions.csv` row: row number, donor, reason. These donors got **no
  letter**. Two categories: for data problems the fix is better data, never
  invented values; a reason starting with `internal validation failure` means the
  pipeline disagreed with its own validator, and that is a **skill defect** to
  report to the user/maintainer, never something to work around by editing data
- every flag and what it means:
  - `tier_mismatch(provided=X,derived=Y)`: the CSV's tier label disagrees with
    the giving data; the letter uses the derived tier; the source system needs a
    look
  - `lapsed_major_hold`: a high-capacity donor gone quiet; the draft is a warm
    re-engagement letter with a capacity-appropriate ask, held for a gift
    officer's personal judgment
  - `data_inconsistency(field)`: the gifts history doesn't reconcile with the
    stated totals. The provided value was used, and for `lifetime_total` that
    value is printed in the letter's opening, so the disputed figure reaches the
    donor even though the ask (computed from `largest_gift`) is unaffected. Say
    that plainly rather than reporting the ask as undistorted and stopping there
  - `over_personalization_cap`: Tier A beyond `limits.max_personalized`; stays
    templated
- Then stop and ask: fix data and re-run, or continue?

**Gate 2, after personalization (or after Gate 1 if the user skipped it).**
Present:

1. Totals from `run_summary.csv` (letters, modes, fallbacks)
2. **Every Tier-A letter accounted for**, all `needs_review`: present a
   review table (donor, treatment, ask, mode, flags, file path) and show full
   letter text for any the user asks about; the human must be able to reach
   every high-value letter, without you pasting all of them unprompted
3. 2–3 randomly chosen Tier-B letters as a spot-check, shown in full
4. Any `templated_fallback` rows and why (paragraph missing or rejected; quote
   the flag)

Re-read `run_summary.csv` before presenting this gate; do not carry figures over
from Gate 1. `apply-personalization` rewrites `generation_mode` and recomputes
`letter_sha256` for every personalized letter, so a hash quoted at Gate 1 is
stale by Gate 2 and quoting it describes a file that no longer exists.

Two rows flagged `duplicate_donor` are also two separate briefs, so the model
drafts a paragraph for each and their letters can legitimately differ. The flag
is what says the rows are the same person; matching hashes are not evidence of
it, and differing hashes are not evidence against it.

Approval means: the drafts on disk are accepted. Sending them is the user's
process, outside this skill.

## briefs.json (produced by `generate`)

```json
{
  "instructions": "…drafting rules embedded here…",
  "campaign": {"organization": "ASPCA", "type": "annual", "name": "2024 Annual Fund", "match_confirmed": false},
  "briefs": {
    "001-robert-svensson": {
      "first_name": "Robert",
      "treatment": "lapsed_major",
      "capacity_tier": "Platinum",
      "region": "Northeast",
      "gift_count": 4,
      "years_supported": "2010-2020",
      "gift_years": [2010, 2013, 2016, 2020],
      "gave_every_year": false,
      "campaign_year": 2024,
      "last_gift_year": 2020,
      "lifetime_total": "$145,000",
      "volunteer": false,
      "ask_amount": "$20,000",
      "tone": "warm, respectful re-engagement; acknowledge the time that has passed without guilt or pressure",
      "note": "lapsed major donor: no gift since 2020; this draft is held for gift-officer review"
    }
  }
}
```

## paragraphs.json (you write this, in one pass)

A single JSON object, donor_id → paragraph string, saved in the run directory:

```json
{
  "001-robert-svensson": "Your generosity from 2010 through 2020 helped animals across the Northeast find safety when they needed it most. We would be honored to welcome you back into that work.",
  "002-earl-fontaine": "…"
}
```

## Drafting rules (enforced by the validator; violations fall back to template)

1. 1–2 sentences, at most 350 characters, in the brief's stated `tone`.
2. Use only facts present in that donor's brief, plus the shared `campaign`
   block of briefs.json (organization, campaign type and name). Together they
   are the whitelist. The letter already states the ask and lifetime total; you
   may echo those two amounts, but introduce **no other numbers**.
3. Never: URLs, honorifics (Mr./Ms./…), bracketed placeholders, program names or
   statistics not in the brief, matching/doubling language (even if a match is
   confirmed, because the match line is a separate templated element), guarantees,
   promises, or tax language.
4. Volunteers (`"volunteer": true`) may be thanked for their time as well as
   their giving; that fact is in the brief precisely so you can use it. Never
   mention volunteering when the flag is false.
5. **Name a year only if it is in that donor's `gift_years`** (or is the
   `campaign_year`). "Since 2015" for a donor whose first gift was 2019 is the
   fabrication reviewers probe first, and it is checked mechanically.
6. **Use continuity language only when `gave_every_year` is true.** "Every year",
   "year after year", "never missed", "without fail" all require an unbroken
   streak across two or more years. A donor with a single gift on record has no
   pattern to describe, however large that gift was.
7. Draft all donors in one pass from one read of `briefs.json`. Do not fetch
   more context per donor; the brief is the whitelist.

Rules 3–6 are enforced by `letter_validator.validate_paragraph`, and the same
list is embedded in `briefs.json` as `instructions` so it travels with the data.
A paragraph that breaks one is rejected and that donor falls back to templated
copy, and per the fallback semantics below, it is not retried. Knowing the rules
up front is how you keep the personalization.

**Fallback semantics (why you never retry):** a rejected or missing paragraph
does not block anything: the letter keeps its pre-approved templated paragraph,
`generation_mode` becomes `templated_fallback`, and a flag records the reason.
Retrying burns tokens to fight a validator that is enforcing policy; report the
fallback at Gate 2 and let the human decide if a redraft is wanted.

## Handling change requests at Gate 2

| The user wants… | The right lever |
|---|---|
| Different recurring wording (openings, campaign copy, tier lines) | Edit `assets/copy.yaml`, re-run `generate` |
| A different personalized paragraph for one donor | Redraft that entry in `paragraphs.json`, re-run `apply-personalization` |
| Different facts (signer, URL, match, offers, year) | Edit the config, re-run `generate` |
| A hand-edit inside one letter file | Discourage; it bypasses validation; route through one of the levers above |
| One of a duplicate pair removed | Never by deleting the letter file: the row stays in `run_summary.csv` and `audit` fails the run on the missing file. Dedupe the source CSV and re-run |

## Re-run semantics

`generate` always writes a fresh `output/run-<id>/` and refuses to overwrite an
existing one (protects in-progress reviews). `apply-personalization` operates
within one run directory and is idempotent: re-applying the same paragraphs is a
no-op, a new paragraphs file replaces cleanly, and a rejected replacement
restores the templated letter.
