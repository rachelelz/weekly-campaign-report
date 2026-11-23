---
name: weekly-campaign-report
description: Turns last week's ad platform CSV exports (Meta Ads Manager, Google Ads, LinkedIn Campaign Manager) into a weekly campaign report and a short manager-ready summary. Use when someone asks for a weekly or Monday marketing report, a week-over-week comparison of paid campaigns, to find which campaigns moved, or to summarize ad performance for a manager. It runs a deterministic Python script for every number, then writes a narrative of at most 200 words on top.
---

# Weekly campaign report

The script does the math. You write the words. Never the other way around.

## Step 1: Get the exports

Ask which platforms the user runs and whether they have last week's CSVs. If not, give them the short pointer for each platform. Ask for two full weeks of daily rows (14 days), so the script can compare this week with the one before.

- **Meta Ads Manager:** Reports, set date range to the last 14 days, Breakdown > Time > Day, Campaign level. Columns: Campaign name, Amount spent, Impressions, Link clicks, Results, Purchases conversion value. Export as CSV.
- **Google Ads:** Campaigns, set the date range, Segment > Time > Day. Columns: Campaign, Cost, Impr., Clicks, Conversions, Conv. value. Download as CSV. The title lines above the table are fine.
- **LinkedIn Campaign Manager:** Account Assets > Campaign Manager, Export, date range, Daily breakdown, Campaign level. Columns: Campaign Name, Total Spent, Impressions, Clicks, Conversions, Start Date.

Menu names change between releases. If a pointer does not match what the user sees, name the columns you need and let them find the export.

Optional: a `tags.csv` (copy `config/tags.template.csv`) with one row per campaign and columns such as audience or offer. Campaign names like `Q1_Prospecting_Video_US` are also read as tags (see `tags_from_names` in `config/thresholds.json`).

Exports usually live in an `exports/` folder at the repo root. That folder is git-ignored.

## Step 2: Run the script

```bash
python3 scripts/report.py exports/*.csv --tags tags.csv --week-ending 2026-03-15
```

- Leave out `--week-ending` to use the latest date in the data.
- Add `--platform meta|google|linkedin` only if the script cannot detect a platform.
- Try `python3 scripts/report.py --demo` to see the output on the example data.

It writes `out/report.md` and `out/summary.json`.

## Step 3: Read before you write

Read `out/summary.json` and `out/report.md`. Start with the "Data notes" section. If rows were skipped, a platform was not detected, a week has fewer than 7 days of data, or the currency is not what the user expects, tell the user before the summary.

## Step 4: Write the summary

At most 200 words, in this order:

1. **What happened.** Total spend, conversions and CPA this week against last week, in one or two sentences.
2. **The 2 or 3 movements that matter.** Start from the "Look at this first" line. Use the movers list, with the numbers from the report. Name the campaign and the platform.
3. **Recommendation.** One next step, labeled "Recommendation:" so the reader can tell it from the facts.

## Rules

- Every number comes from the script output. Do not add, subtract, average or estimate anything yourself. If a number you want is not in the report, say it is not there, or run the script again with different inputs.
- If the data does not show why something moved, write "the data doesn't show why." Do not guess at creative fatigue, audience saturation, seasonality or tracking problems. You can list them as things to check, under the recommendation, clearly marked as unconfirmed.
- No causal claims without evidence in the data. Two numbers moving in the same week is not evidence that one caused the other.
- Flag low volume. If a campaign has fewer than 10 conversions in either week, say the result is based on a small number. The script already guards its flags, but your wording should match.
- Platforms count conversions their own way (attribution windows and methods differ, and one customer can appear in two platforms). Do not present the combined conversions or CPA as one deduplicated number without saying so. Prefer per-platform numbers when you compare efficiency.
- ROAS only covers platforms that export revenue. Say so when you quote it.
- "n/a" means the number cannot be computed (for example, CPA with zero conversions). Do not turn it into 0.
- Plain US English. Short sentences. No hype words. No emoji. Almost no em-dashes.

## Example shape

> Spend was $9,409 this week (+16.0%), with 279 conversions (-2.3%) and a combined CPA of $33.72 (+18.7%). The platforms count conversions differently, so read the combined CPA with care.
>
> Q1_Prospecting_Video_US (Meta) is the main movement: CPA went from $30.00 to $60.00 on 40 and 70 conversions, and CTR fell from 1.50% to 1.17%. The data doesn't show why. Q1_Prospecting_Document_US (LinkedIn) spent $910 with no conversions, against 2 conversions on $840 last week.
>
> Recommendation: check the video campaign's creative and audience settings, and confirm conversion tracking on the LinkedIn campaign, before moving budget.
