"""Offline tests. Run from the repo root: python3 -m unittest discover tests"""
import contextlib
import io
import json
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
                "2026-03-02,G_A_B_US,10,1000,20,--,\n")
        result = self.read("g.csv", text)
        row = result["rows"][0]
        self.assertEqual((row["conversions"], row["revenue"]), (0.0, 0.0))
        self.assertEqual(result["blank_cells"]["conversions"], 1)

    def test_unreadable_number_skips_row(self):
        text = ("Day,Campaign,Cost,Impr.,Clicks\n"
                "2026-03-02,A,10,1000,20\n2026-03-03,B,oops,1000,20\n")
        result = self.read("g.csv", text)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["skipped"]["unreadable number"], 1)

    def test_unknown_columns_reported_and_platform_override(self):
        text = "Day,Campaign,Cost,Mystery\n2026-03-02,A,10,x\n"
        result = self.read("x.csv", text, platform="linkedin")
        self.assertEqual(result["unknown_columns"], ["Mystery"])
        self.assertEqual(result["platform"], "linkedin")

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
                    "current": stats(100.0 * 1, 10000, 100, cpa_conv)}
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


if __name__ == "__main__":
    unittest.main()
