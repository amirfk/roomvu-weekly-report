import os
import requests

SUPERMETRICS_URL = "https://api.supermetrics.com/enterprise/v2/query/data/json"


def _get_api_key():
    key_env = os.environ.get("SUPERMETRICS_API_KEY_ENV", "SUPERMETRICS_API_KEY")
    key = os.environ.get(key_env)
    if not key:
        raise EnvironmentError(f"Missing env var: {key_env}")
    return key


def fetch(ds_id, account_id, fields, date_range_type="last_year_inc",
          start_date=None, end_date=None, settings=None):
    """
    Fetch data from Supermetrics REST API.
    Returns list of dicts keyed by field name.
    """
    payload = {
        "api_key": _get_api_key(),
        "ds_id": ds_id,
        "ds_accounts": [account_id],
        "fields": fields,
        "date_range_type": date_range_type,
    }
    if start_date and end_date:
        payload["date_range_type"] = "custom"
        payload["start_date"] = start_date
        payload["end_date"] = end_date
    if settings:
        payload.update(settings)

    # Supermetrics v2 accepts form-encoded POST with PHP-style array keys
    form_data = {}
    for k, v in payload.items():
        if isinstance(v, list):
            for i, item in enumerate(v):
                form_data[f"{k}[{i}]"] = item
        else:
            form_data[k] = v

    resp = requests.post(SUPERMETRICS_URL, data=form_data, timeout=60)
    if not resp.ok:
        raise ValueError(f"Supermetrics {resp.status_code}: {resp.text[:500]}")
    result = resp.json()

    if result.get("meta", {}).get("status") == "error":
        raise ValueError(f"Supermetrics error: {result}")

    rows = result.get("data", [])
    return [dict(zip(fields, row)) for row in rows]


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
