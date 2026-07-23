import os
import requests

SUPERMETRICS_URL = "https://api.supermetrics.com/enterprise/v2/query/data/json"


def _get_api_key():
    key_env = os.environ.get("SUPERMETRICS_API_KEY_ENV", "SUPERMETRICS_API_KEY")
    key = os.environ.get(key_env)
    if not key:
        raise EnvironmentError(f"Missing env var: {key_env}")
    return key


def fetch(ds_id, account_id, fields, date_range_type="this_year",
          start_date=None, end_date=None, settings=None):
    """
    Fetch data from Supermetrics REST API.
    Returns list of dicts keyed by field name.
    Account IDs are normalised to strip dashes (Google Ads format).
    """
    # Google Ads account IDs must have no dashes in API calls
    clean_account = str(account_id).replace("-", "")

    params = {
        "api_key": _get_api_key(),
        "ds_id": ds_id,
        "date_range_type": date_range_type,
    }
    if start_date and end_date:
        params["date_range_type"] = "custom"
        params["start_date"] = start_date
        params["end_date"] = end_date
    if settings:
        params.update(settings)

    # Build query string — arrays use repeated keys
    from urllib.parse import urlencode
    qs_pairs = list(params.items())
    qs_pairs.append(("ds_accounts[]", clean_account))
    for f in fields:
        qs_pairs.append(("fields[]", f))

    resp = requests.get(SUPERMETRICS_URL, params=qs_pairs, timeout=60)
    if not resp.ok:
        # Include request ID header so it can be sent to Supermetrics support
        req_id = resp.headers.get("X-Request-Id", resp.headers.get("X-SM-Request-Id", "n/a"))
        raise ValueError(f"Supermetrics {resp.status_code} (request_id={req_id}): {resp.text[:600]}")
    result = resp.json()

    if result.get("meta", {}).get("status") == "error":
        raise ValueError(f"Supermetrics error: {result}")

    rows = result.get("data", [])
    # Supermetrics includes a header row as row[0] — skip it
    if rows and rows[0] == list(fields):
        rows = rows[1:]
    elif rows and isinstance(rows[0][0], str) and not _looks_like_data(rows[0][0]):
        rows = rows[1:]
    return [dict(zip(fields, row)) for row in rows]


def _looks_like_data(val: str) -> bool:
    """Return True if val looks like actual data (year|week, a date, a number)."""
    import re
    return bool(re.match(r'^\d', val))


def fetch_google_ads(fields, date_range_type="last_year_inc",
                     start_date=None, end_date=None, settings=None):
    account_id = os.environ.get("GOOGLE_ADS_ACCOUNT_ID", "459-407-5026")
    return fetch("AW", account_id, fields, date_range_type, start_date, end_date, settings)


def fetch_linkedin_ads(fields, date_range_type="last_year_inc",
                       start_date=None, end_date=None, settings=None):
    account_id = os.environ.get("LINKEDIN_ADS_ACCOUNT_ID", "508540143")
    return fetch("LI", account_id, fields, date_range_type, start_date, end_date, settings)


def format_week_label(iso_week):
    """
    Convert '2025-W23' → 'June W1' style label.
    Falls back to the raw value if parsing fails.
    """
    import datetime
    try:
        # ISO week: parse Monday of that week
        year, week = iso_week.split("-W")
        monday = datetime.datetime.strptime(f"{year}-W{int(week):02d}-1", "%G-W%V-%u")
        month_abbr = monday.strftime("%b")
        # Which week of the month (1-5)?
        week_of_month = (monday.day - 1) // 7 + 1
        return f"{month_abbr} W{week_of_month}"
    except Exception:
        return iso_week
