#!/usr/bin/env python3
"""Claude's side of the Daily Tasks chat center.

    python3 chat.py working YYYY-MM-DD           # the user's new messages → "working"
    python3 chat.py reply   YYYY-MM-DD "answer"  # post the answer; their messages → "done"
"""
import json, os, sys, urllib.request
from pathlib import Path

TASKS = Path(os.environ.get("TASKS", Path.home() / "work-tools" / "tasks")).expanduser()   # same folder as server.py


def post(ep, body):
    port = (TASKS / ".port").read_text().strip()
    req = urllib.request.Request(f"http://localhost:{port}{ep}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("working", "reply"):
        sys.exit(__doc__)
    cmd, d = sys.argv[1], sys.argv[2]
    if cmd == "working":
        post("/api/chat/working", {"date": d})
    else:
        post("/api/chat/reply", {"date": d, "text": " ".join(sys.argv[3:])})
    print("ok")
