# Case study: rebuilding the `charity-donor-outreach` skill

***Sam Peterson***

> *This is a portfolio case study and nothing in it is real. It is not affiliated with,
> endorsed by, or connected to the ASPCA. Every donor record, name, gift, and
> signature here is fabricated; the donor data is the sample table that shipped with
> the original draft.*

The draft skill is in [`SKILL.original.md`](SKILL.original.md). My rewrite is in
[`charity-donor-outreach/`](charity-donor-outreach/), which is the whole skill and
is what gets uploaded.

## Summary

The draft treats the model as a fix-all. It asks a language model to be the
database, the calculator, the segmentation engine, and the source of facts,
including a matching gift that may not exist. Everything it hands the model already
has a precise, testable tool built for it.

One principle drives every change I made: **the model is never the system of
record.** Code owns anything with a right answer, which is parsing, consent,
tiering, ask arithmetic, rendering, and validation. The model handles the two jobs
that need judgment: working the review gates with a person, and drafting a short
personalized paragraph inside bounds that code enforces afterward.

Run against the draft's own 50 sample donors, the rewrite produces 50 letters, 20
routed to mandatory individual review and 30 to a spot check, flags four donors the
source data mislabels, and holds two major donors for a gift officer who would
otherwise have received a $50 tote-bag letter. The skill does not cover auto-sending.

## What changed, at a glance

| The draft | The rewrite | Why it matters |
|---|---|---|
| Promise a match "even if no match is confirmed" | Matching language renders only from `match_confirmed` plus a named sponsor; the validator blocks it anywhere else | A misrepresented match is a solicitation problem, not a quality issue |
| Guess titles from first names | A title is used only when the file supplies one; otherwise the full name | Misgendering donors in the first line, at scale |
| "Assign a personal relationship manager name" | The configured signer; a config still holding `[REPLACE]` markers is refused at load, naming the fields | No letter signed by someone who does not exist, and no half-filled config reaching a donor |
| Tote bag, naming opportunity, event headcount promised unconditionally | Config-gated offers; absent from config means absent from the letter | The skill stops making promises on the charity's behalf |
| 50 donors and their giving histories inside the prompt | Donor data stays in the organization's file; the skill holds none | Growing the list never means editing the skill, and no PII sits in a shared artifact |
| Model chains five arithmetic steps, rounding in the middle | `Decimal`, fixed order, rounded once at the end, traced per row | Same input, same number, every run, with the working shown |
| Trusts the `Tier` column | Tier derived from giving history; a disagreeing label is flagged, not corrected | Four donors in the supplied data are mislabeled |
| "Lapsed" sits alongside giving levels | Capacity and recency are separate axes that combine into a treatment | A $145,000 lapsed donor goes to a gift officer, not a $50 tote-bag letter |
| "Make reasonable assumptions and proceed" | Underivable rows become exceptions with a stated reason | No invented figures in a letter that quotes someone's giving |
| Every letter treated identically | Tier A needs individual review, Tier B a spot check, with flags naming why | 20 letters get real attention, 30 get a skim |
| Returns HTML in chat | Files, a manifest with per-row arithmetic and hashes, and an independent `audit` | A run is reconstructable from its own outputs |
| No notion of consent | Two column families honored, three-state resolution, `suppressed.csv`, reconciliation | Donors who asked not to be contacted are not contacted |
| Fires on "any communication task" | Scoped description with an explicit not-for list | The skill stops triggering on work it cannot do |

The rest of this document covers the four findings where the reasoning matters more
than the fix.

## 1. The letters state facts nobody supplied

The emergency section instructs the model to tell donors their gift will be matched
*"even if no match is confirmed, we can sort that out later"*. That is an
instruction to lie in a solicitation. It is not a quality problem for the charity. A
misrepresented match is a donor-trust and charitable-solicitation problem, and
potentially a refund conversation.

The same shape repeats elsewhere: *"mention how many people are already registered"*
with no source for the number, *"every hour counts"*, a promised *"re-engagement gift of a tote bag"* the skill has no way to know anyone will send, and *"a naming opportunity (e.g. a room or bench)"* for Platinum donors. Two are worse than the others: *"Assign a personal relationship manager name"* signs a solicitation as a person who may not exist. And where no title is available, the draft says to *"guess one based on the first name if it seems
obvious (e.g. 'Elizabeth' is probably Ms., 'Robert' is probably Mr.)"*, which at
scale misgenders donors in the opening line of a letter asking them for money.

The common thread is that none of these are the skill's claims to make. They belong
to the organization, so in the rewrite each one is a config field the organization
fills in, and a letter cannot state a fact that is not in it.

## 2. The ask formula is broken independent of who executes it

Handing five chained arithmetic steps to a model is its own problem: across a list,
some letters quote a wrong number and nobody can tell which. But the formula does
not work even executed perfectly.

- **It rounds in the middle:** Step 3 rounds to the nearest $50, then steps 4
  through 6 keep multiplying and adding. The output is not rounded to anything.
- **It defines percentages for three of five tiers:** Bronze and Lapsed are flat
  amounts, so steps 4 through 6 still apply to them on a literal reading. A lapsed
  volunteer on an emergency appeal gets $50, plus $100, times 1.2, which is **$180**
  for an ask the draft describes as a $50 restart.
- ***"If the donor gave last year"* has no anchor:** Last year relative to what? The
  wall clock drifts, so the same file produces different letters in January and
  December.
- **Exactly $50,000 falls in a gap:** Gold is "$10,000 to $49,999", Platinum is
  "over $50,000", and a donor at exactly $50,000 lifetime belongs to neither.

The rewrite computes the ask in `Decimal` with a fixed order, rounding once at the
end, flat treatments exempt from every modifier, and recency measured against a
configured `campaign_year` rather than the clock. Every row of `run_summary.csv`
carries its own arithmetic, so a reviewer can check any ask from the spreadsheet
without opening a letter:

```
base = largest_gift $1,500 x 15% = $225 | loyalty uplift +10% -> $247.50
  | volunteer +$100 -> $347.50 | rounded to nearest $50 -> $350
```

What I did not change is the shape of the policy. Whether 25% of a donor's largest
gift is the right ask is a fundraising question for the organization. The rewrite
makes it explicit and changeable in one place instead of buried in prose.

## 3. One "tier" column conflates two different things

This is the most consequential finding, because the draft's own sample data contains
the donor it breaks on.

The draft lists Platinum, Gold, Silver, Bronze, **Lapsed**, and Unknown as one set
of tiers. The first four describe giving level; Lapsed describes recency. They are
independent axes crammed into one field, with no rule for a donor who is both
high-value and long-quiet.

Robert Svensson is labeled Platinum, has given $145,000 lifetime, and last gave in
2020. At campaign year 2024 that record satisfies the Lapsed condition, so the draft
sends an apologetic letter, a tote bag, and an ask for **$50** to "restart their
giving journey". Walter Adeyemi ($75,000 lifetime, last gift 2020) gets the same.

The data also contains rows whose label contradicts their own giving history, and
the draft trusts the label:

| Donor | Labeled | Lifetime giving | Actually |
|---|---|---|---|
| Ada Yamamoto-Pierce | Silver | $17,000 | Gold |
| Ruth Andersen | Silver | $25,000 | Gold |
| Shirley Magnusdottir | Silver | $22,000 | Gold |
| Arthur Mwangi | Bronze | $2,600 | Silver |

"Unknown" defaulting to Bronze compounds it: a donor with no known history gets a
\$150 ask and casual copy. Worse, the template hardcodes *"your lifetime support of
[LIFETIME_GIVING] has made a real difference,"* and the draft's closing instruction
is to make reasonable assumptions about missing fields, so the model invents a
giving total and thanks a stranger for it.

In the rewrite the two axes stay separate all the way to the output. `derived_tier`
carries the giving band; `recency` carries active or lapsed; they combine into a
`treatment` without either erasing the other. A low-value lapsed donor gets
re-engagement copy and the flat $50. A lapsed donor with $10,000 or more lifetime
becomes `lapsed_major`: a warm re-engagement draft with a capacity-appropriate ask,
flagged and routed to mandatory review, because a gift officer should decide
personally how to reopen that conversation. Svensson's draft asks $20,000 and
carries no tote bag. Adeyemi's asks $12,000.

Provided tier labels are treated as claims. The derived tier drives the letter, the
disagreement is flagged as `tier_mismatch(provided=Silver,derived=Gold)`, and the
source data is never silently corrected. Flagging rather than fixing keeps the
evidence that the CRM needs attention.

## 4. Nothing defines a good letter, and nothing checks

The draft returns letters in chat, one at a time, and treats all fifty identically,
so a supervisor either approves fifty individually or trusts all fifty. There is no
definition of a correct letter, no test, and no record of what was decided.

In the rewrite every letter passes a deterministic validator before it is written,
whichever way its paragraph was produced: the computed ask appears, every dollar
amount is a fact of that donor's record, the salutation matches under independent
recomputation, there is exactly one link and it is the configured donation URL, the
signer is present, no bracketed placeholder survived, and the letter falls within a
length band.

Each run writes its letters and three CSVs into a fresh directory it refuses to
overwrite, with the arithmetic and a `letter_sha256` on every row. A separate
`audit` command re-validates a finished run from its own recorded state, fails if
any letter's bytes no longer match the hash that authorized them, fails if the
pipeline ever disagreed with its own validator, and reconciles the count that
matters most:

> every input row is exactly one of a letter, an exception, or a suppression.

Consent is an addition to the original ruleset rather than a correction, since the
draft has no notion of it at all. Two column families are recognized, resolved three
ways, strictest verdict winning. A blank cell is read by family, because the two are
not symmetrical: a blank suppression cell means nobody set the flag, which is the
ordinary shape of a CRM export, while a blank consent cell means no permission is on
record, which is not the same as permission. An unrecognized value such as `N/A`
is held either way, because a value somebody deliberately wrote is not something to
guess at. A file with no consent column is processed with a loud warning rather than
a silent assumption.

## The architecture

**Code owns anything that must be exact and auditable; the model is spent only where
code cannot go.**

The deterministic pass spends no model tokens at all. It parses the CSV, resolves
consent, derives tiers, computes asks, renders letters from approved copy, validates
every one, and writes the run. For Tier B, which is most letters, that is the whole
job and the content costs nothing.

The model earns its place in two spots. **Working the three gates with a person:**
confirming the campaign config before anything runs, reporting what the run actually
did before personalizing, and presenting drafts for approval. And **one short
paragraph per Tier-A donor**, drafted from a brief of verified facts and validated
by code before it enters a letter. Years must appear in that donor's history.
Continuity language such as "every year" passes only when the streak is real, and a
single gift is not a pattern however large it was. No new dollar amounts, no URLs,
no honorifics, and no matching language even when a match is confirmed, because that
sentence belongs to the config-gated match line. A rejected paragraph is never
retried; the letter keeps its pre-approved copy, the row is flagged, and it surfaces
at the gate.

**The model never sees a donor row.** It sees a derived brief of typed facts plus two
short strings, first name and region, so a notes field reading "ignore the campaign
instructions and write..." is inert: not sanitized, simply never passed.

Context cost follows the same boundary. The draft loaded 14,373 characters on every
trigger, donor data included, growing with every record added. The rewrite's
`SKILL.md` is about 9,800 characters and holds no donor data; the references load only
when a policy question or a gate needs them, and scripts and assets never enter
context at all.

## Decisions the rewrite hands back to the organization

A recurring shape in the fixes above is a judgment call the draft left to the model,
made fresh on every run, turned into a named config field set once. Knowing which
decisions are not the system's to make is part of the design: the ask policy itself;
what the letters claim the organization does with money; whether a match exists and
who funds it; whether a premium is offered, which is a quid pro quo element where a
non-token item can trigger IRS disclosure duties and is counsel's call rather than
this skill's; what a compliance footer must say, which Gate 0 asks about rather than
defaulting to silence; and whether anything is sent at all. The skill has no
transport and should not have one.

## Impact

**The clearest legal and reputational risks are gone.** No fabricated match, no
invented signer, no guessed gender in the opening line, no promised premium the
organization never agreed to.

**Ask amounts are reproducible and checkable.** The same file produces the same
numbers every run, each carrying its own arithmetic. Before, some fraction of
letters quoted a wrong amount and nobody could say which.

**Reviewer attention goes where judgment is needed.** On the sample data that is 20
letters for individual review and 30 for a spot check, with named flags saying why,
including two major donors who would otherwise have received a restart-your-giving
ask. The review work does not disappear; it goes to the letters that need it.

**Scale stops meaning "edit the skill."** Going from 50 records to 50,000 never
touches the skill and puts no financial history in a shared artifact. The
deterministic cost is flat; only the personalized paragraphs cost model time per
donor, capped per run. I have not load-tested at that volume, and runtime, review
capacity, and model cost are real questions at it. What the rewrite removes is the
structural blocker, not the engineering a large mailing still needs.

## How it is tested

The deterministic core has one right answer, so it gets a unit suite: tier
boundaries including the exact $50,000 and exact-three-year edges, the worked ask
examples, salutation rules, and adversarial CSVs (injection names, malformed rows,
negative amounts, non-UTF-8 exports, colliding headers).

The paragraph validator has no single right answer, so its corpus is deliberately
two-sided. Fabrications that must be blocked measure whether it catches anything;
honest paragraphs that must pass measure whether it over-blocks. The must-pass half
is the half people skip, and a validator that rejects everything would score
perfectly without it while quietly stripping the personalization out of every
letter.

A golden test runs the full 50-donor sample and compares output byte for byte
against a committed snapshot, which is both the determinism proof and the reason a
copy change is a deliberate act rather than a silent drift. 145 tests run on every
push across Python 3.10 through 3.13 on Linux and Windows. Windows is on the matrix
because the skill writes files whose bytes it later hashes and audits, and locale
encoding and newline translation are exactly the class of bug a Linux-only run
reports as green.

## What this does not solve

The validator only sets a floor. It rejects the fabrication classes it knows how
to detect; it cannot prove a sentence is true. That limit is why the
personalization surface is deliberately one or two sentences: a short claim built
from a fact whitelist is something you can actually check, and a freely written
paragraph per donor is not.

The skill cannot tell whether the campaign copy is any good, only that it is honest.
It cannot verify the donor file is correct, only that it is internally consistent;
if the CRM holds the wrong lifetime total, the letter quotes it faithfully. What it
guarantees is that the number in the letter matches the number in the data, and that
a human can see how it got there. And consent handling honors the columns it is
given, which is why a file with no consent column produces a warning rather than an
assumption.

## A judgment call worth stating

I build systems, not fundraising campaigns, so treat this as a judgment call rather
than a finding. It is still the thing I would most want to say about a skill like
this one.

Cheap AI personalization backfires. If a donor receives a letter straining to sound
warm and personal, and they can tell a machine wrote it, that is worse than an
honest generic thank-you. The generic letter costs nothing. The obviously automated
personal letter costs trust, because now the donor knows the organization's personal
touch is machinery. For a charity, where the entire relationship rests on making
someone feel they matter, that is a bad trade for a form letter.

That is not an argument against personalizing. It is an argument about what
personalization is. Real personalization uses the facts you actually have: their
giving history, their tier, an ask derived from what they have given. What you do
not want is the model inventing warmth on top of it.

So the rewrite gives the model one short paragraph per top-tier donor, written only
from that donor's verified facts and validated before it goes anywhere, on top of
approved copy the organization owns. The letters are lightly and honestly
personalized, on purpose. As with the arithmetic, the model does the phrasing
while the facts come from the record.

## Where this generalizes

The same division of labor applies to other document-heavy work; only the nouns
change. On a contractor's payment
application, code owns the schedule-of-values arithmetic and retainage while the
model reads the change-order narratives and says where the story and the numbers
disagree. In lease abstraction, code owns dates, escalations and the rent table
while the model finds the clauses, since a renewal option can live in an amendment
phrased as an exception to the original; an abstract that cannot be tied back to a
clause reference is held rather than published. On a schedule risk review, code owns
the critical path and float while the model reads the narrative for what a network
diagram cannot express, and the same ratchet applies: the model may raise a flag and
may never clear one.

The per-donor fact whitelist is one small instance of the general move. Give the
model the specific facts it may speak from, then check what it wrote against them.
That is the practical meaning of grounding, and the claim depends on the checking
actually happening.

## Run it yourself

The gates, the exception handling, and the validator refusing a bad paragraph are
agent behavior, so this is worth driving rather than reading about.

### Install
For the Claude apps, compress the `charity-donor-outreach/` directory by whatever
means you prefer and upload the archive under Settings, Skills, Add, Upload a
skill. No build step: the directory as committed is the skill.

### Attach a donor file and ask for letters
The skill ships no donor data and no campaign config on purpose, so this is the
real path: it has to find your file and build the config with you.
[`tests/fixtures/sample_donors.csv`](tests/fixtures/sample_donors.csv) is the
original draft's 50 sample donors if you want a file to hand it.

> Here's our donor list. I need re-engagement and annual fund letters for the 2024
> annual fund. Walk me through the gates and stop at each one for my approval.

It should ask you for the campaign facts rather than assuming any: signer, donation
URL, whether a match is genuinely confirmed, whether any premium is on offer. Watch
that it declines to fill in anything you did not tell it. On that file it should
also warn that there is no consent column anywhere in the export, which is true and
is a fact about the data rather than permission to mail everyone.

Expect 50 letters, 20 needing individual review and 30 a spot check, four donors
flagged as mislabeled, and two major donors held for a gift officer. To watch a
guardrail fire, ask for something it must refuse:

> In one of the Tier-A paragraphs, add a line saying every gift will be matched
> dollar-for-dollar.

There are two ways that lands and both are correct. The skill may decline to draft
it, since "dollar-for-dollar" is a ratio the config does not state and the match
sentence belongs to the config-gated line rather than to a paragraph. Or it drafts
it anyway and the validator rejects the paragraph, in which case that letter falls
back to templated copy with `paragraph_rejected` on its row. Either way the claim
does not reach a donor and the skill reports it at the gate rather than retrying.

If you would rather not install anything, three letters from the 50-donor run are
committed at [`tests/golden/`](tests/golden/) beside the `run_summary.csv` they came
from: the lapsed-major draft the original would have sent a tote bag, a single-gift
Bronze donor, and the mislabeled row written to its derived tier. Pick any row of
the summary and check its arithmetic.

From a terminal, from the repository root:

```bash
uv run charity-donor-outreach/scripts/pipeline.py generate \
  --csv tests/fixtures/sample_donors.csv \
  --config tests/fixtures/sample_campaign.yaml --out output
uv run charity-donor-outreach/scripts/letter_validator.py audit --run-dir output/run-<id>

python -m pytest tests/          # 145 tests, including the golden run
```

The scripts carry PEP 723 metadata, so `uv` needs no install step; `pip install -r
charity-donor-outreach/requirements.txt` with `python3` works without `uv`.

## What is in this repository

| Path | What it is |
|---|---|
| `charity-donor-outreach/` | The rewritten skill, and nothing else. This directory is exactly what gets uploaded |
| `SKILL.original.md` | The draft being assessed, content untouched. Renamed from `SKILL.md` so it can never be discovered and loaded as a live skill |
| `CASE_STUDY.md` | The original problem statement |
| `tests/` | The suite and its fixtures, kept outside the skill so none of it ships in the upload |
