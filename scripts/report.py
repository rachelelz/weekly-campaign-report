#!/usr/bin/env python3
"""Weekly campaign report.

Reads CSV exports from ad platforms, compares the latest 7 days with the 7
days before, flags campaigns that moved, compares tags against the median,
and writes a markdown report plus a JSON summary. Python 3.9, standard
library only.

    python3 scripts/report.py exports/*.csv --tags tags.csv --week-ending 2026-03-15
    python3 scripts/report.py --demo

Relative paths (exports, --tags, --out) resolve against the folder you run
the command from. Only --demo reads and writes inside the tool's own folder.

Exit codes: 0 when the report is written, 1 when a file can't be read or has
no usable rows, 2 when the options or exports can't give an honest report
(the one-line message says what to change).
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
DERIVED = ("ctr", "cpc", "cpa", "roas")
REQUIRED_FIELDS = {"date", "campaign", "spend"}
PLACEHOLDERS = {"", "--", "---", "-", "\u2014", "\u2013", "n/a", "na", "null", "none"}
PLATFORM_NAMES = {"meta": "Meta Ads", "google": "Google Ads", "linkedin": "LinkedIn Ads"}
SHORT_PLATFORM = {"meta": "Meta", "google": "Google", "linkedin": "LinkedIn"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAILY_EXPORT_HINTS = {"meta": "in Meta Ads Manager: Breakdown > Time > Day",
                      "google": "in Google Ads: Segment > Time > Day",
                      "linkedin": "in LinkedIn Campaign Manager: the daily time breakdown"}


class InputError(Exception):
    """The options or exports can't give an honest report. The command line exits with 2."""


# ---------------------------------------------------------------- parsing

COMMA_DECIMAL = re.compile(r"\d,\d{1,2}$|\d{1,3}(\.\d{3})+,\d+$")
DOT_DECIMAL = re.compile(r"\d\.\d{1,2}$|\d{1,3}(,\d{3})+\.\d+$")
GROUPED_ONLY = re.compile(r"\d{1,3}([,.]\d{3})+$")
SLASH_DATE = re.compile(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
CURRENCY_SYMBOLS = {"\u20ac": "EUR", "\u00a3": "GBP", "$": "USD"}


def number_text(text):
    """The digits, dots, commas and minus sign of a cell ('\u20ac1.234,56' gives '1.234,56')."""
    return re.sub(r"[^\d.,\-]", "", str(text).strip())


def detect_number_style(cells):
    """Decide how one file writes numbers. Returns 'comma', 'us' or 'ambiguous'.
    'comma' means 1.234,56 (comma decimal). 'ambiguous' means the only evidence is
    values like 1,234, so the US reading is kept."""
    comma = dot = grouped = 0
    for cell in cells:
        cleaned = number_text(cell)
        if COMMA_DECIMAL.search(cleaned):
            comma += 1
        elif DOT_DECIMAL.search(cleaned):
            dot += 1
        elif GROUPED_ONLY.search(cleaned):
            grouped += 1
    if comma and not dot:
        return "comma"
    if comma or grouped and not dot:
        return "ambiguous"
    return "us"


def detect_date_order(cells):
    """Decide how one file writes dates like 03/04/2026. Returns (order, example cell).
    Any first number above 12 (15/03/2026) means 'day' first. Otherwise any second
    number above 12 (3/15/2026) means 'month' first. Slash dates with neither are
    'ambiguous' and read month first. order is None when the file has no slash dates."""
    order, example = None, None
    for cell in cells:
        match = SLASH_DATE.match(cell or "")
        if not match:
            continue
        if int(match.group(1)) > 12:
            return "day", cell.strip()
        if int(match.group(2)) > 12:
            order, example = "month", cell.strip()
        elif order is None:
            order, example = "ambiguous", cell.strip()
    return order, example


def parse_number(text, decimal_comma=False):
    """Turn '$1,234.50', '12%', '(5)' into a float. Placeholders like '--' give None.
    With decimal_comma, '1.234,56' is read as 1234.56. Raises ValueError for text
    that is not a number at all."""
    if text is None:
        return None
    raw = str(text).strip()
    if raw.lower() in PLACEHOLDERS:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = number_text(raw)
    if decimal_comma:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
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
    return " ".join(text.replace("\ufeff", "").strip().lower().split())


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


def match_field(key, lookup, cols):
    """The field a normalized header belongs to, from the aliases or the patterns."""
    if key in lookup:
        return lookup[key]
    for field, patterns in cols.get("patterns", {}).items():
        if any(re.fullmatch(p, key) for p in patterns):
            return field
    return None


def map_headers(header, cols):
    """Match a header row to our fields. Returns (field -> column index, unknown headers)."""
    lookup = {a: field for field, aliases in cols["fields"].items() for a in aliases}
    ignore = set(cols.get("ignore", []))
    mapping, unknown = {}, []
    for index, cell in enumerate(header):
        key = normalize_header(cell)
        field = match_field(key, lookup, cols)
        if field and field not in mapping:
            mapping[field] = index
        elif key and not field and key not in ignore:
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
    cells = set()
    for cell in header:
        key = normalize_header(cell)
        cells.add(key)
        cells.add(re.sub(r"\s*\([a-z]{3}\)$", "", key))  # "amount spent (chf)" counts as "amount spent"
    scores = {p: len(cells & set(sigs)) for p, sigs in cols["platform_signatures"].items()}
    best = max(scores, key=lambda p: scores[p])
    return best if scores[best] > 0 else "unknown"


def column_index(keys, names):
    names = set(names)
    return next((i for i, key in enumerate(keys) if key in names), None)


def long_date(day):
    return "%s %d, %d" % (day.strftime("%B"), day.day, day.year)


def read_export(path, cols, platform=None, decimal_comma=False):
    """Read one CSV export. Returns a dict with rows and notes about what was skipped.
    platform names the platform only when it can't be detected from the header.
    Raises InputError when the export is not broken down by day."""
    path = Path(path)
    result = {"file": path.name, "rows": [], "platform": None, "platform_forced": False,
              "preamble_lines": 0, "unknown_columns": [], "skipped": Counter(), "blank_cells": Counter(),
              "currency": None, "number_style": "us", "forced_decimal_comma": False,
              "date_order": None, "date_example": None, "conversions_column": None,
              "revenue_blank": False, "duplicates": 0, "duplicate_of": [], "error": None}
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
    keys = [normalize_header(c) for c in header]
    result["preamble_lines"] = start
    result["unknown_columns"] = unknown
    detected = detect_platform(header, cols)
    if detected == "unknown" and platform:
        detected, result["platform_forced"] = platform, True
    result["platform"] = detected
    if "conversions" in mapping:
        result["conversions_column"] = header[mapping["conversions"]].strip()
    match = re.search(r"\(([A-Za-z]{3})\)", header[mapping["spend"]])
    result["currency"] = match.group(1).upper() if match else None

    body = [cells + [""] * (len(header) - len(cells)) for cells in table[start + 1:]
            if any(c.strip() for c in cells)]
    order, example = detect_date_order(cells[mapping["date"]] for cells in body)
    result["date_order"], result["date_example"] = order, example
    formats = list(cols["date_formats"])
    if order == "day":
        formats = [f for f in formats if f != "%m/%d/%Y"] + ["%d/%m/%Y"]
    end_index = column_index(keys, cols.get("end_date_columns", []))
    currency_index = column_index(keys, cols.get("currency_columns", []))

    data = []
    for cells in body:
        day = parse_date(cells[mapping["date"]], formats)
        name = cells[mapping["campaign"]].strip()
        if day is None:
            result["skipped"]["no valid date (total rows, footers)"] += 1
            continue
        if not name or name == "--":
            result["skipped"]["no campaign name"] += 1
            continue
        if end_index is not None:
            last = parse_date(cells[end_index], formats)
            if last is not None and last != day:
                hint = DAILY_EXPORT_HINTS.get(result["platform"])
                raise InputError(
                    "%s is not broken down by day: a row for %s runs from %s to %s. Export it again "
                    "with one row per campaign per day%s." % (
                        result["file"], name, day.isoformat(), last.isoformat(),
                        " (%s)" % hint if hint else ""))
        data.append((cells, day, name))

    fields = [f for f in METRICS if f in mapping]
    if decimal_comma:
        style = "comma"
        result["forced_decimal_comma"] = True
    else:
        style = detect_number_style(cells[mapping[f]] for cells, _, _ in data for f in fields)
    result["number_style"] = style
    if not result["currency"] and currency_index is not None:
        codes = sorted({cells[currency_index].strip().upper() for cells, _, _ in data
                        if re.fullmatch(r"[A-Za-z]{3}", cells[currency_index].strip())})
        if codes:
            result["currency"] = "/".join(codes)
    if not result["currency"]:
        symbols = {CURRENCY_SYMBOLS[ch] for cells, _, _ in data
                   for ch in cells[mapping["spend"]] if ch in CURRENCY_SYMBOLS}
        if len(symbols) == 1:
            result["currency"] = symbols.pop()

    for cells, day, name in data:
        row = {"platform": result["platform"], "campaign": name, "date": day, "file": result["file"]}
        blanks = []
        try:
            for field in METRICS:
                if field not in mapping:
                    row[field] = None
                    continue
                value = parse_number(cells[mapping[field]], style == "comma")
                if value is None:
                    blanks.append(field)
                    value = 0.0
                row[field] = value
        except ValueError:
            result["skipped"]["unreadable number"] += 1
            continue
        result["blank_cells"].update(blanks)
        result["rows"].append(row)

    # A revenue column with nothing in it (a lead-gen account, say) is missing, not zero.
    if result["rows"] and "revenue" in mapping and result["blank_cells"]["revenue"] == len(result["rows"]):
        for row in result["rows"]:
            row["revenue"] = None
        del result["blank_cells"]["revenue"]
        result["revenue_blank"] = True
    return result


def drop_duplicates(results):
    """Drop rows that repeat an earlier row exactly (same platform, campaign, date and
    numbers), for example from a second download of the same export. Rows for the same
    campaign and day with different numbers are kept. Returns a warning note for those."""
    seen, sources = {}, {}
    for r in results:
        kept = []
        for row in r["rows"]:
            day_key = (row["platform"], row["campaign"], row["date"])
            full_key = day_key + tuple(row[m] for m in METRICS)
            if full_key in seen:
                r["duplicates"] += 1
                if seen[full_key] not in r["duplicate_of"]:
                    r["duplicate_of"].append(seen[full_key])
                continue
            seen[full_key] = r["file"]
            sources.setdefault(day_key, []).append(r["file"])
            kept.append(row)
        r["rows"] = kept
    clashes = sorted(key for key, files in sources.items() if len(files) > 1)
    if not clashes:
        return []
    platform, campaign, day = clashes[0]
    return ["Check this: %s more than once with different numbers, for example %s (%s) on %s in %s. "
            "Those rows were added together. That is right when an export splits each day, for "
            "example by device. If two exports overlap, remove one and run again." % (
                "1 campaign-day appears" if len(clashes) == 1 else "%d campaign-days appear" % len(clashes),
                campaign, SHORT_PLATFORM.get(platform, platform), day.isoformat(),
                join_list(sorted(set(sources[clashes[0]]))))]


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


def name_tags(name, naming):
    """Tags read from the campaign name with the naming convention in thresholds.json."""
    tags = {}
    parts = [p.strip() for p in name.split(naming.get("delimiter", "_"))]
    for tag, position in naming.get("positions", {}).items():
        if position < len(parts) and parts[position]:
            tags[tag] = parts[position]
    return tags


def tags_for(name, tag_table, naming):
    """Tags from the naming convention first, then tags.csv on top."""
    tags = name_tags(name, naming)
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


def unknown_week():
    """A week the exports don't cover: every value is None, so it shows as n/a, not 0."""
    out = {k: None for k in METRICS + DERIVED}
    out["days_active"] = 0
    return out


def total(rows):
    agg = new_agg()
    for row in rows:
        add_row(agg, row)
    return derive(agg)


def group(rows, key_fn):
    groups = {}
    for row in rows:
        add_row(groups.setdefault(key_fn(row), new_agg()), row)
    return groups


def changes(current, previous):
    return {k: pct_change(current[k], previous[k]) for k in METRICS + DERIVED}


# --------------------------------------------------------------- formats

class Fmt:
    def __init__(self, symbol="$"):
        self.symbol = symbol

    def money(self, v, dp=0):
        if v is None:
            return NA
        text = "{:,.{}f}".format(abs(v), dp)
        sign = "-" if v < 0 and text.strip("0.,") else ""
        return sign + self.symbol + text

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


def join_list(items):
    """'a', 'a and b', 'a, b and c'."""
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


def plural(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def day_label(day):
    return "%s %d" % (day.strftime("%b"), day.day)


def window_label(window):
    return "%s to %s" % (day_label(window[0]), day_label(window[1]))


def pretty_platform(platform):
    return PLATFORM_NAMES.get(platform, platform.title())


# ---------------------------------------------------------------- movers
# Each rule returns a flag with the numbers that triggered it. "impact" is a
# rough dollar figure used only to rank flags (see pick_look_first).
# A week the exports don't cover has spend None, so it never counts as a new
# or stopped campaign.

def make_flag(rule, name, platform, impact, headline, trigger, why):
    return {"rule": rule, "campaign": name, "platform": platform, "impact": round(impact, 2),
            "headline": headline, "trigger": trigger, "why_it_matters": why}


def campaign_flags(name, platform, cur, prev, th, f):
    """Apply the threshold rules to one campaign. cur and prev come from derive() or unknown_week()."""
    flags = []
    ch = changes(cur, prev)
    args = (name, platform)
    not_why = " The export does not show why."
    cur_spend, prev_spend = cur["spend"] or 0.0, prev["spend"] or 0.0

    if cur["conversions"] == 0 and cur_spend >= th["zero_conversions_min_spend"]:
        if prev["spend"] is None:
            last = "There is no data for last week to compare."
        else:
            last = "Last week: %s conversions on %s." % (f.count(prev["conversions"]), f.money(prev["spend"]))
        flags.append(make_flag(
            "zero_conversions", *args, cur_spend,
            "%s spent with no conversions" % f.money(cur_spend),
            "0 conversions on %s spend (flag at %s or more)." % (
                f.money(cur_spend), f.money(th["zero_conversions_min_spend"])),
            last + not_why))

    both_conv = (cur["conversions"] or 0) >= th["cpa_min_conversions"] and \
                (prev["conversions"] or 0) >= th["cpa_min_conversions"]
    if both_conv and ch["cpa"] is not None:
        counts = "%s conversions last week and %s this week (minimum %d each week)" % (
            f.count(prev["conversions"]), f.count(cur["conversions"]), th["cpa_min_conversions"])
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

    if prev_spend >= th["spend_up_min_prior_spend"] and ch["spend"] is not None \
            and ch["spend"] * 100 >= th["spend_up_pct"]:
        delta = cur_spend - prev_spend
        flags.append(make_flag(
            "spend_up", *args, delta,
            "spend rose from %s to %s (%s)" % (
                f.money(prev_spend), f.money(cur_spend), f.change(ch["spend"])),
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
            "CTR %s (flag at -%d%%); %s impressions last week and %s this week (minimum %s each "
            "week)." % (f.change(ch["ctr"]), th["ctr_down_pct"], f.count(prev["impressions"]),
                        f.count(cur["impressions"]), f.count(th["ctr_min_impressions"])),
            "Fewer clicks per impression this week.%s" % not_why))

    min_spend = th["new_or_stopped_min_spend"]
    if prev["spend"] == 0 and cur_spend >= min_spend:
        flags.append(make_flag(
            "new", *args, 0.0,
            "new campaign, %s spent on %d of 7 days" % (f.money(cur_spend), cur["days_active"]),
            "No spend last week; spend this week is %s (flag at %s or more)." % (
                f.money(cur_spend), f.money(min_spend)),
            "There is no earlier week to compare. So far: %s conversions at a CPA of %s. "
            "Judge it after a full week." % (f.count(cur["conversions"]), f.money(cur["cpa"], 2))))
    if cur["spend"] == 0 and prev_spend >= min_spend:
        flags.append(make_flag(
            "stopped", *args, 0.0,
            "no spend this week after %s last week" % f.money(prev_spend),
            "Spend %s this week; last week %s (flag at %s or more)." % (
                f.money(0), f.money(prev_spend), f.money(min_spend)),
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


def spent_this_week(campaign):
    return (campaign["current"]["spend"] or 0) > 0


def median_comparison(campaigns, th):
    """Median CPA (or CTR when too few campaigns have conversions) per tag value vs overall.

    A tag value is reported only when at least median_min_campaigns_per_tag campaigns
    with enough volume carry it."""
    minimum = th["median_min_campaigns_per_tag"]
    active = [c for c in campaigns if spent_this_week(c)]
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

def source(row):
    """The export a row came from. Coverage is checked per export."""
    return row.get("file") or "the exports"


def left_out_note(left_out, covered):
    one = len(left_out) == 1
    return ("%s %s left out of the totals and the week-over-week changes, because %s %s not cover "
            "both weeks. The totals cover %s." % (
                join_list(pretty_platform(p) for p in left_out), "is" if one else "are",
                "its export" if one else "their exports", "does" if one else "do",
                join_list(pretty_platform(p) for p in covered)))


def analyze(rows, th, tag_table, week_ending=None):
    """Build the full summary (numbers only) from normalized rows.

    Raises InputError when the chosen week has no data."""
    if not rows:
        raise ValueError("no usable rows found in the exports")
    f = Fmt(th.get("currency_symbol", "$"))
    first_day, last_day = min(r["date"] for r in rows), max(r["date"] for r in rows)
    if week_ending and week_ending > last_day:
        raise InputError("--week-ending %s is after the last date in the exports (%s). Use %s or earlier."
                         % (week_ending.isoformat(), last_day.isoformat(), last_day.isoformat()))
    end = week_ending or last_day
    cur_w, prev_w = week_windows(end)
    cur_rows = [r for r in rows if in_window(r["date"], cur_w)]
    prev_rows = [r for r in rows if in_window(r["date"], prev_w)]
    if not cur_rows:
        raise InputError("no rows fall in the week ending %s (%s). The exports run from %s to %s."
                         % (end.isoformat(), window_label(cur_w), first_day.isoformat(),
                            last_day.isoformat()))
    notes = []
    outside = len(rows) - len(cur_rows) - len(prev_rows)
    if outside:
        notes.append("%s outside the two weeks and %s left out." % (
            plural(outside, "row falls", "rows fall"), "was" if outside == 1 else "were"))
    for label, window, subset in (("This week", cur_w, cur_rows), ("Last week", prev_w, prev_rows)):
        days = len({r["date"] for r in subset})
        if days < 7:
            notes.append("%s (%s) has data for %d of 7 days." % (label, window_label(window), days))

    # Coverage per export: an export without last week's rows can't say a campaign is new.
    has_cur, has_prev = {source(r) for r in cur_rows}, {source(r) for r in prev_rows}
    for name in sorted({source(r) for r in rows}):
        if name in has_cur and name not in has_prev:
            notes.append("%s: no data for last week (%s), so its campaigns are not compared with last "
                         "week and none are flagged as new." % (name, window_label(prev_w)))
        elif name in has_prev and name not in has_cur:
            notes.append("%s: no data for this week (%s), so its campaigns are not compared with last "
                         "week and none are flagged as stopped." % (name, window_label(cur_w)))
        elif name not in has_cur:
            notes.append("%s: no rows in either week, so it is not in the report." % name)
    both = has_cur & has_prev
    platform_sources = {}
    for r in cur_rows + prev_rows:
        platform_sources.setdefault(r["platform"], set()).add(source(r))
    comparable = sorted(p for p, names in platform_sources.items() if names <= both)
    left_out = sorted(p for p in platform_sources if p not in comparable)

    if comparable:
        scope = set(comparable)
        total_c = total(r for r in cur_rows if r["platform"] in scope)
        total_p = total(r for r in prev_rows if r["platform"] in scope)
        note = left_out_note(left_out, comparable) if left_out else None
    else:
        scope = set(platform_sources)
        total_c, total_p = total(cur_rows), unknown_week()
        note = "No platform has data for both weeks, so the totals show this week only."
    totals = {"current": total_c, "previous": total_p, "change": changes(total_c, total_p),
              "platforms": sorted(scope), "left_out": left_out if comparable else [], "note": note}
    if note:
        notes.append(note)
    roas_left_out = sorted({r["platform"] for r in cur_rows + prev_rows
                            if r["platform"] in scope and r["revenue"] is None and r["spend"] > 0})

    platforms = []
    plat_c = group(cur_rows, lambda r: r["platform"])
    plat_p = group(prev_rows, lambda r: r["platform"])
    for name in sorted(platform_sources, key=lambda p: (-(plat_c[p]["spend"] if p in plat_c else 0.0), p)):
        c = derive(plat_c[name]) if name in plat_c else unknown_week()
        p = derive(plat_p[name]) if name in plat_p else unknown_week()
        compared = name in comparable
        platforms.append({"platform": name, "current": c, "previous": p, "compared": compared,
                          "change": changes(c, p) if compared else changes(unknown_week(), unknown_week())})

    key_fn = lambda r: (r["platform"], r["campaign"])
    camp_c, camp_p = group(cur_rows, key_fn), group(prev_rows, key_fn)
    camp_sources = {}
    for r in cur_rows + prev_rows:
        camp_sources.setdefault(key_fn(r), set()).add(source(r))
    naming = th.get("tags_from_names", {})
    campaigns, flags, from_names = [], [], set()
    for key in sorted(camp_sources):
        cur_known, prev_known = camp_sources[key] <= has_cur, camp_sources[key] <= has_prev
        c = derive(camp_c[key]) if key in camp_c else (derive(new_agg()) if cur_known else unknown_week())
        p = derive(camp_p[key]) if key in camp_p and prev_known else \
            (derive(new_agg()) if prev_known else unknown_week())
        compare_c = c if cur_known else unknown_week()
        campaigns.append({"platform": key[0], "campaign": key[1], "tags": tags_for(key[1], tag_table, naming),
                          "current": c, "previous": p, "change": changes(compare_c, p)})
        flags += campaign_flags(key[1], key[0], compare_c, p, th, f)
        table_tags = tag_table.get(key[1].lower(), {})
        from_names |= {t for t in name_tags(key[1], naming) if t not in table_tags}
    campaigns.sort(key=lambda c: -(c["current"]["spend"] or 0.0))
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
    if from_names:
        notes.append("Tags read from campaign names: %s (split on '%s', set by tags_from_names in "
                     "config/thresholds.json). A tags file passed with --tags overrides them." % (
                         ", ".join(sorted(from_names)), naming.get("delimiter", "_")))
    notes += missing_tag_notes(campaigns)

    return {"week_ending": end.isoformat(),
            "current_window": [d.isoformat() for d in cur_w],
            "previous_window": [d.isoformat() for d in prev_w],
            "look_first": pick_look_first(flags, totals, f),
            "totals": totals, "platforms": platforms, "campaigns": campaigns,
            "movers": flags, "medians": medians, "roas_left_out": roas_left_out, "data_notes": notes}


def missing_tag_notes(campaigns):
    active = [c for c in campaigns if spent_this_week(c)]
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
        name = r["file"]
        if r["error"]:
            notes.append("%s: skipped, %s." % (name, r["error"]))
            continue
        line = "%s: read as %s%s, %s used." % (
            name, pretty_platform(r["platform"]), " (set with --platform)" if r["platform_forced"] else "",
            plural(len(r["rows"]), "row", "rows"))
        if r["platform"] == "unknown":
            line += (" Platform not detected. Run again with --platform meta, google or linkedin to name "
                     "it. The flag only changes files whose platform is not detected.")
        notes.append(line)
        if r["preamble_lines"]:
            n = r["preamble_lines"]
            notes.append("%s: %s above the header %s skipped." % (
                name, plural(n, "line", "lines"), "was" if n == 1 else "were"))
        for reason, count in sorted(r["skipped"].items()):
            notes.append("%s: %s skipped, %s." % (name, plural(count, "row", "rows"), reason))
        if r["duplicates"]:
            n, earlier = r["duplicates"], join_list(r["duplicate_of"])
            if n == 1:
                text = "1 row was an exact copy of a row in %s" % earlier
            else:
                text = "%d rows were exact copies of rows in %s" % (n, earlier)
            notes.append("%s: %s (same platform, campaign, date and numbers), so %s dropped." % (
                name, text, "it was" if n == 1 else "they were"))
        if r["unknown_columns"]:
            notes.append("%s: unknown columns ignored: %s." % (name, ", ".join(r["unknown_columns"])))
        if r["blank_cells"]:
            cells = ", ".join("%s (%d)" % kv for kv in sorted(r["blank_cells"].items()))
            notes.append("%s: blank or '--' cells read as 0 in %s." % (name, cells))
        if r["revenue_blank"]:
            notes.append("%s: the revenue column is blank in every row, so it is treated as missing and "
                         "this file's spend is left out of ROAS." % name)
        if normalize_header(r["conversions_column"] or "") == "results":
            notes.append("%s: conversions come from Meta's Results column, which counts whatever each "
                         "campaign optimizes for (purchases, leads, link clicks). Check that the "
                         "campaigns you compare count the same result." % name)
        if r["number_style"] == "comma":
            notes.append("%s: numbers read as European style (comma decimal, dot thousands)%s." % (
                name, ", as set by --decimal-comma" if r["forced_decimal_comma"] else ""))
        elif r["number_style"] == "ambiguous":
            notes.append("%s: the number format was ambiguous (values like 1,234 could be either style). "
                         "Assumed US style: comma for thousands, dot for decimals. If this is a European "
                         "export, run again with --decimal-comma." % name)
        if r["date_order"] == "day":
            example = datetime.strptime(r["date_example"], "%d/%m/%Y").date()
            notes.append("%s: dates read day first (%s is %s)." % (name, r["date_example"], long_date(example)))
        elif r["date_order"] == "ambiguous":
            notes.append("%s: dates like %s could be month first or day first. They were read month "
                         "first (US style)." % (name, r["date_example"]))
        if r["currency"] and r["currency"] != th["currency_code"]:
            notes.append("%s: the export's currency is %s, but the report assumes %s." % (
                name, r["currency"], th["currency_code"]))
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
    end = date.fromisoformat(s["week_ending"])
    t = s["totals"]
    out = [s["look_first"], "", "# Weekly campaign report", "",
           "Week ending %s %s. This week is %s, compared with %s. Every figure comes from the exports "
           "you supplied." % (WEEKDAYS[end.weekday()], s["week_ending"], window_label(cur_w),
                              window_label(prev_w)), "",
           "## Totals", ""]
    mismatch = s.get("currency_mismatch")
    if mismatch:
        out[6:6] = ["Check the currency first: %s %s a different currency than %s. This report shows "
                    "every amount as %s and does not convert. Set `currency_code` and `currency_symbol` "
                    "in `config/thresholds.json` if all your files share one currency." % (
                        join_list(mismatch), "uses" if len(mismatch) == 1 else "use",
                        s["currency_code"], s["currency_code"]), ""]
    rows = []
    for label, key, kind in TOTAL_ROWS:
        if t["current"][key] is None and t["previous"][key] is None:
            continue
        rows.append([label, f.show(kind, t["current"][key]), f.show(kind, t["previous"][key]),
                     f.change(t["change"][key])])
    out += md_table(["", "This week", "Last week", "Change"], rows)
    out.append("")
    if t.get("note"):
        out.append(t["note"])
    out.append("Conversions are counted by each platform's own rules, so treat the totals as a "
               "sum of platform numbers.")
    no_revenue = [pretty_platform(p["platform"]) for p in s["platforms"]
                  if p["platform"] in s.get("roas_left_out", [])]
    if no_revenue:
        one = len(no_revenue) == 1
        out.append("Revenue and ROAS leave out %s, which %s no revenue data in %s." % (
            join_list(no_revenue), "has" if one else "have", "its export" if one else "their exports"))
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


def run(paths, tags_path=None, week_ending=None, platform=None, config_dir=CONFIG_DIR,
        decimal_comma=False):
    """Read the exports and return (markdown, summary)."""
    cols, th = load_config(config_dir)
    results = [read_export(p, cols, platform, decimal_comma) for p in paths]
    clash_notes = drop_duplicates(results)
    rows = [row for r in results for row in r["rows"]]
    tag_table = load_tags_csv(tags_path) if tags_path else {}
    summary = analyze(rows, th, tag_table, week_ending)
    summary["data_notes"] = clash_notes + file_notes(results, th) + summary["data_notes"]
    summary["currency_mismatch"] = ["%s (%s)" % (r["file"], r["currency"]) for r in results
                                    if r["currency"] and r["currency"] != th["currency_code"]]
    summary["currency_code"] = th["currency_code"]
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
    parser.add_argument("--platform", choices=sorted(PLATFORM_NAMES),
                        help="platform for files whose platform can't be detected (detected files keep theirs)")
    parser.add_argument("--out", help="markdown report path (default: ./out/report.md in the current folder)")
    parser.add_argument("--summary", help="JSON summary path (default: summary.json next to the report)")
    parser.add_argument("--decimal-comma", action="store_true",
                        help="read numbers as 1.234,56 in every file (European exports)")
    parser.add_argument("--demo", action="store_true", help="run on examples/ and write examples/sample-report.md")
    args = parser.parse_args(argv)

    paths, tags = args.exports, args.tags
    report_path = Path(args.out) if args.out else Path.cwd() / "out" / "report.md"
    summary_path = Path(args.summary) if args.summary else report_path.parent / "summary.json"
    if args.demo:
        paths = sorted(str(p) for p in (ROOT / "examples" / "exports").glob("*.csv"))
        tags = str(ROOT / "examples" / "tags.example.csv")
        report_path = Path(args.out) if args.out else ROOT / "examples" / "sample-report.md"
        summary_path = Path(args.summary) if args.summary else ROOT / "out" / "summary.json"
    if not paths:
        parser.error("give at least one CSV export, or use --demo")
    week_ending = None
    if args.week_ending:
        try:
            week_ending = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
        except ValueError:
            parser.error("--week-ending must look like 2026-03-15")
    try:
        markdown, summary = run(paths, tags, week_ending, args.platform, decimal_comma=args.decimal_comma)
    except InputError as error:
        print("Error: %s" % error, file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    write_outputs(markdown, summary, report_path, summary_path)
    print(summary["look_first"])
    print("Report: %s" % report_path)
    print("Summary: %s" % summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
