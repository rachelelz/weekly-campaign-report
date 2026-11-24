# weekly-campaign-report

[![tests](https://github.com/rachelelz/weekly-campaign-report/actions/workflows/tests.yml/badge.svg)](https://github.com/rachelelz/weekly-campaign-report/actions/workflows/tests.yml)

Takes last week's CSV exports from Meta Ads, Google Ads and LinkedIn Campaign Manager and writes the Monday report: totals week over week, each platform side by side, the campaigns that moved and by how much, and which audiences, offers and creative types beat the median. Claude then writes the short summary for your manager on top.

Every calculation happens in the Python script. Claude only writes the words around numbers the script produced.

## The problem

On Monday morning someone downloads three exports that use three different names for the same column, pastes them into a spreadsheet, strips out the currency symbols, works out cost per conversion, lines it up against last week, and writes a paragraph for their manager.

## Try it in two minutes

```bash
# needs Python 3.9 or newer, nothing to install
git clone https://github.com/rachelelz/weekly-campaign-report
cd weekly-campaign-report
python3 scripts/report.py --demo
```

The example exports cover two weeks of made-up campaigns for a fictional outdoor-gear shop. Here's the start of [`examples/sample-report.md`](examples/sample-report.md):

```text
Look at this first: Q1_Prospecting_Video_US (Meta), cost per conversion went from $30.00 to $60.00 (+100.0%).

# Weekly campaign report

Week ending Sunday 2026-03-15. This week is Mar 9 to Mar 15, compared with Mar 2 to Mar 8. Every figure comes from the exports you supplied.

## Totals

|  | This week | Last week | Change |
| --- | ---: | ---: | ---: |
| Spend | $9,409 | $8,112 | +16.0% |
| Impressions | 696,700 | 670,100 | +4.0% |
| Clicks | 10,853 | 10,804 | +0.5% |
| Conversions | 279 | 285.5 | -2.3% |
| Revenue | $24,705 | $25,065 | -1.4% |
| CTR | 1.56% | 1.61% | -3.4% |
| CPC | $0.87 | $0.75 | +15.5% |
| CPA | $33.72 | $28.41 | +18.7% |
| ROAS | 3.01x | 3.59x | -16.1% |
```

Each flagged campaign comes with the numbers that triggered the flag. A LinkedIn campaign spent $910 with no conversions, after 2 conversions on $840 the week before. A $49 test campaign whose cost per conversion more than doubled is left out, because it's under the 10-conversion minimum.

Claude writes the short summary on top. This is the start of [`examples/sample-summary.md`](examples/sample-summary.md), written from the demo's `out/summary.json` with the rules in `SKILL.md`:

```text
Data notes: Meta's conversions come from its Results column, which counts whatever each campaign optimizes for. One Google row with no valid date was skipped. The Meta export had 11 blank conversion cells and 11 blank revenue cells, read as 0.

Spend was $9,409 this week (+16.0%), with 279 conversions (-2.3%) and a combined CPA of $33.72 (+18.7%). The platforms count conversions differently, so read the combined numbers with care.
```

## Use it on your own campaigns

1. Export 14 days of daily, campaign-level data from each platform you run. `SKILL.md` has a short pointer for each one. Ad platforms move their menus around, so if a pointer doesn't match what you see, look for the columns it names.
2. Put the CSVs in an `exports/` folder (it's git-ignored) and run:

   ```bash
   python3 scripts/report.py exports/*.csv
   ```

   The report goes to `out/report.md` and the numbers to `out/summary.json`. The `out/` folder is git-ignored too.

3. Optional: add a `tags.csv` that labels each campaign by audience, offer or creative type, and pass it with `--tags`:

   ```bash
   python3 scripts/report.py exports/*.csv --tags tags.csv
   ```

   Or name your campaigns like `Q1_Prospecting_Video_US` and the script reads the tags from the name.

As a Claude Code skill:

```bash
git clone https://github.com/rachelelz/weekly-campaign-report ~/.claude/skills/weekly-campaign-report
```

```text
Write my weekly campaign report. The exports are in ~/Downloads/week-11.
```

Claude runs the script, reads the data notes first, and writes a summary of at most 200 words: what happened, the two or three movements that matter with their numbers, and one next step labeled as a recommendation. When the export doesn't show why something changed, it says so.

## How it works

- Read. Each platform names its columns differently ("Amount spent (USD)", "Cost", "Total Spent"). `config/columns.json` maps them to one set of names. The script also handles currency symbols, thousands separators, "--" cells, the title lines Google Ads puts above the table, and rows that are exact copies, such as a second download of the same export.
- Compare. Rows are grouped by campaign into this week and last week. Spend, impressions, clicks, conversions and revenue are added up, then CTR, CPC, cost per conversion and ROAS are worked out from those totals. When a number can't be calculated, the report says "n/a" and doesn't show a 0 in its place. A platform whose export doesn't cover both weeks isn't compared: it's left out of the week-over-week totals, its campaigns aren't flagged as new or stopped, and the data notes say so.
- Flag. A campaign is flagged when its cost per conversion rises or falls 25% or more, its click-through rate falls 20% or more, its spend jumps 30% or more, it spends $250 with no conversions, or it starts or stops. Each flag has a minimum so small campaigns don't fill the report: 10 conversions for cost per conversion, 10,000 impressions for click-through rate, and a dollar minimum for spend jumps and for new or stopped campaigns. All of these live in `config/thresholds.json`.
- Rank. The "Look at this first" line goes to the flag with the most money behind it, for example the extra spend caused by a higher cost per conversion.
- Median. For each tag value with at least 3 campaigns, the report compares its median cost per conversion with the overall median (or click-through rate when too few campaigns have conversions).
- Summary. Claude reads `out/summary.json` and `out/report.md` and writes the short version.

## What it won't do

- Add up conversions as if they were unique customers. Each platform counts conversions its own way, and the report says so under the totals.
- Convert currencies. Everything is treated as one currency. If an export's currency doesn't match the one in `config/thresholds.json`, the report says so at the top.
- Explain why a campaign changed, since that isn't in any export.

The column names are written from each platform's standard export format, and the demo files are made up. They haven't been tested against a live account's export yet. Any column the script doesn't recognize, apart from a short list of known extras like CTR and campaign ID, is listed under "Data notes", so a renamed column shows up instead of disappearing. European number formats like `1.234,56` are detected per file, or you can force them with `--decimal-comma`, and the data notes say when a file is read as European style. Day-first dates like `15/03/2026` are detected per file too.

## Privacy

The script reads the CSVs you give it, writes to `out/`, and sends nothing anywhere. With the Claude Code skill, the report and summary go to your model provider when Claude reads them. Use anonymized campaign names if your company requires it.

## What went wrong while building it

- ROAS was wrong on the first run. LinkedIn's export has no revenue column, but its spend was still counted, which made return on ad spend look worse than it was. ROAS now only uses spend from platforms that report revenue, and the report says which platforms it left out.
- Every campaign looked new. When the chosen week had no data for the week before, every campaign was flagged as newly launched, and the top line announced a brand campaign that had been running all along. New-campaign flags now switch off when there's no previous week to compare with.
- That fix only checked the files together. A second review ran two-week Meta and Google exports next to a LinkedIn export with one week in it. Both LinkedIn campaigns were flagged as new, and total spend looked 34.8% up when Meta and Google together were up 17.4%. Coverage is now checked per export, and a platform without data for both weeks is left out of the totals.
- The first version's 40 tests all passed on the first run. That's suspicious, so I checked the spend and conversion totals for each platform separately against the raw CSVs, and read the tests that assert specific numbers. They held up. The test for the "every campaign looked new" bug came after.

## What I'd change next

- Go one level down to ad sets and ads, so a flagged campaign can be traced to the creative that changed.
- Add budget pacing: spend to date against a monthly budget, with a flag when a campaign is on track to overspend.
- Keep a history of past weeks so the report can show four-week trends.

## Files

```
SKILL.md                    instructions Claude Code follows
scripts/report.py           read, normalize, compare, flag, write the report
config/columns.json         column names per platform
config/thresholds.json      flag thresholds, volume guards, campaign-name tag rules
config/tags.template.csv    optional campaign tags
examples/exports/           two weeks of made-up exports for each platform
examples/tags.example.csv   tags for the demo campaigns
examples/sample-report.md   the demo output
examples/sample-summary.md  the summary Claude writes from the demo output
tests/test_report.py        86 offline tests: python3 -m unittest discover tests
```

## License

MIT. Built with Claude Code by [Rachel Elz](https://github.com/rachelelz).
