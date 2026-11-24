"""Offline tests. Run from the repo root: python3 -m unittest discover tests"""
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import report  # noqa: E402

COLS, TH = report.load_config()
FMT = report.Fmt("$")


def write(directory, name, text, encoding="utf-8"):
    path = Path(directory) / name
    path.write_bytes(text.encode(encoding))
    return path


def stats(spend, impressions, clicks, conversions, revenue=None, days=7):
    """A derived metrics dict for one campaign-week, built the same way the tool builds it."""
    agg = report.new_agg()
    row = {"spend": spend, "impressions": impressions, "clicks": clicks,
           "conversions": conversions, "revenue": revenue, "date": date(2026, 3, 9)}
    report.add_row(agg, row)
    result = report.derive(agg)
    result["days_active"] = days
    return result


def flag_rules(cur, prev):
    flags = report.campaign_flags("Camp", "meta", cur, prev, TH, FMT)
    return {f["rule"] for f in flags}


class ParsingTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(report.parse_number("$1,234.50"), 1234.5)
        self.assertEqual(report.parse_number("12,345"), 12345.0)
        self.assertEqual(report.parse_number("3.5%"), 3.5)
        self.assertEqual(report.parse_number("(5.00)"), -5.0)
        self.assertEqual(report.parse_number(" 7 "), 7.0)

    def test_placeholders_are_none(self):
        for cell in ("--", "", "  ", "n/a", "\u2014"):
            self.assertIsNone(report.parse_number(cell))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            report.parse_number("abc")

    def test_dates(self):
        fmts = COLS["date_formats"]
        self.assertEqual(report.parse_date("2026-03-02", fmts), date(2026, 3, 2))
        self.assertEqual(report.parse_date("3/2/2026", fmts), date(2026, 3, 2))
        self.assertEqual(report.parse_date("Mar 2, 2026", fmts), date(2026, 3, 2))
        self.assertIsNone(report.parse_date("Total: Account", fmts))


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def read(self, name, text, encoding="utf-8", platform=None):
        return report.read_export(write(self.tmp.name, name, text, encoding), COLS, platform)

    def test_meta_aliases(self):
        text = ("Reporting starts,Reporting ends,Campaign name,Amount spent (USD),Impressions,"
                "Link clicks,Results,Purchases conversion value\n"
                "2026-03-02,2026-03-02,A_Prospecting_Video_US,100.50,5000,60,3,270.00\n")
        result = self.read("m.csv", text)
        self.assertEqual(result["platform"], "meta")
        row = result["rows"][0]
        self.assertEqual((row["spend"], row["impressions"], row["clicks"], row["conversions"], row["revenue"]),
                         (100.5, 5000.0, 60.0, 3.0, 270.0))
        self.assertEqual(row["date"], date(2026, 3, 2))
        self.assertEqual(result["currency"], "USD")

    def test_google_aliases_and_header_rows_skipped(self):
        text = ('Campaign performance\n"March 2, 2026 - March 15, 2026"\n\n'
                "Day,Campaign,Cost,Impr.,Clicks,Conversions,Conv. value\n"
                '"Mar 2, 2026",G_Brand_Text_US,"1,234.56","12,345",300,"5.5","--"\n'
                'Total: Account,--,"1,234.56","12,345",300,5.5,--\n')
        result = self.read("g.csv", text)
        self.assertEqual(result["platform"], "google")
        self.assertEqual(result["preamble_lines"], 3)
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual((row["spend"], row["impressions"], row["conversions"]), (1234.56, 12345.0, 5.5))
        self.assertEqual(sum(result["skipped"].values()), 1)

    def test_linkedin_aliases_with_bom_and_currency_symbol(self):
        text = ("Start Date,Campaign Name,Total Spent,Impressions,Clicks,Conversions\n"
                '3/2/2026,L_Prospecting_Document_US,"$1,014.20","20,000",150,0\n')
        result = self.read("l.csv", text, encoding="utf-8-sig")
        self.assertEqual(result["platform"], "linkedin")
        row = result["rows"][0]
        self.assertEqual(row["spend"], 1014.2)
        self.assertIsNone(row["revenue"])

    def test_dash_and_blank_cells_become_zero_and_are_counted(self):
        text = ("Day,Campaign,Cost,Impr.,Clicks,Conversions,Conv. value\n"
                "2026-03-02,G_A_B_US,10,1000,20,--,\n"
                "2026-03-03,G_A_B_US,10,1000,20,1,50\n")
        result = self.read("g.csv", text)
        row = result["rows"][0]
        self.assertEqual((row["conversions"], row["revenue"]), (0.0, 0.0))
        self.assertEqual(result["blank_cells"]["conversions"], 1)
        self.assertEqual(result["blank_cells"]["revenue"], 1)
        self.assertFalse(result["revenue_blank"])

    def test_unreadable_number_skips_row(self):
        text = ("Day,Campaign,Cost,Impr.,Clicks\n"
                "2026-03-02,A,10,1000,20\n2026-03-03,B,oops,1000,20\n")
        result = self.read("g.csv", text)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["skipped"]["unreadable number"], 1)

    def test_unknown_columns_reported_and_platform_override(self):
        text = "Date,Campaign,Spend,Mystery\n2026-03-02,A,10,x\n"
        result = self.read("x.csv", text, platform="linkedin")
        self.assertEqual(result["unknown_columns"], ["Mystery"])
        self.assertEqual(result["platform"], "linkedin")
        self.assertTrue(result["platform_forced"])
        self.assertIn("(set with --platform)", report.file_notes([result], TH)[0])

    def test_platform_flag_leaves_detected_files_alone(self):
        text = "Day,Campaign,Cost,Impr.\n2026-03-02,A,10,100\n"
        result = self.read("g.csv", text, platform="linkedin")
        self.assertEqual(result["platform"], "google")
        self.assertFalse(result["platform_forced"])

    def test_undetected_platform_note_says_what_the_flag_changes(self):
        result = self.read("x.csv", "Date,Campaign,Spend\n2026-03-02,A,10\n")
        self.assertEqual(result["platform"], "unknown")
        note = report.file_notes([result], TH)[0]
        self.assertIn("--platform meta, google or linkedin", note)
        self.assertIn("only changes files whose platform is not detected", note)

    def test_amount_spent_with_any_currency_code(self):
        text = ("Reporting starts,Campaign name,Amount spent (CHF),Impressions,Link clicks,Results\n"
                "2026-03-02,A_B_C,12.50,1000,20,1\n")
        result = self.read("m.csv", text)
        self.assertEqual(result["rows"][0]["spend"], 12.5)
        self.assertEqual(result["currency"], "CHF")
        self.assertEqual(result["platform"], "meta")
        self.assertEqual(result["unknown_columns"], [])

    def test_currency_code_column_sets_currency(self):
        text = ("Day,Campaign,Currency code,Cost,Impr.\n"
                "2026-03-02,G_A_B_NL,EUR,12.50,800\n2026-03-03,G_A_B_NL,EUR,12.50,800\n")
        result = self.read("g.csv", text)
        self.assertEqual(result["currency"], "EUR")
        self.assertEqual(result["unknown_columns"], [])
        self.assertTrue(any("currency is EUR" in n for n in report.file_notes([result], TH)))

    def test_linkedin_currency_column_is_read(self):
        result = report.read_export(ROOT / "examples" / "exports" / "linkedin-ads.example.csv", COLS)
        self.assertEqual(result["currency"], "USD")

    def test_revenue_column_blank_in_every_row_is_missing(self):
        text = ("Reporting starts,Reporting ends,Campaign name,Amount spent (USD),Impressions,"
                "Link clicks,Results,Purchases conversion value\n"
                "2026-03-02,2026-03-02,M_Leadgen_Form_US,500,40000,400,20,\n"
                "2026-03-03,2026-03-03,M_Leadgen_Form_US,500,40000,400,20,\n")
        result = self.read("m.csv", text)
        self.assertTrue(result["revenue_blank"])
        self.assertEqual([r["revenue"] for r in result["rows"]], [None, None])
        self.assertNotIn("revenue", result["blank_cells"])
        notes = report.file_notes([result], TH)
        self.assertTrue(any("blank in every row" in n and "left out of ROAS" in n for n in notes))

    def test_day_first_dates_are_detected(self):
        rows = "".join("%02d/03/2026,L_A_B_NL,100,2000,20,1\n" % d for d in range(2, 16))
        result = self.read("l.csv", "Start Date (in UTC),Campaign Name,Total Spent,Impressions,Clicks,"
                                    "Conversions\n" + rows)
        self.assertEqual(result["date_order"], "day")
        self.assertEqual(len(result["rows"]), 14)
        self.assertEqual(result["rows"][-1]["date"], date(2026, 3, 15))
        notes = report.file_notes([result], TH)
        self.assertTrue(any("dates read day first (13/03/2026 is March 13, 2026)" in n for n in notes))

    def test_month_first_dates_need_no_note(self):
        text = "Start Date,Campaign Name,Total Spent\n3/2/2026,A,10\n3/15/2026,A,10\n"
        result = self.read("l.csv", text)
        self.assertEqual(result["date_order"], "month")
        self.assertEqual(result["rows"][1]["date"], date(2026, 3, 15))
        self.assertFalse(any("dates" in n for n in report.file_notes([result], TH)))

    def test_ambiguous_slash_dates_get_a_note(self):
        result = self.read("l.csv", "Start Date,Campaign Name,Total Spent\n3/2/2026,A,10\n3/4/2026,A,10\n")
        self.assertEqual(result["date_order"], "ambiguous")
        self.assertEqual(result["rows"][0]["date"], date(2026, 3, 2))
        notes = report.file_notes([result], TH)
        self.assertTrue(any("3/2/2026 could be month first or day first" in n for n in notes))

    def test_export_not_broken_down_by_day_stops(self):
        text = ("Reporting starts,Reporting ends,Campaign name,Amount spent (USD),Impressions,"
                "Link clicks,Results\n2026-03-02,2026-03-15,M_A_B_US,5000,400000,4000,100\n")
        with self.assertRaises(report.InputError) as caught:
            self.read("meta_agg.csv", text)
        self.assertIn("not broken down by day", str(caught.exception))
        self.assertIn("Breakdown > Time > Day", str(caught.exception))

    def test_meta_results_column_gets_a_note(self):
        meta = report.read_export(ROOT / "examples" / "exports" / "meta-ads.example.csv", COLS)
        google = report.read_export(ROOT / "examples" / "exports" / "google-ads.example.csv", COLS)
        results_notes = [n for n in report.file_notes([meta, google], TH) if "Results column" in n]
        self.assertEqual(len(results_notes), 1)
        self.assertTrue(results_notes[0].startswith("meta-ads.example.csv:"))

    def test_one_row_is_singular(self):
        result = self.read("g.csv", "Day,Campaign,Cost\n2026-03-02,A,10\n")
        self.assertIn("1 row used.", report.file_notes([result], TH)[0])

    def test_utf16_tab_separated_export(self):
        text = "Day\tCampaign\tCost\n2026-03-02\tA\t10\n"
        result = self.read("t.csv", text, encoding="utf-16")
        self.assertEqual(len(result["rows"]), 1)

    def test_file_without_header_gives_error(self):
        result = self.read("bad.csv", "hello,world\n1,2\n")
        self.assertIsNotNone(result["error"])


class MathTests(unittest.TestCase):
    def test_week_windows(self):
        current, previous = report.week_windows(date(2026, 3, 15))
        self.assertEqual(current, (date(2026, 3, 9), date(2026, 3, 15)))
        self.assertEqual(previous, (date(2026, 3, 2), date(2026, 3, 8)))

    def test_default_week_ending_is_latest_date(self):
        rows = [{"platform": "meta", "campaign": "A_B_C_US", "date": date(2026, 3, d), "spend": 10.0,
                 "impressions": 100.0, "clicks": 5.0, "conversions": 1.0, "revenue": None}
                for d in range(2, 16)]
        summary = report.analyze(rows, TH, {})
        self.assertEqual(summary["week_ending"], "2026-03-15")
        self.assertEqual(summary["totals"]["current"]["spend"], 70.0)
        self.assertEqual(summary["totals"]["previous"]["spend"], 70.0)

    def test_no_previous_week_means_no_new_campaign_flags(self):
        rows = [{"platform": "meta", "campaign": "A_B_C_US", "date": date(2026, 3, d), "spend": 500.0,
                 "impressions": 100.0, "clicks": 5.0, "conversions": 1.0, "revenue": None}
                for d in range(9, 16)]
        summary = report.analyze(rows, TH, {})
        self.assertEqual(summary["movers"], [])
        self.assertTrue(any("no data for last week" in n for n in summary["data_notes"]))

    def test_divide_by_zero_is_none_and_shows_na(self):
        zero = stats(0.0, 0.0, 0.0, 0.0)
        for key in ("ctr", "cpc", "cpa"):
            self.assertIsNone(zero[key])
        self.assertEqual(FMT.money(zero["cpa"], 2), "n/a")
        self.assertEqual(FMT.rate(zero["ctr"]), "n/a")
        self.assertEqual(FMT.change(report.pct_change(5.0, 0.0)), "n/a")

    def test_conversions_zero_gives_na_cpa_not_zero(self):
        self.assertIsNone(stats(100.0, 1000.0, 10.0, 0.0)["cpa"])

    def test_roas_uses_only_spend_with_revenue(self):
        agg = report.new_agg()
        day = date(2026, 3, 9)
        report.add_row(agg, {"spend": 100.0, "impressions": 1.0, "clicks": 1.0, "conversions": 1.0,
                             "revenue": 300.0, "date": day})
        report.add_row(agg, {"spend": 100.0, "impressions": 1.0, "clicks": 1.0, "conversions": 1.0,
                             "revenue": None, "date": day})
        self.assertEqual(report.derive(agg)["roas"], 3.0)


class MoverTests(unittest.TestCase):
    def test_cpa_up_flagged_with_volume(self):
        prev = stats(1000, 50000, 500, 50)    # CPA 20
        cur = stats(1000, 50000, 500, 30)     # CPA 33.33
        self.assertIn("cpa_up", flag_rules(cur, prev))

    def test_cpa_up_not_flagged_with_low_volume(self):
        prev = stats(100, 5000, 50, 4)        # CPA 25
        cur = stats(100, 5000, 50, 2)         # CPA 50, but under 10 conversions
        self.assertNotIn("cpa_up", flag_rules(cur, prev))

    def test_cpa_down_flagged(self):
        prev = stats(1000, 50000, 500, 20)
        cur = stats(1000, 50000, 500, 40)
        self.assertIn("cpa_down", flag_rules(cur, prev))

    def test_ctr_down_needs_impressions(self):
        big_prev, big_cur = stats(500, 50000, 1000, 10), stats(500, 50000, 700, 10)
        self.assertIn("ctr_down", flag_rules(big_cur, big_prev))
        small_prev, small_cur = stats(50, 4000, 80, 1), stats(50, 4000, 40, 1)
        self.assertNotIn("ctr_down", flag_rules(small_cur, small_prev))

    def test_spend_up_needs_prior_spend(self):
        self.assertIn("spend_up", flag_rules(stats(1000, 1, 1, 1), stats(500, 1, 1, 1)))
        self.assertNotIn("spend_up", flag_rules(stats(150, 1, 1, 1), stats(50, 1, 1, 1)))

    def test_zero_conversions_needs_spend(self):
        self.assertIn("zero_conversions", flag_rules(stats(900, 20000, 150, 0), stats(800, 20000, 150, 2)))
        self.assertNotIn("zero_conversions", flag_rules(stats(40, 2000, 20, 0), stats(40, 2000, 20, 1)))

    def test_new_and_stopped(self):
        empty = report.derive(report.new_agg())
        self.assertIn("new", flag_rules(stats(900, 31000, 780, 24, days=5), empty))
        self.assertIn("stopped", flag_rules(empty, stats(900, 31000, 780, 24)))
        self.assertNotIn("new", flag_rules(stats(30, 1000, 10, 1), empty))

    def test_look_first_picks_largest_dollar_impact(self):
        flags = [
            {"rule": "zero_conversions", "campaign": "A", "platform": "meta", "impact": 900.0, "headline": "a"},
            {"rule": "cpa_up", "campaign": "B", "platform": "meta", "impact": 1200.0, "headline": "b"},
        ]
        totals = {"current": stats(1, 1, 1, 1), "change": {}}
        line = report.pick_look_first(flags, totals, FMT)
        self.assertTrue(line.startswith("Look at this first: B"))

    def test_look_first_without_flags(self):
        cur, prev = stats(100, 1000, 10, 5), stats(100, 1000, 10, 5)
        totals = {"current": cur, "change": report.changes(cur, prev)}
        self.assertIn("no campaign crossed a flag threshold", report.pick_look_first([], totals, FMT))


class TagTests(unittest.TestCase):
    def test_tags_from_naming_convention(self):
        tags = report.tags_for("Q4_Prospecting_Video_US", {}, TH["tags_from_names"])
        self.assertEqual(tags, {"objective": "Prospecting", "creative": "Video"})

    def test_tags_csv_overrides_and_adds(self):
        table = {"q4_prospecting_video_us": {"objective": "Awareness", "audience": "Cold"}}
        tags = report.tags_for("Q4_Prospecting_Video_US", table, TH["tags_from_names"])
        self.assertEqual(tags["objective"], "Awareness")
        self.assertEqual(tags["audience"], "Cold")

    def test_name_without_delimiter_has_no_tags(self):
        self.assertEqual(report.tags_for("Brand", {}, TH["tags_from_names"]), {})

    def test_median_needs_three_campaigns_per_tag_value(self):
        def camp(name, cpa_conv, tag):
            return {"platform": "meta", "campaign": name, "tags": {"audience": tag},
                    "current": stats(100.0, 10000, 100, cpa_conv)}
        campaigns = [camp("a", 10, "Cold"), camp("b", 5, "Cold"), camp("c", 4, "Cold"),
                     camp("d", 20, "Warm"), camp("e", 25, "Warm")]
        result = report.median_comparison(campaigns, TH)
        values = {g["value"] for g in result["groups"]}
        self.assertEqual(values, {"Cold"})
        self.assertEqual([t["value"] for t in result["too_small"]], ["Warm"])
        self.assertEqual(result["metric"], "CPA")

    def test_median_uses_ctr_when_no_conversions(self):
        def camp(name, clicks, tag):
            return {"platform": "meta", "campaign": name, "tags": {"audience": tag},
                    "current": stats(100.0, 10000, clicks, 0)}
        campaigns = [camp("a", 100, "X"), camp("b", 200, "X"), camp("c", 300, "X")]
        result = report.median_comparison(campaigns, TH)
        self.assertEqual(result["metric"], "CTR")
        self.assertAlmostEqual(result["groups"][0]["median"], 0.02)

    def test_low_volume_campaign_excluded_from_cpa_median(self):
        campaigns = [{"platform": "meta", "campaign": n, "tags": {"audience": "X"},
                      "current": stats(100.0, 10000, 100, conv)}
                     for n, conv in (("a", 10), ("b", 5), ("c", 4), ("d", 1))]
        result = report.median_comparison(campaigns, TH)
        self.assertEqual(result["groups"][0]["campaigns"], 3)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = sorted(str(p) for p in (ROOT / "examples" / "exports").glob("*.csv"))
        cls.markdown, cls.summary = report.run(paths, str(ROOT / "examples" / "tags.example.csv"))

    def test_look_first_is_first_line(self):
        first = self.markdown.splitlines()[0]
        self.assertTrue(first.startswith("Look at this first: Q1_Prospecting_Video_US"))

    def test_totals_match_hand_checked_numbers(self):
        cur = self.summary["totals"]["current"]
        prev = self.summary["totals"]["previous"]
        self.assertEqual(cur["spend"], 9409.0)
        self.assertEqual(prev["spend"], 8112.0)
        self.assertEqual(cur["conversions"], 279.0)
        self.assertEqual(prev["conversions"], 285.5)

    def test_expected_flags_and_no_noise_from_tiny_campaign(self):
        flags = {(m["campaign"], m["rule"]) for m in self.summary["movers"]}
        self.assertIn(("Q1_Prospecting_Video_US", "cpa_up"), flags)
        self.assertIn(("Q1_Prospecting_Document_US", "zero_conversions"), flags)
        self.assertIn(("Q1_Prospecting_Shopping_US", "new"), flags)
        self.assertFalse([f for f in flags if f[0] == "Q1_Test_Carousel_US"])
        self.assertFalse([f for f in flags if f[0] == "Q1_Retargeting_Static_US"])

    def test_google_total_row_not_double_counted(self):
        google = [p for p in self.summary["platforms"] if p["platform"] == "google"][0]
        self.assertEqual(google["current"]["spend"], 3080.0)

    def test_creative_tags_below_minimum_are_not_reported(self):
        groups = {(g["tag"], g["value"]) for g in self.summary["medians"]["groups"]}
        self.assertIn(("creative", "Text"), groups)
        self.assertNotIn(("creative", "Video"), groups)

    def test_cli_writes_report_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, summary = Path(tmp) / "r.md", Path(tmp) / "s.json"
            paths = sorted(str(p) for p in (ROOT / "examples" / "exports").glob("*.csv"))
            with contextlib.redirect_stdout(io.StringIO()):
                code = report.main(paths + ["--week-ending", "2026-03-15", "--out", str(out),
                                            "--summary", str(summary)])
            self.assertEqual(code, 0)
            self.assertTrue(out.read_text(encoding="utf-8").startswith("Look at this first:"))
            self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["week_ending"], "2026-03-15")

    def test_demo_matches_committed_sample_report(self):
        sample = (ROOT / "examples" / "sample-report.md").read_text(encoding="utf-8")
        self.assertEqual(sample, self.markdown)

    def test_report_has_no_fake_zero_for_missing_values(self):
        self.assertIn("| Q1_Prospecting_Document_US (LinkedIn) | $910 | 0 | n/a |", self.markdown)

    def test_triggers_list_last_week_first_like_the_money(self):
        triggers = {m["rule"]: m["trigger"] for m in self.summary["movers"]
                    if m["campaign"] == "Q1_Prospecting_Video_US"}
        self.assertIn("70 conversions last week and 40 this week", triggers["cpa_up"])
        self.assertIn("210,000 impressions last week and 205,000 this week", triggers["ctr_down"])

    def test_window_line_names_the_weekday(self):
        self.assertIn("Week ending Sunday 2026-03-15. This week is Mar 9 to Mar 15", self.markdown)

    def test_note_says_which_tags_came_from_names(self):
        notes = [n for n in self.summary["data_notes"] if n.startswith("Tags read from campaign names")]
        self.assertEqual(len(notes), 1)
        self.assertIn("creative, objective", notes[0])
        self.assertIn("--tags overrides them", notes[0])

    def test_no_names_note_when_tags_file_covers_every_tag(self):
        rows = [{"platform": "meta", "campaign": "Q1_Prospecting_Video_US", "date": date(2026, 3, d),
                 "spend": 10.0, "impressions": 100.0, "clicks": 5.0, "conversions": 1.0, "revenue": None}
                for d in range(2, 16)]
        table = {"q1_prospecting_video_us": {"objective": "Awareness", "creative": "Reel"}}
        summary = report.analyze(rows, TH, table)
        self.assertFalse(any("Tags read from campaign names" in n for n in summary["data_notes"]))


def google_rows(days, campaign="G_Brand_Text_US", spend=100):
    return "".join("2026-03-%02d,%s,%s,2000,200,10,1000\n" % (d, campaign, spend) for d in days)


GOOGLE_HEADER = "Day,Campaign,Cost,Impr.,Clicks,Conversions,Conv. value\n"
LINKEDIN_HEADER = "Start Date,Campaign Name,Total Spent,Impressions,Clicks,Conversions\n"


class CoverageTests(unittest.TestCase):
    """Each export is checked for rows in both weeks, so one short export can't skew the rest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.demo = {p.name.split("-")[0]: str(p) for p in (ROOT / "examples" / "exports").glob("*.csv")}

    def file(self, name, text):
        return str(write(self.tmp.name, name, text))

    def test_export_with_one_week_is_left_out_of_totals_and_new_flags(self):
        li = self.file("li_7days.csv", LINKEDIN_HEADER + "".join(
            "3/%d/2026,L_Prospecting_Document_US,200,5000,50,0\n" % d for d in range(9, 16)))
        markdown, summary = report.run([self.demo["meta"], self.demo["google"], li])
        self.assertFalse([m for m in summary["movers"] if m["platform"] == "linkedin" and m["rule"] == "new"])
        zero = [m for m in summary["movers"] if m["rule"] == "zero_conversions"][0]
        self.assertIn("no data for last week to compare", zero["why_it_matters"])
        self.assertAlmostEqual(summary["totals"]["current"]["spend"], 5119.0 + 3080.0, places=2)
        self.assertEqual(summary["totals"]["left_out"], ["linkedin"])
        linkedin = [p for p in summary["platforms"] if p["platform"] == "linkedin"][0]
        self.assertFalse(linkedin["compared"])
        self.assertIsNone(linkedin["change"]["spend"])
        self.assertIn("LinkedIn Ads is left out of the totals and the week-over-week changes", markdown)
        self.assertTrue(any(n.startswith("li_7days.csv: no data for last week") for n in summary["data_notes"]))

    def test_export_without_this_week_gives_no_stopped_flags(self):
        google = self.file("g.csv", GOOGLE_HEADER + google_rows(range(2, 16)))
        li = self.file("li_old.csv", LINKEDIN_HEADER + "".join(
            "3/%d/2026,L_Prospecting_Document_US,200,5000,50,1\n" % d for d in range(2, 9)))
        _, summary = report.run([google, li])
        self.assertFalse([m for m in summary["movers"] if m["rule"] == "stopped"])
        self.assertEqual(summary["totals"]["platforms"], ["google"])
        self.assertTrue(any(n.startswith("li_old.csv: no data for this week") for n in summary["data_notes"]))

    def test_no_previous_week_shows_last_week_as_na(self):
        markdown, summary = report.run([self.file("one.csv", GOOGLE_HEADER + google_rows(range(9, 16), spend=500))])
        self.assertIsNone(summary["totals"]["previous"]["spend"])
        self.assertIn("| Spend | $3,500 | n/a | n/a |", markdown)
        self.assertEqual(summary["movers"], [])

    def test_blank_revenue_file_is_left_out_of_roas(self):
        meta = self.file("meta_leads.csv", "Reporting starts,Reporting ends,Campaign name,Amount spent (USD),"
                         "Impressions,Link clicks,Results,Purchases conversion value\n" + "".join(
                             "2026-03-%02d,2026-03-%02d,M_Leadgen_Form_US,500,40000,400,20,\n" % (d, d)
                             for d in range(2, 16)))
        google = self.file("g.csv", GOOGLE_HEADER + google_rows(range(2, 16)))
        markdown, summary = report.run([meta, google])
        self.assertAlmostEqual(summary["totals"]["current"]["roas"], 10.0)
        meta_row = [p for p in summary["platforms"] if p["platform"] == "meta"][0]
        self.assertIsNone(meta_row["current"]["roas"])
        self.assertIn("Revenue and ROAS leave out Meta Ads, which has no revenue data in its export.", markdown)

    def run_cli(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = report.main(list(args) + ["--out", str(Path(self.tmp.name) / "r.md")])
        return code, err.getvalue()

    def test_week_ending_after_the_data_exits_2(self):
        code, err = self.run_cli(*sorted(self.demo.values()), "--week-ending", "2026-03-22")
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("after the last date in the exports (2026-03-15)", err)
        self.assertFalse((Path(self.tmp.name) / "r.md").exists())

    def test_week_ending_before_the_data_exits_2(self):
        code, err = self.run_cli(*sorted(self.demo.values()), "--week-ending", "2026-02-01")
        self.assertEqual(code, 2)
        self.assertIn("no rows fall in the week ending 2026-02-01", err)

    def test_export_not_by_day_exits_2(self):
        meta = self.file("meta_agg.csv", "Reporting starts,Reporting ends,Campaign name,Amount spent (USD)\n"
                                         "2026-03-02,2026-03-15,M_A_B_US,5000\n")
        code, err = self.run_cli(meta)
        self.assertEqual(code, 2)
        self.assertIn("meta_agg.csv is not broken down by day", err)


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = sorted(str(p) for p in (ROOT / "examples" / "exports").glob("*.csv"))

    def test_second_download_of_an_export_is_dropped(self):
        meta = ROOT / "examples" / "exports" / "meta-ads.example.csv"
        copy = Path(self.tmp.name) / "meta-ads (1).csv"
        copy.write_bytes(meta.read_bytes())
        _, summary = report.run(self.paths + [str(copy)])
        self.assertEqual(summary["totals"]["current"]["spend"], 9409.0)
        self.assertEqual(summary["totals"]["previous"]["conversions"], 285.5)
        self.assertIn("meta-ads (1).csv: read as Meta Ads, 0 rows used.", summary["data_notes"])
        self.assertTrue(any(n.startswith("meta-ads (1).csv: 70 rows were exact copies of rows in "
                                         "meta-ads.example.csv") for n in summary["data_notes"]))

    def test_same_campaign_day_with_different_numbers_is_kept_with_a_warning(self):
        a = write(self.tmp.name, "a.csv", GOOGLE_HEADER + google_rows(range(2, 16)))
        b = write(self.tmp.name, "b.csv", GOOGLE_HEADER + google_rows([9], spend=150))
        _, summary = report.run([str(a), str(b)])
        self.assertEqual(summary["totals"]["current"]["spend"], 850.0)
        warning = summary["data_notes"][0]
        self.assertTrue(warning.startswith("Check this: 1 campaign-day appears more than once"))
        self.assertIn("G_Brand_Text_US (Google) on 2026-03-09 in a.csv and b.csv", warning)


class FormatTests(unittest.TestCase):
    def test_negative_money_puts_the_sign_first(self):
        self.assertEqual(FMT.money(-5), "-$5")
        self.assertEqual(FMT.money(-1234.5, 2), "-$1,234.50")
        self.assertEqual(FMT.money(-0.4), "$0")
        self.assertEqual(FMT.money(5), "$5")

    def test_currency_warning_grammar_with_two_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = "Day,Campaign,Currency code,Cost\n"
            body = "".join("2026-03-%02d,G_A_B_NL,EUR,10\n" % d for d in range(2, 16))
            paths = [str(write(tmp, name, header + body.replace("G_A_B_NL", name[0])))
                     for name in ("a.csv", "b.csv")]
            markdown, _ = report.run(paths)
        self.assertIn("Check the currency first: a.csv (EUR) and b.csv (EUR) use a different currency "
                      "than USD.", markdown)

    def test_source_has_no_literal_invisible_characters(self):
        source = (ROOT / "scripts" / "report.py").read_text(encoding="utf-8")
        self.assertNotIn("\ufeff", source)


class NumberFormatTests(unittest.TestCase):
    def read(self, text, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            return report.read_export(write(tmp, "m.csv", text), COLS, **kwargs)

    def test_euro_with_symbol_is_comma_decimal(self):
        result = self.read('Day,Campaign,Cost,Impr.\n2026-03-02,A,"\u20ac100,00","1.000"\n')
        self.assertEqual(result["number_style"], "comma")
        self.assertEqual(result["rows"][0]["spend"], 100.0)
        self.assertEqual(result["rows"][0]["impressions"], 1000.0)
        self.assertEqual(result["currency"], "EUR")

    def test_dotted_thousands_with_comma_decimal(self):
        result = self.read('Day,Campaign,Cost\n2026-03-02,A,"1.234,56"\n')
        self.assertEqual(result["number_style"], "comma")
        self.assertEqual(result["rows"][0]["spend"], 1234.56)

    def test_us_file_stays_us(self):
        result = self.read('Day,Campaign,Cost\n2026-03-02,A,"1,234.50"\n2026-03-03,A,"99.10"\n')
        self.assertEqual(result["number_style"], "us")
        self.assertEqual(result["rows"][0]["spend"], 1234.5)

    def test_ambiguous_file_keeps_us_reading(self):
        result = self.read('Day,Campaign,Cost\n2026-03-02,A,"1,234"\n')
        self.assertEqual(result["number_style"], "ambiguous")
        self.assertEqual(result["rows"][0]["spend"], 1234.0)
        notes = report.file_notes([result], TH)
        self.assertTrue(any("ambiguous" in n and "m.csv" in n and "US style" in n for n in notes))

    def test_forced_decimal_comma(self):
        result = self.read('Day,Campaign,Cost\n2026-03-02,A,"1,234"\n', decimal_comma=True)
        self.assertEqual(result["rows"][0]["spend"], 1.234)
        notes = report.file_notes([result], TH)
        self.assertTrue(any("European style" in n and "--decimal-comma" in n for n in notes))

    def test_comma_file_always_gets_a_note(self):
        result = self.read('Day,Campaign,Cost\n2026-03-02,A,"100,00"\n')
        notes = report.file_notes([result], TH)
        self.assertTrue(any("m.csv" in n and "European style" in n for n in notes))

    def test_currency_mismatch_shows_in_opening_lines(self):
        rows = "".join('2026-03-%02d,A,"100,00",1000,20,2\n' % d for d in range(2, 16))
        header = "Reporting starts,Campaign name,Amount spent (EUR),Impressions,Link clicks,Results\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "eur.csv", header + rows)
            markdown, summary = report.run([str(path)])
        self.assertEqual(summary["totals"]["current"]["spend"], 700.0)
        top = markdown.split("## Totals")[0]
        self.assertIn("eur.csv (EUR)", top)
        self.assertIn("Check the currency", top)


class WorkingFolderTests(unittest.TestCase):
    def run_cli(self, cwd, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "report.py")] + list(args),
                              cwd=str(cwd), capture_output=True, text=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        exports = self.work / "exports"
        exports.mkdir()
        for p in (ROOT / "examples" / "exports").glob("*.csv"):
            (exports / p.name).write_bytes(p.read_bytes())
        (self.work / "tags.csv").write_bytes((ROOT / "examples" / "tags.example.csv").read_bytes())
        self.repo_out = ROOT / "out"
        self.before = sorted(self.repo_out.rglob("*")) if self.repo_out.exists() else None

    def tearDown(self):
        self.tmp.cleanup()

    def assert_repo_untouched(self):
        after = sorted(self.repo_out.rglob("*")) if self.repo_out.exists() else None
        self.assertEqual(self.before, after)

    def test_default_output_goes_to_working_folder(self):
        done = self.run_cli(self.work, "exports/meta-ads.example.csv", "exports/google-ads.example.csv",
                            "--tags", "tags.csv")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.work / "out" / "report.md").exists())
        self.assertTrue((self.work / "out" / "summary.json").exists())
        self.assert_repo_untouched()

    def test_summary_goes_next_to_custom_report(self):
        done = self.run_cli(self.work, "exports/meta-ads.example.csv", "--out", "report.md")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.work / "report.md").exists())
        self.assertTrue((self.work / "summary.json").exists())
        self.assertFalse((self.work / "out").exists())
        self.assert_repo_untouched()

    def test_tags_file_is_only_read_with_the_flag(self):
        done = self.run_cli(self.work, "exports/meta-ads.example.csv")
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads((self.work / "out" / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse([c for c in summary["campaigns"] if "audience" in c["tags"]])
        done = self.run_cli(self.work, "exports/meta-ads.example.csv", "--tags", "tags.csv")
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads((self.work / "out" / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue([c for c in summary["campaigns"] if "audience" in c["tags"]])


def read_text(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def code_blocks(text, lang):
    """The contents of every fenced block with this language tag ('' for none)."""
    blocks, current, current_lang = [], None, None
    for line in text.splitlines(keepends=True):
        fence = line.strip()
        if fence.startswith("```"):
            if current is None:
                current, current_lang = [], fence[3:]
            else:
                if current_lang == lang:
                    blocks.append("".join(current))
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


class DocsTests(unittest.TestCase):
    """The README and SKILL.md are public. These keep them true to the code."""

    @classmethod
    def setUpClass(cls):
        cls.readme, cls.skill = read_text("README.md"), read_text("SKILL.md")
        cls.sample, cls.summary_text = read_text("examples", "sample-report.md"), \
            read_text("examples", "sample-summary.md")

    def test_readme_run_uses_default_output_and_outputs_are_ignored(self):
        runs = [line for line in self.readme.splitlines() if "scripts/report.py exports/" in line]
        self.assertTrue(runs)
        self.assertFalse([line for line in runs if "--out" in line])
        self.assertIn("out/report.md", self.readme)
        self.assertIn("out/summary.json", self.readme)
        ignored = read_text(".gitignore").splitlines()
        for entry in ("out/", "/exports/", "/report.md", "/summary.json"):
            self.assertIn(entry, ignored)

    def test_readme_tags_step_passes_the_flag(self):
        self.assertIn("python3 scripts/report.py exports/*.csv --tags tags.csv", self.readme)

    def test_readme_excerpt_is_a_faithful_part_of_the_sample(self):
        self.assertIn("](examples/sample-report.md)", self.readme)
        excerpt = [b for b in code_blocks(self.readme, "text") if b.startswith("Look at this first")][0]
        self.assertIn(excerpt, self.sample)
        self.assertIn("| --- | ---: | ---: | ---: |", excerpt)

    def test_readme_shows_the_start_of_the_sample_summary(self):
        self.assertIn("](examples/sample-summary.md)", self.readme)
        first = self.summary_text.splitlines()[0]
        self.assertTrue([b for b in code_blocks(self.readme, "text") if b.startswith(first)])

    def test_sample_summary_follows_the_skill_rules(self):
        text = self.summary_text
        self.assertLessEqual(len(text.split()), 200)
        self.assertEqual(text.count("Recommendation:"), 1)
        self.assertIn("doesn't show why", text)
        self.assertNotIn("\u2014", text)
        self.assertTrue(text.startswith("Data notes"))
        numbers = re.findall(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?[%x]?", text)
        self.assertTrue(numbers)
        for number in numbers:
            self.assertIn(number.rstrip(","), self.sample)

    def test_skill_command_uses_placeholders_and_stops_on_errors(self):
        self.assertNotIn("--week-ending 2026-03-15", self.skill)
        self.assertIn("--week-ending <YYYY-MM-DD>", self.skill)
        self.assertIn("exit code", self.skill)
        self.assertIn("Results", self.skill)
        self.assertIn("70 conversions last week and 40 this week", self.skill)

    def test_readme_files_list_is_complete_and_test_count_is_current(self):
        files = [b for b in code_blocks(self.readme, "") if b.startswith("SKILL.md")][0]
        listed = [line.split()[0] for line in files.splitlines() if line.strip()]
        for path in listed:
            self.assertTrue((ROOT / path).exists(), path)
        for path in ("examples/tags.example.csv", "examples/sample-summary.md"):
            self.assertIn(path, listed)
        count = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]).countTestCases()
        self.assertEqual(int(re.search(r"tests/test_report.py\s+(\d+) offline tests", files).group(1)), count)


if __name__ == "__main__":
    unittest.main()
