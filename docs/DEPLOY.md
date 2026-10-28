# Deploying PeakShift on Catalyst AppSail

The server uses only the Python standard library, so there is nothing to `pip install` for the app itself.

Where a step depends on how the Catalyst console or CLI looks today, it says **check**. The prompts below follow
Zoho's AppSail documentation; if your screen differs, follow your screen and keep the values.

## Before you start

* The QuickML endpoint is published and `python predict.py ...` works on your computer.
* You have a `.env` file with the endpoint key, org id and Zoho client credentials (see the README).
* Node.js is installed: `node -v` prints a version.

## 1. Install the Catalyst CLI and sign in

```
npm install -g zcatalyst-cli
catalyst login
```

A browser window opens. Sign in with the same Zoho account as your Catalyst project. If the CLI asks for a data centre, pick **IN**.

## 2. Connect this folder to your project and add AppSail

Open PowerShell **in the project folder** (the one that contains `app`, `tests` and `README.md`) and run:

```
catalyst init
```

Answer the prompts like this:

| Prompt | Answer |
|---|---|
| Portal / project | choose your existing project (the one named `hacksphere-...`) |
| Services | **AppSail** (space bar to tick, Enter to continue) |
| Runtime type | **Catalyst-Managed Runtime** |
| Download a sample project? | **N** |
| Use the current directory as the source? | **N**, then type the full path of the `app` folder, for example `C:\Users\you\peakshift\app` |
| App name | `PeakShift` |
| Build path | the same full path to the `app` folder |
| Stack | the newest **Python** option (3.9 or higher) |

The CLI writes `catalyst.json` in the project folder. `app/app-config.json` already exists in this repository
with the start command `python3 -u app.py`. **Check** that it still has that command after `init`.

## 3. Deploy

```
catalyst deploy
```

When it finishes the CLI prints the app's address. You can also find it in the console under
Serverless, then AppSail, then `PeakShift`.

## 4. Give the app its secrets

The secrets are **not** in `app-config.json` (that file is committed to Git). Set them in the console instead:

Serverless, then AppSail, then `PeakShift`, then the environment variables section (**check** the exact name), and add:

```
QUICKML_ENDPOINT_KEY
CATALYST_ORG
ZOHO_CLIENT_ID
ZOHO_CLIENT_SECRET
ZOHO_REFRESH_TOKEN
```

The other settings (`QUICKML_ENVIRONMENT`, `ZOHO_ACCOUNTS_URL`, `USE_DATASTORE`, `DATASTORE_TABLE`) are already in `app-config.json`.
Restart or redeploy the app if the console asks you to.

## 5. Check it works

1. Open the app address. The pill at the top right should read **Model live** and a forecast should be on screen.
2. Open `<app address>/api/health`. It should show `"model_credentials_configured": true`.
3. Click each of the four days, try the stress-test slider, and upload `sample_test_input.csv`.

| What you see | Meaning |
|---|---|
| "Model credentials not set" | one of the five variables in step 4 is missing |
| error `model_auth` | wrong client id, secret or refresh token. Create a new refresh token with `zoho_auth_setup.py` |
| error `model_rejected` | endpoint key, org id or `QUICKML_ENVIRONMENT` does not match the endpoint |
| error `model_network` | the app could not reach Zoho. Try again in a minute |

## 6. Make sure the judges can open it

Judges must open the link **without signing in**.

1. Open the app address in a **private/incognito window** where you are not signed in to Zoho, and on your phone using mobile data.
2. If you see a Zoho sign-in page, the app is protected. **Check** the AppSail settings for an authentication or access option and
   turn it off, and check whether the Development address is public. If the Development address is always private, use **Deploy to Production**
   in the console. In that case also confirm the QuickML endpoint works from Production and set `QUICKML_ENVIRONMENT` to `Production`.
   I could not test this part from outside your account, so do this check yourself and, if stuck, ask the organizers.

## 7. Optional: log runs to Catalyst Data Store

The run log works without this (it uses a local file). To also store runs in Data Store:

1. Console, then Cloud Scale, then Data Store, then **Create Table** named `forecast_runs`.
2. Add these columns, all of type **Var Char**: `run_id, created_at, source, rows, peak_kw, peak_time, avg_kw, overload_hours, storage_hours, min_soc_pct, model_status, latency_ms, temp_delta_c`.
3. Put the Catalyst SDK next to the code, then deploy again:

   ```
   cd app
   python -m pip install zcatalyst-sdk -t .
   cd ..
   catalyst deploy
   ```
4. Run a forecast. The "Run log" panel should say **Stored in: Catalyst Data Store**, and rows appear in the table.
   If not, `/api/health` shows the reason under `datastore_error`. The app keeps working either way.

## 8. Optional: saved results for the four days

Run once on your computer, then deploy again:

```
python scripts/build_cache.py
catalyst deploy
```

If the model endpoint is ever unreachable, the four preset days still show real model output from this file, labelled "Saved result".

## Submission checklist

* [ ] App address opens in a private window and on a phone, with no sign-in
* [ ] Address pasted into `README.md` and into the form
* [ ] `python predict.py --input sample_test_input.csv --output predictions.csv` works with the real endpoint
* [ ] `python test_eval.py` prints `SUCCESS`
* [ ] `python -m unittest discover -s tests` passes
* [ ] `.env` is **not** in the repository or the zip (check `git status`); `.env.example` is
* [ ] Screenshots of the live app added to `docs/screenshots/`
* [ ] Repository is public, or the zip is named `YourTeamName_Submission.zip`
* [ ] Organizers told how the evaluator should authenticate for `predict.py`; a separate Zoho client created just for the judges if they need credentials
* [ ] Form submitted before **28 October 2026**, with time to spare
