"""One-time helper: turn a Self Client "code" into a long-lived refresh token.

Steps (do this once):
 1. Open https://api-console.zoho.in  -> Add Client -> "Self Client" -> Create.
 2. Copy the Client ID and Client Secret.
 3. Open the "Generate Code" tab, scope = QuickML.deployment.READ,
    time duration = 10 minutes, description = anything -> Create. Copy the code.
 4. Run:  python zoho_auth_setup.py --client-id XXX --client-secret YYY --code ZZZ
 5. Put the printed refresh token in your .env file (never commit .env).
"""

import argparse

import requests

parser = argparse.ArgumentParser()
parser.add_argument("--client-id", required=True)
parser.add_argument("--client-secret", required=True)
parser.add_argument("--code", required=True)
parser.add_argument("--accounts-url", default="https://accounts.zoho.in")
args = parser.parse_args()

resp = requests.post(
    f"{args.accounts_url.rstrip('/')}/oauth/v2/token",
    data={
        "grant_type": "authorization_code",
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "code": args.code,
    },
    timeout=30,
)
body = resp.json()
if "refresh_token" in body:
    print("SUCCESS. Add this line to your .env file:\n")
    print(f"ZOHO_REFRESH_TOKEN={body['refresh_token']}")
else:
    print("Failed:", body)
