"""Local fake Telegram Bot API server for tests (127.0.0.1 only; no real network)."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeTelegram:
    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[tuple[int, dict] | str] = []  # responses in order; "sleep" => hang past the client timeout
        self.default = (200, {"ok": True, "result": {"message_id": 42, "chat": {"id": 1, "type": "private"}}})
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                item = outer.script.pop(0) if outer.script else outer.default
                if item == "sleep":
                    time.sleep(1.5)
                    return
                status, payload = item
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
