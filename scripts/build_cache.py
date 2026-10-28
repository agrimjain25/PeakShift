"""Save real model answers for the preset days, so the demo still works if the endpoint is ever unreachable.

Needs your credentials (.env). Run once, after the endpoint is published:

    python scripts/build_cache.py

It calls the live QuickML endpoint for each preset day at every stress-test setting and writes
app/data/preset_cache.json. The app only reads this file when a live call fails, and then it
labels the result "Saved result" on screen. Nothing in it is invented: every number came from the model.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))

import forecast_core as core  # noqa: E402


def main():
    if not core.credentials_configured():
        sys.exit("Credentials are missing. Fill in .env first (see .env.example).")
    with open(os.path.join(ROOT, "app", "data", "presets.json"), encoding="utf-8") as fh:
        presets = json.load(fh)["presets"]
    entries = {}
    started = time.time()
    for preset in presets:
        for delta in range(-5, 9):
            clean, _ = core.clean_rows(preset["rows"], float(delta))
            features = core.build_features(clean)
            values, meta = core.call_quickml(features)
            entries[core.features_key(features)] = {"values": [round(v, 6) for v in values], "label": "{0} {1:+d}C".format(preset["id"], delta)}
        print("saved", preset["id"], "(" + meta["mode"] + ")")
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "entries": entries}
    path = os.path.join(ROOT, "app", "data", "preset_cache.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    print("wrote {0} entries to {1} in {2:.0f}s".format(len(entries), os.path.normpath(path), time.time() - started))


if __name__ == "__main__":
    main()
