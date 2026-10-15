#!/usr/bin/env python3
"""Local spec-review server. Serves the interactive PRD and persists decisions
and queued comments to disk so Claude can read them back."""

import json
import os
import shutil
import http.server
import socket
import socketserver
import subprocess
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
INBOX = os.path.join(HERE, "INBOX.md")
PORT_FILE = os.path.join(HERE, ".port")
# PORT pins the port; otherwise the first free one from 7777 up is taken, so two
# pages can run side by side. The chosen port is written to .port.
START_PORT = int(os.environ.get("PORT", "7777"))
PINNED = "PORT" in os.environ

PORT = None                       # set once bound; used in the wake-up prompt
HTTPD = None                      # the running server, so Discard can stop it
PENDING = {"t": None}             # time of the last question/reply not yet passed to Claude
BUNDLE_S = 8                      # messages within this many seconds become one wake-up


def agent():
    """The herdr pane of the Claude session working on this page, from <page>/.agent
    (written at setup: echo $HERDR_PANE_ID > .agent). None → INBOX.md is the only way back."""
    try:
        with open(os.path.join(HERE, ".agent")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def wake(text):
    """Type a prompt into the Claude session (herdr), as the App Maker's app server does.
    Replaces relying on an INBOX.md watcher, which Claude could forget and the harness could reap
    (user, 2026-09-30: "It should never be missed")."""
    a = agent()
    if not a:
        return False
    try:
        return subprocess.run(["herdr", "agent", "prompt", a, text], capture_output=True,
                              text=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def discard():
    """The user pressed Discard (user, 2026-09-30: "a delete button to close the review and just
    discard it"). Move the page folder to the Trash, so it can still be recovered, tell Claude,
    and stop serving. Runs in its own thread: shutdown() can't be called from a request."""
    time.sleep(0.5)                                   # let the response reach the browser first
    name = os.path.basename(HERE.rstrip("/"))
    dest = os.path.join(os.path.expanduser("~/.Trash"), f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.move(HERE, dest)
    wake(f"The user discarded the html-worker page that was at {HERE}. It was moved to {dest} and its "
         f"server on port {PORT} has stopped. Nothing to do but acknowledge it in one line.")
    if HTTPD:
        HTTPD.shutdown()


def addressed_cmd():
    return f"curl -s -X POST localhost:{PORT}/api/comment/addressed -d '{{}}'"


def working_cmd():
    # Statuses go queued → sent → working → done (user, 2026-10-08). Claude marks "working" when it picks a round up.
    return f"First mark them as being worked on: curl -s -X POST localhost:{PORT}/api/comment/working -d '{{}}'."


def bundle_messages():
    """Questions and replies go straight to Claude, but a burst becomes one wake-up."""
    while True:
        time.sleep(2)
        t = PENDING["t"]
        if t is None or time.time() - t < BUNDLE_S:
            continue
        PENDING["t"] = None
        wake(f"The user wrote on the page at {HERE}. {working_cmd()} Read {INBOX} and answer each open question "
             f"ON THE PAGE with reply.py, as it says. Then: {addressed_cmd()}")


def _empty():
    return {"decisions": {}, "comments": [], "batches": []}


def load():
    if not os.path.exists(STATE):
        return _empty()
    try:
        with open(STATE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty()
    base = _empty()
    base.update(data)
    return base


def save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE)


def fmt(v):
    """A decision value as text. Lists can hold objects, e.g. the learnings box saves
    {text, t} items; ", ".join on those crashed Send before Claude was woken (2026-10-14)."""
    if isinstance(v, list):
        return ", ".join(x.get("text", str(x)) if isinstance(x, dict) else str(x) for x in v)
    return v


def write_inbox(state):
    """Human-readable snapshot Claude reads after a send."""
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    rnd = len(state["batches"])
    prev = state["batches"][-2]["decisions"] if len(state["batches"]) > 1 else {}
    cur = state["batches"][-1]["decisions"] if state["batches"] else state["decisions"]
    lines = [f"# Spec review inbox", "", f"Round {rnd} sent {now}", ""]

    changed = {k: v for k, v in cur.items() if prev.get(k) != v}
    if rnd > 1:
        lines.append(f"## Changed since round {rnd - 1} ({len(changed)})")
        lines.append("")
        if not changed:
            lines.append("_No decisions changed._")
        for k, v in changed.items():
            val = fmt(v)
            was = fmt(prev.get(k)) or "unanswered"
            lines.append(f"- **{k}** — {val}  _(was: {was})_")
        lines.append("")

    unanswered = [g for g in state.get("groups", []) if g not in cur]
    if unanswered:
        lines.append(f"## Still unanswered: {', '.join(unanswered)}")
        lines.append("")

    pending = [c for c in state["comments"] if c.get("status") == "sent"
               and not c.get("addressed")]
    # Three kinds, handled differently: change requests revise the page; questions are
    # answered in the terminal so the page does not churn; notes are context to take
    # on board, acknowledged in the terminal. Comments from before the split have no
    # kind and count as change requests.
    changes = [c for c in pending if c.get("kind") not in ("question", "note")]
    questions = [c for c in pending if c.get("kind") == "question"]
    notes = [c for c in pending if c.get("kind") == "note"]

    def emit(title, items):
        lines.append(title)
        lines.append("")
        if not items:
            lines.append("_None._")
        for c in items:
            lines.append(f"### {c.get('anchorLabel') or c.get('anchor')}  ·  id `{c.get('id')}`")
            quoted = (c.get("anchorText") or "").strip()
            if quoted:
                lines.append(f"> {quoted[:300]}")
            lines.append("")
            lines.append(c.get("text", ""))
            for m in c.get("messages", []):   # the conversation so far, oldest first
                lines.append(f"- **{'Claude' if m.get('from') == 'claude' else 'You'}:** {m.get('text', '')}")
            lines.append("")

    emit(f"## Change requests ({len(changes)}) — revise the page", changes)
    emit(f"## Questions ({len(questions)}) — answer on the page: "
         f"python3 ~/.claude/skills/html-worker/reply.py <page_dir> <id> \"answer\" (add --suggest if it implies a change); do not revise the page",
         questions)
    emit(f"## Comments ({len(notes)}) — context to take on board; acknowledge on the page with reply.py",
         notes)

    lines.append(f"## Decisions as of round {rnd}")
    lines.append("")
    if not cur:
        lines.append("_None recorded._")
    for k, v in cur.items():
        lines.append(f"- **{k}** — {fmt(v)}")
    lines.append("")

    with open(INBOX, "w") as f:
        f.write("\n".join(lines))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def api(ep, payload):
    """Every page action, on the page whose STATE/INBOX this module points at.
    Returns (response, event): event is "send" (a round), "message" (a question, note or reply that
    goes straight to Claude) or None. This server writes INBOX.md for both, so a session without the
    App Maker still finds them; the App Maker's app server also wakes the session."""
    state = load()
    event = None
    if ep == "/api/groups":
        state["groups"] = payload.get("groups", [])
    elif ep == "/api/decision":
        if payload["value"] is None:  # null clears a decision, e.g. a retired id
            state["decisions"].pop(payload["id"], None)
        else:
            state["decisions"][payload["id"]] = payload["value"]
        # baseline: Claude wrote it (e.g. carrying answers over), so record it in the last round too,
        # or Send counts Claude's write as the user's unsent change (user, 2026-09-30).
        if payload.get("baseline"):
            # no round yet: a page-level baseline stands in for "the last round"
            last = state["batches"][-1]["decisions"] if state["batches"] else state.setdefault("baseline", {})
            if payload["value"] is None:
                last.pop(payload["id"], None)
            else:
                last[payload["id"]] = payload["value"]
    elif ep == "/api/comment":
        # Changes and notes queue until Send (user, 2026-09-28); questions go straight away ("now") and get answered in the page.
        payload["status"] = "sent" if payload.pop("now", False) else "queued"
        payload["ts"] = now_iso()
        payload.setdefault("messages", [])
        state["comments"].append(payload)
        event = "message" if payload["status"] == "sent" else None
    elif ep == "/api/comment/delete":
        state["comments"] = [c for c in state["comments"] if c.get("id") != payload.get("id")]
    elif ep == "/api/comment/update":
        for c in state["comments"]:
            if c.get("id") == payload.get("id"):
                c.update({k: v for k, v in payload.get("fields", {}).items() if k in ("suggest", "text", "kind")})
    elif ep == "/api/reply":
        # the user continues a conversation → it goes to Claude again
        for c in state["comments"]:
            if c.get("id") == payload.get("id"):
                m = c.setdefault("messages", [])
                last = m[-1] if m else None
                if (last and last.get("from") == "you" and last.get("text") == payload.get("text", "")
                        and (datetime.now(timezone.utc) - datetime.fromisoformat(last["t"])).total_seconds() < 10):
                    break                      # the same reply twice in a row (a double press) — keep one
                m.append({"from": "you", "t": now_iso(), "text": payload.get("text", "")})
                c["status"], c["addressed"], c["working"] = "sent", False, False
                event = "message"
    elif ep == "/api/answer":
        # Claude's answer, written by reply.py — shown under the question in the page's thread
        for c in state["comments"]:
            if c.get("id") == payload.get("id"):
                c.setdefault("messages", []).append({"from": "claude", "t": now_iso(), "text": payload.get("text", "")})
                c["addressed"], c["working"] = True, False
                if payload.get("suggest"):
                    c["suggest"] = True
    elif ep == "/api/comment/working":
        # Claude has picked these up: sent → working
        n = 0
        for c in state["comments"]:
            if c.get("status") == "sent" and not c.get("addressed"):
                c["working"] = True
                n += 1
        save(state)
        return {"ok": True, "working": n}, None
    elif ep == "/api/comment/addressed":
        n = 0
        for c in state["comments"]:
            if c.get("status") == "sent" and not c.get("addressed"):
                c["addressed"] = True
                n += 1
            c["working"] = False
        # the round itself is done too: a round of only decisions has no comments to mark, and the
        # page's "working" bar would otherwise wait forever (user, 2026-10-06)
        if state["batches"]:
            state["batches"][-1]["addressed"] = True
        save(state)
        return {"ok": True, "addressed": n}, None
    elif ep == "/api/send":
        ids = []
        for c in state["comments"]:
            if c.get("status") == "queued":
                c["status"] = "sent"
                ids.append(c.get("id"))
        state["batches"].append({
            "round": len(state["batches"]) + 1,
            "ts": now_iso(),
            "commentIds": ids,
            "decisions": dict(state["decisions"]),
        })
        save(state)
        write_inbox(state)
        return {"ok": True, "sent": len(ids)}, "send"
    else:
        return {"error": "unknown endpoint"}, None
    save(state)
    if event == "message":
        write_inbox(state)
    return {"ok": True, "comments": state["comments"]}, event


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        pass  # quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/state":
            return self._json(load())
        if self.path == "/api/agent":      # the session in <page>/.agent, woken on Send
            a = agent()
            if not a:
                return self._json({"agent": None, "status": None})
            try:
                r = json.loads(subprocess.run(["herdr", "agent", "get", a], capture_output=True,
                                              text=True, timeout=5).stdout)["result"]["agent"]
                return self._json({"agent": r.get("agent", a), "status": r.get("agent_status")})
            except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
                return self._json({"agent": a, "status": "not running"})
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/discard":
            threading.Thread(target=discard, daemon=True).start()
            return self._json({"ok": True})
        obj, event = api(self.path, payload)
        if event == "send":
            rnd = len(load()["batches"])
            obj["woke"] = wake(f"The user sent round {rnd} on the page at {HERE}. {working_cmd()} Read {INBOX} and act on it: "
                               f"change requests → revise the page; open questions and notes → answer on the page "
                               f"with reply.py. Then: {addressed_cmd()}")
        elif event == "message":
            PENDING["t"] = time.time()
        return self._json(obj, 404 if obj.get("error") == "unknown endpoint" else 200)


def in_use(port):
    # A connect probe catches listeners that SO_REUSEADDR would let us bind over on macOS.
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def bind():
    host = os.environ.get("HOST", "127.0.0.1")
    for port in [START_PORT] if PINNED else range(START_PORT, START_PORT + 100):
        if in_use(port):
            continue
        try:
            return socketserver.TCPServer((host, port), Handler), port
        except OSError:
            continue
    raise SystemExit(f"port {START_PORT} is in use" if PINNED
                     else f"no free port in {START_PORT}-{START_PORT + 99}")


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    httpd, port = bind()
    PORT, HTTPD = port, httpd
    threading.Thread(target=bundle_messages, daemon=True).start()
    with open(PORT_FILE, "w") as f:
        f.write(str(port))
    with httpd:
        print(f"spec review running at http://localhost:{port}")
        httpd.serve_forever()
