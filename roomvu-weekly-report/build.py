"""Build the Roomvu weekly ROI cohort reveal.js deck."""
import sys
import datetime
import yaml
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

from metabase_client import fetch_question


ROOT = Path(__file__).parent
OUTPUT = ROOT.parent / "index.html"  # served from repo root by GitHub Pages


def load_config():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def parse_rows(rows: list[dict], fixed_cols: list[str]) -> dict:
    """
    Returns:
      week_cols: list of week column names (in original key order)
      parsed:    list of row dicts with typed values + shading info per week cell
    """
    if not rows:
        return {"week_cols": [], "parsed": []}

    # Detect week columns from first row, preserving insertion order
    week_cols = [k for k in rows[0].keys() if k not in fixed_cols]

    parsed = []
    for raw in rows:
        row = {}
        # Fixed columns
        row["week_idx"] = raw.get("week_idx", "")
        row["week"] = raw.get("week", "")
        row["Registration"] = raw.get("Registration", "")
        # Amount_Spent may arrive as a string "6,130" or a numeric 6130.0
        amount_raw = raw.get("Amount_Spent", "")
        if isinstance(amount_raw, (int, float)):
            row["Amount_Spent"] = str(int(amount_raw))
        else:
            row["Amount_Spent"] = str(amount_raw).replace(",", "") if amount_raw else ""
        try:
            row["Amount_Spent_display"] = f"{int(row['Amount_Spent']):,}"
        except (ValueError, TypeError):
            row["Amount_Spent_display"] = row["Amount_Spent"]

        # Week ROI columns — values may be strings "92%" or floats 0.92 / 92.0
        row["week_cells"] = {}
        for col in week_cols:
            raw_val = raw.get(col, "")
            if raw_val == "" or raw_val is None:
                row["week_cells"][col] = {"text": "", "color": None}
            else:
                # Numeric: Metabase may send 0.92 (fraction) or 92.0 (percent)
                if isinstance(raw_val, (int, float)):
                    pct = float(raw_val)
                    # Heuristic: values < 5 are almost certainly fractions (0.92 → 92%)
                    if pct < 5:
                        pct *= 100
                else:
                    pct_str = str(raw_val).replace("%", "").strip()
                    try:
                        pct = float(pct_str)
                    except ValueError:
                        row["week_cells"][col] = {"text": str(raw_val), "color": None}
                        continue
                row["week_cells"][col] = {
                    "text": f"{pct:.0f}%",
                    "color": _roi_color(pct),
                }

        parsed.append(row)

    return {"week_cols": week_cols, "parsed": parsed}


def _roi_color(pct: float) -> str:
    """Map a ROI % to an RGB hex color. Blank cells are handled by callers."""
    # Clamp to [0, 150]
    clamped = max(0.0, min(150.0, pct))
    break_even = 100.0

    if clamped <= break_even:
        # 0% → full red, 100% → neutral yellow
        t = clamped / break_even          # 0..1
        r = 220
        g = int(20 + t * (220 - 20))     # 20 → 220
        b = int(20 + t * (180 - 20))     # 20 → 180
    else:
        # 100% → neutral yellow, 150% → full green
        t = (clamped - break_even) / (150.0 - break_even)  # 0..1
        r = int(220 - t * (220 - 20))    # 220 → 20
        g = 210
        b = int(180 + t * (20 - 180))    # 180 → 20

    # Choose text color for legibility (simple luminance check)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    text = "#111" if lum > 140 else "#fff"
    return f"background:{_rgb(r,g,b)};color:{text}"


def _rgb(r, g, b):
    return f"rgb({r},{g},{b})"


def build():
    cfg = load_config()
    url_env = cfg["metabase_url_env"]
    key_env = cfg["metabase_api_key_env"]
    fixed_cols = cfg["fixed_columns"]

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    tmpl = env.get_template("deck.html.j2")

    slides_data = []
    for slide_cfg in cfg["slides"]:
        qid = slide_cfg["metabase_question"]
        title = slide_cfg["title"]
        if not qid:
            print(f"  [SKIP] '{title}' — question_id is 0, set it in config.yaml")
            slides_data.append({"title": title, "skipped": True})
            continue
        try:
            rows = fetch_question(qid, url_env, key_env)
            table = parse_rows(rows, fixed_cols)
            slides_data.append({
                "title": title,
                "skipped": False,
                "week_cols": table["week_cols"],
                "rows": table["parsed"],
                "row_count": len(table["parsed"]),
            })
            print(f"  [OK]   '{title}' — {len(table['parsed'])} rows, "
                  f"week cols: {table['week_cols']}")
        except Exception as exc:
            print(f"  [ERR]  '{title}' — {exc}", file=sys.stderr)
            slides_data.append({"title": title, "skipped": True, "error": str(exc)})

    generated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = tmpl.render(
        deck_title=cfg["deck"]["title"],
        generated=generated,
        slides=slides_data,
        logo_url="https://8y56.mjt.lu/img2/8y56/9273d331-017b-485d-81c4-42366a9f558f/content",
    )

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"\nWrote {OUTPUT}")
    built = sum(1 for s in slides_data if not s.get("skipped"))
    skipped = len(slides_data) - built
    print(f"Build summary: {built} slide(s) built, {skipped} skipped.")


if __name__ == "__main__":
    build()
