---
name: charity-donor-outreach
description: >-
  Use this skill whenever a user mentions donors, fundraising, money, emails,
  letters, charity, nonprofits, campaigns, giving, volunteers, events, reports,
  grants, sponsorships, or any kind of outreach or communication task.
---

# Charity Donor Outreach Letter Generator

Use this skill when someone uploads a CSV or donor list and wants to generate
personalised outreach letters for a fundraising campaign.

## What to do

1. Read the uploaded file and extract donor name, giving history, tier, and region.
2. Look up their tier in the tier info below and select the right tone and ask amount.
3. Calculate the recommended ask amount using the formula below.
4. Use the giving history for each donor from the tables below to personalise the letter.
5. Fill in the letter template and return it in-chat as HTML.

## Donor Tiers

**Platinum** (lifetime giving over $50,000): Very formal tone. Assign a
personal relationship manager name. Always ask for 40% of their largest
single gift. Mention a naming opportunity (e.g. a room or bench).

**Gold** (lifetime giving $10,000–$49,999): Warm but professional. Ask for
25% of their largest single gift. Mention legacy giving options.

**Silver** (lifetime giving $1,000–$9,999): Friendly tone. Ask for 15% of
their largest single gift. Mention monthly giving upgrade.

**Bronze** (lifetime giving under $1,000): Casual and encouraging. Ask for
a flat $150 regardless of history. Mention peer fundraising pages.

**Lapsed** (no gift in over 3 years): Apologetic tone. Offer a re-engagement
gift of a tote bag. Ask for $50 to "restart their giving journey."

**Unknown**: Default to Bronze treatment.

## Campaign Types and Messaging Angles

**Emergency Appeal**: Use urgency language. Mention that every hour counts.
Tell the donor their gift will be matched (even if no match is confirmed —
we can sort that out later).

**Annual Fund**: Focus on consistency and community. Mention the donor's
giving streak if they have one.

**Capital Campaign**: Focus on legacy and permanence. Use building/construction
metaphors.

**Event Fundraiser**: Focus on fun and social proof. Mention how many people
are already registered.

**Unknown campaign**: Default to Annual Fund messaging.

## Ask Amount Calculation

Calculate the recommended ask using the following steps:

1. Take the donor's largest single gift from their record.
2. Multiply by the tier percentage (Platinum 40%, Gold 25%, Silver 15%).
3. Round to the nearest $50.
4. If the donor gave last year, add a 10% "loyalty uplift".
5. If the donor is also a volunteer, add another $100 flat.
6. If the campaign is an Emergency Appeal, multiply the final number by 1.2.
7. Output that number as the ask amount.

## Salutation Rules

- Platinum and Gold: use "Dear [Title] [Last Name],"
- Silver and Bronze: use "Hi [First Name],"
- Lapsed: use "We've missed you, [First Name]!"
- If no title is available, guess one based on the first name if it seems
  obvious (e.g. "Elizabeth" is probably Ms., "Robert" is probably Mr.).

## Donor Giving Histories

Use the table below to look up each donor's full giving history by name.
Reference this data when personalising the campaign paragraph and calculating
the ask amount.

| Donor Name            | Tier      | Region        | Gifts (Year: Amount)                                                        | Largest Gift | Lifetime Total | Last Gift Year | Volunteer |
|-----------------------|-----------|---------------|-----------------------------------------------------------------------------|--------------|----------------|----------------|-----------|
| Robert Svensson       | Platinum  | Northeast     | 2010: $25,000, 2013: $30,000, 2016: $40,000, 2020: $50,000                 | $50,000      | $145,000       | 2020           | No        |
| Earl Fontaine         | Platinum  | Southeast     | 2012: $50,000, 2015: $60,000, 2018: $75,000, 2022: $90,000                 | $90,000      | $275,000       | 2022           | Yes       |
| Walter Adeyemi        | Platinum  | Midwest       | 2014: $20,000, 2017: $25,000, 2020: $30,000                                | $30,000      | $75,000        | 2020           | No        |
| Ralph Osei-Bonsu      | Platinum  | West          | 2013: $40,000, 2016: $50,000, 2019: $65,000, 2023: $80,000                 | $80,000      | $235,000       | 2023           | Yes       |
| Victor Ambrosius      | Platinum  | International | 2011: $30,000, 2014: $35,000, 2017: $45,000, 2021: $55,000                 | $55,000      | $165,000       | 2021           | No        |
| James Whitfield       | Gold      | Northeast     | 2015: $5,000, 2017: $8,000, 2019: $10,000, 2022: $12,000                   | $12,000      | $35,000        | 2022           | Yes       |
| Dorothy Callahan      | Gold      | Southeast     | 2020: $2,000, 2021: $2,500, 2022: $3,000, 2023: $3,500                     | $3,500       | $11,000        | 2023           | No        |
| Deborah Sorenson      | Gold      | Midwest       | 2016: $4,000, 2018: $5,000, 2020: $6,000, 2022: $7,000                     | $7,000       | $22,000        | 2022           | No        |
| Angela Petersen       | Gold      | West          | 2017: $3,500, 2019: $4,500, 2021: $5,500                                   | $5,500       | $13,500        | 2021           | No        |
| Grace Thornton        | Gold      | International | 2020: $2,500, 2021: $3,000, 2022: $3,500, 2023: $4,000                     | $4,000       | $13,000        | 2023           | No        |
| Linda Petrov          | Gold      | Northeast     | 2019: $10,000, 2021: $12,500, 2023: $15,000                                | $15,000      | $37,500        | 2023           | No        |
| Helen Magnusson       | Gold      | Southeast     | 2018: $8,000, 2020: $10,000, 2022: $12,000                                 | $12,000      | $30,000        | 2022           | No        |
| Maria Yamamoto        | Gold      | Midwest       | 2018: $6,000, 2020: $7,500, 2022: $9,000                                   | $9,000       | $22,500        | 2022           | No        |
| Emma Bergstrom        | Gold      | West          | 2019: $5,000, 2021: $6,500, 2023: $8,000                                   | $8,000       | $19,500        | 2023           | No        |
| Nora Bergqvist        | Gold      | International | 2018: $9,000, 2020: $11,000, 2022: $13,000                                 | $13,000      | $33,000        | 2022           | No        |
| Nancy Okafor          | Gold      | Southeast     | 2017: $5,000, 2019: $6,000, 2021: $7,500                                   | $7,500       | $18,500        | 2021           | No        |
| Vera Johansson        | Gold      | International | 2017: $7,000, 2019: $8,000, 2021: $9,000                                   | $9,000       | $24,000        | 2021           | No        |
| Margaret Alcott       | Silver    | Northeast     | 2019: $500, 2020: $750, 2021: $1,000, 2022: $1,200, 2023: $1,500           | $1,500       | $4,950         | 2023           | No        |
| Harold Mensah         | Silver    | Southeast     | 2021: $1,000, 2022: $1,200, 2023: $1,400                                   | $1,400       | $3,600         | 2023           | Yes       |
| Barbara Jensen        | Silver    | Midwest       | 2021: $1,000, 2022: $1,500, 2023: $2,000                                   | $2,000       | $4,500         | 2023           | No        |
| Betty Nakagawa        | Silver    | West          | 2021: $1,500, 2022: $2,000, 2023: $2,500                                   | $2,500       | $6,000         | 2023           | No        |
| Ada Yamamoto-Pierce   | Silver    | International | 2020: $3,500, 2021: $4,000, 2022: $4,500, 2023: $5,000                     | $5,000       | $17,000        | 2023           | Yes       |
| Claire Oduya          | Silver    | Northeast     | 2018: $1,500, 2019: $2,000, 2023: $3,000                                   | $3,000       | $6,500         | 2023           | Yes       |
| Ruth Andersen         | Silver    | Southeast     | 2019: $3,000, 2020: $4,000, 2021: $5,000, 2022: $6,000, 2023: $7,000       | $7,000       | $25,000        | 2023           | Yes       |
| Janet Okonkwo         | Silver    | Midwest       | 2019: $2,000, 2021: $2,500, 2023: $3,000                                   | $3,000       | $7,500         | 2023           | Yes       |
| Shirley Magnusdottir  | Silver    | West          | 2020: $4,000, 2021: $5,000, 2022: $6,000, 2023: $7,000                     | $7,000       | $22,000        | 2023           | Yes       |
| Owen Oduya            | Silver    | International | 2021: $800, 2022: $1,000, 2023: $1,200                                     | $1,200       | $3,000         | 2023           | Yes       |
| Susan Nakamura        | Silver    | Northeast     | 2020: $500, 2021: $750, 2022: $1,000, 2023: $1,250, 2024: $1,500           | $1,500       | $5,000         | 2024           | Yes       |
| Joe Iwamoto           | Silver    | West          | 2020: $750, 2021: $900, 2022: $1,100, 2023: $1,300                         | $1,300       | $4,050         | 2023           | Yes       |
| Michael Torres        | Lapsed    | Northeast     | 2017: $500, 2018: $600, 2019: $700                                          | $700         | $1,800         | 2019           | No        |
| Charles Kimura        | Lapsed    | Southeast     | 2015: $400, 2016: $400, 2017: $400                                          | $400         | $1,200         | 2017           | No        |
| Paul Achebe           | Lapsed    | Midwest       | 2017: $300, 2018: $300, 2019: $300                                          | $300         | $900           | 2019           | No        |
| Frank Watanabe        | Lapsed    | West          | 2016: $350, 2017: $350, 2018: $350                                          | $350         | $1,050         | 2018           | No        |
| Lars Achebe-Nielsen   | Lapsed    | International | 2015: $400, 2016: $400, 2017: $400                                          | $400         | $1,200         | 2017           | No        |
| Thomas Bergmann       | Lapsed    | Northeast     | 2014: $300, 2015: $300                                                      | $300         | $600           | 2015           | No        |
| Frank Dimitriou       | Lapsed    | Southeast     | 2016: $200, 2017: $200, 2018: $200                                          | $200         | $600           | 2018           | No        |
| Raymond Volkov        | Lapsed    | Midwest       | 2019: $250, 2020: $250                                                      | $250         | $500           | 2020           | No        |
| Henry Obi             | Lapsed    | West          | 2018: $150, 2019: $200                                                      | $200         | $350           | 2019           | No        |
| Felix Mensah-Bonsu    | Lapsed    | International | 2019: $300, 2020: $300                                                      | $300         | $600           | 2020           | No        |
| Patricia Huang        | Bronze    | Northeast     | 2021: $200, 2022: $250                                                      | $250         | $450           | 2022           | No        |
| Brenda Kowalski       | Bronze    | Southeast     | 2023: $150                                                                  | $150         | $150           | 2023           | No        |
| Carol Eriksson        | Bronze    | Midwest       | 2023: $100                                                                  | $100         | $100           | 2023           | No        |
| Kathy Lindberg        | Bronze    | West          | 2022: $125                                                                  | $125         | $125           | 2022           | No        |
| Iris Kowalczyk        | Bronze    | International | 2023: $200                                                                  | $200         | $200           | 2023           | No        |
| David Osei            | Bronze    | Northeast     | 2022: $75, 2023: $100                                                       | $100         | $175           | 2023           | No        |
| George Nwosu          | Bronze    | Southeast     | 2022: $50, 2023: $75                                                        | $75          | $125           | 2023           | No        |
| Samuel Lindqvist      | Bronze    | Midwest       | 2022: $50                                                                   | $50          | $50            | 2022           | No        |
| Jack Nkemdirim        | Bronze    | West          | 2023: $50                                                                   | $50          | $50            | 2023           | No        |
| Igor Volkov           | Bronze    | International | 2022: $75                                                                   | $75          | $75            | 2022           | No        |
| Arthur Mwangi         | Bronze    | Midwest       | 2020: $500, 2021: $600, 2022: $700, 2023: $800                              | $800         | $2,600         | 2023           | Yes       |

## HTML Letter Template

Use this template and fill in the [PLACEHOLDERS]:

```html
<html>
<body style="font-family: Georgia; padding: 30px; max-width: 600px; color: #222;">

  <p style="text-align:right; color: #888;">[DATE]</p>

  <p>[SALUTATION]</p>

  <p>On behalf of everyone at <strong>[CHARITY NAME]</strong>, I want to
  personally thank you for your incredible generosity. Your lifetime support
  of <strong>$[LIFETIME_GIVING]</strong> has made a real difference.</p>

  <p>[CAMPAIGN_PARAGRAPH — insert 2 sentences of campaign-specific messaging
  based on campaign type above]</p>

  <p>Today, I'd like to invite you to make a gift of
  <strong>$[ASK_AMOUNT]</strong>. [TIER_SPECIFIC_LINE — e.g. naming
  opportunity, legacy giving, monthly upgrade, peer page]</p>

  <p>To give, simply reply to this email or visit our donation page at
  <strong>[DONATION_URL]</strong>.</p>

  <p>With gratitude,<br>
  <strong>[RELATIONSHIP_MANAGER_NAME]</strong><br>
  [TITLE], [CHARITY NAME]</p>

</body>
</html>
```

If the donor file has missing fields, make reasonable assumptions and
proceed.
