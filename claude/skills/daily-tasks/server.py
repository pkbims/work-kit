#!/usr/bin/env python3
"""Daily task list: say what you're doing today, get task cards, tick them off.

    python3 ~/.claude/skills/daily-tasks/server.py

One file per calendar date in ~/work-tools/tasks/YYYY-MM-DD.json (TASKS=<dir> to change it). "Make my task list" sends what you
said to Claude with `claude -p` (no tools, no saved session) and gets back 2-5 tasks
with 2-4 steps each. Ticking, adding and removing are plain saves; Claude isn't involved.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = Path(os.environ.get("TASKS", Path.home() / "work-tools" / "tasks")).expanduser().resolve()
PORT_FILE = TASKS / ".port"
START_PORT = int(os.environ.get("PORT", "7800"))
PINNED = "PORT" in os.environ
MODEL = os.environ.get("TASKS_MODEL", "sonnet")
# Card making may open links in what was said (user, 2026-10-23). Read-only tools only: WebFetch, plus any
# connector read tools named in TASKS_LINK_TOOLS (space separated tool names).
LINK_TOOLS = ["WebFetch", *os.environ.get("TASKS_LINK_TOOLS", "").split()]
LOCK = threading.Lock()

PROMPT = """You turn a spoken plan for a workday into task cards.
Return ONLY JSON, no prose, shaped exactly:
{"tasks":[{"icon":"<one emoji>","title":"<max 6 words>","why":"<max 8 words, what it's for>","minutes":<whole number>,"steps":["<max 7 words>", ...]}]}
Rules: 2-5 tasks, each with 2-4 steps. Plain words, no jargon. Keep the speaker's order.
"minutes" is the time limit for the task: if the speaker gives a duration or a limit ("30 minutes each",
"two hours"), use it; otherwise estimate realistically, 15-240.
Merge small things into one task rather than making many tiny ones.
If an existing list is given and the speaker is adding to it, keep the existing tasks and
repeat done steps' text exactly so they stay ticked."""

ADD_PROMPT = """You turn one spoken task into a single task card.
Return ONLY JSON, no prose, shaped exactly:
{"icon":"<one emoji>","title":"<max 6 words>","why":"<max 8 words, what it's for>","minutes":<whole number>,"steps":["<max 7 words>", ...]}
Rules: exactly one task, 2-4 steps. "minutes" is its time limit: the speaker's if given, else a realistic
estimate, 15-240. Plain words, no jargon. Even if the speaker mentions
several things, keep them as steps of this one task."""


def path(d: str) -> Path:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError(f"bad date {d!r}")
    return TASKS / f"{d}.json"


def load(d: str) -> dict:
    p = path(d)
    return json.loads(p.read_text()) if p.exists() else {"date": d, "tasks": []}


def save(d: str, data: dict) -> None:
    p = path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(p)  # atomic, so a crash mid-write never leaves half a file


def dates() -> list[str]:
    ds = {p.stem for p in TASKS.glob("????-??-??.json")} | {date.today().isoformat()}
    return sorted(ds)


def view(d: str) -> dict:
    return {"dates": dates(), "today": date.today().isoformat(), "list": load(d)}


def ask(prompt: str, text: str) -> dict:
    out = subprocess.run(
        ["claude", "-p", "--no-session-persistence", "--tools", "WebFetch", "--allowedTools", *LINK_TOOLS, "--model", MODEL,
         "--output-format", "json", "--system-prompt", prompt],
        input=text, capture_output=True, text=True, timeout=180, cwd=str(TASKS),
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:400] or "claude failed")
    reply = json.loads(out.stdout).get("result", "")
    return json.loads(reply[reply.find("{"):reply.rfind("}") + 1])  # tolerate a fence around the JSON


def card(t: dict, done: set[str] = frozenset()) -> dict:
    """A new card starts in Planned with Claude's time limit. Board fields (user, 2026-10-13, Motion-style):
    minutes = time limit; stage = planned | started | completed; started_at / done_at = ISO times set by the page."""
    try:
        minutes = max(5, min(600, int(t.get("minutes") or 30)))
    except (TypeError, ValueError):
        minutes = 30
    steps = [{"text": s, "done": s in done} for s in t.get("steps", [])]
    return {"icon": t.get("icon", "•"), "title": t.get("title", ""), "why": t.get("why", ""), "minutes": minutes,
            "stage": "completed" if steps and all(s["done"] for s in steps) else "planned", "steps": steps}


def generate(d: str, spoken: str) -> dict:
    cur = load(d)
    got = ask(PROMPT, f"Spoken plan:\n{spoken}\n\nExisting list (may be empty):\n{json.dumps(cur['tasks'], ensure_ascii=False)}")
    done = {s["text"] for t in cur["tasks"] for s in t["steps"] if s.get("done")}
    tasks = [card(t, done) for t in got.get("tasks", [])]
    # A task that survives the rewrite keeps its place on the board, its timer and any time limit he set.
    old = {t.get("title"): t for t in cur["tasks"]}
    for t in tasks:
        o = old.get(t["title"])
        if o:
            for k in ("stage", "started_at", "done_at", "paused_at", "minutes"):
                if k in o:
                    t[k] = o[k]
    with LOCK:
        cur = load(d)
        cur["tasks"], cur["spoken"] = tasks, spoken
        save(d, cur)
    return view(d)


def add(d: str, spoken: str) -> dict:
    """One spoken task becomes one new card. The existing list is never sent to Claude,
    so nothing already there can be reworded or lose its ticks."""
    if not spoken.strip():
        raise ValueError("say the task first")
    new = card(ask(ADD_PROMPT, f"Spoken task:\n{spoken}"))
    with LOCK:
        cur = load(d)  # re-read: ticks made while Claude was writing must survive
        cur["tasks"].append(new)
        save(d, cur)
    return view(d)


def move(src: str, dst: str, index: int) -> dict:
    """Move one card to another day, keeping its steps and ticks exactly as they are.
    Both files are written under the one lock, so a move can't race a tick on either day."""
    if src == dst:
        raise ValueError("that card is already on that day")
    path(src), path(dst)  # validates both dates before anything is written
    with LOCK:
        a = load(src)
        if not 0 <= index < len(a["tasks"]):
            raise ValueError("that card isn't there any more — reload the page")
        card_ = a["tasks"].pop(index)
        b = load(dst)
        b["tasks"].append(card_)
        save(dst, b)
        save(src, a)   # the source is written last: a crash mid-move duplicates a card
                       # rather than losing one, and a duplicate is easy to delete.
    return view(src)


# ---- Thoughts (user, 2026-10-13): one running list of rough thoughts across all days, newest first. ----
# tasks/thoughts.json = [{"id", "text", "t", "edited"?}]. Never sent to Claude.
THOUGHTS = TASKS / "thoughts.json"


def thoughts() -> list[dict]:
    return json.loads(THOUGHTS.read_text()) if THOUGHTS.exists() else []


def save_thoughts(items: list[dict]) -> None:
    tmp = THOUGHTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    tmp.replace(THOUGHTS)


def thought(op: str, p: dict) -> list[dict]:
    with LOCK:
        items = thoughts()
        if op == "add":
            text = str(p.get("text", "")).strip()
            if not text:
                raise ValueError("write a thought first")
            items.insert(0, {"id": f"th{int(time.time() * 1000)}", "text": text, "t": now()})
        else:
            hit = next((x for x in items if x["id"] == p.get("id")), None)
            if not hit:
                raise ValueError("that thought isn't there any more — reload the page")
            if op == "edit":
                text = str(p.get("text", "")).strip()
                if not text:
                    raise ValueError("a thought can't be empty — delete it instead")
                hit["text"], hit["edited"] = text, now()
            elif op == "delete":
                items.remove(hit)
            else:
                raise ValueError("unknown thought action")
        save_thoughts(items)
    return items


# ---- Chat center (user, 2026-10-09: "I want a chat center in my daily tasks server as well") ----
# One thread per day, kept in that day's file under "chat". A message wakes the Claude session chosen in
# "Goes to" (a herdr pane, saved in tasks/.agent). That session marks it working and answers with chat.py.
AGENT_FILE = TASKS / ".agent"
PORT = None


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def herdr_sessions() -> list[str]:
    """Every running herdr session. Pane ids like w1:p1 repeat across sessions, so a session is
    always named explicitly; the server may run in a different session from the one it wakes."""
    try:
        out = subprocess.run(["herdr", "session", "list"], capture_output=True, text=True, timeout=5).stdout
        return [l.split()[0] for l in out.splitlines()[1:] if len(l.split()) > 1 and l.split()[1] == "running"]
    except Exception:
        return []


def agents() -> list[dict]:
    out = []
    for sess in herdr_sessions():
        try:
            r = json.loads(subprocess.run(["herdr", "--session", sess, "agent", "list"], capture_output=True,
                                          text=True, timeout=5).stdout)
        except Exception:
            continue
        for a in r.get("result", {}).get("agents", []):
            if a.get("agent") == "claude":
                out.append({"id": f"{sess}/{a['pane_id']}", "session": sess, "pane": a["pane_id"],
                            "status": a.get("agent_status"), "name": a.get("name") or "",
                            "title": a.get("terminal_title_stripped") or a.get("cwd", "")})
    return out


def chosen() -> str | None:
    return AGENT_FILE.read_text().strip() if AGENT_FILE.exists() else None


def wake(text: str) -> bool:
    pick = chosen()                      # "session/pane"
    if not pick or "/" not in pick:
        return False
    sess, pane = pick.split("/", 1)
    try:
        return subprocess.run(["herdr", "--session", sess, "agent", "prompt", pane, text], capture_output=True,
                              text=True, timeout=20).returncode == 0
    except Exception:
        return False


def chat_send(d: str, text: str) -> dict:
    if not text.strip():
        raise ValueError("write a message first")
    with LOCK:
        cur = load(d)
        cur.setdefault("chat", []).append({"id": f"m{int(time.time() * 1000)}", "from": "you", "text": text.strip(),
                                           "t": now(), "status": "sent"})
        save(d, cur)
    tool = HERE / "chat.py"
    woke = wake(f'The user wrote in the Daily Tasks chat ({d}, page at localhost:{PORT}): "{text.strip()}". '
                f"First: python3 {tool} working {d}. You can read that day's list at {path(d)}; to change it, use "
                f"the page server (POST localhost:{PORT}/api/add with {{date, text}} adds one card; never hand-edit the "
                f"file), and never write today's goals for him. Then answer in the chat, short and plain: "
                f'python3 {tool} reply {d} "your answer"')
    return {"ok": True, "woke": woke, "chat": load(d).get("chat", [])}


def chat_mark(d: str, frm: str, to: str) -> None:
    with LOCK:
        cur = load(d)
        for m in cur.get("chat", []):
            if m.get("from") == "you" and m.get("status") == frm:
                m["status"] = to
        save(d, cur)


def chat_reply(d: str, text: str) -> dict:
    with LOCK:
        cur = load(d)
        chat = cur.setdefault("chat", [])
        for m in chat:
            if m.get("from") == "you" and m.get("status") in ("sent", "working"):
                m["status"] = "done"
        chat.append({"id": f"m{int(time.time() * 1000)}", "from": "claude", "text": text.strip(), "t": now()})
        save(d, cur)
    return {"ok": True}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json", code)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send((HERE / "page.html").read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/chat"):
            m = re.search(r"date=(\d{4}-\d{2}-\d{2})", self.path)
            d = m.group(1) if m else date.today().isoformat()
            p = path(d)
            return self._json({"chat": load(d).get("chat", []), "version": p.stat().st_mtime if p.exists() else 0})
        if self.path == "/api/agents":
            return self._json({"agents": agents(), "chosen": chosen()})
        if self.path == "/api/thoughts":
            return self._json({"thoughts": thoughts()})
        if self.path.startswith("/api/state"):
            m = re.search(r"date=(\d{4}-\d{2}-\d{2})", self.path)
            return self._json(view(m.group(1) if m else date.today().isoformat()))
        self.send_error(404)

    def do_POST(self):
        try:
            p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            d = p.get("date", date.today().isoformat())
            if self.path == "/api/tasks":
                with LOCK:
                    cur = load(d)
                    cur["tasks"] = p["tasks"]
                    save(d, cur)
                return self._json({"ok": True})
            if self.path == "/api/goals":  # hand-typed only; never sent to Claude
                with LOCK:
                    cur = load(d)
                    cur["goals"] = p["goals"]
                    save(d, cur)
                return self._json({"ok": True})
            if self.path == "/api/chat":
                return self._json(chat_send(d, p.get("text", "")))
            if self.path == "/api/chat/working":
                chat_mark(d, "sent", "working")
                return self._json({"ok": True})
            if self.path == "/api/chat/reply":
                return self._json(chat_reply(d, p.get("text", "")))
            if self.path == "/api/agent":
                pick = str(p.get("id", ""))
                if not re.fullmatch(r"[\w.-]{1,40}/[\w:.-]{1,40}", pick):
                    raise ValueError("bad session/pane id")
                AGENT_FILE.write_text(pick)
                return self._json({"ok": True, "chosen": pick})
            if self.path in ("/api/thoughts/add", "/api/thoughts/edit", "/api/thoughts/delete"):
                return self._json({"thoughts": thought(self.path.rsplit("/", 1)[1], p)})
            if self.path == "/api/generate":
                return self._json(generate(d, p.get("text", "")))
            if self.path == "/api/add":
                return self._json(add(d, p.get("text", "")))
            if self.path == "/api/move":
                return self._json(move(d, p["to"], int(p["index"])))
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)


def in_use(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def bind():
    host = os.environ.get("HOST", "127.0.0.1")
    for port in [START_PORT] if PINNED else range(START_PORT, START_PORT + 100):
        if in_use(port):
            continue
        try:
            return socketserver.ThreadingTCPServer((host, port), Handler), port
        except OSError:
            continue
    raise SystemExit(f"port {START_PORT} is in use" if PINNED else f"no free port from {START_PORT}")


if __name__ == "__main__":
    TASKS.mkdir(parents=True, exist_ok=True)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    httpd, port = bind()
    PORT = port
    PORT_FILE.write_text(str(port))
    print(f"daily tasks at http://localhost:{port}  (tasks: {TASKS})")
    httpd.serve_forever()
