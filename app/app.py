"""PeakShift server: web dashboard and JSON API in one process.

Standard library only, so Catalyst AppSail needs no pip install step.
Start command:  python3 -u app.py
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import forecast_core as core
import grid_logic as grid
from store import RunStore

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
MAX_BODY_BYTES = 1_000_000
RELAY_MAX_ROWS = 500
VERSION = "1.0.0"

# Numbers shown on the "model" panel. They come from the QuickML evaluation screen.
MODEL_CARD = {
    "name": "XGBoost regression, trained and published in Zoho Catalyst QuickML",
    "rmse_kw": 9.1286,
    "mae_kw": 7.3162,
    "r2": 0.9667,
    "mape_pct": 5.2054,
    "trained_on": "4,000 hourly rows, 31 Dec 2014 to 16 Jun 2015",
    "features": core.MODEL_FEATURES,
}

STORE = RunStore()
STARTED = time.time()


def load_presets() -> Dict[str, Any]:
    try:
        with open(os.path.join(HERE, "data", "presets.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"columns": [], "presets": []}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_forecast(raw_rows, battery, temp_delta_c, source, request, log=True) -> Dict[str, Any]:
    temp_delta_c = max(-10.0, min(10.0, temp_delta_c))
    clean, report = core.clean_rows(raw_rows, temp_delta_c)
    features = core.build_features(clean)
    demand, meta = core.predict_demand(features)
    sigma = float(core.cfg("FORECAST_SIGMA_KW", str(grid.DEFAULT_SIGMA_KW)) or grid.DEFAULT_SIGMA_KW)
    plan = grid.analyse(clean, demand, battery, sigma)

    run_id = "{0}-{1}".format(datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"), secrets.token_hex(3))
    summary = plan["summary"]
    result = {
        "run_id": run_id,
        "created_at": utc_now(),
        "source": source,
        "model": dict(meta, **{k: MODEL_CARD[k] for k in ("rmse_kw", "mae_kw", "r2")}),
        "cleaning": report,
        **plan,
    }
    result["signals"] = {
        "run_id": run_id,
        "generated_at": result["created_at"],
        "transitions": plan["transitions"],
        "alerts": plan["alerts"],
    }
    if log:
        STORE.last_signals = result["signals"]
        STORE.add({
            "run_id": run_id,
            "created_at": result["created_at"],
            "source": source,
            "rows": len(clean),
            "peak_kw": summary["peak_kw"],
            "peak_time": summary["peak_timestamp"],
            "avg_kw": summary["avg_kw"],
            "overload_hours": summary["overload_hours"],
            "storage_hours": summary["storage_hours"],
            "min_soc_pct": summary["min_soc_pct"],
            "model_status": meta["status"],
            "latency_ms": meta["latency_ms"],
            "temp_delta_c": temp_delta_c,
        }, request)
    result["storage"] = STORE.storage_label
    return result


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


class Handler(BaseHTTPRequestHandler):
    server_version = "PeakShift/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---- plumbing -------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("{0} {1}\n".format(self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                         "script-src 'self'; frame-ancestors 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str, kind: str = "error", extra: Optional[Dict[str, str]] = None) -> None:
        self._send(status, json.dumps({"error": message, "kind": kind}).encode("utf-8"),
                   "application/json; charset=utf-8", dict({"Cache-Control": "no-store"}, **(extra or {})))

    def _read_chunked(self) -> Optional[bytes]:
        """Read a Transfer-Encoding: chunked body (proxies such as AppSail's may send one)."""
        data = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                self.close_connection = True
                self._error(400, "The request body is malformed.", "bad_request", {"Connection": "close"})
                return None
            if size == 0:
                while True:  # skip trailers up to the blank line
                    trailer = self.rfile.readline(65537)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            if len(data) + size > MAX_BODY_BYTES:
                self.close_connection = True
                self._error(413, "That upload is too large. The limit is 1 MB.", "too_large", {"Connection": "close"})
                return None
            data += self.rfile.read(size)
            self.rfile.readline()  # CRLF after each chunk
        return bytes(data)

    def _read_chunked(self) -> Optional[bytes]:
        """Read a Transfer-Encoding: chunked body (some proxies, including AppSail's, send POSTs this way)."""
        data = bytearray()
        too_big = False
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if size == 0:
                while True:  # skip trailers
                    trailer = self.rfile.readline(65537)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunk = self.rfile.read(size)
            self.rfile.readline()
            if len(data) + len(chunk) <= MAX_BODY_BYTES:
                data.extend(chunk)
            else:
                too_big = True
        if too_big:
            self.close_connection = True
            self._error(413, "That upload is too large. The limit is 1 MB.", "too_large", {"Connection": "close"})
            return None
        return bytes(data)

    def _read_body(self) -> Optional[bytes]:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._read_chunked()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            remaining = min(length, 8_000_000)  # read and drop a moderately oversized body so the client can see our answer
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            self._error(413, "That upload is too large. The limit is 1 MB.", "too_large", {"Connection": "close"})
            return None
        return self.rfile.read(length) if length else b""

    # ---- routes ---------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                return self._json(200, {
                    "status": "ok", "version": VERSION, "time": utc_now(),
                    "uptime_s": int(time.time() - STARTED),
                    "missing_settings": [k for k in ("QUICKML_ENDPOINT_KEY", "CATALYST_ORG", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN") if not core.cfg(k)],
                    "model_credentials_configured": core.credentials_configured(),
                    "storage": STORE.storage_label,
                    "datastore_error": STORE.datastore_error,
                })
            if path == "/api/config":
                presets = load_presets()
                return self._json(200, {
                    "version": VERSION,
                    "rules": {
                        "storage_threshold_kw": grid.STORAGE_THRESHOLD_KW,
                        "overload_threshold_kw": grid.OVERLOAD_THRESHOLD_KW,
                        "peak_window": [grid.PEAK_START_HOUR, grid.PEAK_END_HOUR],
                        "sigma_kw": grid.DEFAULT_SIGMA_KW,
                    },
                    "battery_defaults": grid.DEFAULT_BATTERY,
                    "battery_limits": grid.BATTERY_LIMITS,
                    "model": MODEL_CARD,
                    "presets": presets.get("presets", []),
                    "what_if": {"min": -5, "max": 8, "humidity_per_degree": core.HUMIDITY_PER_DEGREE},
                    "model_credentials_configured": core.credentials_configured(),
                })
            if path == "/api/runs":
                return self._json(200, {"runs": STORE.recent(20), "storage": STORE.storage_label})
            if path == "/api/signals/latest":
                if STORE.last_signals is None:
                    return self._error(404, "No forecast has been run yet.", "empty")
                return self._json(200, STORE.last_signals)
            return self._static(path)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "Something went wrong on the server.", "server")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/predict":
            return self._predict_relay()
        if parsed.path != "/api/forecast":
            return self._error(404, "Unknown address.", "not_found")
        body = self._read_body()
        if body is None:
            return
        try:
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type in ("text/csv", "text/plain", "application/csv"):
                query = parse_qs(parsed.query)
                battery = {k: query[k][0] for k in grid.DEFAULT_BATTERY if k in query}
                rows = core.parse_csv_text(body.decode("utf-8-sig", "replace"))
                delta = to_float(query.get("temp_delta_c", ["0"])[0])
                source = "csv"
                log = query.get("log", ["1"])[0] != "0"
            else:
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    return self._error(400, "The request is not valid JSON.", "bad_request")
                rows = payload.get("rows")
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    return self._error(400, "Send a list of rows, each with timestamp, ambient_temp_c, humidity_pct and solar_irradiance_wm2. (The server received {0} bytes.)".format(len(body)), "bad_request")
                battery = payload.get("battery") or {}
                delta = to_float(payload.get("temp_delta_c"))
                source = str(payload.get("source") or "form")[:40]
                log = payload.get("log") is not False
            result = run_forecast(rows, battery, delta, source, self, log)
            return self._json(200, result)
        except core.IntakeError as exc:
            return self._error(400, str(exc), "intake")
        except core.QuickMLError as exc:
            status = 503 if exc.kind in ("network", "busy", "unavailable") else 502
            return self._error(status, str(exc), "model_" + exc.kind)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "Something went wrong on the server.", "server")

    def _predict_relay(self) -> None:
        """Relay for predict.py when the caller has no Zoho credentials: feature rows in, kW out.

        The caller builds the 7 model features; the secrets stay on the server."""
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            rows = payload.get("features") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows or len(rows) > RELAY_MAX_ROWS:
                return self._error(400, "Send {{\"features\": [...]}} with 1 to {0} rows.".format(RELAY_MAX_ROWS), "bad_request")
            features = []
            for row in rows:
                if not isinstance(row, dict) or any(k not in row for k in core.MODEL_FEATURES):
                    return self._error(400, "Every row needs: " + ", ".join(core.MODEL_FEATURES), "bad_request")
                clean = {}
                for k in core.MODEL_FEATURES:
                    v = to_float(row[k], float("nan"))
                    if v != v:
                        return self._error(400, "Feature '{0}' must be a number.".format(k), "bad_request")
                    clean[k] = v
                features.append(clean)
            values, meta = core.call_quickml(features)
            return self._json(200, {"predictions": values, "model": meta})
        except ValueError:
            return self._error(400, "The request is not valid JSON.", "bad_request")
        except core.QuickMLError as exc:
            status = 503 if exc.kind in ("network", "busy", "unavailable") else 502
            return self._error(status, str(exc), "model_" + exc.kind)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "Something went wrong on the server.", "server")

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            return self._error(404, "Page not found.", "not_found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            data = fh.read()
        cache = "no-cache"  # small files; always pick up a fresh deploy
        return self._send(200, data, ctype, {"Cache-Control": cache})


def main() -> None:
    port = int(os.getenv("X_ZOHO_CATALYST_LISTEN_PORT") or os.getenv("PORT") or 9000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print("PeakShift {0} listening on port {1}".format(VERSION, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
