"""Data intake, cleaning, feature building and the QuickML client.

Standard library only. The feature recipe here must match what the model was trained
on (see model/MODEL_CARD.md) and what predict.py sends. tests/test_core.py checks that
the two stay identical.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

# Published QuickML endpoint (India data centre). Not a secret.
QUICKML_ENDPOINT_URL = "https://api.catalyst.zoho.in/quickml/v1/project/105503000000013052/endpoints/predict"

WEATHER_COLUMNS = ["ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]
MODEL_FEATURES = WEATHER_COLUMNS + ["hour", "day_of_week", "is_peak", "is_morning_rush"]

# Plausible physical ranges. Values outside are treated as sensor errors (missing).
VALID_RANGE = {
    "ambient_temp_c": (-50.0, 60.0),
    "humidity_pct": (0.0, 100.0),
    "solar_irradiance_wm2": (0.0, 1500.0),
}
# Used only when a whole column is empty. Averages of the training data.
FALLBACK_VALUE = {"ambient_temp_c": 18.5, "humidity_pct": 70.8, "solar_irradiance_wm2": 0.0}

# In the training data humidity falls about 1.2 percentage points per degree of warming.
# The heatwave slider moves humidity the same way so the what-if rows stay realistic.
HUMIDITY_PER_DEGREE = -1.19

MAX_ROWS = 168  # one week
TIMEOUT_S = 20
MAX_WORKERS = 8

COLUMN_ALIASES = {
    "timestamp": "timestamp", "time": "timestamp", "datetime": "timestamp", "date_time": "timestamp",
    "ambient_temp_c": "ambient_temp_c", "temperature": "ambient_temp_c", "temp": "ambient_temp_c",
    "temp_c": "ambient_temp_c", "ambient_temp": "ambient_temp_c", "temperature_c": "ambient_temp_c",
    "humidity_pct": "humidity_pct", "humidity": "humidity_pct", "humidity_%": "humidity_pct",
    "solar_irradiance_wm2": "solar_irradiance_wm2", "solar_irradiance": "solar_irradiance_wm2",
    "irradiance": "solar_irradiance_wm2", "solar": "solar_irradiance_wm2", "solar_irradiance_w/m2": "solar_irradiance_wm2",
}

TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
              "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
              "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


class IntakeError(ValueError):
    """The uploaded or typed data cannot be used. The message is safe to show to the user."""


class QuickMLError(RuntimeError):
    """The model endpoint could not be reached or rejected the request."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------- settings ---
_dotenv_loaded = False


def _load_dotenv() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    for folder in (HERE, os.path.dirname(HERE)):
        path = os.path.join(folder, ".env")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cfg(name: str, default: Optional[str] = None) -> Optional[str]:
    _load_dotenv()
    return os.environ.get(name) or (os.environ.get("QUICKML_ORG_ID") if name == "CATALYST_ORG" else None) or default


def credentials_configured() -> bool:
    has_auth = bool(cfg("QUICKML_ACCESS_TOKEN")) or all(
        cfg(k) for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"))
    return bool(has_auth and cfg("QUICKML_ENDPOINT_KEY") and cfg("CATALYST_ORG"))


# ----------------------------------------------------------------------- intake ---
def _normalise_header(name: str) -> str:
    key = name.strip().lstrip("﻿").lower().replace(" ", "_").replace("(", "").replace(")", "")
    return COLUMN_ALIASES.get(key, key)


def parse_csv_text(text: str) -> List[Dict[str, str]]:
    """Read CSV text into raw string rows. Accepts comma, semicolon or tab separators."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise IntakeError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise IntakeError("The file is empty.")
    columns = [_normalise_header(h) for h in header]
    missing = [c for c in ["timestamp"] + WEATHER_COLUMNS if c not in columns]
    if missing:
        raise IntakeError("Missing column(s): {0}. Expected: timestamp, ambient_temp_c, humidity_pct, solar_irradiance_wm2.".format(", ".join(missing)))
    rows = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        rows.append({columns[i]: (values[i].strip() if i < len(values) else "") for i in range(len(columns))})
    if not rows:
        raise IntakeError("The file has a header but no data rows.")
    return rows


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value).strip()
    if not text:
        return None
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def _parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", ".") if str(value).count(",") == 1 and "." not in str(value) else str(value).strip()
        if text == "" or text.lower() in ("nan", "null", "none", "na", "n/a", "-"):
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def clean_rows(raw_rows: List[Dict[str, Any]], temp_delta_c: float = 0.0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Validate, sort, de-duplicate and fill gaps. Returns clean rows and a plain-language report."""
    report: Dict[str, Any] = {
        "rows_in": len(raw_rows),
        "rows_used": 0,
        "dropped_bad_timestamp": 0,
        "duplicates_removed": 0,
        "unsorted_fixed": False,
        "invalid_to_missing": {c: 0 for c in WEATHER_COLUMNS},
        "clipped": {c: 0 for c in WEATHER_COLUMNS},
        "filled": {c: 0 for c in WEATHER_COLUMNS},
        "gaps": 0,
        "truncated": 0,
        "what_if_temp_delta_c": temp_delta_c,
        "warnings": [],
    }
    if not raw_rows:
        raise IntakeError("There are no rows to forecast.")

    parsed: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in raw_rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is None:
            report["dropped_bad_timestamp"] += 1
            continue
        parsed.append((ts, row))
    if not parsed:
        raise IntakeError("No row has a readable timestamp. Use the format YYYY-MM-DD HH:MM:SS.")

    order = [t for t, _ in parsed]
    if order != sorted(order):
        report["unsorted_fixed"] = True
        parsed.sort(key=lambda item: item[0])

    unique: List[Tuple[datetime, Dict[str, Any]]] = []
    seen = set()
    for ts, row in parsed:
        if ts in seen:
            report["duplicates_removed"] += 1
            continue
        seen.add(ts)
        unique.append((ts, row))

    if len(unique) > MAX_ROWS:
        report["truncated"] = len(unique) - MAX_ROWS
        unique = unique[:MAX_ROWS]

    values: Dict[str, List[Optional[float]]] = {c: [] for c in WEATHER_COLUMNS}
    for ts, row in unique:
        for col in WEATHER_COLUMNS:
            number = _parse_number(row.get(col))
            if number is not None:
                low, high = VALID_RANGE[col]
                if col == "humidity_pct" and 100.0 < number <= 105.0:
                    number, report["clipped"][col] = 100.0, report["clipped"][col] + 1
                elif col == "solar_irradiance_wm2" and -20.0 <= number < 0.0:
                    number, report["clipped"][col] = 0.0, report["clipped"][col] + 1
                elif number < low or number > high:
                    number = None
                    report["invalid_to_missing"][col] += 1
            values[col].append(number)

    for col in WEATHER_COLUMNS:
        series = values[col]
        known = [i for i, v in enumerate(series) if v is not None]
        if not known:
            report["warnings"].append("Every {0} value is missing, so the training average ({1}) was used.".format(col, FALLBACK_VALUE[col]))
            report["filled"][col] = len(series)
            values[col] = [FALLBACK_VALUE[col]] * len(series)
            continue
        filled = list(series)
        for i, v in enumerate(series):
            if v is not None:
                continue
            report["filled"][col] += 1
            before = max((k for k in known if k < i), default=None)
            after = min((k for k in known if k > i), default=None)
            if before is not None and after is not None:
                frac = (i - before) / float(after - before)
                filled[i] = series[before] + frac * (series[after] - series[before])
            else:
                filled[i] = series[before if before is not None else after]
        values[col] = filled

    clean: List[Dict[str, Any]] = []
    for idx, (ts, _row) in enumerate(unique):
        temp = values["ambient_temp_c"][idx]
        hum = values["humidity_pct"][idx]
        if temp_delta_c:
            temp = temp + temp_delta_c
            hum = min(100.0, max(0.0, hum + HUMIDITY_PER_DEGREE * temp_delta_c))
        clean.append({
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "ambient_temp_c": round(temp, 2),
            "humidity_pct": round(hum, 2),
            "solar_irradiance_wm2": round(values["solar_irradiance_wm2"][idx], 2),
        })

    for a, b in zip(unique, unique[1:]):
        if (b[0] - a[0]).total_seconds() != 3600:
            report["gaps"] += 1
    report["rows_used"] = len(clean)

    notes = report["warnings"]
    if report["dropped_bad_timestamp"]:
        notes.append("{0} row(s) were skipped because the timestamp could not be read.".format(report["dropped_bad_timestamp"]))
    if report["duplicates_removed"]:
        notes.append("{0} duplicate timestamp(s) were removed.".format(report["duplicates_removed"]))
    if report["gaps"]:
        notes.append("{0} step(s) between rows are not exactly one hour.".format(report["gaps"]))
    if report["truncated"]:
        notes.append("Only the first {0} rows were used ({1} extra rows ignored).".format(MAX_ROWS, report["truncated"]))
    if len(clean) != 24:
        notes.append("You gave {0} hour(s). A full forecast window is 24.".format(len(clean)))
    return clean, report


def build_features(clean: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Weather + calendar features, in the same recipe as the training file."""
    out = []
    for row in clean:
        ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
        hour = ts.hour
        out.append({
            "ambient_temp_c": row["ambient_temp_c"],
            "humidity_pct": row["humidity_pct"],
            "solar_irradiance_wm2": row["solar_irradiance_wm2"],
            "hour": hour,
            "day_of_week": ts.weekday(),  # Monday = 0, same as pandas dayofweek
            "is_peak": 1 if 18 <= hour <= 22 else 0,
            "is_morning_rush": 1 if 6 <= hour <= 9 else 0,
        })
    return out


# ------------------------------------------------------------------ preset cache ---
def features_key(features: List[Dict[str, Any]]) -> str:
    blob = json.dumps([[f[k] for k in MODEL_FEATURES] for f in features], separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def load_cache() -> Dict[str, Any]:
    path = os.path.join(HERE, "data", "preset_cache.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"generated_at": None, "entries": {}}


# ---------------------------------------------------------------------- QuickML ---
_token_lock = threading.Lock()
_token = {"value": None, "expires": 0.0}
_batch_supported: Optional[bool] = None


def _http_json(url: str, payload: Optional[Dict[str, Any]], headers: Dict[str, str], form: bool = False) -> Tuple[int, Any]:
    if form:
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        headers = dict(headers, **{"Content-Type": "application/x-www-form-urlencoded"})
    else:
        data = json.dumps(payload).encode("utf-8")
        headers = dict(headers, **{"Content-Type": "application/json"})
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    timeout = float(cfg("QUICKML_TIMEOUT", str(TIMEOUT_S)))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QuickMLError("Could not reach the model endpoint ({0}).".format(getattr(exc, "reason", exc)), "network")


def _access_token(stale: Optional[str] = None) -> str:
    with _token_lock:
        fresh = _token["value"] and time.time() < _token["expires"]
        if fresh and _token["value"] != stale:
            return _token["value"]
        refresh, client_id, secret = cfg("ZOHO_REFRESH_TOKEN"), cfg("ZOHO_CLIENT_ID"), cfg("ZOHO_CLIENT_SECRET")
        if refresh and client_id and secret:
            accounts = cfg("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
            status, body = _http_json(accounts + "/oauth/v2/token", {
                "grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": client_id, "client_secret": secret}, {}, form=True)
            if status != 200 or "access_token" not in body:
                raise QuickMLError("Zoho sign-in failed ({0}). Check the client ID, secret and refresh token.".format(
                    body.get("error", status) if isinstance(body, dict) else status), "auth")
            _token["value"] = body["access_token"]
            _token["expires"] = time.time() + int(body.get("expires_in", 3600)) - 120
            return _token["value"]
        static = cfg("QUICKML_ACCESS_TOKEN")
        if static:
            return static
        raise QuickMLError("The model credentials are not set on the server.", "config")


def _headers(stale: Optional[str] = None) -> Dict[str, str]:
    key, org = cfg("QUICKML_ENDPOINT_KEY"), cfg("CATALYST_ORG")
    if not key or not org:
        raise QuickMLError("The model credentials are not set on the server.", "config")
    return {
        "X-QUICKML-ENDPOINT-KEY": key,
        "Authorization": "Zoho-oauthtoken " + _access_token(stale),
        "CATALYST-ORG": org,
        "Environment": cfg("QUICKML_ENVIRONMENT", "Development") or "Development",
    }


def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = cfg("QUICKML_ENDPOINT_URL", QUICKML_ENDPOINT_URL)
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            headers = _headers()
            status, body = _http_json(url, payload, headers)
            if status == 401:
                stale = headers["Authorization"].split(" ", 1)[1]
                status, body = _http_json(url, payload, _headers(stale))
            if status in (429, 500, 502, 503, 504):
                last = QuickMLError("The model endpoint is busy (HTTP {0}).".format(status), "busy")
                time.sleep(0.5 * (attempt + 1))
                continue
            if status != 200:
                raise QuickMLError("The model endpoint rejected the request (HTTP {0}).".format(status), "rejected")
            return body
        except QuickMLError as exc:
            if exc.kind in ("auth", "config", "rejected"):
                raise
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise last if isinstance(last, QuickMLError) else QuickMLError("The model endpoint did not answer.", "network")


def _values(body: Any) -> List[float]:
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, list):
        raise QuickMLError("The model endpoint sent an unexpected reply.", "rejected")
    out = []
    for item in result:
        if isinstance(item, dict):
            item = next(iter(item.values()))
        out.append(float(item))
    return out


def call_quickml(features: List[Dict[str, Any]]) -> Tuple[List[float], Dict[str, Any]]:
    """Send feature rows to the live endpoint. Tries one batch call, then one call per row."""
    global _batch_supported
    started = time.time()
    _headers()  # fetch the sign-in token once, before any parallel calls
    mode = "per-row"
    values: Optional[List[float]] = None
    if len(features) > 1 and _batch_supported is not False:
        try:
            batch = _values(_post({"data": features}))
            if len(batch) == len(features):
                values, mode, _batch_supported = batch, "batch", True
            else:
                _batch_supported = False
        except QuickMLError as exc:
            if exc.kind == "rejected":
                _batch_supported = False
            elif exc.kind != "network":
                raise
    if values is None:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            values = list(pool.map(lambda f: _values(_post({"data": f}))[0], features))
    for v in values:
        if math.isnan(v) or math.isinf(v):
            raise QuickMLError("The model returned an invalid number.", "rejected")
    return values, {"status": "live", "mode": mode, "latency_ms": int((time.time() - started) * 1000)}


def predict_demand(features: List[Dict[str, Any]]) -> Tuple[List[float], Dict[str, Any]]:
    """Live prediction, or a clearly labelled saved result for a known preset if the endpoint is down."""
    try:
        return call_quickml(features)
    except QuickMLError as exc:
        cache = load_cache()
        entry = cache.get("entries", {}).get(features_key(features))
        if entry and len(entry.get("values", [])) == len(features):
            return [float(v) for v in entry["values"]], {
                "status": "cached", "mode": "saved", "latency_ms": 0,
                "saved_at": cache.get("generated_at"), "reason": str(exc)}
        raise
