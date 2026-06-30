import os
import requests

_TIMEOUT = 60


def execute_sql(sql: str, database_id: int, url_env: str, key_env: str) -> list[dict]:
    """Run a native SQL query via POST /api/dataset and return rows as dicts."""
    base_url = os.environ.get(url_env, "").rstrip("/")
    api_key  = os.environ.get(key_env, "")
    if not base_url:
        raise EnvironmentError(f"Environment variable {url_env} is not set")
    if not api_key:
        raise EnvironmentError(f"Environment variable {key_env} is not set")

    endpoint = f"{base_url}/api/dataset"
    resp = requests.post(
        endpoint,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"database": database_id, "type": "native", "native": {"query": sql}},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
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
    resp = requests.post(
        endpoint,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
