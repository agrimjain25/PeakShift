# Model card: hourly demand forecast

| | |
|---|---|
| Task | Predict hourly energy demand (kW) of the microgrid |
| Platform | Zoho Catalyst QuickML, pipeline `demand_forecast_v1`, endpoint `demand_forecast_endpoint` |
| Algorithm | XGBoost regression (gbtree, squared error) |
| Training data | `train_telemetry.csv`: 4,000 hourly rows, 31 Dec 2014 to 16 Jun 2015 |
| Target | `target_demand_kw` |

## Features (7)

| Feature | Source |
|---|---|
| `ambient_temp_c`, `humidity_pct`, `solar_irradiance_wm2` | the three weather columns, unchanged |
| `hour` | hour of the timestamp, 0 to 23 |
| `day_of_week` | Monday = 0 |
| `is_peak` | 1 when the hour is 18 to 22, else 0 |
| `is_morning_rush` | 1 when the hour is 6 to 9, else 0 |

`model/train_enriched.csv` is the exact file uploaded to QuickML. `predict.py` and the web app build the same
columns from the timestamp before calling the endpoint; `tests/test_core.py` checks that the code reproduces
`train_enriched.csv` row for row and that `predict.py` and the app produce identical inputs.

No lag or rolling features are used: the evaluation sends only weather and timestamp, so past demand is never available.

## QuickML settings

Learning rate 0.1, 300 estimators, max depth 5, subsample 0.8, column sample 1, min child weight 1, alpha 0, lambda 1.

## Scores (QuickML evaluation screen)

| RMSE | MAE | R squared | MAPE |
|---|---|---|---|
| 9.1286 kW | 7.3162 kW | 0.9667 | 5.21 % |

These were measured by QuickML on data it held back from training. They are probably a little optimistic for the
judges' test set, which comes from a later period (mid June to July). A stricter check, training only on the first
80 % of rows in time order and testing on the last 20 %, gave an RMSE of about 12 kW with a comparable gradient-boosting
model. The app therefore shows a "likely range" of plus or minus 10 kW rather than 9.1 kW.

## What was tried and left out

Extra features (cooling degrees, weekend flag, month) and a linear-plus-boosting blend moved the time-ordered
RMSE only from 12.5 to about 12.1 kW. That is within noise, so the simpler model was kept.

## Known limits

* Trees cannot predict beyond temperatures seen in training (maximum 38.3 C).
* Trained on one site over six months; it has not seen autumn.
