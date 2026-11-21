"""
TradingView -> MetaTrader 5 relay  (Python 3.8+, standard library only)

TradingView sends an alert (webhook POST) here; the Gold Trade Manager EA
polls GET /poll every second and trades the new alerts.

Run on your VPS (the one running MT5 is fine):
    python tv_relay.py --secret CHANGE_ME --port 80

TradingView webhooks only reach ports 80 / 443, so open port 80 in the
VPS firewall. Webhook URL in TradingView:  http://YOUR-VPS-IP/webhook
EA input "Relay poll URL":                 http://YOUR-VPS-IP/poll

Alert message (JSON), examples:
    {"secret":"CHANGE_ME","action":"buy"}
    {"secret":"CHANGE_ME","action":"sell","lots":0.05}
    {"secret":"CHANGE_ME","action":"close"}
    For a Pine strategy: {"secret":"CHANGE_ME","action":"{{strategy.order.action}}"}
Actions: buy, sell, close, close_buy, close_sell
"""
import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACTIONS = {"buy", "long", "sell", "short", "close", "close_buy", "close_sell"}
KEEP = 500                      # signals kept in memory / on disk
lock = threading.Lock()
state = {"next_id": 1, "signals": []}
ARGS = None


def load():
    if os.path.exists(ARGS.store):
        try:
            with open(ARGS.store) as f:
                state.update(json.load(f))
        except Exception as e:
            print("could not read store:", e)


def save():
    tmp = ARGS.store + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, ARGS.store)


def parse_text(text):
    """Find buy / sell / close in free alert text (first keyword wins)."""
    t = re.sub(r"[^a-z_ ]", " ", text.lower())
    words = t.split()
    is_close = any(w in ("close", "exit", "flat", "close_buy", "close_sell") for w in words)
    side = ""
    for w in words:
        if w in ("buy", "long", "bull", "bullish", "close_buy"):
            side = "buy"; break
        if w in ("sell", "short", "bear", "bearish", "close_sell"):
            side = "sell"; break
    if is_close:
        return "close_" + side if side else "close"
    return side


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/webhook":
            return self._send(404, "not found")
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)).decode("utf-8", "replace")
        url_secret = parse_qs(u.query).get("secret", [""])[0]
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError
        except ValueError:
            # any indicator's own alert text, e.g. "Master Suite BUY XAUUSD @ 4071"
            msg = {"action": parse_text(raw)}
        secret = str(msg.get("secret", "") or url_secret)
        if secret != ARGS.secret:
            print("rejected: bad secret")
            return self._send(403, "bad secret")
        action = str(msg.get("action", "")).strip().lower()
        if action not in ACTIONS:
            action = parse_text(action)
        if action not in ACTIONS:
            print("ignored, no buy/sell/close found in:", raw[:200])
            return self._send(400, "unknown action")
        try:
            lots = float(msg.get("lots", 0) or 0)
        except (TypeError, ValueError):
            lots = 0.0
        with lock:
            sig = {"id": state["next_id"], "ts": int(time.time()), "action": action, "lots": lots}
            state["next_id"] += 1
            state["signals"] = (state["signals"] + [sig])[-KEEP:]
            save()
        print(time.strftime("%H:%M:%S"), "alert", sig)
        self._send(200, "ok %d" % sig["id"])

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, "relay up")
        if u.path != "/poll":
            return self._send(404, "not found")
        if q.get("secret", [""])[0] != ARGS.secret:
            return self._send(403, "bad secret")
        try:
            after = int(q.get("after", ["0"])[0])
        except ValueError:
            after = 0
        with lock:
            if after > state["next_id"]:      # relay was reset: resend recent ones
                after = 0
            rows = [s for s in state["signals"] if s["id"] > after]
        text = "".join("%d;%d;%s;%g\n" % (s["id"], s["ts"], s["action"], s["lots"]) for s in rows)
        self._send(200, text)

    def log_message(self, *a):          # keep the console quiet for polls
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--secret", default=os.environ.get("SECRET", ""))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "80")))
    p.add_argument("--store", default="tv_signals.json")
    ARGS = p.parse_args()
    if not ARGS.secret:
        raise SystemExit("set --secret or the SECRET environment variable")
    load()
    print("relay listening on port", ARGS.port)
    ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler).serve_forever()
