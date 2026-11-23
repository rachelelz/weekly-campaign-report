Look at this first: Q1_Prospecting_Video_US (Meta), cost per conversion went from $30.00 to $60.00 (+100.0%).

# Weekly campaign report

Week ending 2026-03-15. This week is Mar 9 to Mar 15, compared with Mar 2 to Mar 8. Every figure comes from the exports you supplied.

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

Conversions are counted by each platform's own rules, so treat the totals as a sum of platform numbers.
Revenue and ROAS leave out LinkedIn Ads, which has no revenue column in its export.

## By platform

| Platform | Spend | Conversions | CPA | CTR | ROAS | Spend vs last week | CPA vs last week |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Meta Ads | $5,119 | 152 | $33.68 | 1.53% | 2.66x | +6.2% | +27.8% |
| Google Ads | $3,080 | 123 | $25.04 | 1.71% | 3.59x | +42.6% | +11.9% |
| LinkedIn Ads | $1,210 | 4 | $302.50 | 0.95% | n/a | +7.1% | +60.6% |

## Movers

- **Q1_Prospecting_Video_US** (Meta): cost per conversion went from $30.00 to $60.00 (+100.0%).
  - Triggered by: CPA +100.0% (flag at +25%); 40 and 70 conversions (minimum 10 each week).
  - Why it matters: At this week's 40 conversions, the higher rate costs about $1,200 more than last week's rate would have. The export does not show why.
- **Q1_Prospecting_Document_US** (LinkedIn): $910 spent with no conversions.
  - Triggered by: 0 conversions on $910 spend (flag at $250 or more).
  - Why it matters: Last week: 2 conversions on $840. The export does not show why.
- **Q1_Prospecting_Video_US** (Meta): click-through rate fell from 1.50% to 1.17% (-22.0%).
  - Triggered by: CTR -22.0% (flag at -20%); impressions 205,000 and 210,000 (minimum 10,000 each week).
  - Why it matters: Fewer clicks per impression this week. The export does not show why.
- **Q1_Prospecting_Shopping_US** (Google): new campaign, $900 spent on 5 of 7 days.
  - Triggered by: No spend last week; spend this week is $900 (flag at $100 or more).
  - Why it matters: There is no earlier week to compare. So far: 24 conversions at a CPA of $37.50. Judge it after a full week.

Thresholds and volume guards are in `config/thresholds.json`.

## Campaigns

| Campaign | Spend | Conversions | CPA | CTR | CPA vs last week |
| --- | ---: | ---: | ---: | ---: | ---: |
| Q1_Prospecting_Video_US (Meta) | $2,400 | 40 | $60.00 | 1.17% | +100.0% |
| Q1_Prospecting_Static_US (Meta) | $1,450 | 37 | $39.19 | 1.39% | -0.7% |
| Q1_Prospecting_Text_US (Google) | $1,120 | 22 | $50.91 | 3.00% | -0.5% |
| Q1_Prospecting_Document_US (LinkedIn) | $910 | 0 | n/a | 0.73% | n/a |
| Q1_Prospecting_Shopping_US (Google) | $900 | 24 | $37.50 | 2.52% | n/a |
| Q1_Retargeting_Static_US (Meta) | $720 | 58 | $12.41 | 3.00% | -0.7% |
| Q1_Retargeting_Video_US (Meta) | $500 | 16 | $31.25 | 2.00% | +10.7% |
| Q1_Brand_Text_US (Google) | $430 | 53.5 | $8.04 | 7.69% | -0.5% |
| Q1_Retargeting_Display_US (Google) | $360 | 12.5 | $28.80 | 0.42% | -1.5% |
| Q1_Retargeting_Lead_US (LinkedIn) | $300 | 4 | $75.00 | 1.44% | +3.4% |
| Q1_Retargeting_Text_US (Google) | $270 | 11 | $24.55 | 5.08% | -5.6% |
| Q1_Test_Carousel_US (Meta) | $49 | 1 | $49.00 | 0.83% | +133.3% |

## What beats the median

Metric: CPA for this week. Overall median across 10 campaigns: $34.38. Lower is better. Only campaigns with enough volume count, and a tag value needs at least 3 campaigns to appear.

| Tag | Value | Campaigns counted | Median CPA | vs overall median | Result |
| --- | --- | ---: | ---: | ---: | ---: |
| Audience | Cold | 4 of 5 | $45.05 | +31.1% | behind the median |
| Audience | Warm | 5 of 5 | $28.80 | -16.2% | beats the median |
| Creative | Text | 3 of 3 | $24.55 | -28.6% | beats the median |
| Objective | Prospecting | 4 of 5 | $45.05 | +31.1% | behind the median |
| Objective | Retargeting | 5 of 5 | $28.80 | -16.2% | beats the median |
| Offer | Evergreen | 6 of 7 | $31.87 | -7.3% | beats the median |
| Offer | Spring sale | 4 of 4 | $34.38 | +0.0% | in line with the median |

## Data notes

- google-ads.example.csv: read as Google Ads, 61 rows used.
- google-ads.example.csv: 3 lines above the header were skipped.
- google-ads.example.csv: 1 row skipped, no valid date (total rows, footers).
- linkedin-ads.example.csv: read as LinkedIn Ads, 28 rows used.
- meta-ads.example.csv: read as Meta Ads, 70 rows used.
- meta-ads.example.csv: unknown columns ignored: Frequency.
- meta-ads.example.csv: blank or '--' cells read as 0 in conversions (11), revenue (11).
- Currency: all amounts are treated as USD. The tool does not convert currencies.
- Each platform counts conversions and revenue by its own rules, so the same customer can appear in more than one platform. Read the totals as a sum of platform numbers, not as unique conversions.
- Not enough campaigns to compare (need 3 with enough volume; counted of total in brackets): audience=Brand (1 of 1), creative=Carousel (0 of 1), creative=Display (1 of 1), creative=Document (0 of 1), creative=Lead (1 of 1), creative=Shopping (1 of 1), creative=Static (2 of 2), creative=Video (2 of 2), objective=Brand (1 of 1), objective=Test (0 of 1).
- No 'audience' tag for: Q1_Test_Carousel_US.
- No 'offer' tag for: Q1_Test_Carousel_US.
