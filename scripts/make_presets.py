"""Build app/data/presets.json: four real 24-hour weather days from the starter-kit CSVs.

Usage (from the project root):
    python scripts/make_presets.py --train train_telemetry.csv --sample sample_test_input.csv

Only the weather columns are used. Demand is never copied into a preset: the model
has to predict it live.
"""

import argparse
import csv
import json
import os
import statistics
from collections import defaultdict

COLUMNS = ["timestamp", "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]


def full_days(path):
    days = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            days[row["timestamp"][:10]].append({
                "timestamp": row["timestamp"],
                "ambient_temp_c": float(row["ambient_temp_c"]),
                "humidity_pct": float(row["humidity_pct"]),
                "solar_irradiance_wm2": float(row["solar_irradiance_wm2"]),
            })
    complete = {}
    for day, rows in days.items():
        hours = [r["timestamp"][11:13] for r in rows]
        if len(rows) == 24 and hours == ["{0:02d}".format(h) for h in range(24)]:
            complete[day] = rows
    return complete


def mean_temp(rows):
    return statistics.mean(r["ambient_temp_c"] for r in rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "data", "presets.json"))
    args = parser.parse_args()

    sample = full_days(args.sample)
    train = full_days(args.train)
    by_temp = sorted(sample, key=lambda d: mean_temp(sample[d]))

    picks = [
        ("heatwave", "Heatwave day", "Hottest full day in the sample test file", "sample_test_input.csv", by_temp[-1], sample),
        ("typical", "Typical summer day", "Median day in the sample test file", "sample_test_input.csv", by_temp[len(by_temp) // 2], sample),
    ]
    spring = min(train, key=lambda d: abs(mean_temp(train[d]) - 17.0))
    winter = min(train, key=lambda d: mean_temp(train[d]))
    picks.append(("spring", "Spring day", "Training-file day closest to a 17 C average", "train_telemetry.csv", spring, train))
    picks.append(("winter", "Winter day", "Coldest full day in the training file", "train_telemetry.csv", winter, train))

    presets = []
    for pid, name, note, source, day, pool in picks:
        rows = pool[day]
        presets.append({
            "id": pid,
            "name": name,
            "date": day,
            "note": note,
            "source": source,
            "mean_temp_c": round(mean_temp(rows), 1),
            "max_temp_c": max(r["ambient_temp_c"] for r in rows),
            "rows": rows,
        })

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"columns": COLUMNS, "presets": presets}, fh, indent=1)
    for p in presets:
        print("{0:9s} {1}  mean {2} C  max {3} C  ({4})".format(p["id"], p["date"], p["mean_temp_c"], p["max_temp_c"], p["source"]))


if __name__ == "__main__":
    main()
