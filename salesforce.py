"""Salesforce callout — POST a deliverable result back to a Salesforce Apex REST resource.

Auth: OAuth 2.0 JWT bearer flow. Credentials come from AWS Secrets Manager (secret
name in LLM_SETTINGS.sf_secret_name, default 'sf/jwt/credentials'). The secret is a
JSON object with:
    client_id    — Connected App consumer key
    username     — integration user
    private_key  — RSA private key PEM (matches the cert uploaded to the Connected App)
    login_url    — https://login.salesforce.com  (or test.salesforce.com for sandbox)

notify() never raises — Salesforce being down must not kill the analysis pipeline.
It is a no-op (returns False) when sf_enabled is false, so the worker runs fine
before Salesforce is wired up.
"""

import json
import threading
import time

import boto3
import requests

try:
    import jwt  # PyJWT
except Exception:  # pragma: no cover - import guard
    jwt = None

_lock = threading.Lock()
_secret = None
_cache = {"access_token": None, "instance_url": None, "exp": 0}


def _now() -> int:
    return int(time.time())


def _load_secret(settings):
    global _secret
    if _secret is None:
        sm = boto3.client("secretsmanager", region_name=settings.aws_region)
        raw = sm.get_secret_value(SecretId=settings.sf_secret_name)["SecretString"]
        _secret = json.loads(raw)
    return _secret


def _get_token(settings):
    """Return (access_token, instance_url), cached until ~1h. Thread-safe."""
    now = _now()
    with _lock:
        if _cache["access_token"] and _cache["exp"] - 60 > now:
            return _cache["access_token"], _cache["instance_url"]

        sec = _load_secret(settings)
        login_url = (sec.get("login_url") or settings.sf_audience).rstrip("/")
        assertion = jwt.encode(
            {
                "iss": sec["client_id"],
                "sub": sec["username"],
                "aud": login_url,
                "exp": now + 180,
            },
            sec["private_key"],
            algorithm="RS256",
        )
        resp = requests.post(
            login_url + "/services/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=settings.sf_timeout,
        )
        resp.raise_for_status()
        d = resp.json()
        _cache.update({
            "access_token": d["access_token"],
            "instance_url": d["instance_url"].rstrip("/"),
            "exp": now + 3600,
        })
        return _cache["access_token"], _cache["instance_url"]


def notify(settings, payload) -> bool:
    """POST `payload` to the Apex REST resource. Returns True on 2xx. Never raises."""
    if not settings.sf_enabled:
        print("[SF] disabled (SF_ENABLED=false), skip")
        return False
    if jwt is None:
        print("[SF] PyJWT not installed — cannot sign JWT")
        return False

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
                with _lock:
                    _cache["access_token"] = None
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if 200 <= r.status_code < 300:
                print(f"[SF] sent {payload.get('deliverableResultId')} → {payload.get('result')}")
                return True
            print(f"[SF] HTTP {r.status_code}: {(r.text or '')[:300]}")
            return False
        except Exception as exc:
            print(f"[SF] error: {exc}")
            time.sleep(2 ** attempt)
    print(f"[SF] giving up after retries: {payload.get('deliverableResultId')}")
    return False
