"""Salesforce callout — OAuth client_credentials, then Bearer POST of the result
to an Apex REST endpoint.

Step 1: POST {sf_login_url}/services/oauth2/token
        grant_type=client_credentials&client_id=..&client_secret=..
        -> { access_token, instance_url, token_type: Bearer }
Step 2: POST {instance_url}{sf_apex_path}
        Authorization: Bearer <access_token>
        the FULL result.json produced by the LLM stage (score, result, reasoning,
        positives, negatives, deliverable, deliverableResultId, attempt, video).
        This matches what the Apex @RestResource expects (reads deliverableResultId
        and result).

Credentials/URL come from env (.env on the EC2) — never committed.
notify() never raises (SF down must not kill analysis); it returns a log dict
describing the outcome (so the caller can store it next to the result in S3).
"""

import threading
import time
from datetime import datetime, timezone

import requests

_lock = threading.Lock()
_cache = {"access_token": None, "instance_url": None}


def _get_token(settings):
    """Return (access_token, instance_url). Cached until a 401 invalidates it."""
    with _lock:
        if _cache["access_token"]:
            return _cache["access_token"], _cache["instance_url"]

    r = requests.post(
        settings.sf_login_url.rstrip("/") + "/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.sf_client_id,
            "client_secret": settings.sf_client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=settings.sf_timeout,
    )
    r.raise_for_status()
    d = r.json()
    with _lock:
        _cache["access_token"] = d["access_token"]
        _cache["instance_url"] = (d.get("instance_url") or settings.sf_login_url).rstrip("/")
        return _cache["access_token"], _cache["instance_url"]


def _clear_token():
    with _lock:
        _cache["access_token"] = None
        _cache["instance_url"] = None


def notify(settings, result: dict) -> dict:
    """POST the FULL result.json to the Apex endpoint. Never raises.

    Returns a log dict describing the outcome (request sent, Salesforce response,
    HTTP status, success flag, error) so the caller can persist it in S3 next to
    the result.json."""
    log = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enabled": settings.sf_enabled,
        "deliverableResultId": result.get("deliverableResultId"),
        "endpoint": None,
        "success": False,
        "statusCode": None,
        "attempts": 0,
        "request": None,
        "response": None,
        "error": None,
    }

    if not settings.sf_enabled:
        print("[SF] disabled (SF_ENABLED=false), skip")
        log["error"] = "SF_ENABLED=false"
        return log

    # send the FULL result.json exactly as produced by the LLM stage
    payload = result
    log["request"] = payload

    for attempt in range(3):
        log["attempts"] = attempt + 1
        try:
            token, instance = _get_token(settings)
            url = instance + settings.sf_apex_path
            log["endpoint"] = url
            r = requests.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                timeout=settings.sf_timeout,
            )
            log["statusCode"] = r.status_code
            try:
                log["response"] = r.json()
            except Exception:
                log["response"] = (r.text or "")[:1000]

            if r.status_code == 401:          # token stale → drop cache and retry
                _clear_token()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if 200 <= r.status_code < 300:
                log["success"] = True
                print(f"[SF] sent {log['deliverableResultId']} → HTTP {r.status_code}")
                return log
            print(f"[SF] HTTP {r.status_code}: {(r.text or '')[:300]}")
            log["error"] = f"HTTP {r.status_code}"
            return log
        except Exception as exc:
            print(f"[SF] error: {exc}")
            log["error"] = str(exc)
            time.sleep(2 ** attempt)
    print(f"[SF] giving up after retries: {log['deliverableResultId']}")
    if not log["error"]:
        log["error"] = "exhausted retries"
    return log
