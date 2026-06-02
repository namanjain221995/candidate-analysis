"""Salesforce callout — POST a deliverable result JSON to an Apex REST endpoint.

The request body is the full result.json (which includes the deliverable-result id)
plus the connected-app client credentials, which the Apex resource uses to
authenticate the call:

    { ...result.json fields..., "clientId": "...", "clientSecret": "..." }

Credentials and the endpoint URL come from env (.env on the EC2) — never committed.

notify() never raises — Salesforce being down must not kill the analysis pipeline.
It is a no-op (returns False) when sf_enabled is false.
"""

import time

import requests


def notify(settings, result: dict) -> bool:
    """POST result (+ client credentials) to the Apex endpoint. Returns True on 2xx.
    Never raises."""
    if not settings.sf_enabled:
        print("[SF] disabled (SF_ENABLED=false), skip")
        return False
    if not settings.sf_endpoint:
        print("[SF] no SF_ENDPOINT configured, skip")
        return False

    body = dict(result)
    body["clientId"] = settings.sf_client_id
    body["clientSecret"] = settings.sf_client_secret

    for attempt in range(3):
        try:
            r = requests.post(
                settings.sf_endpoint, json=body,
                headers={"Content-Type": "application/json"},
                timeout=settings.sf_timeout,
            )
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if 200 <= r.status_code < 300:
                print(f"[SF] sent {result.get('deliverableResultId')} → {result.get('result')}")
                return True
            print(f"[SF] HTTP {r.status_code}: {(r.text or '')[:300]}")
            return False
        except Exception as exc:
            print(f"[SF] error: {exc}")
            time.sleep(2 ** attempt)
    print(f"[SF] giving up after retries: {result.get('deliverableResultId')}")
    return False
