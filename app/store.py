"""Run log: remembers every forecast so the dashboard can show a history.

Each run is kept in memory, appended to a local JSON-lines file (survives app restarts
inside the same container) and, when the Catalyst SDK is installed and a table exists,
also written to Catalyst Data Store. Data Store is best effort: if it fails the run
still succeeds and the reason is shown on /api/health.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from forecast_core import cfg

DATASTORE_COLUMNS = [
    "run_id", "created_at", "source", "rows", "peak_kw", "peak_time", "avg_kw",
    "overload_hours", "storage_hours", "min_soc_pct", "model_status", "latency_ms", "temp_delta_c",
]


class RunStore:
    def __init__(self, keep: int = 50):
        self._lock = threading.Lock()
        self._runs: Deque[Dict[str, Any]] = deque(maxlen=keep)
        self.last_signals: Optional[Dict[str, Any]] = None
        self.datastore_error: Optional[str] = None
        self.datastore_writes = 0
        folder = cfg("LOG_DIR") or os.path.join(tempfile.gettempdir(), "peakshift")
        self._path = os.path.join(folder, "runs.jsonl")
        try:
            os.makedirs(folder, exist_ok=True)
            self._load()
        except OSError:
            self._path = ""

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as fh:
            lines = fh.readlines()[-self._runs.maxlen:]
        for line in lines:
            try:
                self._runs.append(json.loads(line))
            except ValueError:
                continue

    @property
    def storage_label(self) -> str:
        if self.datastore_writes:
            return "Catalyst Data Store"
        return "Local log file" if self._path else "Memory only"

    def add(self, record: Dict[str, Any], request: Any = None) -> None:
        with self._lock:
            self._runs.append(record)
            if self._path:
                try:
                    with open(self._path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record) + "\n")
                except OSError:
                    pass
        self._write_datastore(record, request)

    def _write_datastore(self, record: Dict[str, Any], request: Any) -> None:
        if cfg("USE_DATASTORE", "1") != "1" or request is None:
            return
        try:
            import zcatalyst_sdk  # installed only if you chose to bundle it
        except ImportError:
            self.datastore_error = "zcatalyst-sdk is not bundled with the app"
            return
        try:
            table = cfg("DATASTORE_TABLE", "forecast_runs")
            catalyst = zcatalyst_sdk.initialize(req=request)
            row = {k: str(record.get(k, "")) for k in DATASTORE_COLUMNS}
            catalyst.datastore().table(table).insert_row(row)
            self.datastore_writes += 1
            self.datastore_error = None
        except Exception as exc:  # noqa: BLE001 - never let logging break a forecast
            self.datastore_error = "{0}: {1}".format(type(exc).__name__, str(exc)[:160])

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._runs)[-limit:][::-1]
