#!/usr/bin/env python3
"""Monitoring center: one page that lists every local server, what it is, and whether it's up.

    python3 ~/.claude/skills/monitoring-center/server.py      # looks for pages under ~/work-tools (SERIES_ROOT=<dir> to change)

Two sources, merged:
  1. Live: every TCP port listening under this user whose process is python/bun/node,
     with its <title>, command and working folder (via lsof). Nothing needs registering.
  2. Known: the Planner, the Agent Monitor, and every html-worker page folder under
     ~/work-tools (a folder with server.py + index.html). Stopped ones get a Start button, which
     launches them in the herdr servers-tab via ~/.claude/scripts/server-pane.sh.

Stop sends SIGTERM to the process listening on that port, after the page asks to confirm.
It re-checks that the pid is still the listener on that port (pids get reused), and it
refuses to stop itself. If the server ran in a pane of the herdr servers-tab, that pane is
closed too; a pane anywhere else (the user's session, an agent's tab) is left alone.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import socketserver
import signal
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("SERIES_ROOT", Path.home() / "work-tools")).expanduser().resolve()
START_PORT = int(os.environ.get("PORT", "7700"))
PINNED = "PORT" in os.environ
RUNTIMES = ("python", "bun", "node", "deno")  # skips macOS services (AirPlay, rapportd) and editor helpers
SKIP_DIRS = {"node_modules", ".git", ".trash", "whisper.cpp", "out"}

# Page folders placed by hand: folder (relative to the series) → group, and a name for pages
# whose <title> isn't one (the pomodoro timer's title is its countdown, "25:00").
# These are defaults; anything the user drags or renames on the page wins over them.
PLACE: dict = {}

# What the user arranged on the page, so a rescan (or a restart) keeps their layout.
#   places: row key  → group id      (a card dragged into a category)
#   labels: group id → display name  (a category renamed; the id never changes)
#   groups: [group id]               (categories the user made, in creation order)
LAYOUT = HERE / "layout.json"
BUILTIN_ORDER = {"Daily": 0, "Video": 1, "Series": 2, "Archive": 90, "Other": 99}


def layout() -> dict:
    base = {"places": {}, "labels": {}, "groups": []}
    try:
        base.update(json.loads(LAYOUT.read_text()))
    except (OSError, ValueError):
        pass
    for k in ("places", "labels"):
        if not isinstance(base[k], dict):
            base[k] = {}
    if not isinstance(base["groups"], list):
        base["groups"] = []
    return base


def save_layout(lay: dict) -> None:
    tmp = LAYOUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(lay, indent=2))
    tmp.replace(LAYOUT)


def group_order(g: str, custom: list) -> int:
    if g in BUILTIN_ORDER:
        return BUILTIN_ORDER[g]
    if g in custom:
        return 60 + custom.index(g)
    m = re.fullmatch(r"app_(\d+)", g)
    return 3 + int(m.group(1)) if m else 50

# Servers that aren't html-worker folders. "match" finds them in a process's command line.
KNOWN = [
    {"name": "Daily Tasks", "what": "Today's task cards (Planner)", "group": "Daily",
     "match": "daily-tasks/server.py", "cwd": ".", "where": "~/.claude/skills/daily-tasks", "cmd": "python3 ~/.claude/skills/daily-tasks/server.py"},
    {"name": "Agent Monitor", "what": "Every Claude session's context, cost and plan use (no tokens)", "group": "Daily",
     "match": " monitor.py", "cwd": "agent-monitor", "where": "agent-monitor", "cmd": "python3 monitor.py"},
]


def sh(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def listening() -> list[dict]:
    """Every listening TCP port under this user, with pid, command and cwd."""
    out, cur, seen = [], {}, set()
    for line in sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-u", os.environ.get("USER", ""), "-Fpcn"]).splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            cur = {"pid": int(val)}
        elif tag == "c":
            cur["proc"] = val
        elif tag == "n":
            m = re.search(r":(\d+)$", val)
            if m and (cur["pid"], m.group(1)) not in seen:
                seen.add((cur["pid"], m.group(1)))
                out.append({**cur, "port": int(m.group(1))})
    out = [s for s in out if any(r in s.get("proc", "").lower() for r in RUNTIMES)]
    # Full command line and cwd, one call each for all pids.
    pids = sorted({s["pid"] for s in out})
    cmds, cwds, panes_by_pid = {}, {}, {}
    if pids:
        for line in sh(["ps", "-o", "pid=,command=", "-p", ",".join(map(str, pids))]).splitlines():
            pid, _, cmd = line.strip().partition(" ")
            cmds[int(pid)] = cmd.strip()
        pid = None
        for line in sh(["lsof", "-a", "-p", ",".join(map(str, pids)), "-d", "cwd", "-Fpn"]).splitlines():
            if line[:1] == "p":
                pid = int(line[1:])
            elif line[:1] == "n" and pid:
                cwds[pid] = line[1:]
        # Environment too (ps eww appends it), for the herdr pane each server runs in.
        for line in sh(["ps", "eww", "-o", "pid=,command=", "-p", ",".join(map(str, pids))]).splitlines():
            pid_s, _, rest = line.strip().partition(" ")
            m = re.search(r"\bHERDR_PANE_ID=(\S+)", rest)
            if m:
                panes_by_pid[int(pid_s)] = m.group(1)
    for s in out:
        s["cmd"], s["cwd"] = cmds.get(s["pid"], ""), cwds.get(s["pid"], "")
        s["pane"] = panes_by_pid.get(s["pid"])
    return out


def title_of(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.2) as r:
            m = re.search(rb"<title>([^<]*)</title>", r.read(20000), re.I)
            return m.group(1).decode("utf-8", "replace").strip() if m else ""
    except Exception:
        return None  # not HTTP, or not answering


def app_roots() -> list:
    """Folders the App Maker runs one server for: app_N/ and research/<niche>/ with a .appmaker.json."""
    roots = [p for p in ROOT.iterdir() if p.is_dir() and (p / ".appmaker.json").exists()]
    if (ROOT / "research").is_dir():
        roots += [p for p in (ROOT / "research").iterdir() if p.is_dir() and (p / ".appmaker.json").exists()]
    return roots


APP_SERVER = ROOT / ".claude" / "skills" / "app-maker" / "scripts" / "app_server.py"


def app_servers() -> list[dict]:
    """One row per app server (App Maker, 2026-09-26): it serves every page of that app."""
    out = []
    for r in app_roots():
        rel = str(r.relative_to(ROOT))
        out.append({"name": f"{rel} — app server", "what": f"Serves every page of {rel} (the App Maker opens them)",
                    "group": rel.split("/")[-1] if rel.startswith("research/") else rel,
                    "match": f"app_server.py {r}", "cwd": rel, "where": rel,
                    "cmd": f"python3 {APP_SERVER} {r}", **PLACE.get(rel, {})})
    return out


def worker_pages() -> list[dict]:
    """html-worker page folders: server.py + index.html side by side — except pages inside an app,
    which that app's one server serves (they must not get a second server writing their answers)."""
    pages = []
    inside = app_roots()
    for sp in ROOT.rglob("server.py"):
        rel = sp.parent.relative_to(ROOT)
        if set(rel.parts) & SKIP_DIRS or rel.parts[:1] == (".claude",) or not (sp.parent / "index.html").exists():
            continue
        if any(r == sp.parent or r in sp.parent.parents for r in inside):
            continue
        m = re.search(r"<title>([^<]*)</title>", (sp.parent / "index.html").read_text(errors="replace"), re.I)
        app = next((p for p in rel.parts if re.fullmatch(r"(app|practice)_\d+", p)), None)
        pages.append({"name": (m.group(1).strip() if m else rel.name) or rel.name, "what": "html-worker page",
                      "group": app or ("Archive" if rel.parts[0] == "archived_research" else "Series"),
                      "dir": str(sp.parent), "cwd": str(rel), "cmd": "python3 server.py", **PLACE.get(str(rel), {})})
    return pages


def herdr_panes() -> dict[str, str]:
    """pane id → the label of the tab it's in, for every pane in every workspace."""
    try:
        labels, where = {}, {}
        for w in json.loads(sh(["herdr", "workspace", "list"]))["result"]["workspaces"]:
            wid = w["workspace_id"]
            for t in json.loads(sh(["herdr", "tab", "list", "--workspace", wid]))["result"]["tabs"]:
                labels[t["tab_id"]] = t.get("label") or ""
            for p in json.loads(sh(["herdr", "pane", "list", "--workspace", wid]))["result"]["panes"]:
                where[p["pane_id"]] = labels.get(p["tab_id"], "")
        return where
    except (ValueError, KeyError, TypeError):
        return {}  # not inside herdr, or its CLI changed: cards just show no pane


def state() -> dict:
    live = listening()
    tabs = herdr_panes()
    for s in live:
        s["pane_tab"] = tabs.get(s["pane"]) if s.get("pane") else None
    with ThreadPoolExecutor(8) as ex:
        titles = list(ex.map(title_of, [s["port"] for s in live]))
    for s, t in zip(live, titles):
        s["title"] = t
    used = set()
    rows = []

    def claim(pred):
        for i, s in enumerate(live):
            if i not in used and pred(s):
                used.add(i)
                return s
        return None

    # Each row carries a key that survives a rescan, a restart and a new port, so the
    # group the user dragged it into sticks to the server rather than to today's pid.
    for k in KNOWN:  # a known server in its own folder (cwd ".") is matched by command alone
        here = str((ROOT / k["cwd"]).resolve())
        s = claim(lambda s: k["match"] in s["cmd"] and (k["cwd"] == "." or s["cwd"] == here))
        rows.append({**k, "key": "known:" + k["match"], "up": bool(s), "port": s and s["port"], "title": s and s["title"],
                     "pid": s and s["pid"], "pane": s and s["pane"], "pane_tab": s and s["pane_tab"]})
    for k in app_servers():
        s = claim(lambda s: k["match"] in s["cmd"])
        rows.append({**k, "key": "appserver:" + k["cwd"], "up": bool(s), "port": s and s["port"], "title": s and s["title"],
                     "pid": s and s["pid"], "pane": s and s["pane"], "pane_tab": s and s["pane_tab"]})
    for p in worker_pages():
        s = claim(lambda s: s["cwd"] == p["dir"] and "server.py" in s["cmd"])
        rows.append({**p, "key": "page:" + p["cwd"], "up": bool(s), "port": s and s["port"], "title": s and s["title"],
                     "pid": s and s["pid"], "pane": s and s["pane"], "pane_tab": s and s["pane_tab"]})
    # Anything else listening: shown so nothing is hidden, but with no Start button.
    for i, s in enumerate(live):
        if i in used or s["port"] == MY_PORT:
            continue
        where = s["cwd"].replace(str(ROOT), "~/work-tools") if s["cwd"] else "?"
        rows.append({"name": s["title"] or f"{s['proc']} on {s['port']}", "what": s["cmd"][:120], "group": "Other",
                     "key": "other:" + (s["cwd"] or f"{s['proc']}:{s['port']}"),
                     "cwd": where, "up": True, "port": s["port"], "title": s["title"], "pid": s["pid"], "other": True,
                     "pane": s["pane"], "pane_tab": s["pane_tab"]})

    lay = layout()
    for r in rows:
        r["home"] = r["group"]                       # where the scan would put it, for "Reset"
        r["group"] = lay["places"].get(r["key"], r["group"])
    # Homes stay in the list even when empty: the page hides them, but a dragged card needs
    # its home to exist as a drop target so it can be put back.
    names = {r["group"] for r in rows} | {r["home"] for r in rows} | set(lay["groups"]) | {"Daily"}
    groups = sorted(
        ({"id": g, "label": lay["labels"].get(g, g), "custom": g in lay["groups"]} for g in names),
        key=lambda g: (group_order(g["id"], lay["groups"]), g["label"].lower()))
    return {"rows": rows, "root": str(ROOT), "groups": groups}


SERVERS_TAB = "servers-tab"


def pane_of(pid: int) -> str | None:
    """The herdr pane a process runs in: every process started in a pane inherits HERDR_PANE_ID.
    (HERDR_TAB_ID is inherited too but goes stale when the pane is moved; the pane id doesn't.)"""
    m = re.search(r"\bHERDR_PANE_ID=(\S+)", sh(["ps", "eww", "-o", "command=", "-p", str(pid)]))
    return m.group(1) if m else None


def in_servers_tab(pane: str) -> bool:
    try:
        info = json.loads(sh(["herdr", "pane", "get", pane]))["result"]["pane"]
        tabs = json.loads(sh(["herdr", "tab", "list", "--workspace", info["workspace_id"]]))["result"]["tabs"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(t.get("tab_id") == info["tab_id"] and t.get("label") == SERVERS_TAB for t in tabs)


def stop(pid: int, port: int) -> str:
    if port == MY_PORT:
        raise ValueError("that's the monitoring center itself")
    if not any(s["pid"] == pid and s["port"] == port for s in listening()):
        raise ValueError("that server isn't running any more (or the port changed hands)")
    pane = pane_of(pid)  # read before the process is gone
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):  # up to 2 s for a clean exit
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        raise RuntimeError("asked it to stop, but it's still running")
    if not pane:
        return "stopped"
    if in_servers_tab(pane):
        sh(["herdr", "pane", "close", pane])
        return "stopped, and closed its pane"
    return f"stopped; its pane isn't in {SERVERS_TAB}, so it was left open"


def start(row_cwd: str, cmd: str) -> str:
    cwd = (ROOT / row_cwd).resolve()
    if ROOT not in cwd.parents and cwd != ROOT:
        raise ValueError("folder outside ~/work-tools")
    helper = Path.home() / ".claude" / "scripts" / "server-pane.sh"
    if os.environ.get("HERDR_ENV") == "1" and helper.exists():
        out = subprocess.run([str(helper), str(cwd), cmd], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or "server-pane.sh failed")
        return "started in servers-tab"
    # Outside herdr: a detached process, logging next to the page.
    log = open(cwd / "server.log", "a")
    subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=log, stderr=log, start_new_session=True)
    return "started in the background"


def edit_layout(p: dict) -> str:
    """One endpoint for every arrangement the page can make: move a card, rename a
    category, add one, or delete one. Each call is read-modify-write on one small file."""
    lay = layout()
    act = p.get("act")
    if act == "place":                                   # a card dragged into a category
        key, group = str(p["key"]), str(p["group"])
        if p.get("home") == group:
            lay["places"].pop(key, None)                 # back where the scan puts it: forget the override
        else:
            lay["places"][key] = group
        save_layout(lay)
        return f"moved to {lay['labels'].get(group, group)}"
    if act == "rename":                                  # the id stays, only the display name changes
        gid, label = str(p["group"]), str(p.get("label", "")).strip()[:40]
        if not label:
            raise ValueError("a category needs a name")
        if label == gid:
            lay["labels"].pop(gid, None)
        else:
            lay["labels"][gid] = label
        save_layout(lay)
        return f"renamed to {label}"
    if act == "add":
        label = str(p.get("label", "")).strip()[:40]
        if not label:
            raise ValueError("a category needs a name")
        gid = label
        n = 2
        while gid in lay["groups"] or gid in BUILTIN_ORDER:
            gid, n = f"{label} {n}", n + 1
        lay["groups"].append(gid)
        if gid != label:
            lay["labels"][gid] = label
        save_layout(lay)
        return gid                                       # the page needs the id it got
    if act == "delete":                                  # only a category the user made
        gid = str(p["group"])
        if gid not in lay["groups"]:
            raise ValueError("that category comes from the scan, so it can't be deleted")
        lay["groups"].remove(gid)
        lay["labels"].pop(gid, None)
        lay["places"] = {k: v for k, v in lay["places"].items() if v != gid}   # its cards go home
        save_layout(lay)
        return "deleted"
    raise ValueError("unknown layout action")


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
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send((HERE / "page.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._json(state())
        self.send_error(404)

    def do_POST(self):
        try:
            p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/start":
                known = {(r["cwd"], r["cmd"]) for r in KNOWN + app_servers() + worker_pages()}
                if (p.get("cwd"), p.get("cmd")) not in known:  # only start what's on the list
                    raise ValueError("unknown server")
                return self._json({"ok": True, "msg": start(p["cwd"], p["cmd"])})
            if self.path == "/api/restart":
                # stop, then start the same listed command in the same folder (only listed servers)
                known = {(r["cwd"], r["cmd"]) for r in KNOWN + app_servers() + worker_pages()}
                if (p.get("cwd"), p.get("cmd")) not in known:
                    raise ValueError("only listed servers can be restarted")
                stop(int(p["pid"]), int(p["port"]))
                for _ in range(30):                        # let the old port close, so it comes back on the same one
                    if not in_use(int(p["port"])):
                        break
                    time.sleep(0.1)
                return self._json({"ok": True, "msg": "restarted — " + start(p["cwd"], p["cmd"])})
            if self.path == "/api/stop":
                return self._json({"ok": True, "msg": stop(int(p["pid"]), int(p["port"]))})
            if self.path == "/api/layout":
                return self._json({"ok": True, "msg": edit_layout(p)})
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 400)


def in_use(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    host = os.environ.get("HOST", "127.0.0.1")
    for MY_PORT in [START_PORT] if PINNED else range(START_PORT, START_PORT + 50):
        if not in_use(MY_PORT):
            break
    httpd = socketserver.ThreadingTCPServer((host, MY_PORT), Handler)
    (HERE / ".port").write_text(str(MY_PORT))
    print(f"monitoring center at http://localhost:{MY_PORT}")
    httpd.serve_forever()
