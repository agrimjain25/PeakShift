# PeakShift: microgrid dispatch console

HackSphere 3.0 · Smart Energy Microgrid & Demand Forecasting Engine

PeakShift forecasts the next 24 hours of microgrid demand with a regression model trained and published in
**Zoho Catalyst QuickML**, then decides hour by hour whether to run on the **main grid** or on **renewable
battery storage**, raises **critical overload** alerts, and shows the whole plan on a dashboard hosted on
**Zoho Catalyst AppSail**.

* **Live app:** https://peakshift-50047164924.development.catalystappsail.in
* **Model endpoint:** QuickML pipeline `demand_forecast_v1`, endpoint `demand_forecast_endpoint`

> Screenshots: add real ones from the deployed app to `docs/screenshots/` and link them here.

## What it does

1. **Takes weather in three ways.** Four real days to click, a 24-row form, or a CSV upload.
2. **Cleans it.** Bad timestamps, duplicates, wrong order, impossible readings and gaps are fixed or reported in plain words (the "Data check" panel).
3. **Calls the QuickML endpoint** for every hour and gets a demand forecast in kW.
4. **Applies the transition rules** (below) to produce a source for each hour, control signals and alerts.
5. **Shows it.** Load curve with likely range, a dispatch ribbon with one slab per hour, battery charge, an alert banner, an hour-by-hour table, a signal feed (with JSON), a run log and the model's scores.

### Dispatch rules (from the problem statement)

| Condition | Result |
|---|---|
| predicted demand > 160 kW, or hour 18 to 22 inclusive | **Renewable Storage Mode** |
| demand <= 160 kW and outside 18:00 to 22:59 | **Main Grid Mode** |
| predicted demand >= 200 kW | **Critical Overload Alert** (separate from the mode) |

Decisions use the demand rounded to 4 decimals, the same precision as `predictions.csv`, so the app and a script reading that file agree.
The rules and the battery model are covered by 29 unit tests, including exactly 160 kW, exactly 200 kW and the hours 17, 18, 22 and 23.

### Beyond the brief

* **How sure is each call?** Every hour shows the chance its decision survives a typical model error (about 10 kW). Hours close to 160 or 200 kW are flagged as close calls.
* **Heatwave stress test.** A slider warms every hour (humidity moves with it, as in the training data) and asks the model again. The original curve stays on the chart as a dashed line.
* **Battery check.** The dispatch rules say when to use storage, not whether the battery can cover it. PeakShift simulates the battery and warns when it would run flat, and sizes a battery that would get through the window.
* **Replay.** A simulated clock walks through the day and the "active state" panel follows it. Click the chart, drag the slider or use the arrow keys to jump to an hour.
* **Control signals as data.** `SWITCH_TO_RENEWABLE_STORAGE`, `SWITCH_TO_MAIN_GRID` and `CRITICAL_OVERLOAD_ALERT` events are listed on screen, copyable as JSON and served at `/api/signals/latest`.
* **Fails clearly.** If the model endpoint cannot be reached the page says so. If you have saved results for the preset days (`scripts/build_cache.py`) those are shown, labelled "Saved result".
* **Light and dark themes, keyboard use, phone layout.**

## Architecture

```
 Browser (dashboard, plain JS + SVG)
    |  JSON / CSV
    v
 Zoho Catalyst AppSail   app/app.py  (Python standard library only)
    |   forecast_core.py   cleaning, features, QuickML client (OAuth refresh, batch or per-row)
    |   grid_logic.py      transition rules, battery model, alerts, confidence
    |   store.py           run log -> local file, and Catalyst Data Store if available
    v
 Zoho Catalyst QuickML   endpoint demand_forecast_endpoint  (XGBoost regression)
```

| Catalyst service | Used for |
|---|---|
| QuickML | trains, publishes and serves the demand model |
| AppSail | hosts the backend and the dashboard |
| Data Store | optional: persists the run log (table `forecast_runs`); the app falls back to a local file if it is not set up |

Not used: Slate (the dashboard is served by AppSail), Stratus (uploaded CSVs are processed in memory and not stored), Functions.

The server has **no third-party dependencies**, so deploying to AppSail needs no `pip install` step.

## The model

XGBoost regression, 7 features: the three weather columns plus `hour`, `day_of_week`, `is_peak` and `is_morning_rush`.
QuickML evaluation: **RMSE 9.13 kW, MAE 7.32 kW, R squared 0.967**. Details, settings and caveats are in
[`model/MODEL_CARD.md`](model/MODEL_CARD.md). The training file is `model/train_enriched.csv`.

## Judges: `predict.py`

```
python predict.py --input sample_test_input.csv --output predictions.csv
python test_eval.py
```

`predict.py` builds the same features as the app, signs in to Zoho with a refresh token (so it keeps working past the
one-hour life of an access token), calls the endpoint in one batch or, if the endpoint does not accept batches, one row at a
time in parallel, and writes `timestamp,target_demand_kw` with one row per input row. It never invents numbers: if the
endpoint fails it stops with an error (set `ALLOW_FALLBACK=1` to allow a rough formula for offline testing only).

**No credentials needed to run it.** If no Zoho credentials are found, `predict.py` sends the model features to this project's public app (`/api/predict` on AppSail), which holds the secrets and calls the QuickML endpoint. Same model, same numbers. With credentials set, it calls QuickML directly.

It needs `pandas`, `numpy` and `requests` (`pip install -r requirements.txt`).

## Configuration

Secrets are read from environment variables, or from a `.env` file next to the code (copy `.env.example`). `.env` is git-ignored.

| Variable | Meaning |
|---|---|
| `QUICKML_ENDPOINT_KEY` | endpoint key from the QuickML endpoint page |
| `CATALYST_ORG` | Catalyst organisation id |
| `QUICKML_ENVIRONMENT` | `Development` (default) or `Production` |
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` | Self Client credentials, scope `QuickML.deployment.READ` (create the token with `zoho_auth_setup.py`) |
| `ZOHO_ACCOUNTS_URL` | `https://accounts.zoho.in` (India data centre, default) |
| `QUICKML_ENDPOINT_URL` | override the endpoint address in the code |
| `QUICKML_ACCESS_TOKEN` | optional ready-made access token (expires in about an hour) |
| `FORECAST_SIGMA_KW` | typical model error used for the likely range, default 10 |
| `USE_DATASTORE`, `DATASTORE_TABLE` | Data Store logging switch and table name (default `forecast_runs`) |
| `LOG_DIR` | where the local run log is kept (default: system temp folder) |
| `PORT` | local port (on AppSail the platform sets `X_ZOHO_CATALYST_LISTEN_PORT`) |

## Run it locally

```
cd app
python app.py            # needs the variables above; opens on http://localhost:9000
```

No credentials handy? Start the stand-in model and point the app at it. **The stand-in is not the real model**; it only imitates the shape of the data.

```
python tests/mock_quickml.py --port 8099
# in a second terminal, set the variables the first one prints, then:
cd app && python app.py
```

## Tests

```
python -m unittest discover -s tests -v
```

76 tests: the rules, the battery model, data cleaning, the QuickML client (batch and per-row, one sign-in shared across threads, outage handling),
every API route, bad input, oversized uploads, path traversal, and a check that `predict.py` and the app build identical model inputs.

## Deploying on Catalyst AppSail

See [`docs/DEPLOY.md`](docs/DEPLOY.md).

## API

| Route | |
|---|---|
| `POST /api/forecast` | JSON `{"rows":[{timestamp, ambient_temp_c, humidity_pct, solar_irradiance_wm2}...], "battery":{...}, "temp_delta_c":0}` or a raw CSV body with `Content-Type: text/csv` |
| `GET /api/signals/latest` | control signals of the latest run |
| `GET /api/runs` | recent runs |
| `GET /api/config`, `GET /api/health` | rules, presets, battery defaults; status |

```
curl -X POST https://<app-url>/api/forecast -H "Content-Type: text/csv" --data-binary @sample_test_input.csv
```

## Assumptions and limits

* The **battery and solar model is a simple, stated simulation** (editable on screen), not data from the problem statement. The dispatch decisions themselves follow the statement exactly and do not depend on it.
* The **likely range** uses a fixed error of about 10 kW, a judgement based on the model's validation RMSE of 9.1 kW and a stricter time-ordered check of about 12 kW.
* "Replay" is a simulated clock over the forecast, not live telemetry.
* A request is capped at 168 rows (one week) and 1 MB.
* The model was trained on December to June data and has not seen autumn.

## Project layout

```
app/                 AppSail source (server, logic, dashboard in static/, data/)
tests/               unit and end-to-end tests, stand-in QuickML
scripts/             make_presets.py, build_cache.py
model/               training file, model card
predict.py           judges' prediction script          test_eval.py   starter self-test
zoho_auth_setup.py   one-time helper to create the refresh token
```
