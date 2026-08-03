# Business Rules: charity-donor-outreach v2.2.1

This is the authoritative specification for the donor-outreach pipeline. Every rule here is
implemented in `scripts/pipeline.py` and enforced by `scripts/letter_validator.py`, and each one
is covered by the project's test suite. If this document and the code ever disagree, the code
has a bug.

Rules marked **[DECISION]** resolve an ambiguity or defect in the original skill; the full list
of divergences is in §12 and should be reviewed and signed off by fundraising staff.

Contents: 1. Inputs · 2. Identity · 3. Field parsing · 4. Capacity tier & recency ·
5. Treatment matrix · 6. Ask calculation · 7. Salutations · 8. Letter composition ·
9. Review tiers, flags, exceptions · 10. Limits & exit codes · 11. Letter acceptance criteria ·
12. Divergences from the original skill

---

## 1. Inputs

### 1.1 Donor CSV
One row per donor. Recognized columns (header names case-insensitive, spaces/underscores
interchangeable):

| Column | Required | Notes |
|---|---|---|
| `donor_name` | yes | Non-empty. |
| `tier` | no | One of Platinum/Gold/Silver/Bronze/Lapsed/Unknown (case-insensitive). Treated as a *claim*, never trusted (§4.3). |
| `region` | no | Free text; used only if `regional_notes` in config has a matching entry. |
| `gifts` | no | History string, e.g. `2010: $25,000, 2013: $30,000`. |
| `largest_gift` | see note | Currency. If absent, derived as max of `gifts` amounts. |
| `lifetime_total` | see note | Currency. If absent, derived as sum of `gifts` amounts. |
| `last_gift_year` | see note | Integer year. If absent, derived as max of `gifts` years. |
| `volunteer` | no | Yes/No/true/false/1/0. Absent → No (a documented default, not a data guess). |
| `title`, `first_name`, `last_name` | no | Override name parsing (§3.1) when present. |

A row missing `largest_gift`, `lifetime_total`, or `last_gift_year` with no `gifts` string to
derive it from becomes an **exception** (§9.3). **[DECISION]** The original instructed "make
reasonable assumptions and proceed"; this pipeline never invents giving data.

### 1.2 Campaign config (`campaign_config.yaml`)
The **only** source of campaign facts. A fact absent from config cannot appear in any letter.

Required: `organization`, `campaign.type` (one of `emergency | annual | capital | event`),
`campaign_year` (integer, the anchor for all recency math; **never** the wall clock),
`signer.name`, `signer.title`, `donation_url` (https).

Optional (with defaults): `campaign.name`; `letter_date` (default: generation date; set it
explicitly for reproducible runs); `match_confirmed` (false; if true, `match_sponsor` is
required, config error otherwise); `event.registered_count` (omitted → the event paragraph
simply omits the statistic); `offers.naming_opportunity` (false); `offers.reengagement_gift`
(null, e.g. `"an ASPCA tote bag"`); `regional_notes` (map of region → P.S. text);
`compliance_footer` (null); `limits.max_rows` (500); `limits.max_personalized` (25).

**Premiums and quid pro quo:** a re-engagement gift is a quid pro quo element. Token items
are generally de minimis, but a non-token premium can trigger IRS written-disclosure duties
(the $75 quid-pro-quo rule) and affect deductibility, and that is the organization's counsel's
call, not this skill's. If the premium is more than a token item, route the offer to counsel
and carry any required disclosure in `compliance_footer`.

**[DECISION]** `campaign.type` is validated strictly; there is no "unknown campaign defaults to
Annual Fund". Staff state the campaign explicitly or the run refuses (exit 2).

### 1.3 Consent & suppression columns

Recognized column families (either polarity, any subset may be present):

| Family | Columns | Truthy value means |
|---|---|---|
| Suppression | `do_not_contact`, `opt_out`, `do_not_email`, `no_contact`, `deceased` | never contact |
| Consent | `consent_to_contact`, `mail_ok`, `opt_in`, `solicit` | contact is permitted |

Resolution is three-state: a truthy suppression value or falsy consent value → **suppressed**
(no letter, row in `suppressed.csv` with the source column named; `deceased` carries its own
reason). An **unrecognized** token in either family → the row is **held** as an exception; a
value somebody deliberately wrote is never guessed at. When several columns are present, the
strictest verdict wins (suppress > hold > contact), so an explicit `opt_out=Yes` beats an
explicit `consent_to_contact=Yes`. Suppression is evaluated **before** parsing: a suppressed
row is not processed further.

**[DECISION]** A **blank** cell is read according to what its column encodes, because the two
families are not symmetrical:

| Blank in | Means | Verdict |
|---|---|---|
| a suppression column (`do_not_contact`, `opt_out`, `deceased`, …) | nobody set the flag | **contact** |
| a consent column (`consent_to_contact`, `mail_ok`, …) | no permission is on record | **held** |

Fail-closed is kept where it carries meaning (absence of recorded permission is not
permission) and dropped where it does not. A suppression column exists to record an
exception; in the ordinary shape of a CRM export only the flagged donors carry a value, so
treating blank as unreadable there would hold every unflagged row and refuse the whole file
for doing nothing wrong. This is a change from the earlier rule (blank always held), made
after that shape was tested.

If the file has **no** consent/suppression column at all, every row is treated as
contactable and the run says so loudly (stdout warning + recorded in `_state.json`):
the missing column is surfaced as a fact for the user to confirm rather than
silently read as permission.

**[DECISION]** Consent handling is an *addition* to the original ruleset rather than a
correction of it, because the original had no notion of consent anywhere. Exports predating that
policy will have no such column, which is exactly the case the warning above exists for.

---

## 2. Identity

`donor_id = {row_number:03d}-{slug(donor_name)}` assigned at ingest. The slug lowercases,
collapses everything outside `[a-z0-9]` to `-`, trims to 40 chars, and falls back to `donor`
if empty. The row-number prefix makes IDs collision-proof, which any real donor file needs:
shared surnames, hyphenated variants of one family name, and two records for the same person
all collapse to the same slug, and name-keyed filenames would silently overwrite each other.

donor_id keys everything: letter filenames (`letters/{donor_id}.html`), `briefs.json`,
`paragraphs.json`, and `run_summary.csv` rows. Slugging is also the path-safety defense: raw
names never touch the filesystem.

---

## 3. Field parsing

### 3.1 Names
If `first_name`/`last_name` columns exist they win. Otherwise `donor_name` splits on
whitespace: first token → first name, remainder → last name (hyphenated surnames like
"Osei-Bonsu" stay intact). A single-token name is a mononym: informal salutations use it
directly; formal salutations use the full name.

### 3.2 Currency
Accepts `$1,200`, `1200`, `1200.00`. Parsed to `Decimal`. Negative amounts → exception, and
so are `NaN` and `Infinity`, which `Decimal` would otherwise accept.

### 3.3 Gifts history
`YYYY: $AMOUNT` pairs, comma- or semicolon-separated. Anything else in the cell (`$5k`, a
negative gift, free text) makes the row an exception rather than a partial read. Used to derive missing fields (§1.1) and to
cross-check provided ones: if provided `largest_gift` ≠ max(gifts), `lifetime_total` ≠
sum(gifts), or `last_gift_year` ≠ max year, the letter still generates from the **provided**
values (they are authoritative) but the row gets a `data_inconsistency` flag (§9.2).

### 3.4 Years
`last_gift_year > campaign_year` → exception (future-dated data). **[DECISION]**

---

## 4. Capacity tier & recency: two independent axes

**[DECISION]** The original conflated giving level and recency in one "tier" field, which is
how a $145,000 donor ends up with a $50 tote-bag letter. This pipeline derives two values.

### 4.1 Capacity tier (from `lifetime_total`)
| Tier | Interval |
|---|---|
| Platinum | ≥ $50,000 |
| Gold | $10,000 – $49,999.99 |
| Silver | $1,000 – $9,999.99 |
| Bronze | < $1,000 |

**[DECISION]** The original left exactly $50,000 in a gap (Gold ended at $49,999, Platinum
started "over $50,000"). $50,000 is Platinum.

### 4.2 Recency status
`lapsed` iff `campaign_year − last_gift_year > 3`, else `active`. Strictly greater: a donor
whose last gift was exactly 3 years ago (2021 at campaign_year 2024) is **not** lapsed.

### 4.3 The provided `tier` column is a claim, not a fact
Platinum/Gold/Silver/Bronze labels are capacity claims → compared against the derived tier;
mismatch → `tier_mismatch` flag, and the **derived** tier drives treatment. "Lapsed" labels are
recency claims, mapped to recency status (never a capacity mismatch). "Unknown"/blank → no
claim. **[DECISION]** Never silently correct, never silently trust: derive, use, and flag, so
the audit trail survives. (The original defaulted Unknown to Bronze, soliciting strangers with
a $150 ask; underivable rows are exceptions here.)

---

## 5. Treatment matrix

| Recency | Capacity | Treatment | Ask | Copy | Review |
|---|---|---|---|---|---|
| active | Platinum | `platinum` | formula 40% | formal, naming line if config-gated | A |
| active | Gold | `gold` | formula 25% | warm-professional, legacy line | A |
| active | Silver | `silver` | formula 15% | friendly, monthly-upgrade line | B |
| active | Bronze | `bronze` | flat $150 | casual, peer-page line | B |
| lapsed | ≥ $10,000 lifetime | `lapsed_major` | formula (capacity %) | warm re-engagement, no gift premium | **A, draft-and-hold**, `lapsed_major_hold` flag, gift-officer note |
| lapsed | < $10,000 lifetime | `lapsed` | flat $50 | warm re-engagement; gift premium only if config-gated | B |

**[DECISION]** Lapsed-major donors get a capacity-appropriate draft held for
mandatory human review; they never receive the $50/tote-bag treatment.

---

## 6. Ask calculation

All arithmetic in `Decimal`. Percentages: Platinum 40%, Gold 25%, Silver 15%.

**Formula-based treatments** (`platinum`, `gold`, `silver`, `lapsed_major`):
1. `base = largest_gift × tier_percentage` (unrounded)
2. Loyalty uplift ×1.10 iff `last_gift_year ∈ {campaign_year, campaign_year − 1}`
   (impossible for `lapsed_major` by definition)
3. Volunteer bonus +$100 iff volunteer
4. Emergency multiplier ×1.20 iff `campaign.type == emergency`
5. **Final** round to the nearest $50 using ROUND_HALF_UP; floor $50
6. Sanity: if ask > largest_gift → `ask_exceeds_largest_gift` flag (letter still generates;
   a human sees the flag)

**Flat treatments**: `bronze` = $150, `lapsed` = $50, **no modifiers**. **[DECISION]** The
original's step order would have turned the $50 re-engagement ask into $180 on an emergency
campaign for a lapsed volunteer; flat means flat.

**[DECISION]** Rounding happens once, at the end (the original rounded mid-stream, then
multiplied, producing $384-style asks), and uses ROUND_HALF_UP (float `round()` is banker's
rounding: it turns a $225 raw ask into $200 instead of $250).

Worked examples (annual campaign, campaign_year 2024, all covered in tests):
- Harold Mensah (Silver, largest $1,400, gave 2023, volunteer): 210 → ×1.10 = 231 → +100 = 331 → **$350**
- Susan Nakamura (Silver, largest $1,500, gave 2024, current-year gifts count as "recent", volunteer): 225 → 247.50 → 347.50 → **$350**
- Robert Svensson (lapsed_major, capacity Platinum, largest $50,000): 20,000 → **$20,000**, held for review
- Michael Torres (lapsed, lifetime $1,800): **$50** flat
- Earl Fontaine on an emergency campaign (Platinum, largest $90,000, gave 2022, volunteer): 36,000 → +100 → ×1.20 = 43,320 → **$43,300**

## 7. Salutations

| Treatment | Rule |
|---|---|
| platinum, gold, lapsed_major | `Dear {title} {last_name},` if title present; else `Dear {first_name} {last_name},`. **Never guess a title** and never downgrade a major donor to first-name casual. Mononym → `Dear {name},` |
| silver, bronze, lapsed | `Hi {first_name},` |

**[DECISION]** The original guessed honorifics from first names ("Elizabeth is probably Ms."),
which misgenders donors mid-solicitation. No guessing, ever.

**[DECISION]** The original's lapsed salutation, `We've missed you, {first}!`, is dropped:
whether anyone missed this donor is a claim about the organization's own conduct, and a
greeting is not entitled to it. Warmth belongs in the re-engagement opening, which states
only facts (time has passed; the giving history is real).

---

## 8. Letter composition

One HTML letter per donor, assembled from `assets/letter_template.html.j2` +
`assets/copy.yaml` blocks (approved strings, editable by staff without touching code), rendered
with Jinja2 (autoescape on, StrictUndefined). Blocks in order:

1. Date (`letter_date` or generation date)
2. Salutation (§7)
3. Opening gratitude, variant by history: multi-gift active / lapsed re-engagement /
   single-gift (no "lifetime support" celebration of a single $150 gift). With no gifts
   column, single-gift applies only when lifetime equals largest gift; otherwise the
   letter cites the lifetime total rather than a gift nobody itemized
4. Campaign paragraph, templated from `copy.yaml` per campaign type, OR the AI-drafted
   paragraph for Tier A when personalization is applied (§9.1)
5. Match line, **only** if `match_confirmed: true` (renders sponsor name)
6. Ask sentence with the computed amount, + tier line (naming/legacy/monthly/peer/re-engagement;
   naming and gift-premium lines are config-gated offers)
7. Optional regional P.S. (only if `regional_notes[region]` exists)
8. Donation CTA; the config URL is the only link in the letter
9. Signature, config signer only (**[DECISION]** the original invented "relationship manager"
   personas; letters are signed by real, configured staff)
10. Optional compliance footer (state-solicitation disclosure / unsubscribe line)

Currency renders as whole dollars with thousands separators: `$145,000`, `$1,250`. USD only
(documented limitation).

---

## 9. Review tiers, flags, exceptions

### 9.1 Review tiers
- **A** (`needs_review`, mandatory individual human review): treatments `platinum`, `gold`,
  `lapsed_major`. Eligible for AI personalization (capped by `limits.max_personalized`;
  `generation_mode` = `ai_personalized`, or `templated_fallback` if the drafted paragraph was
  missing/rejected; zero retries, the batch never blocks on a paragraph).
- **B** (`spot_check`): treatments `silver`, `bronze`, `lapsed`. Always fully templated.

### 9.2 Flags (letter exists; column `flags` in run_summary, `;`-separated)
`tier_mismatch(provided=…,derived=…)` · `lapsed_major_hold` · `ask_exceeds_largest_gift` ·
`data_inconsistency(…)` · `over_personalization_cap` (Tier A beyond the cap; stays templated)

### 9.3 Exceptions (no letter; row in `exceptions.csv` with row number, name, reason)
Missing/underivable required fields · unparseable or negative amounts · future-dated
`last_gift_year` · empty name · post-render validation failure, recorded with the reason
prefix `internal validation failure:`, by definition a pipeline defect, not a data
problem; the `audit` command fails any run containing one.

**Contract:** every input row produces exactly one of {letter, exception row, suppressed
row}. Counts must reconcile: letters + exceptions + suppressed = input rows (the `audit`
command enforces this).

---

## 10. Limits & exit codes

`limits.max_rows` (default 500): exceeded → refuse cleanly with "split the file" guidance,
exit 4, nothing generated. `limits.max_personalized` (default 25): personalization slots go to the
highest lifetime totals first (row order as tiebreak); Tier-A donors beyond the cap stay
templated with an `over_personalization_cap` flag, and briefs.json contains at most the cap.

Exit codes: **0** completed (possibly with exceptions recorded) · **2** invalid config ·
**3** input file (CSV, paragraphs.json, run state) unreadable or structurally invalid ·
**4** max_rows exceeded · **1** unexpected crash.

Each run writes to a fresh `output/run-<timestamp>/` directory; re-runs never overwrite an
in-progress human review. Letter *content* is deterministic given the same CSV + config
(+ fixed `letter_date`).

---

## 11. Letter acceptance criteria (enforced by `letter_validator.py` on every letter)

1. No unresolved `[PLACEHOLDER]` / `{{ … }}` tokens
2. The computed ask appears, correctly formatted, and every `$` amount in the letter belongs to
   the whitelist for that donor (ask, lifetime total, gift amounts from their own record)
3. Salutation exactly matches §7 for the record (independent recomputation)
4. Banned-content scan: matching/"doubled"/"2x" language (unless `match_confirmed`),
   guarantees, tax advice. This is a **backstop**; real containment is layered
   (config-gated copy → instructed briefs → this scan → mandatory Tier-A human review)
5. Exactly one hyperlink, equal to the config donation URL
6. Config signer present
7. Word count within 80–320, compliance footer excluded

Checks 1, 2 and 4 skip text the organization approved in config (compliance footer,
regional notes) and the donor's own name in the salutation: a disclosure must be able to
name a premium's value or deductibility, and those are not the letter's claims.
8. AI paragraphs additionally: ≤2 sentences, ≤350 chars, no URLs, no new dollar amounts,
   no honorifics

---

## 12. Divergences from the original skill (for staff sign-off)

| # | Original | v2 behavior |
|---|---|---|
| 1 | Claim gifts "will be matched (even if no match is confirmed)" | Match language only when `match_confirmed: true` + sponsor named; validator blocks otherwise |
| 2 | Guess honorifics from first names | Never; formal full-name fallback |
| 3 | "Assign a personal relationship manager name" | Signer from config only |
| 4 | "Missing fields: make reasonable assumptions and proceed" | Exceptions report; never invent |
| 5 | Tier data trusted / donor table baked into the prompt | Runtime CSV sole source; derived tier + mismatch flags |
| 6 | $50,000 in a tier gap | Platinum ≥ $50,000 |
| 7 | Lapsed overrides capacity → $50 tote-bag letters to major donors | Two axes; lapsed-major = draft-and-hold |
| 8 | Unknown tier → Bronze treatment | Derive from data; underivable → exception |
| 9 | Round mid-formula; float rounding | Single final ROUND_HALF_UP to $50; Decimal throughout |
| 10 | "Gave last year" unanchored | `∈ {campaign_year, campaign_year−1}`, config-anchored |
| 11 | Modifiers ambiguous for flat asks | Flat asks exempt |
| 12 | Tote bag / naming opportunity / event stats promised unconditionally | Config-gated offers; absent → not mentioned |
| 13 | Unknown campaign → Annual messaging | Strict campaign validation; no silent default |
| 14 | HTML returned in-chat, one donor at a time | Files on disk + run_summary + exceptions; volume caps |
| 15 | No compliance elements | Optional config-gated disclosure/unsubscribe footer |
| 16 | No notion of donor consent anywhere | Consent/suppression column families honored fail-closed (§1.3); files without the column trigger a loud warning; an addition the original lacked entirely |
| 17 | Lapsed greeting "We've missed you, [First]!" | Dropped (§7): an organizational-conduct claim a greeting is not entitled to; lapsed donors get the standard informal greeting and a gentler campaign paragraph on emergency appeals |
