#!/usr/bin/env python3
"""Local QA fixture with one visual defect and one validation defect.

Run with ``--optimistic`` to model an interface that reports success without
persisting the record. The fixture is intentionally small and has no tests of
its own; DevCrew QA is supposed to find the defects.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler

from loopback_http import ThreadingLoopbackHTTPServer


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inventory notes</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 24px; color: #17202a; }
  main { max-width: 720px; }
  label { display: block; margin-top: 12px; font-weight: 600; }
  input { width: 100%; box-sizing: border-box; padding: 8px; margin-top: 4px; }
  button { margin-top: 16px; padding: 10px 18px; }
  .preview { margin-top: 16px; padding: 8px; border: 1px solid #aab7b8; }
  #status { margin-top: 12px; font-weight: 600; }
  @media (max-width: 500px) {
    .preview { max-width: 160px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  }
</style>
</head>
<body>
<main>
  <h1>Inventory notes</h1>
  <form id="item-form">
    <label for="name">Item name</label>
    <input id="name" name="name" autocomplete="off">
    <label for="label">Run label</label>
    <input id="label" name="label" autocomplete="off">
    <button id="save" type="submit">Save item</button>
  </form>
  <div id="status"></div>
  <div id="item-id"></div>
  <div class="preview" id="preview"></div>
</main>
<script>
const form = document.getElementById('item-form');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const response = await fetch('/api/items', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name: document.getElementById('name').value,
      label: document.getElementById('label').value,
    }),
  });
  if (!response.ok) {
    document.getElementById('status').textContent = 'Error';
    return;
  }
  const item = await response.json();
  document.getElementById('status').textContent = 'Saved';
  document.getElementById('item-id').textContent = 'ID ' + item.id;
  document.getElementById('preview').textContent = item.label;
});
</script>
</body>
</html>
"""


class Store:
    def __init__(self, optimistic: bool):
        self.optimistic = optimistic
        self.items: dict[str, dict[str, str]] = {}
        self.next_id = 1
        self.lock = threading.Lock()

    def create(self, payload: dict[str, str]) -> dict[str, str]:
        with self.lock:
            identifier = str(self.next_id)
            self.next_id += 1
            item = {"id": identifier, "name": payload.get("name", ""), "label": payload.get("label", "")}
            if "secret" in payload:
                item["secret"] = payload["secret"]
            if not self.optimistic:
                self.items[identifier] = item
            return item

    def get(self, identifier: str) -> dict[str, str] | None:
        with self.lock:
            return self.items.get(identifier)


class Handler(BaseHTTPRequestHandler):
    store: Store

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/health":
            self._send(200, b'{"status":"ready"}', "application/json")
        elif self.path.startswith("/api/items/"):
            identifier = self.path.rsplit("/", 1)[-1]
            item = self.store.get(identifier)
            if item is None:
                self._send(404, b'{"error":"not found"}', "application/json")
            else:
                self._send(200, json.dumps(item).encode("utf-8"), "application/json")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self):
        if self.path != "/api/items":
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b'{"error":"invalid json"}', "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"invalid payload"}', "application/json")
            return
        item = self.store.create(payload)
        self._send(201, json.dumps(item).encode("utf-8"), "application/json")

    def log_message(self, *_args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--optimistic", action="store_true")
    args = parser.parse_args()
    Handler.store = Store(args.optimistic)
    server = ThreadingLoopbackHTTPServer(("127.0.0.1", args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
