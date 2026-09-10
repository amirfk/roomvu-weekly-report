"""Supermetrics REST client with a committed on-disk fallback.

Two layers protect the deck from the monthly row quota (429 API_ROW_QUOTA_EXCEEDED):
  1. Google daily dataset (cache/google_daily_campaign.json): one incremental
     pull per build of Date x Campaignid x Cost; every Google request whose
     fields are within {Date, Yearweekiso, Campaignid, Cost} is answered from
     it, so the deck makes ~1 Google API call per build instead of ~8.
  2. Per-request cache (cache/req_<ds>_<fields>.json): the last successful rows
     for any other request (clicks, LinkedIn). Served when the API refuses.
"""
import os
import json
import hashlib
import datetime
import requests
from pathlib import Path

SUPERMETRICS_URL = "https://api.supermetrics.com/enterprise/v2/query/data/json"
CACHE_DIR = Path(__file__).parent / "cache"
GOOGLE_DAILY = CACHE_DIR / "google_daily_campaign.json"
GOOGLE_DAILY_START = "2025-08-01"
_GOOGLE_DERIVABLE = {"Date", "Yearweekiso", "Campaignid", "Cost"}
_google_daily = None          # per-process memo
_REQ_CACHE_MAX_AGE_DAYS = 90


def _get_api_key():
    key_env = os.environ.get("SUPERMETRICS_API_KEY_ENV", "SUPERMETRICS_API_KEY")
    key = os.environ.get(key_env)
    if not key:
        raise EnvironmentError(f"Missing env var: {key_env}")
    return key


def _clean(account_id):
    return str(account_id).replace("-", "")      # Google Ads IDs carry no dashes in API calls


def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ")


def _looks_like_data(val: str) -> bool:
    import re
    return bool(re.match(r'^\d', val))


def _api_call(ds_id, account, fields, date_range_type, start_date, end_date, settings):
    params = {"api_key": _get_api_key(), "ds_id": ds_id, "date_range_type": date_range_type}
    if start_date and end_date:
        params["date_range_type"] = "custom"
        params["start_date"] = start_date
        params["end_date"] = end_date
    if settings:
        params.update(settings)
    # The API silently truncates at 1000 rows by default.
    params.setdefault("max_rows", 50000)
    qs_pairs = list(params.items())
    qs_pairs.append(("ds_accounts[]", account))
    for f in fields:
        qs_pairs.append(("fields[]", f))
    resp = requests.get(SUPERMETRICS_URL, params=qs_pairs, timeout=60)
    if not resp.ok:
        req_id = resp.headers.get("X-Request-Id", resp.headers.get("X-SM-Request-Id", "n/a"))
        raise ValueError(f"Supermetrics {resp.status_code} (request_id={req_id}): {resp.text[:600]}")
    result = resp.json()
    if result.get("meta", {}).get("status") == "error":
        raise ValueError(f"Supermetrics error: {result}")
    rows = result.get("data", [])
    if rows and rows[0] == list(fields):
        rows = rows[1:]
    elif rows and isinstance(rows[0][0], str) and not _looks_like_data(rows[0][0]):
        rows = rows[1:]
    return [dict(zip(fields, row)) for row in rows]


# ── layer 2: per-request cache ────────────────────────────────────────────────

def _req_key(ds_id, account, fields, date_range_type, start_date, end_date):
    drt = "custom" if (start_date and end_date) else date_range_type
    raw = json.dumps([ds_id, account, list(fields), drt, start_date, end_date])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _req_file(ds_id, fields):
    slug = "_".join(f.lower() for f in fields)
    return CACHE_DIR / f"req_{ds_id}_{slug}.json"


def _req_cache_put(ds_id, account, fields, date_range_type, start_date, end_date, rows):
    path = _req_file(ds_id, fields)
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    key = _req_key(ds_id, account, fields, date_range_type, start_date, end_date)
    store[key] = {"fetched_at": _now(), "date_range_type": date_range_type,
                  "start": start_date, "end": end_date, "rows": rows}
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=_REQ_CACHE_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    store = {k: v for k, v in store.items() if v.get("fetched_at", "9999") >= cutoff}
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(store, indent=0), encoding="utf-8")


def _req_cache_get(ds_id, account, fields, date_range_type, start_date, end_date):
    path = _req_file(ds_id, fields)
    if not path.exists():
        return None
    store = json.loads(path.read_text(encoding="utf-8"))
    return store.get(_req_key(ds_id, account, fields, date_range_type, start_date, end_date))


def fetch(ds_id, account_id, fields, date_range_type="this_year",
          start_date=None, end_date=None, settings=None):
    """Fetch rows (list of dicts keyed by field name); falls back to the last
    successful identical request when the API refuses."""
    account = _clean(account_id)
    try:
        rows = _api_call(ds_id, account, fields, date_range_type, start_date, end_date, settings)
    except Exception as exc:
        hit = _req_cache_get(ds_id, account, fields, date_range_type, start_date, end_date)
        if hit is None:
            raise
        print(f"  [WARN] Supermetrics {ds_id} {list(fields)} unavailable ({str(exc)[:70]}...) "
              f"- using cached rows fetched {hit['fetched_at']}")
        return hit["rows"]
    try:
        _req_cache_put(ds_id, account, fields, date_range_type, start_date, end_date, rows)
    except Exception as exc:                      # cache write must never break a build
        print(f"  [WARN] could not write Supermetrics request cache: {exc}")
    return rows


# ── layer 1: Google daily dataset ─────────────────────────────────────────────

def _load_google_daily():
    if GOOGLE_DAILY.exists():
        return json.loads(GOOGLE_DAILY.read_text(encoding="utf-8"))
    return None


def _ensure_google_daily(account):
    """Refresh (incrementally) and return
    {"start","end","fetched_at","rows":[[date, campaign_id, cost], ...]}."""
    global _google_daily
    if _google_daily is not None:
        return _google_daily
    ds = _load_google_daily()
    today = datetime.date.today()
    if ds and ds.get("rows"):
        fetch_from = (datetime.date.fromisoformat(ds["end"]) - datetime.timedelta(days=3)).isoformat()
    else:
        fetch_from = GOOGLE_DAILY_START
    try:
        new = _api_call("AW", account, ["Date", "Campaignid", "Cost"], "custom",
                        fetch_from, today.isoformat(), None)
        new_rows = [[str(r["Date"])[:10], str(r["Campaignid"]), float(r.get("Cost") or 0)] for r in new]
        old_rows = [r for r in (ds or {}).get("rows", []) if r[0] < fetch_from]
        ds = {"start": (ds or {}).get("start") or fetch_from, "end": today.isoformat(),
              "fetched_at": _now(), "rows": old_rows + new_rows}
        CACHE_DIR.mkdir(exist_ok=True)
        GOOGLE_DAILY.write_text(json.dumps(ds, separators=(",", ":")), encoding="utf-8")
        print(f"  [INFO] Google daily dataset refreshed: {len(new_rows)} rows from {fetch_from}, "
              f"{len(ds['rows'])} total ({ds['start']}..{ds['end']})")
    except Exception as exc:
        if not ds:
            raise
        print(f"  [WARN] Supermetrics Google refresh failed ({str(exc)[:70]}...) "
              f"- using cached dataset through {ds['end']} (fetched {ds.get('fetched_at')})")
    _google_daily = ds
    return ds


def _resolve_range(date_range_type, start_date, end_date):
    today = datetime.date.today()
    if start_date and end_date:
        return start_date, end_date
    if date_range_type == "last_year_inc":
        return f"{today.year - 1}-01-01", today.isoformat()
    if date_range_type == "this_year":
        return f"{today.year}-01-01", today.isoformat()
    return "1900-01-01", today.isoformat()


def _google_from_daily(ds, fields, date_range_type, start_date, end_date):
    s, e = _resolve_range(date_range_type, start_date, end_date)
    agg = {}
    for d, cid, cost in ds["rows"]:
        if d < s or d > e:
            continue
        key = []
        for f in fields:
            if f == "Date":
                key.append(d)
            elif f == "Campaignid":
                key.append(cid)
            elif f == "Yearweekiso":
                y, w, _ = datetime.date.fromisoformat(d).isocalendar()
                key.append(f"{y}|{w:02d}")
        key = tuple(key)
        agg[key] = agg.get(key, 0.0) + cost
    out = []
    for key, cost in sorted(agg.items()):
        row, i = {}, 0
        for f in fields:
            if f == "Cost":
                row[f] = round(cost, 4)
            else:
                row[f] = key[i]
                i += 1
        out.append(row)
    return out


def fetch_google_ads(fields, date_range_type="last_year_inc",
                     start_date=None, end_date=None, settings=None):
    account = _clean(os.environ.get("GOOGLE_ADS_ACCOUNT_ID", "459-407-5026"))
    if set(fields) <= _GOOGLE_DERIVABLE and "Cost" in fields:
        return _google_from_daily(_ensure_google_daily(account), fields,
                                  date_range_type, start_date, end_date)
    return fetch("AW", account, fields, date_range_type, start_date, end_date, settings)


def fetch_linkedin_ads(fields, date_range_type="last_year_inc",
                       start_date=None, end_date=None, settings=None):
    account_id = os.environ.get("LINKEDIN_ADS_ACCOUNT_ID", "508540143")
    # "LIA" = LinkedIn Ads. ("LI" is rejected by the REST API: PARAM_TYPE_INVALID_DATA_SOURCE_ID.)
    return fetch("LIA", account_id, fields, date_range_type, start_date, end_date, settings)


def format_week_label(iso_week):
    """Convert '2025-W23' -> 'Jun W1' style label; raw value if parsing fails."""
    try:
        year, week = iso_week.split("-W")
        monday = datetime.datetime.strptime(f"{year}-W{int(week):02d}-1", "%G-W%V-%u")
        return f"{monday.strftime('%b')} W{(monday.day - 1) // 7 + 1}"
    except Exception:
        return iso_week
