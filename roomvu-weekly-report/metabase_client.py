import os
import time
import requests

_TIMEOUT = 120
_RETRIES = 2          # heavy cohort cards occasionally exceed the read timeout


def _post(endpoint, headers, body):
    """POST with retries on timeouts / connection errors / 5xx."""
    last = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.post(endpoint, headers=headers, json=body, timeout=_TIMEOUT)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} Server Error for url: {endpoint}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status < 500:
                raise                                  # 4xx is not transient
            if attempt < _RETRIES:
                print(f"  [WARN] Metabase {endpoint.rsplit('/api/', 1)[-1]} attempt {attempt + 1} failed ({str(exc)[:60]}); retrying")
                time.sleep(5 * (attempt + 1))
    raise last


def execute_sql(sql: str, database_id: int, url_env: str, key_env: str) -> list[dict]:
    """Run a native SQL query via POST /api/dataset and return rows as dicts."""
    base_url = os.environ.get(url_env, "").rstrip("/")
    api_key  = os.environ.get(key_env, "")
    if not base_url:
        raise EnvironmentError(f"Environment variable {url_env} is not set")
    if not api_key:
        raise EnvironmentError(f"Environment variable {key_env} is not set")

    endpoint = f"{base_url}/api/dataset"
    resp = _post(endpoint, {"X-API-KEY": api_key, "Content-Type": "application/json"},
                 {"database": database_id, "type": "native", "native": {"query": sql}})
    payload = resp.json()
    # Metabase returns {data: {cols: [...], rows: [...]}}
    data = payload.get("data", {})
    cols = [c["name"] for c in data.get("cols", [])]
    return [dict(zip(cols, row)) for row in data.get("rows", [])]


def fetch_question(question_id: int, url_env: str, key_env: str,
                   parameters: list | None = None) -> list[dict]:
    """Fetch a Metabase card's JSON results via POST /api/card/{id}/query/json."""
    if not question_id:
        raise ValueError("question_id is 0 or unset")

    base_url = os.environ.get(url_env, "").rstrip("/")
    api_key = os.environ.get(key_env, "")

    if not base_url:
        raise EnvironmentError(f"Environment variable {url_env} is not set")
    if not api_key:
        raise EnvironmentError(f"Environment variable {key_env} is not set")

    endpoint = f"{base_url}/api/card/{question_id}/query/json"
    body = {}
    if parameters:
        body["parameters"] = parameters
    resp = _post(endpoint, {"X-API-KEY": api_key, "Content-Type": "application/json"}, body)
    return resp.json()
