"""Salesforce callout — OAuth client_credentials, then Bearer POST of the result
to an Apex REST endpoint.

Step 1: POST {sf_login_url}/services/oauth2/token
        grant_type=client_credentials&client_id=..&client_secret=..
        -> { access_token, instance_url, token_type: Bearer }
Step 2: POST {instance_url}{sf_apex_path}
        Authorization: Bearer <access_token>
        { "resultId": ..., "status": "Pass"/"Fail", "feedback": ..., "score": ... }

Credentials/URL come from env (.env on the EC2) — never committed.
notify() never raises (SF down must not kill analysis) and is a no-op when disabled.
"""

import threading
import time

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


def notify(settings, result: dict) -> bool:
    """POST the deliverable result to the Apex endpoint. Returns True on 2xx.
    Never raises."""
    if not settings.sf_enabled:
        print("[SF] disabled (SF_ENABLED=false), skip")
        return False

    payload = {
        "resultId": result.get("deliverableResultId"),
        "status": "Pass" if result.get("result") == "PASS" else "Fail",
        "feedback": result.get("reasoning", ""),
        "score": result.get("score"),
    }

    for attempt in range(3):
        try:
            token, instance = _get_token(settings)
            url = instance + settings.sf_apex_path
            r = requests.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                timeout=settings.sf_timeout,
            )
            if r.status_code == 401:          # token stale → drop cache and retry
                _clear_token()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if 200 <= r.status_code < 300:
                print(f"[SF] sent {payload['resultId']} → {payload['status']}")
                return True
            print(f"[SF] HTTP {r.status_code}: {(r.text or '')[:300]}")
            return False
        except Exception as exc:
            print(f"[SF] error: {exc}")
            time.sleep(2 ** attempt)
    print(f"[SF] giving up after retries: {payload['resultId']}")
    return False
