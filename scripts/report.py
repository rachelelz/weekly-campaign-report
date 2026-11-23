#!/usr/bin/env python3
"""Weekly campaign report.

Reads CSV exports from ad platforms, compares the latest 7 days with the 7
days before, flags campaigns that moved, compares tags against the median,
and writes a markdown report plus a JSON summary. Python 3.9, standard
library only.

    python3 scripts/report.py exports/*.csv --tags tags.csv --week-ending 2026-03-15
    python3 scripts/report.py --demo
"""
import argparse
import csv
import io
import json
import re
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
NA = "n/a"
METRICS = ("spend", "impressions", "clicks", "conversions", "revenue")
REQUIRED_FIELDS = {"date", "campaign", "spend"}
PLACEHOLDERS = {"", "--", "---", "-", "\u2014", "\u2013", "n/a", "na", "null", "none"}
PLATFORM_NAMES = {"meta": "Meta Ads", "google": "Google Ads", "linkedin": "LinkedIn Ads"}
SHORT_PLATFORM = {"meta": "Meta", "google": "Google", "linkedin": "LinkedIn"}


# ---------------------------------------------------------------- parsing

def parse_number(text):
    """Turn '$1,234.50', '12%', '(5)' into a float. Placeholders like '--' give None.
    Raises ValueError for text that is not a number at all."""
    if text is None:
        return None
    raw = str(text).strip()
    if raw.lower() in PLACEHOLDERS:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^\d.,\-]", "", raw).replace(",", "")
    if not re.search(r"\d", cleaned):
        raise ValueError("cannot read number: %r" % raw)
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError("cannot read number: %r" % raw)
    return -abs(value) if negative else value


def parse_date(text, formats):
    """Return a date, or None when no format matches (for example 'Total: Account')."""
    raw = (text or "").strip()
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def normalize_header(text):
    return " ".join(text.replace("﻿", "").strip().lower().split())


def pct_change(current, previous):
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def safe_div(top, bottom):
    if top is None or not bottom:
        return None
    return top / bottom


# ---------------------------------------------------------------- reading

def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_config(config_dir=CONFIG_DIR):
    config_dir = Path(config_dir)
    return load_json(config_dir / "columns.json"), load_json(config_dir / "thresholds.json")


def decode_bytes(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def map_headers(header, cols):
    """Match a header row to our fields. Returns (field -> column index, unknown headers)."""
    lookup = {a: field for field, aliases in cols["fields"].items() for a in aliases}
    ignore = set(cols.get("ignore", []))
    mapping, unknown = {}, []
    for index, cell in enumerate(header):
        key = normalize_header(cell)
        if key in lookup and lookup[key] not in mapping:
            mapping[lookup[key]] = index
        elif key and key not in ignore and key not in lookup:
            unknown.append(cell.strip())
    return mapping, unknown


def find_header(table, cols):
    """Skip lines above the table (Google Ads adds a title and date range)."""
    for index, row in enumerate(table):
        mapping, unknown = map_headers(row, cols)
        if REQUIRED_FIELDS <= set(mapping):
            return index, mapping, unknown
    return None


def detect_platform(header, cols):
    cells = {normalize_header(c) for c in header}
    scores = {p: len(cells & set(sigs)) for p, sigs in cols["platform_signatures"].items()}
    best = max(scores, key=lambda p: scores[p])
    return best if scores[best] > 0 else "unknown"


def read_export(path, cols, platform=None):
    """Read one CSV export. Returns a dict with rows and notes about what was skipped."""
    path = Path(path)
    result = {"file": path.name, "rows": [], "platform": None, "preamble_lines": 0,
              "unknown_columns": [], "skipped": Counter(), "blank_cells": Counter(),
              "currency": None, "error": None}
    text = decode_bytes(path.read_bytes())
    found = None
    for delimiter in (",", "\t", ";"):
        table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        found = find_header(table, cols)
        if found:
            break
    if not found:
        result["error"] = "no header row with date, campaign and spend columns"
        return result
    start, mapping, unknown = found
    header = table[start]
    result["preamble_lines"] = start
    result["unknown_columns"] = unknown
    result["platform"] = platform or detect_platform(header, cols)
    match = re.search(r"\(([A-Za-z]{3})\)", header[mapping["spend"]])
    result["currency"] = match.group(1).upper() if match else None

    for cells in table[start + 1:]:
        if not any(c.strip() for c in cells):
            continue
        cells = cells + [""] * (len(header) - len(cells))
        day = parse_date(cells[mapping["date"]], cols["date_formats"])
        name = cells[mapping["campaign"]].strip()
        if day is None:
            result["skipped"]["no valid date (total rows, footers)"] += 1
            continue
        if not name or name == "--":
            result["skipped"]["no campaign name"] += 1
            continue
        row = {"platform": result["platform"], "campaign": name, "date": day}
        try:
            for field in METRICS:
                if field not in mapping:
                    row[field] = None
                    continue
                value = parse_number(cells[mapping[field]])
                if value is None:
                    result["blank_cells"][field] += 1
                    value = 0.0
                row[field] = value
        except ValueError:
            result["skipped"]["unreadable number"] += 1
            continue
        result["rows"].append(row)
    return result


def load_tags_csv(path):
    """Return {campaign name (lowercase): {tag: value}} from a tags CSV."""
    table = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        key = next((f for f in (reader.fieldnames or []) if f.strip().lower() == "campaign"), None)
        if key is None:
            raise ValueError("the tags file needs a 'campaign' column")
        for record in reader:
            name = (record.get(key) or "").strip()
            if not name:
                continue
            table[name.lower()] = {
                k.strip().lower(): v.strip() for k, v in record.items()
                if k and k.strip().lower() != "campaign" and v and v.strip()}
    return table


def tags_for(name, tag_table, naming):
    """Tags from the naming convention first, then tags.csv on top."""
    tags = {}
    parts = [p.strip() for p in name.split(naming.get("delimiter", "_"))]
    for tag, position in naming.get("positions", {}).items():
        if position < len(parts) and parts[position]:
            tags[tag] = parts[position]
    tags.update(tag_table.get(name.lower(), {}))
    return tags


# ----------------------------------------------------------- aggregation

def week_windows(week_ending):
    """Current week = the 7 days ending on week_ending. Previous week = the 7 days before."""
    current = (week_ending - timedelta(days=6), week_ending)
    previous = (week_ending - timedelta(days=13), week_ending - timedelta(days=7))
    return current, previous


def in_window(day, window):
    return window[0] <= day <= window[1]


def new_agg():
    agg = {m: None for m in METRICS}
    agg.update(spend=0.0, rev_spend=0.0, days=set())
    return agg


def add_row(agg, row):
    """None means the export has no such column, so it stays None until a row has a value."""
    for field in METRICS:
        if row[field] is not None:
            agg[field] = (agg[field] or 0.0) + row[field]
    if row["revenue"] is not None:
        agg["rev_spend"] += row["spend"]
    if row["spend"] > 0:
        agg["days"].add(row["date"])


def derive(agg):
    """Add CTR, CPC, CPA and ROAS. Anything that cannot be computed stays None (shown as n/a)."""
    out = {m: agg[m] for m in METRICS}
    out["ctr"] = safe_div(agg["clicks"], agg["impressions"])
    out["cpc"] = safe_div(agg["spend"], agg["clicks"])
    out["cpa"] = safe_div(agg["spend"], agg["conversions"])
    out["roas"] = safe_div(agg["revenue"], agg["rev_spend"])
    out["days_active"] = len(agg["days"])
    return out


def group(rows, key_fn):
    groups = {}
    for row in rows:
        add_row(groups.setdefault(key_fn(row), new_agg()), row)
    return groups


def changes(current, previous):
    return {k: pct_change(current[k], previous[k])
            for k in (*METRICS, "ctr", "cpc", "cpa", "roas")}


# --------------------------------------------------------------- formats

class Fmt:
    def __init__(self, symbol="$"):
        self.symbol = symbol

    def money(self, v, dp=0):
        return NA if v is None else "{}{:,.{}f}".format(self.symbol, v, dp)

    def count(self, v):
        if v is None:
            return NA
        return "{:,.0f}".format(v) if abs(v - round(v)) < 1e-9 else "{:,.1f}".format(v)

    def rate(self, v):
        return NA if v is None else "{:.2f}%".format(v * 100)

    def ratio(self, v):
        return NA if v is None else "{:.2f}x".format(v)

    def change(self, v):
        return NA if v is None else "{:+.1f}%".format(v * 100)

    def show(self, kind, v):
        if kind == "money":
            return self.money(v)
        if kind == "money2":
            return self.money(v, 2)
        return getattr(self, kind)(v)


def day_label(day):
    return "%s %d" % (day.strftime("%b"), day.day)


def window_label(window):
    return "%s to %s" % (day_label(window[0]), day_label(window[1]))


def pretty_platform(platform):
    return PLATFORM_NAMES.get(platform, platform.title())


# ---------------------------------------------------------------- movers
# Each rule returns a flag with the numbers that triggered it. "impact" is a
# rough dollar figure used only to rank flags (see pick_look_first).

def make_flag(rule, name, platform, impact, headline, trigger, why):
    return {"rule": rule, "campaign": name, "platform": platform, "impact": round(impact, 2),
            "headline": headline, "trigger": trigger, "why_it_matters": why}


def campaign_flags(name, platform, cur, prev, th, f):
    """Apply the threshold rules to one campaign. cur and prev come from derive()."""
    flags = []
    ch = changes(cur, prev)
    args = (name, platform)
    not_why = " The export does not show why."

    if (cur["conversions"] == 0 and cur["spend"] >= th["zero_conversions_min_spend"]):
        flags.append(make_flag(
            "zero_conversions", *args, cur["spend"],
            "%s spent with no conversions" % f.money(cur["spend"]),
            "0 conversions on %s spend (flag at %s or more)." % (
                f.money(cur["spend"]), f.money(th["zero_conversions_min_spend"])),
            "Last week: %s conversions on %s.%s" % (
                f.count(prev["conversions"]), f.money(prev["spend"]), not_why)))

    both_conv = (cur["conversions"] or 0) >= th["cpa_min_conversions"] and \
                (prev["conversions"] or 0) >= th["cpa_min_conversions"]
    if both_conv and ch["cpa"] is not None:
        counts = "%s and %s conversions (minimum %d each week)" % (
            f.count(cur["conversions"]), f.count(prev["conversions"]), th["cpa_min_conversions"])
        extra = (cur["cpa"] - prev["cpa"]) * cur["conversions"]
        move = "cost per conversion went from %s to %s (%s)" % (
            f.money(prev["cpa"], 2), f.money(cur["cpa"], 2), f.change(ch["cpa"]))
        if ch["cpa"] * 100 >= th["cpa_up_pct"]:
            flags.append(make_flag(
                "cpa_up", *args, extra, move,
                "CPA %s (flag at +%d%%); %s." % (f.change(ch["cpa"]), th["cpa_up_pct"], counts),
                "At this week's %s conversions, the higher rate costs about %s more than last "
                "week's rate would have.%s" % (f.count(cur["conversions"]), f.money(extra), not_why)))
        elif ch["cpa"] * 100 <= -th["cpa_down_pct"]:
            flags.append(make_flag(
                "cpa_down", *args, -extra, move,
                "CPA %s (flag at -%d%%); %s." % (f.change(ch["cpa"]), th["cpa_down_pct"], counts),
                "At this week's %s conversions, the lower rate saves about %s against last "
                "week's rate." % (f.count(cur["conversions"]), f.money(-extra))))

    if prev["spend"] >= th["spend_up_min_prior_spend"] and ch["spend"] is not None \
            and ch["spend"] * 100 >= th["spend_up_pct"]:
        delta = cur["spend"] - prev["spend"]
        flags.append(make_flag(
            "spend_up", *args, delta,
            "spend rose from %s to %s (%s)" % (
                f.money(prev["spend"]), f.money(cur["spend"]), f.change(ch["spend"])),
            "Spend %s (flag at +%d%%); last week's spend was at least %s." % (
                f.change(ch["spend"]), th["spend_up_pct"], f.money(th["spend_up_min_prior_spend"])),
            "%s more went into this campaign than last week." % f.money(delta)))

    enough_views = (cur["impressions"] or 0) >= th["ctr_min_impressions"] and \
                   (prev["impressions"] or 0) >= th["ctr_min_impressions"]
    if enough_views and ch["ctr"] is not None and ch["ctr"] * 100 <= -th["ctr_down_pct"]:
        flags.append(make_flag(
            "ctr_down", *args, 0.0,
            "click-through rate fell from %s to %s (%s)" % (
                f.rate(prev["ctr"]), f.rate(cur["ctr"]), f.change(ch["ctr"])),
            "CTR %s (flag at -%d%%); impressions %s and %s (minimum %s each week)." % (
                f.change(ch["ctr"]), th["ctr_down_pct"], f.count(cur["impressions"]),
                f.count(prev["impressions"]), f.count(th["ctr_min_impressions"])),
            "Fewer clicks per impression this week.%s" % not_why))

    min_spend = th["new_or_stopped_min_spend"]
    if prev["spend"] == 0 and cur["spend"] >= min_spend:
        flags.append(make_flag(
            "new", *args, 0.0,
            "new campaign, %s spent on %d of 7 days" % (f.money(cur["spend"]), cur["days_active"]),
            "No spend last week; spend this week is %s (flag at %s or more)." % (
                f.money(cur["spend"]), f.money(min_spend)),
            "There is no earlier week to compare. So far: %s conversions at a CPA of %s. "
            "Judge it after a full week." % (f.count(cur["conversions"]), f.money(cur["cpa"], 2))))
    if cur["spend"] == 0 and prev["spend"] >= min_spend:
        flags.append(make_flag(
            "stopped", *args, 0.0,
            "no spend this week after %s last week" % f.money(prev["spend"]),
            "Spend %s this week; last week %s (flag at %s or more)." % (
                f.money(0), f.money(prev["spend"]), f.money(min_spend)),
            "It produced %s conversions last week. The export does not say whether it was "
            "paused or ran out of budget." % f.count(prev["conversions"])))
    return flags


RULE_ORDER = ["zero_conversions", "cpa_up", "spend_up", "ctr_down", "cpa_down", "stopped", "new"]


def sort_flags(flags):
    return sorted(flags, key=lambda x: (-x["impact"], RULE_ORDER.index(x["rule"]), x["campaign"]))


def pick_look_first(flags, totals, f):
    """The "Look at this first" rule.

    1. Every flag gets a rough dollar impact: extra cost at this week's volume for a CPA
       rise, spend for a campaign with zero conversions, the spend increase for a spend jump.
       CTR drops, new campaigns and stopped campaigns count as 0, so they only lead when
       nothing carries dollars.
    2. The flag with the largest impact leads. Ties go to the order in RULE_ORDER.
    3. With no flags, the line says so and gives total spend and CPA.
    """
    if flags:
        top = sort_flags(flags)[0]
        return "Look at this first: %s (%s), %s." % (
            top["campaign"], SHORT_PLATFORM.get(top["platform"], top["platform"]), top["headline"])
    cur, ch = totals["current"], totals["change"]
    return ("Look at this first: no campaign crossed a flag threshold. Spend was %s (%s) and CPA "
            "was %s (%s)." % (f.money(cur["spend"]), f.change(ch["spend"]),
                              f.money(cur["cpa"], 2), f.change(ch["cpa"])))


# ---------------------------------------------------------------- medians

def metric_value(cur, metric, th):
    """The campaign's value for the median, or None when its volume is too small."""
    if metric == "CPA":
        enough = (cur["conversions"] or 0) >= th["median_min_conversions"]
        return cur["cpa"] if enough else None
    enough = (cur["impressions"] or 0) >= th["median_min_impressions"]
    return cur["ctr"] if enough else None


def median_comparison(campaigns, th):
    """Median CPA (or CTR when too few campaigns have conversions) per tag value vs overall.

    A tag value is reported only when at least median_min_campaigns_per_tag campaigns
    with enough volume carry it."""
    minimum = th["median_min_campaigns_per_tag"]
    active = [c for c in campaigns if c["current"]["spend"] > 0]
    scored = []
    for metric in ("CPA", "CTR"):
        scored = [(c, metric_value(c["current"], metric, th)) for c in active]
        scored = [(c, v) for c, v in scored if v is not None]
        if len(scored) >= minimum:
            break
    else:
        return None
    lower_is_better = metric == "CPA"
    overall = statistics.median(v for _, v in scored)
    values, totals = {}, Counter()
    for c in active:
        for tag, value in c["tags"].items():
            totals[(tag, value)] += 1
    for c, v in scored:
        for tag, value in c["tags"].items():
            values.setdefault((tag, value), []).append(v)
    groups, too_small = [], []
    for key in sorted(totals):
        counted = values.get(key, [])
        if len(counted) < minimum:
            too_small.append({"tag": key[0], "value": key[1], "campaigns": len(counted),
                              "total_campaigns": totals[key]})
            continue
        median = statistics.median(counted)
        diff = safe_div(median - overall, overall)
        if diff is None or abs(diff) * 100 <= th["median_inline_pct"]:
            verdict = "in line with the median"
        elif (diff < 0) == lower_is_better:
            verdict = "beats the median"
        else:
            verdict = "behind the median"
        groups.append({"tag": key[0], "value": key[1], "campaigns": len(counted),
                       "total_campaigns": totals[key], "median": median,
                       "vs_overall": diff, "verdict": verdict})
    return {"metric": metric, "lower_is_better": lower_is_better, "overall_median": overall,
            "overall_count": len(scored), "min_campaigns": minimum, "groups": groups, "too_small": too_small}


# --------------------------------------------------------------- analysis

def analyze(rows, th, tag_table, week_ending=None):
    """Build the full summary (numbers only) from normalized rows."""
    if not rows:
        raise ValueError("no usable rows found in the exports")
    f = Fmt(th.get("currency_symbol", "$"))
    end = week_ending or max(r["date"] for r in rows)
    cur_w, prev_w = week_windows(end)
    cur_rows = [r for r in rows if in_window(r["date"], cur_w)]
    prev_rows = [r for r in rows if in_window(r["date"], prev_w)]
    notes = []
    outside = len(rows) - len(cur_rows) - len(prev_rows)
    if outside:
        notes.append("%d rows fall outside the two weeks and were left out." % outside)
    for label, window, subset in (("This week", cur_w, cur_rows), ("Last week", prev_w, prev_rows)):
        days = len({r["date"] for r in subset})
        if days < 7:
            notes.append("%s (%s) has data for %d of 7 days." % (label, window_label(window), days))

    total_c = derive(group(cur_rows, lambda r: "all").get("all", new_agg()))
    total_p = derive(group(prev_rows, lambda r: "all").get("all", new_agg()))
    totals = {"current": total_c, "previous": total_p, "change": changes(total_c, total_p)}

    platforms = []
    plat_c = group(cur_rows, lambda r: r["platform"])
    plat_p = group(prev_rows, lambda r: r["platform"])
    for name in sorted(set(plat_c) | set(plat_p), key=lambda p: -(plat_c.get(p) or new_agg())["spend"]):
        c, p = derive(plat_c.get(name) or new_agg()), derive(plat_p.get(name) or new_agg())
        platforms.append({"platform": name, "current": c, "previous": p, "change": changes(c, p)})

    key_fn = lambda r: (r["platform"], r["campaign"])
    camp_c, camp_p = group(cur_rows, key_fn), group(prev_rows, key_fn)
    naming = th.get("tags_from_names", {})
    campaigns, flags = [], []
    for key in sorted(set(camp_c) | set(camp_p)):
        c, p = derive(camp_c.get(key) or new_agg()), derive(camp_p.get(key) or new_agg())
        campaigns.append({"platform": key[0], "campaign": key[1], "tags": tags_for(key[1], tag_table, naming),
                          "current": c, "previous": p, "change": changes(c, p)})
        flags += campaign_flags(key[1], key[0], c, p, th, f)
    campaigns.sort(key=lambda c: -c["current"]["spend"])
    if not prev_rows:  # without a last week, every campaign would look new
        flags = [x for x in flags if x["rule"] != "new"]
        notes.append("There is no data for last week, so new-campaign flags and comparisons are off.")
    flags = sort_flags(flags)

    medians = median_comparison(campaigns, th)
    if medians is None:
        notes.append("Median comparison skipped: fewer than %d campaigns have enough volume." %
                     th["median_min_campaigns_per_tag"])
    else:
        small = ["%s=%s (%d of %d)" % (t["tag"], t["value"], t["campaigns"], t["total_campaigns"])
                 for t in medians["too_small"]]
        if small:
            notes.append("Not enough campaigns to compare (need %d with enough volume; counted of total "
                         "in brackets): %s." % (
                th["median_min_campaigns_per_tag"], ", ".join(small)))
    notes += missing_tag_notes(campaigns)

    return {"week_ending": end.isoformat(),
            "current_window": [d.isoformat() for d in cur_w],
            "previous_window": [d.isoformat() for d in prev_w],
            "look_first": pick_look_first(flags, totals, f),
            "totals": totals, "platforms": platforms, "campaigns": campaigns,
            "movers": flags, "medians": medians, "data_notes": notes}


def missing_tag_notes(campaigns):
    active = [c for c in campaigns if c["current"]["spend"] > 0]
    dims = sorted({t for c in active for t in c["tags"]})
    notes = []
    for dim in dims:
        missing = [c["campaign"] for c in active if dim not in c["tags"]]
        if missing:
            notes.append("No '%s' tag for: %s." % (dim, ", ".join(missing)))
    if not dims:
        notes.append("No tags found. Add a tags file or use a naming convention to get the median comparison.")
    return notes


def file_notes(results, th):
    notes = []
    for r in results:
        if r["error"]:
            notes.append("%s: skipped, %s." % (r["file"], r["error"]))
            continue
        line = "%s: read as %s, %d rows used." % (r["file"], pretty_platform(r["platform"]), len(r["rows"]))
        if r["platform"] == "unknown":
            line += " Platform not detected. Use --platform meta, google or linkedin."
        notes.append(line)
        if r["preamble_lines"]:
            notes.append("%s: %d lines above the header were skipped." % (r["file"], r["preamble_lines"]))
        for reason, count in sorted(r["skipped"].items()):
            notes.append("%s: %d %s skipped, %s." % (
                r["file"], count, "row" if count == 1 else "rows", reason))
        if r["unknown_columns"]:
            notes.append("%s: unknown columns ignored: %s." % (r["file"], ", ".join(r["unknown_columns"])))
        if r["blank_cells"]:
            cells = ", ".join("%s (%d)" % kv for kv in sorted(r["blank_cells"].items()))
            notes.append("%s: blank or '--' cells read as 0 in %s." % (r["file"], cells))
        if r["currency"] and r["currency"] != th["currency_code"]:
            notes.append("%s: the spend header says %s but the report assumes %s." % (
                r["file"], r["currency"], th["currency_code"]))
    notes.append("Currency: all amounts are treated as %s. The tool does not convert currencies." %
                 th["currency_code"])
    notes.append("Each platform counts conversions and revenue by its own rules, so the same "
                 "customer can appear in more than one platform. Read the totals as a sum of "
                 "platform numbers, not as unique conversions.")
    return notes


# ---------------------------------------------------------------- output

def md_table(headers, rows, left=1):
    """Markdown table. The first `left` columns are left-aligned, the rest right-aligned."""
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" if i < left else "---:" for i in range(len(headers))) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


TOTAL_ROWS = [("Spend", "spend", "money"), ("Impressions", "impressions", "count"),
              ("Clicks", "clicks", "count"), ("Conversions", "conversions", "count"),
              ("Revenue", "revenue", "money"), ("CTR", "ctr", "rate"), ("CPC", "cpc", "money2"),
              ("CPA", "cpa", "money2"), ("ROAS", "roas", "ratio")]


def render_markdown(s, f):
    cur_w, prev_w = [date.fromisoformat(d) for d in s["current_window"]], \
                    [date.fromisoformat(d) for d in s["previous_window"]]
    t = s["totals"]
    out = [s["look_first"], "", "# Weekly campaign report", "",
           "Week ending %s. This week is %s, compared with %s. Every figure comes from the exports "
           "you supplied." % (s["week_ending"], window_label(cur_w), window_label(prev_w)), "",
           "## Totals", ""]
    rows = []
    for label, key, kind in TOTAL_ROWS:
        if t["current"][key] is None and t["previous"][key] is None:
            continue
        rows.append([label, f.show(kind, t["current"][key]), f.show(kind, t["previous"][key]),
                     f.change(t["change"][key])])
    out += md_table(["", "This week", "Last week", "Change"], rows)
    no_revenue = [pretty_platform(p["platform"]) for p in s["platforms"] if p["current"]["revenue"] is None]
    out += ["", "Conversions are counted by each platform's own rules, so treat the totals as a "
            "sum of platform numbers."]
    if no_revenue:
        out.append("Revenue and ROAS leave out %s, which has no revenue column in its export." %
                   ", ".join(no_revenue))
    out += ["", "## By platform", ""]
    rows = []
    for p in s["platforms"]:
        c, ch = p["current"], p["change"]
        rows.append([pretty_platform(p["platform"]), f.money(c["spend"]), f.count(c["conversions"]),
                     f.money(c["cpa"], 2), f.rate(c["ctr"]), f.ratio(c["roas"]),
                     f.change(ch["spend"]), f.change(ch["cpa"])])
    out += md_table(["Platform", "Spend", "Conversions", "CPA", "CTR", "ROAS", "Spend vs last week",
                     "CPA vs last week"], rows)

    out += ["", "## Movers", ""]
    if not s["movers"]:
        out.append("No campaign crossed a threshold this week.")
    for m in s["movers"]:
        out += ["- **%s** (%s): %s." % (m["campaign"], SHORT_PLATFORM.get(m["platform"], m["platform"]),
                                         m["headline"]),
                "  - Triggered by: %s" % m["trigger"],
                "  - Why it matters: %s" % m["why_it_matters"]]
    out += ["", "Thresholds and volume guards are in `config/thresholds.json`.", "", "## Campaigns", ""]
    rows = []
    for c in s["campaigns"]:
        cur = c["current"]
        rows.append(["%s (%s)" % (c["campaign"], SHORT_PLATFORM.get(c["platform"], c["platform"])),
                     f.money(cur["spend"]), f.count(cur["conversions"]), f.money(cur["cpa"], 2),
                     f.rate(cur["ctr"]), f.change(c["change"]["cpa"])])
    out += md_table(["Campaign", "Spend", "Conversions", "CPA", "CTR", "CPA vs last week"], rows)

    out += ["", "## What beats the median", ""]
    m = s["medians"]
    if m is None:
        out.append("Not enough campaigns with enough volume for a median comparison.")
    else:
        fmt_kind = "money2" if m["metric"] == "CPA" else "rate"
        better = "Lower" if m["lower_is_better"] else "Higher"
        out += ["Metric: %s for this week. Overall median across %d campaigns: %s. %s is better. "
                "Only campaigns with enough volume count, and a tag value needs at least %s "
                "campaigns to appear." % (m["metric"], m["overall_count"],
                                         f.show(fmt_kind, m["overall_median"]), better,
                                         m["min_campaigns"]), ""]
        rows = [[g["tag"].capitalize(), g["value"], "%d of %d" % (g["campaigns"], g["total_campaigns"]),
                 f.show(fmt_kind, g["median"]), f.change(g["vs_overall"]), g["verdict"]]
                for g in m["groups"]]
        if rows:
            out += md_table(["Tag", "Value", "Campaigns counted", "Median " + m["metric"],
                             "vs overall median", "Result"], rows, left=2)
        else:
            out.append("No tag value has enough campaigns to report.")

    out += ["", "## Data notes", ""] + ["- " + n for n in s["data_notes"]]
    return "\n".join(out) + "\n"


def jsonable(value):
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def run(paths, tags_path=None, week_ending=None, platform=None, config_dir=CONFIG_DIR):
    """Read the exports and return (markdown, summary)."""
    cols, th = load_config(config_dir)
    results = [read_export(p, cols, platform) for p in paths]
    rows = [row for r in results for row in r["rows"]]
    tag_table = load_tags_csv(tags_path) if tags_path else {}
    summary = analyze(rows, th, tag_table, week_ending)
    summary["data_notes"] = file_notes(results, th) + summary["data_notes"]
    return render_markdown(summary, Fmt(th.get("currency_symbol", "$"))), jsonable(summary)


def write_outputs(markdown, summary, report_path, summary_path):
    for path, text in ((report_path, markdown), (summary_path, json.dumps(summary, indent=2) + "\n")):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write a weekly campaign report from CSV exports.")
    parser.add_argument("exports", nargs="*", help="CSV exports from Meta, Google or LinkedIn")
    parser.add_argument("--tags", help="tags CSV: campaign, then one column per tag")
    parser.add_argument("--week-ending", help="last day of the current week, YYYY-MM-DD (default: latest date in the data)")
    parser.add_argument("--platform", choices=sorted(PLATFORM_NAMES), help="force the platform for all files")
    parser.add_argument("--out", help="markdown report path (default: out/report.md)")
    parser.add_argument("--summary", help="JSON summary path (default: out/summary.json)")
    parser.add_argument("--demo", action="store_true", help="run on examples/ and write examples/sample-report.md")
    args = parser.parse_args(argv)

    paths, tags = args.exports, args.tags
    report_path = args.out or ROOT / "out" / "report.md"
    if args.demo:
        paths = sorted(str(p) for p in (ROOT / "examples" / "exports").glob("*.csv"))
        tags = str(ROOT / "examples" / "tags.example.csv")
        report_path = args.out or ROOT / "examples" / "sample-report.md"
    if not paths:
        parser.error("give at least one CSV export, or use --demo")
    week_ending = None
    if args.week_ending:
        try:
            week_ending = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
        except ValueError:
            parser.error("--week-ending must look like 2026-03-15")
    try:
        markdown, summary = run(paths, tags, week_ending, args.platform)
    except (OSError, ValueError) as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    summary_path = args.summary or ROOT / "out" / "summary.json"
    write_outputs(markdown, summary, report_path, summary_path)
    print(summary["look_first"])
    print("Report: %s" % report_path)
    print("Summary: %s" % summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
