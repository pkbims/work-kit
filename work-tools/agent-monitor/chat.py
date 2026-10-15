#!/usr/bin/env python3
"""Claude's side of the Agent Monitor chat center.

    python3 chat.py working            # the user's new messages → "working"
    python3 chat.py reply "answer"     # post the answer; their messages → "done"
"""
import json, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def post(ep, body):
    port = (HERE / ".port").read_text().strip()
    req = urllib.request.Request(f"http://localhost:{port}{ep}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("working", "reply"):
        sys.exit(__doc__)
    if sys.argv[1] == "working":
        post("/api/chat/working", {})
    else:
        post("/api/chat/reply", {"text": " ".join(sys.argv[2:])})
    print("ok")
