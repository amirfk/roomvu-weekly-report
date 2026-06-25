"""Build the Roomvu weekly acquisition & ROI cohort reveal.js deck."""
import os
import sys
import datetime
import yaml
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

from metabase_client import fetch_question
import supermetrics_client as sm

ROOT = Path(__file__).parent
OUTPUT = ROOT.parent / "index.html"  # served from repo root by GitHub Pages


def load_config():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


# ── Metabase / cohort table ───────────────────────────────────────────────────

def _roi_color(pct: float) -> str:
    clamped = max(0.0, min(150.0, pct))
    break_even = 100.0
    if clamped <= break_even:
        t = clamped / break_even
        r = 220
        g = int(20 + t * (220 - 20))
        b = int(20 + t * (180 - 20))
    else:
        t = (clamped - break_even) / (150.0 - break_even)
        r = int(220 - t * (220 - 20))
        g = 210
        b = int(180 + t * (20 - 180))
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    text = "#111" if lum > 140 else "#fff"
    return f"background:rgb({r},{g},{b});color:{text}"


def parse_cohort_rows(rows: list, fixed_cols: list) -> dict:
    if not rows:
        return {"week_cols": [], "parsed": []}
    week_cols = [k for k in rows[0].keys() if k not in fixed_cols]
    parsed = []
    for raw in rows:
        row = {}
        row["week_idx"] = raw.get("week_idx", "")
        row["week"] = raw.get("week", "")
        row["Registration"] = raw.get("Registration", "")
        amount_raw = raw.get("Amount_Spent", "")
        if isinstance(amount_raw, (int, float)):
            row["Amount_Spent"] = str(int(amount_raw))
        else:
            row["Amount_Spent"] = str(amount_raw).replace(",", "") if amount_raw else ""
        try:
            row["Amount_Spent_display"] = f"{int(row['Amount_Spent']):,}"
        except (ValueError, TypeError):
            row["Amount_Spent_display"] = row["Amount_Spent"]

        row["week_cells"] = {}
        for col in week_cols:
            raw_val = raw.get(col, "")
            if raw_val == "" or raw_val is None:
                row["week_cells"][col] = {"text": "", "color": None}
            else:
                if isinstance(raw_val, (int, float)):
                    pct = float(raw_val)
                    if pct < 5:
                        pct *= 100
                else:
                    pct_str = str(raw_val).replace("%", "").strip()
                    try:
                        pct = float(pct_str)
                    except ValueError:
                        row["week_cells"][col] = {"text": str(raw_val), "color": None}
                        continue
                row["week_cells"][col] = {"text": f"{pct:.0f}%", "color": _roi_color(pct)}
        parsed.append(row)
    return {"week_cols": week_cols, "parsed": parsed}


def build_cohort_slide(slide_cfg, url_env, key_env, fixed_cols):
    qid = slide_cfg.get("metabase_question", 0)
    title = slide_cfg["title"]
    if not qid:
        print(f"  [SKIP] '{title}' — question_id not set")
        return {"title": title, "render": "cohort_table", "skipped": True}
    rows = fetch_question(qid, url_env, key_env)
    table = parse_cohort_rows(rows, fixed_cols)
    print(f"  [OK]   '{title}' — {len(table['parsed'])} rows, cols: {table['week_cols']}")
    return {
        "title": title,
        "render": "cohort_table",
        "skipped": False,
        "week_cols": table["week_cols"],
        "rows": table["parsed"],
    }


# ── Meta KPI slides ──────────────────────────────────────────────────────────

def _first_row(rows: list) -> dict:
    return rows[0] if rows else {}


def _fmt_currency(val) -> str:
    try:
        return f"${int(round(float(val))):,}"
    except (TypeError, ValueError):
        return str(val) if val is not None else "—"


def _fmt_number(val) -> str:
    try:
        return f"{int(round(float(val))):,}"
    except (TypeError, ValueError):
        return str(val) if val is not None else "—"


def build_meta_kpi_slide(slide_cfg, url_env, key_env):
    title = slide_cfg["title"]
    qid_reg = slide_cfg.get("metabase_question_registrations", 0)
    qid_spend = slide_cfg.get("metabase_question_spend", 0)

    reg_row = {}
    spend_row = {}
    errors = []

    if qid_reg:
        try:
            reg_row = _first_row(fetch_question(qid_reg, url_env, key_env))
        except Exception as exc:
            errors.append(f"registrations: {exc}")
    if qid_spend:
        try:
            spend_row = _first_row(fetch_question(qid_spend, url_env, key_env))
        except Exception as exc:
            errors.append(f"spend: {exc}")

    merged = {**reg_row, **spend_row}

    def pick(d, *keys):
        for k in keys:
            if k in d:
                return d[k]
        return None

    registrations = pick(merged, "Registrations", "registrations", "Registration", "Count")
    cost          = pick(merged, "Cost", "cost", "Amount_Spent", "Spend")
    cpa           = pick(merged, "CPA", "cpa", "Cost_Per_Acquisition")
    subscriptions = pick(merged, "Subscriptions", "subscriptions", "Subscription")
    revenue       = pick(merged, "Immediate_Revenue", "Im. Revenue", "Im_Revenue", "Revenue", "revenue")

    if cpa is None and registrations and cost:
        try:
            cpa = round(float(cost) / float(registrations), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    kpis = [
        {"label": "Registrations", "value": _fmt_number(registrations)},
        {"label": "Cost",          "value": _fmt_currency(cost)},
        {"label": "CPA",           "value": _fmt_currency(cpa)},
        {"label": "Subscriptions", "value": _fmt_number(subscriptions)},
        {"label": "Im. Revenue",   "value": _fmt_currency(revenue)},
    ]

    # Resolve creative: prefer explicit URL, fall back to file in assets/creatives/
    creative_url = slide_cfg.get("creative_url", "")
    if not creative_url:
        creative_file = slide_cfg.get("creative_file", "")
        if creative_file:
            creative_url = f"roomvu-weekly-report/assets/creatives/{creative_file}"

    print(f"  [OK]   '{title}' — KPIs: {kpis}")
    return {
        "title": title,
        "render": "meta_kpi",
        "platform": slide_cfg.get("platform", "meta"),
        "creative_url": creative_url,
        "kpis": kpis,
        "skipped": False,
        "error": "; ".join(errors) if errors else None,
    }


# ── Supermetrics / chart slides ───────────────────────────────────────────────

def _fetch_chart_data(chart_cfg, url_env=None, key_env=None):
    """Fetch one chart's data. Returns (labels, values)."""
    source = chart_cfg["source"]
    x_field = chart_cfg["x_field"]
    y_field = chart_cfg.get("y_field", "")

    if source == "metabase_ratio":
        # Fetch two questions, join on join_field, compute numerator/denominator * 100
        num_rows = fetch_question(chart_cfg["metabase_question_numerator"], url_env, key_env)
        den_rows = fetch_question(chart_cfg["metabase_question_denominator"], url_env, key_env)
        join_field = chart_cfg.get("join_field", "week_idx")
        num_field  = chart_cfg["numerator_field"]
        den_field  = chart_cfg["denominator_field"]

        den_map = {str(r.get(join_field)): r for r in den_rows}
        labels = []
        values = []
        for row in num_rows:
            key = str(row.get(join_field))
            den_row = den_map.get(key)
            if den_row is None:
                continue
            raw_x = row.get(x_field, "")
            try:
                ratio = float(row[num_field]) / float(den_row[den_field]) * 100
                ratio = round(ratio, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                ratio = 0
            labels.append(str(raw_x))
            values.append(ratio)
        return labels, values

    elif source == "metabase":
        qid = chart_cfg["metabase_question"]
        rows = fetch_question(qid, url_env, key_env)
    else:
        fields = chart_cfg["fields"]
        if source == "google_ads":
            rows = sm.fetch_google_ads(fields, date_range_type="last_year_inc")
        elif source == "linkedin_ads":
            rows = sm.fetch_linkedin_ads(fields, date_range_type="last_year_inc")
        else:
            raise ValueError(f"Unknown chart source: {source}")

    labels = []
    values = []
    for row in rows:
        raw_x = row.get(x_field, "")
        raw_y = row.get(y_field, 0)
        # Format week label
        if "-W" in str(raw_x):
            labels.append(sm.format_week_label(str(raw_x)))
        else:
            labels.append(str(raw_x))
        # Numeric value
        try:
            values.append(round(float(raw_y), 2))
        except (TypeError, ValueError):
            values.append(0)

    return labels, values


def build_chart_slide(slide_cfg, url_env=None, key_env=None):
    title = slide_cfg["title"]
    render = slide_cfg.get("render", "dual_line_chart")
    charts_cfg = slide_cfg.get("charts", [])
    platform_icon = slide_cfg.get("platform_icon", "")

    charts_data = []
    for chart_cfg in charts_cfg:
        try:
            labels, values = _fetch_chart_data(chart_cfg, url_env, key_env)
            charts_data.append({
                "label": chart_cfg["label"],
                "labels": labels,
                "data": values,
                "format": chart_cfg.get("format", "number"),
                "color": chart_cfg.get("color", "#4A90D9"),
            })
            print(f"  [OK]   '{title}' chart '{chart_cfg['label']}' — {len(values)} points")
        except Exception as exc:
            print(f"  [ERR]  '{title}' chart '{chart_cfg['label']}' — {exc}", file=sys.stderr)
            charts_data.append({
                "label": chart_cfg["label"],
                "labels": [],
                "data": [],
                "format": chart_cfg.get("format", "number"),
                "color": chart_cfg.get("color", "#4A90D9"),
                "error": str(exc),
            })

    return {
        "id": slide_cfg.get("id", ""),
        "title": title,
        "render": render,
        "platform": slide_cfg.get("platform", ""),
        "platform_icon": platform_icon,
        "skipped": False,
        "charts": charts_data,
    }


# ── Main build ────────────────────────────────────────────────────────────────

def build():
    cfg = load_config()
    url_env = cfg["metabase_url_env"]
    key_env = cfg["metabase_api_key_env"]
    fixed_cols = cfg["fixed_columns"]

    # Pass account IDs to env so supermetrics_client picks them up
    os.environ.setdefault("GOOGLE_ADS_ACCOUNT_ID", str(cfg.get("google_ads_account_id", "")))
    os.environ.setdefault("LINKEDIN_ADS_ACCOUNT_ID", str(cfg.get("linkedin_ads_account_id", "")))

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    tmpl = env.get_template("deck.html.j2")

    slides_data = []
    for slide_cfg in cfg["slides"]:
        render = slide_cfg.get("render", "cohort_table")
        try:
            if render == "cohort_table":
                slides_data.append(build_cohort_slide(slide_cfg, url_env, key_env, fixed_cols))
            elif render == "meta_kpi":
                slides_data.append(build_meta_kpi_slide(slide_cfg, url_env, key_env))
            elif render in ("dual_line_chart", "line_chart"):
                slides_data.append(build_chart_slide(slide_cfg, url_env, key_env))
            else:
                print(f"  [SKIP] Unknown render type '{render}'")
                slides_data.append({"title": slide_cfg.get("title", "?"), "skipped": True})
        except Exception as exc:
            title = slide_cfg.get("title", "?")
            print(f"  [ERR]  '{title}' — {exc}", file=sys.stderr)
            slides_data.append({"title": title, "render": render, "skipped": True, "error": str(exc)})

    generated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Compute most recently completed Wed–Tue week
    today = datetime.datetime.utcnow().date()
    # week_end = most recent Tuesday (weekday 1)
    days_since_tuesday = (today.weekday() - 1) % 7
    week_end = today - datetime.timedelta(days=days_since_tuesday)
    week_start = week_end - datetime.timedelta(days=6)
    def _fmt_date(d):
        return d.strftime("%-d %b").replace(" 0", " ")  # "17 Jun"
    week_label = f"{_fmt_date(week_start)} - {_fmt_date(week_end)}"
    deck_title = f"Acquisition Report - {week_label}"

    html = tmpl.render(
        deck_title=deck_title,
        week_label=week_label,
        generated=generated,
        slides=slides_data,
        logo_url="https://8y56.mjt.lu/img2/8y56/9273d331-017b-485d-81c4-42366a9f558f/content",
    )

    OUTPUT.write_text(html, encoding="utf-8")
    built = sum(1 for s in slides_data if not s.get("skipped"))
    skipped = len(slides_data) - built
    print(f"\nWrote {OUTPUT}")
    print(f"Build summary: {built} slide(s) built, {skipped} skipped.")


if __name__ == "__main__":
    build()
