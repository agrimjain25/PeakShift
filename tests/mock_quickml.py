"""A stand-in for the QuickML endpoint and Zoho sign-in, for tests and local UI work.

IT IS NOT THE REAL MODEL. The demand formula below only imitates the shape of the
training data (low at night, morning rush, evening peak, rises with temperature) so
the dashboard has something realistic to draw.

Run it:   python tests/mock_quickml.py --port 8099 [--single-only] [--fail]
Then start the app with the variables printed on startup.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Mean demand by hour in the training file, and how strongly demand follows temperature.
HOURLY_MEAN = [92.4, 91.9, 91.8, 90.8, 91.5, 91.8, 136.1, 177.3, 181.1, 186.5, 159.4, 163.7,
               165.3, 170.2, 172.4, 171.0, 173.0, 171.3, 220.6, 217.8, 215.0, 209.4, 204.7, 114.8]


def demand(feature):
    hour = int(feature["hour"])
    slope = 0.4 if hour <= 5 or hour == 23 else (1.3 if hour <= 9 else (3.5 if hour <= 17 else 2.5))
    return HOURLY_MEAN[hour] + slope * (float(feature["ambient_temp_c"]) - 18.5)


class State:
    def __init__(self, batch=True, fail=False):
        self.batch = batch
        self.fail = fail
        self.calls = 0
        self.token_requests = 0
        self.last_headers = {}
        self.lock = threading.Lock()


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            with state.lock:
                if self.path.startswith("/oauth"):
                    state.token_requests += 1
                    return self._reply(200, {"access_token": "mock-token", "expires_in": 3600})
                state.calls += 1
                state.last_headers = {k.lower(): v for k, v in self.headers.items()}
            if state.fail:
                return self._reply(503, {"error": "mock outage"})
            if self.headers.get("Authorization") != "Zoho-oauthtoken mock-token" \
                    or self.headers.get("X-QUICKML-ENDPOINT-KEY") != "test-key" \
                    or self.headers.get("CATALYST-ORG") != "test-org":
                return self._reply(401, {"error": "bad credentials"})
            data = json.loads(raw)["data"]
            if isinstance(data, list):
                if not state.batch:
                    return self._reply(400, {"error": "single row only"})
                return self._reply(200, {"result": [demand(d) for d in data]})
            return self._reply(200, {"result": [demand(data)]})

    return Handler


def serve(port=0, batch=True, fail=False):
    """Start the mock in a background thread. Returns (server, state, base_url)."""
    state = State(batch, fail)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, "http://127.0.0.1:{0}".format(server.server_port)


def env_for(base_url):
    return {
        "QUICKML_ENDPOINT_URL": base_url + "/predict",
        "ZOHO_ACCOUNTS_URL": base_url,
        "QUICKML_ENDPOINT_KEY": "test-key",
        "CATALYST_ORG": "test-org",
        "ZOHO_CLIENT_ID": "id",
        "ZOHO_CLIENT_SECRET": "secret",
        "ZOHO_REFRESH_TOKEN": "refresh",
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--single-only", action="store_true", help="reject batch requests")
    parser.add_argument("--fail", action="store_true", help="answer every prediction with HTTP 503")
    args = parser.parse_args()
    srv, _, url = serve(args.port, batch=not args.single_only, fail=args.fail)
    print("Mock QuickML running at " + url)
    for key, value in env_for(url).items():
        print("  set {0}={1}".format(key, value))
    threading.Event().wait()
