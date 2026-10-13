#!/usr/bin/env python3
"""Local read-only HTTP API for the BTC 15m agent. Bearer-key auth.

Endpoints (all GET, all JSON):
  /status            engine state: paper/live PnL, open positions, kills
  /activity?n=10     latest decisions/opens/settles/errors
  /positions         open positions (paper + live)
  /pnl               settled P&L by sleeve
  /gate              poppin-gate check
  /config            guardrails

Auth:  Authorization: Bearer <key from .api_key>
Binds 127.0.0.1:8787 only. Key file is 0600.

  ./api.py &                      # start
  curl -H "Authorization: Bearer $(cat .api_key)" \
    localhost:8787/status
"""
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import readers  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".api_key")
PORT = 8787


def get_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            return f.read().strip()
    key = "btc_" + secrets.token_urlsafe(32)
    with open(KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    return key


API_KEY = get_key()

ROUTES = {
    "/status": readers.get_status,
    "/positions": readers.get_positions,
    "/pnl": readers.get_pnl,
    "/gate": readers.get_gate,
    "/config": readers.get_config,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {API_KEY}":
            return self._send({"error": "unauthorized"}, 401)
        u = urlparse(self.path)
        if u.path == "/activity":
            try:
                n = int(parse_qs(u.query).get("n", ["10"])[0])
            except ValueError:
                n = 10
            return self._send(readers.get_activity(n))
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send({"error": "not found"}, 404)
        try:
            return self._send(fn())
        except Exception as e:  # never leak a stack to the client
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"btc agent api on 127.0.0.1:{PORT} (key in .api_key)", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
