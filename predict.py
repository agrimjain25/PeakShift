"""HackSphere 3.0 - demand forecasting client.

Calls the published Zoho Catalyst QuickML endpoint and returns one predicted
demand (kW) per input row.

Usage:
    python predict.py --input sample_test_input.csv --output predictions.csv

Credentials are NEVER hard-coded. They are read from environment variables or
from a local ".env" file next to this script (see .env.example):

    QUICKML_ENDPOINT_KEY   endpoint key shown in the QuickML endpoint details
    CATALYST_ORG           Catalyst org id
    QUICKML_ENVIRONMENT    "Development" (default) or "Production"
    ZOHO_CLIENT_ID         \
    ZOHO_CLIENT_SECRET      } used to get a fresh OAuth access token
    ZOHO_REFRESH_TOKEN     /  (scope QuickML.deployment.READ)
    QUICKML_ACCESS_TOKEN   optional: a ready access token (expires in ~1 hour)
    ZOHO_ACCOUNTS_URL      default https://accounts.zoho.in (India data centre)
    QUICKML_ENDPOINT_URL   optional override of the endpoint URL below
    ALLOW_FALLBACK         set to 1 to use a rough formula if the API fails
    PEAKSHIFT_RELAY_URL    optional override of the relay address (see below)
    PEAKSHIFT_RELAY        set to 0 to turn the relay off

If NO credentials are found, predict.py does not stop: it sends the model features to the public PeakShift app
(Catalyst AppSail), which holds the secrets and calls the QuickML endpoint for it. Same model, same numbers.
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

# Published QuickML endpoint (India data centre). Not a secret.
QUICKML_ENDPOINT_URL = "https://api.catalyst.zoho.in/quickml/v1/project/105503000000013052/endpoints/predict"

# Public PeakShift app on Catalyst AppSail. If this machine has no Zoho credentials, predict.py sends the
# model features here and the app (which holds the secrets) calls the QuickML endpoint. Not a secret.
RELAY_URL = "https://peakshift-50047164924.development.catalystappsail.in/api/predict"
RELAY_CHUNK = 100
RELAY_PARALLEL = 3

REQUIRED_COLUMNS = ["timestamp", "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]
WEATHER_COLUMNS = ["ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]
# Same features, same order as the training file (train_enriched.csv)
MODEL_FEATURES = [
    "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2",
    "hour", "day_of_week", "is_peak", "is_morning_rush",
]

# Typical values from the training data, used only if a weather value is missing
# and cannot be filled from neighbouring hours.
DEFAULT_WEATHER = {"ambient_temp_c": 18.5, "humidity_pct": 70.8, "solar_irradiance_wm2": 0.0}

MAX_WORKERS = 8
TIMEOUT = 30
RETRIES = 3

_token_cache = {"token": None, "expires": 0.0}
_token_lock = threading.Lock()


# ---------------------------------------------------------------- config ----
def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _cfg(name, default=None):
    _load_dotenv()
    return os.environ.get(name) or (os.environ.get("QUICKML_ORG_ID") if name == "CATALYST_ORG" else None) or default


# ------------------------------------------------------- data preparation ---
def clean_and_build_features(input_df: pd.DataFrame) -> pd.DataFrame:
    """Validate, clean and turn raw telemetry into the model's feature table."""
    missing = [c for c in REQUIRED_COLUMNS if c not in input_df.columns]
    if missing:
        raise ValueError(f"Input is missing required column(s): {missing}")

    df = input_df.copy()
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().any():
        bad = int(ts.isna().sum())
        raise ValueError(f"{bad} row(s) have an unreadable timestamp")

    for col in WEATHER_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        df[col] = df[col].interpolate(limit_direction="both")  # fill gaps from neighbours
        df[col] = df[col].fillna(DEFAULT_WEATHER[col])
    df["humidity_pct"] = df["humidity_pct"].clip(0, 100)
    df["solar_irradiance_wm2"] = df["solar_irradiance_wm2"].clip(lower=0)

    hour = ts.dt.hour
    df["hour"] = hour
    df["day_of_week"] = ts.dt.dayofweek  # Monday = 0, same as training
    df["is_peak"] = hour.between(18, 22).astype(int)
    df["is_morning_rush"] = hour.between(6, 9).astype(int)
    return df[MODEL_FEATURES].reset_index(drop=True)


# ----------------------------------------------------------------- auth -----
def _access_token(force_refresh=False, stale=None):
    """Return a cached access token; fetch a new one only when needed.

    Zoho limits how many tokens can be generated, so only one thread refreshes
    at a time and the others reuse the result.
    """
    with _token_lock:
        return _access_token_locked(force_refresh, stale)


def _access_token_locked(force_refresh, stale):
    still_valid = _token_cache["token"] and time.time() < _token_cache["expires"]
    if still_valid and not (force_refresh and _token_cache["token"] == stale):
        return _token_cache["token"]

    refresh = _cfg("ZOHO_REFRESH_TOKEN")
    client_id = _cfg("ZOHO_CLIENT_ID")
    client_secret = _cfg("ZOHO_CLIENT_SECRET")
    if refresh and client_id and client_secret:
        accounts = _cfg("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
        resp = requests.post(
            f"{accounts}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if "access_token" not in body:
            raise RuntimeError(f"Token refresh failed: {body}")
        _token_cache["token"] = body["access_token"]
        _token_cache["expires"] = time.time() + int(body.get("expires_in", 3600)) - 120
        return _token_cache["token"]

    static = _cfg("QUICKML_ACCESS_TOKEN")
    if static:
        return static
    raise RuntimeError(
        "No credentials found. Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and "
        "ZOHO_REFRESH_TOKEN (or QUICKML_ACCESS_TOKEN) in the environment or .env file."
    )


def _headers(force_refresh=False, stale=None):
    key = _cfg("QUICKML_ENDPOINT_KEY")
    org = _cfg("CATALYST_ORG")
    if not key or not org:
        raise RuntimeError("Set QUICKML_ENDPOINT_KEY and CATALYST_ORG in the environment or .env file.")
    return {
        "X-QUICKML-ENDPOINT-KEY": key,
        "Authorization": f"Zoho-oauthtoken {_access_token(force_refresh, stale)}",
        "CATALYST-ORG": org,
        "Environment": _cfg("QUICKML_ENVIRONMENT", "Development"),
        "Content-Type": "application/json",
    }


# ------------------------------------------------------------- API calls ----
def _post(session, payload):
    """POST with retry. On 401 the token is refreshed once. Other 4xx errors are not retried."""
    url = _cfg("QUICKML_ENDPOINT_URL", QUICKML_ENDPOINT_URL)
    last_error = None
    for attempt in range(RETRIES):
        try:
            headers = _headers()
            resp = session.post(url, json=payload, headers=headers, timeout=TIMEOUT)
            if resp.status_code == 401:
                stale = headers["Authorization"].split(" ", 1)[1]
                resp = session.post(url, json=payload, headers=_headers(True, stale), timeout=TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(0.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            raise RuntimeError(f"QuickML rejected the request: {exc} {exc.response.text[:200]}") from exc
        except requests.RequestException as exc:  # network problems: retry
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"QuickML call failed after {RETRIES} attempts: {last_error}")


def _values(body):
    result = body.get("result")
    if not isinstance(result, list):
        raise ValueError(f"Unexpected response shape: {str(body)[:200]}")
    out = []
    for item in result:
        if isinstance(item, dict):
            item = next(iter(item.values()))
        out.append(float(item))
    return out


def _have_credentials():
    has_auth = bool(_cfg("QUICKML_ACCESS_TOKEN")) or all(
        _cfg(k) for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"))
    return bool(has_auth and _cfg("QUICKML_ENDPOINT_KEY") and _cfg("CATALYST_ORG"))


def _predict_via_relay(records):
    """No local credentials: ask the deployed PeakShift app to call the endpoint for us (order preserved)."""
    url = _cfg("PEAKSHIFT_RELAY_URL", RELAY_URL)
    print("No Zoho credentials found; using the PeakShift relay at " + url, file=sys.stderr)
    session = requests.Session()
    chunks = [records[i:i + RELAY_CHUNK] for i in range(0, len(records), RELAY_CHUNK)]

    def send(chunk):
        last = None
        for attempt in range(RETRIES):
            try:
                resp = session.post(url, json={"features": chunk}, timeout=120)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"Relay rejected the request: HTTP {resp.status_code} {resp.text[:200]}")
                values = [float(v) for v in resp.json()["predictions"]]
                if len(values) != len(chunk):
                    raise RuntimeError("Relay returned the wrong number of predictions.")
                return values
            except requests.RequestException as exc:
                last = exc
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Relay call failed after {RETRIES} attempts: {last}")

    with ThreadPoolExecutor(max_workers=RELAY_PARALLEL) as pool:
        parts = list(pool.map(send, chunks))
    return [v for part in parts for v in part]


def _predict_rows(features: pd.DataFrame):
    records = features.to_dict(orient="records")
    if not _have_credentials() and _cfg("PEAKSHIFT_RELAY", "1") != "0":
        return _predict_via_relay(records)
    session = requests.Session()
    _headers()  # fetch the token once, up front, before any parallel calls

    # 1) Try one batch request (fast). Works only if the endpoint accepts a list.
    if len(records) > 1:
        try:
            values = _values(_post(session, {"data": records}))
            if len(values) == len(records):
                return values
        except Exception:  # noqa: BLE001 - fall back to one call per row
            pass

    # 2) One request per row (always supported), run in parallel.
    def one(rec):
        return _values(_post(session, {"data": rec}))[0]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return list(pool.map(one, records))


def _fallback(input_df):
    hours = pd.to_datetime(input_df["timestamp"]).dt.hour
    base = 120.0 + 30.0 * np.sin(2 * np.pi * (hours - 6) / 24)
    cooling = np.maximum(0.0, pd.to_numeric(input_df["ambient_temp_c"], errors="coerce").fillna(24.0) - 24.0) * 8.5
    return (base + cooling).values


def predict(input_df: pd.DataFrame):
    """Return predicted demand (kW), one value per input row, in input order."""
    features = clean_and_build_features(input_df)
    try:
        values = _predict_rows(features)
    except Exception as exc:  # noqa: BLE001
        if _cfg("ALLOW_FALLBACK") == "1":
            print(f"WARNING: QuickML call failed ({exc}). Using rough fallback formula!", file=sys.stderr)
            return np.array(_fallback(input_df), dtype=float)
        raise
    return np.array(values, dtype=float)


# ------------------------------------------------------------------ CLI -----
def main():
    parser = argparse.ArgumentParser(description="Predict hourly demand via the QuickML endpoint")
    parser.add_argument("--input", default="sample_test_input.csv", help="CSV with timestamp + weather columns")
    parser.add_argument("--output", default="predictions.csv", help="where to write timestamp,target_demand_kw")
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    preds = predict(df)
    out = pd.DataFrame({"timestamp": df["timestamp"], "target_demand_kw": np.round(preds, 4)})
    out.to_csv(args.output, index=False)
    print(f"Wrote {len(out)} predictions to {args.output}")
    print(out.head())


if __name__ == "__main__":
    main()
