#!/usr/bin/env python3
"""Agent monitor (user, 2026-10-14): every Claude session in every herdr session, workspace and tab, with its
context, cost and the plan limits, on one page. A plain script, like the ticker: it uses no model and spends no
tokens.

    cd ~/work-tools/agent-monitor && python3 monitor.py      # first free port from 7830, written to .port

Where the numbers come from:
  ~/.claude/statusline.py saves each session's status-line payload to ~/.claude/agent-monitor/live/, keyed by
  the herdr pane it runs in (every ~30 s and on every turn). That gives context %, tokens, cost, model, the
  prompt-cache state and the 5-hour / weekly plan use, exactly, with no screen reading.
  `herdr --session S agent list` says which panes still have Claude running and whether each is working.
  Tokens today come from the session's transcript (usage.py beside this file, cached by size).

The loop (every 20 s) also keeps a short history and an event log, and applies a policy: past 70% context it
would ask the agent to wrap up, past 85% it's critical (both set from the page). It logs what it would do; with
"auto" on in data/settings.json it also runs Fresh start by itself at red. Buttons do the same by hand.
"""
from __future__ import annotations

import glob
import http.server
import json
import shlex
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE = Path.home() / ".claude" / "agent-monitor" / "live"
DATA = HERE / "data"
EVENTS, HISTORY = DATA / "events.jsonl", DATA / "history.jsonl"
START_PORT = int(os.environ.get("PORT", "7830"))
WARN, CRIT = 70, 85            # defaults; data/settings.json overrides (set from the page)
SETTINGS = Path(__file__).resolve().parent / "data" / "settings.json"


def thresholds():
    try:
        d = json.loads(SETTINGS.read_text())
        return int(d.get("warn", WARN)), int(d.get("crit", CRIT))
    except Exception:
        return WARN, CRIT


def auto_on():
    """Automatic Fresh start at red (user, 2026-10-15). Off unless switched on from the page."""
    try:
        return bool(json.loads(SETTINGS.read_text()).get("auto"))
    except Exception:
        return False


AUTO_TRIED: dict = {}          # key -> when auto Fresh start last fired, so a failure isn't retried every 20 s
GONE_AFTER = 6 * 3600          # a closed session stays on the page this long
sys.path.insert(0, str(HERE))
try:
    import usage as _usage      # transcript token counting
except Exception:               # the page still works without tokens-today
    _usage = None

LOCK = threading.Lock()
STATE: dict = {"rows": [], "plan": None, "totals": {}, "t": 0}
PREV: dict = {}                 # key -> last level, for "crossed 70%" events
LOGGED: dict | None = None      # key -> last level written to the log (survives restarts, so no repeats)


def sh(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def herdr_json(sess, *args):
    try:
        return json.loads(sh(["herdr", "--session", sess, *args]))["result"]
    except Exception:
        return {}


def sessions():
    out = sh(["herdr", "session", "list"]).splitlines()[1:]
    return [l.split()[0] for l in out if len(l.split()) > 1 and l.split()[1] == "running"]


def topology(sess):
    """Live Claude panes in one herdr session, with their workspace and tab names."""
    wl = herdr_json(sess, "workspace", "list").get("workspaces", [])
    ws = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in wl}
    wnum = {w["workspace_id"]: w.get("number", 0) for w in wl}
    tabs, tpanes, tnum = {}, {}, {}
    for w in ws:
        for t in herdr_json(sess, "tab", "list", "--workspace", w).get("tabs", []):
            tabs[t["tab_id"]] = t.get("label") or t["tab_id"]
            tpanes[t["tab_id"]], tnum[t["tab_id"]] = t.get("pane_count", 1), t.get("number", 0)
    panes = {}
    for a in herdr_json(sess, "agent", "list").get("agents", []):
        if a.get("agent") != "claude":
            continue
        panes[a["pane_id"]] = {"name": a.get("name") or "", "status": a.get("agent_status") or "unknown",
                               "title": a.get("terminal_title_stripped") or "", "cwd": a.get("cwd") or "",
                               "workspace": ws.get(a.get("workspace_id"), a.get("workspace_id")),
                               "tab": tabs.get(a.get("tab_id"), a.get("tab_id")),
                               "workspace_id": a.get("workspace_id"), "tab_id": a.get("tab_id"),
                               "tab_panes": tpanes.get(a.get("tab_id"), 1),
                               "order": (wnum.get(a.get("workspace_id"), 0), tnum.get(a.get("tab_id"), 0))}
    return panes


def today_tokens(path):
    if not _usage or not path or not os.path.exists(path):
        return None
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        f = _usage._file(path, midnight)
        return sum(sum(c[k] for k in _usage.KEYS) for c in f["calls"].values())
    except Exception:
        return None


_LU = {}


def last_used(path):
    """When the session was last really used: the newest prompt or answer in its transcript. Not the file's
    mtime: Claude Code appends bookkeeping lines (away summaries, bridge state) to idle transcripts, and the
    status line refreshes every 30 s regardless (2026-10-14: every session read "38 min ago")."""
    if not path or not os.path.exists(path):
        return None
    st = os.stat(path)
    hit = _LU.get(path)
    if hit and hit[0] == (st.st_size, st.st_mtime):
        return hit[1]
    found = None
    with open(path, "rb") as f:
        for back in (256 * 1024, None):              # the tail is nearly always enough; else the whole file
            f.seek(max(0, st.st_size - back) if back else 0)
            for line in reversed(f.read().decode("utf-8", "replace").splitlines()):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") in ("user", "assistant") and d.get("timestamp") and not d.get("isMeta"):
                    found = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                    break
            if found or not back or st.st_size <= back:
                break
    _LU[path] = ((st.st_size, st.st_mtime), found)
    return found


def level(pct):
    warn, crit = thresholds()
    return "crit" if pct is not None and pct >= crit else "warn" if pct is not None and pct >= warn else "ok"


def event(kind, text, **extra):
    DATA.mkdir(exist_ok=True)
    with open(EVENTS, "a") as f:
        f.write(json.dumps({"t": time.time(), "kind": kind, "text": text, **extra}) + "\n")


def tail(path, n):
    if not path.exists():
        return []
    lines = path.read_text().splitlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]


def gather():
    now = time.time()
    live = {s: topology(s) for s in sessions()}
    rows, plan = [], None
    snaps = []
    for f in glob.glob(str(LIVE / "*.json")):
        try:
            snaps.append(json.load(open(f)))
        except Exception:
            continue
    # One row per Claude session: if a session left snapshots under two panes, keep the newest.
    newest = {}
    for sn in snaps:
        sid = (sn.get("payload") or {}).get("session_id") or id(sn)
        if sid not in newest or sn.get("t", 0) > newest[sid].get("t", 0):
            newest[sid] = sn
    snaps = sorted(newest.values(), key=lambda sn: -sn.get("t", 0))
    # Which live pane each snapshot belongs to. A pane moved to another workspace gets a new id, but the Claude
    # inside keeps reporting its old one (2026-10-14: yt-backend showed as closed after its move to w3). So a
    # snapshot whose pane is gone but which is still updating is matched to an unclaimed live pane in the same
    # herdr session by name, then terminal title, then folder.
    claimed = {(sn.get("herdr_session"), sn.get("pane")) for sn in snaps if sn.get("pane") in live.get(sn.get("herdr_session"), {})}
    where = {}
    for sn in snaps:
        sess, pane = sn.get("herdr_session", ""), sn.get("pane", "")
        if pane in live.get(sess, {}):
            where[id(sn)] = pane
            continue
        if now - sn.get("t", 0) > 180:          # not updating: really closed
            continue
        p = sn.get("payload") or {}
        free = [(pid, a) for pid, a in live.get(sess, {}).items() if (sess, pid) not in claimed]
        sname, cwd = (p.get("session_name") or "").strip(), p.get("cwd") or ""
        hit = (next((pid for pid, a in free if sname and a["name"] == sname), None)
               or next((pid for pid, a in free if sname and a["title"] == sname), None)
               or next((pid for pid, a in free if a["cwd"] and cwd.startswith(a["cwd"].rstrip("/") + "/") or a["cwd"] == cwd), None))
        if hit:
            where[id(sn)] = hit
            claimed.add((sess, hit))
    for snap in snaps:
        p, sess = snap.get("payload", {}), snap.get("herdr_session", "")
        pane = where.get(id(snap), snap.get("pane", ""))
        alive = id(snap) in where
        if not alive and now - snap.get("t", 0) > GONE_AFTER:
            continue
        info = live.get(sess, {}).get(pane, {})
        cw, cost, cache, rl = p.get("context_window") or {}, p.get("cost") or {}, p.get("prompt_cache") or {}, p.get("rate_limits") or {}
        if rl and (plan is None or snap["t"] > plan["t"]):
            plan = {"t": snap["t"], "five_h": (rl.get("five_hour") or {}).get("used_percentage"),
                    "five_h_reset": (rl.get("five_hour") or {}).get("resets_at"),
                    "week": (rl.get("seven_day") or {}).get("used_percentage"),
                    "week_reset": (rl.get("seven_day") or {}).get("resets_at")}
        pct = cw.get("used_percentage")
        row = {"key": f"{sess}/{pane}", "herdr_session": sess, "pane": pane, "alive": alive,
               "name": info.get("name") or "", "status": info.get("status") if alive else "closed",
               "title": info.get("title") or p.get("session_name") or "", "workspace": info.get("workspace") or snap.get("workspace"),
               "tab": info.get("tab") or snap.get("tab"), "cwd": p.get("cwd") or info.get("cwd") or "",
               "workspace_id": info.get("workspace_id"), "tab_id": info.get("tab_id"), "tab_panes": info.get("tab_panes"),
               "order": info.get("order") or (999, 999),
               "model": (p.get("model") or {}).get("display_name"), "effort": (p.get("effort") or {}).get("level"),
               "model_id": (p.get("model") or {}).get("id"), "handover": (HANDOVERS.get(f"{sess}/{pane}") or {}).get("phase"),
               "transcript": p.get("transcript_path"),
               "ctx_pct": pct, "ctx_tokens": cw.get("total_input_tokens"), "ctx_size": cw.get("context_window_size"),
               "cost": cost.get("total_cost_usd"), "cache_warm": cache.get("warm"), "cache_expires": cache.get("expires_at"),
               "recache_tokens": cache.get("recache_tokens_if_cold"), "updated": snap.get("t"),
               "session_id": p.get("session_id"), "tokens_today": today_tokens(p.get("transcript_path")),
               "last_used": last_used(p.get("transcript_path")),
               "level": level(pct)}
        # what deserves a look, in plain words
        notes = []
        if row["level"] == "crit":
            notes.append("restart soon")
        elif row["level"] == "warn":
            notes.append("wrap up soon")
        if alive and row["status"] in ("idle", "done") and (pct or 0) >= 50 and row["cache_warm"] and row["cache_expires"]:
            mins = (row["cache_expires"] - now) / 60
            if mins < 20:
                notes.append(f"cache expires in {max(0, round(mins))} min")
        if alive and row["status"] == "working" and now - (row["updated"] or now) > 15 * 60:
            notes.append("no update for 15+ min")
        row["notes"] = notes
        rows.append(row)
        # policy (prototype): log level changes and what it would do; never acts by itself
        k, lv = row["key"], row["level"]
        global LOGGED
        if LOGGED is None:
            LOGGED = {e.get("key"): e["kind"] for e in tail(EVENTS, 1000) if e.get("kind") in ("warn", "crit")}
        if lv == "ok":
            LOGGED.pop(k, None)
        if alive and PREV.get(k) != lv and lv != "ok" and LOGGED.get(k) != lv:
            LOGGED[k] = lv
            who = row["name"] or row["title"] or k
            event(lv, f"{who} crossed {thresholds()[0] if lv == 'warn' else thresholds()[1]}% context ({pct}%).",
                  would=f"ask {who} to wrap up and restart fresh" if lv == "warn" else f"restart {who} after it saves its notes",
                  key=k)
        PREV[k] = lv
        # Auto Fresh start at red. Only sessions in use: working, or idle with a warm cache. A cold idle session
        # would re-read its whole context at full price just to restart, and nobody is waiting on it.
        # Never one at a permission prompt (the message would land on it). At most once an hour per session.
        if (auto_on() and alive and lv == "crit" and not row["handover"]
                and (row["status"] == "working" or (row["status"] in ("idle", "done") and row["cache_warm"]))
                and now - AUTO_TRIED.get(k, 0) > 3600):
            AUTO_TRIED[k] = now
            threading.Thread(target=_auto_fresh, args=(dict(row),), daemon=True).start()
    order = {"working": 0, "blocked": 1, "idle": 2, "done": 2, "unknown": 3, "closed": 4}
    # Herdr's own order: session, then workspace, then tab (live first; closed ones at the end of their session)
    rows.sort(key=lambda r: (r["herdr_session"], not r["alive"], tuple(r["order"]), order.get(r["status"], 3)))
    alive = [r for r in rows if r["alive"]]
    totals = {"live": len(alive), "working": sum(r["status"] == "working" for r in alive),
              "ctx_tokens": sum(r["ctx_tokens"] or 0 for r in alive),
              "cost_live": round(sum(r["cost"] or 0 for r in alive), 2),
              "tokens_today": sum(r["tokens_today"] or 0 for r in rows),
              "flagged": sum(bool(r["notes"]) for r in alive)}
    with LOCK:
        STATE.update(rows=rows, plan=plan, totals=totals, t=now)
    # one history point a minute: plan use and how much context is held open
    last = tail(HISTORY, 1)
    if not last or now - last[0]["t"] >= 60:
        DATA.mkdir(exist_ok=True)
        with open(HISTORY, "a") as f:
            f.write(json.dumps({"t": now, "five_h": (plan or {}).get("five_h"), "week": (plan or {}).get("week"),
                                "ctx_tokens": totals["ctx_tokens"], "working": totals["working"]}) + "\n")


def loop():
    while True:
        try:
            gather()
        except Exception as e:
            event("error", f"monitor loop: {e}")
        time.sleep(20)


WRAP = ("From the agent monitor: your context is at {pct}%. Please wrap up: finish or pause what you're doing, "
        "save or commit your work, write a short note of where you are and what's next, then say you're ready "
        "for a fresh session.")


# The monitor's own tab: never closed from its own page.
SELF_TAB = (os.path.basename(os.path.dirname(os.environ.get("HERDR_SOCKET_PATH", ""))) if "/sessions/" in os.environ.get("HERDR_SOCKET_PATH", "")
            else "default", os.environ.get("HERDR_TAB_ID"))


def close_tab(p):
    """Close a herdr tab (user, 2026-10-14: "close the herdr session mean closing the tab"). Ends every pane in it."""
    sess, tab = str(p.get("herdr_session", "")), str(p.get("tab_id", ""))
    with LOCK:
        rows = [r for r in STATE["rows"] if r["alive"] and r["herdr_session"] == sess and r["tab_id"] == tab]
    if not rows:
        raise ValueError("that tab isn't open any more")
    if (sess, tab) == SELF_TAB:
        raise ValueError("that's the monitor's own tab")
    r = subprocess.run(["herdr", "--session", sess, "tab", "close", tab], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout).strip()[:200] or "herdr refused")
    event("action", f"Closed tab \"{rows[0]['tab']}\" in {sess} ({', '.join(x['name'] or x['title'] for x in rows)}).")
    gather()
    return {"ok": True}


# ---- Fresh start (user, 2026-10-14: "can this be one button, wrap-up and restart?") ----
# 1 wrap up: ask the session to save its work and give its handover note as its reply ("HANDOVER NOTE" first).
#   Not as a file: writing outside its folder stops at a permission prompt (tested 2026-10-14).
# 2 wait (up to 20 min) for that reply in its transcript and for the session to be idle; the monitor saves the
#   note under data/handovers/ for the record.
# 3 restart: /exit (Esc if it stops at a prompt), then a fresh Claude in the same pane, same model and effort,
#   with the note pasted into its first message, so it needs no file permission either.
HANDOVERS = {}          # key -> {"phase", "note", "t"}
NOTES = DATA / "handovers"
FRESH_WRAP = ("From the agent monitor: you're getting a fresh start (your context is at {pct}%). Please finish or "
              "pause what you're doing and save or commit your work. Then end your reply with your handover note, "
              "starting with a line that says exactly HANDOVER NOTE, and covering: what you were doing, where it "
              "stands, what's next, and any file paths or commands the next session needs. Don't write it to a file. "
              "After that reply you'll be restarted with a clean context, and the new session starts from the note.")
FRESH_BUSY = ("From the agent monitor: you're getting a fresh start (your context is at {pct}%). Don't stop now: "
              "first finish the task you're on, and answer the user as you normally would. Then save or commit your "
              "work and end that reply with your handover note, starting with a line that says exactly HANDOVER NOTE, "
              "and covering: what you were doing, where it stands, what's next, and any file paths or commands the "
              "next session needs. Don't write it to a file. After that reply you'll be restarted with a clean "
              "context, and the new session starts from the note.")
FRESH_FIRST = ("You're continuing work from a previous session that was restarted for a fresh context (nothing is "
               "wrong). Its handover note follows. Carry on from it, and ask the user if anything is unclear.\n\n{text}")


def _note_since(path, t0):
    """The handover note from the session's replies after t0: the text from the last 'HANDOVER NOTE' line on."""
    if not path or not os.path.exists(path):
        return None
    texts = []
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant" or not d.get("timestamp"):
            continue
        if datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp() < t0:
            continue
        for c in (d.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c["text"])
    full = "\n".join(texts)
    i = full.rfind("HANDOVER NOTE")
    return full[i:].strip() if i >= 0 else None


def _agent_in(sess, pane):
    a = next((a for a in herdr_json(sess, "agent", "list").get("agents", []) if a.get("pane_id") == pane), None)
    return a if a and a.get("agent") == "claude" else None


def _fresh(row):
    k, sess, pane, who = row["key"], row["herdr_session"], row["pane"], row["name"] or row["title"] or row["key"]
    h = HANDOVERS[k]
    try:
        t0, text = h["t"] - 5, None
        while True:
            a = _agent_in(sess, pane)
            st = a.get("agent_status") if a else None
            h["phase"] = "waiting for your OK in its tab" if st == "blocked" else h["label"]
            if st in ("idle", "done"):
                text = _note_since(row.get("transcript"), t0)
                if text:
                    break
            if not a:
                raise RuntimeError("it closed before handing over")
            if time.time() - t0 > h["wait"] * 60:
                raise RuntimeError(f"no handover note after {h['wait']} min, so it wasn't restarted")
            time.sleep(8)
        Path(h["note"]).write_text(text + "\n")
        h["phase"] = "restarting"
        sh(["herdr", "--session", sess, "agent", "prompt", pane, "/exit"], timeout=20)
        def gone(secs):                           # has Claude left the pane?
            for _ in range(secs):
                if not _agent_in(sess, pane):
                    return True
                time.sleep(1)
            return False
        if not gone(30):
            sh(["herdr", "--session", sess, "pane", "send-keys", pane, "esc"])   # "unsent feedback draft" prompt
            if not gone(15):
                raise RuntimeError("it didn't exit, so nothing was restarted. Check its tab")
        # Start where it was working, not in the home folder: Claude asks "trust this folder?" every time in home,
        # but never in a folder under one already trusted (tested 2026-10-15). Its shell is home after /exit.
        work = row.get("cwd") or ""
        if work and os.path.isdir(work) and os.path.realpath(work) != os.path.realpath(os.path.expanduser("~")):
            sh(["herdr", "--session", sess, "pane", "run", pane, "cd " + shlex.quote(work)], timeout=20)
            time.sleep(1)
        import re
        name = re.sub(r"[^a-z0-9_-]", "-", (row["name"] or row["title"] or "agent").lower()).strip("-")[:28] or "agent"
        if not name[0].isalpha():
            name = "a-" + name
        args = ["herdr", "--session", sess, "agent", "start", name, "--kind", "claude", "--pane", pane, "--"]
        if row.get("model_id"):
            args += ["--model", row["model_id"]]
        if row.get("effort"):
            args += ["--effort", row["effort"]]
        args += ["--name", row["name"] or row["title"] or name]
        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            # A new Claude can stop at a question before it's ready, e.g. "do you trust this folder?" for a session
            # in the home folder (2026-10-14). That answer is the user's, and Claude never remembers it for the home
            # folder. Wait up to 12 h for it: giving up after 30 min lost orch-shelf's note (2026-10-15).
            if "agent_not_ready" not in (r.stdout + r.stderr):
                raise RuntimeError("the new session didn't start: " + (r.stderr or r.stdout).strip()[:160])
            h["phase"] = "waiting for you to press Yes (trust folder) in its tab"
            for _ in range(12 * 720):
                a = _agent_in(sess, pane)
                if not a:
                    raise RuntimeError("the new session closed before it was ready")
                if a.get("agent_status") in ("idle", "done"):
                    break
                time.sleep(5)
            else:
                raise RuntimeError(f"the new session waited 12 h for your answer in its tab; its note is in {h['note']}")
        sh(["herdr", "--session", sess, "agent", "prompt", name, FRESH_FIRST.format(text=text)], timeout=20)
        h["phase"] = None
        event("action", f"Fresh start done for {who}: it saved a note and restarted with a clean context.", key=k)
    except Exception as e:
        h["phase"] = None
        event("error", f"Fresh start for {who} stopped: {e}", key=k)


def fresh_start(row):
    k = row["key"]
    if (HANDOVERS.get(k) or {}).get("phase"):
        raise ValueError("a fresh start is already running for it")
    NOTES.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in (row["name"] or row["title"] or "agent").lower())[:40]
    note = str(NOTES / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}.md")
    # A busy session gets the message queued mid-task (user, 2026-10-14: "ask it to fresh-start after completing
    # the task it is currently in"), and up to 2 hours to finish that task before handing over.
    busy = row["status"] == "working"
    r = subprocess.run(["herdr", "--session", row["herdr_session"], "agent", "prompt", row["pane"],
                        (FRESH_BUSY if busy else FRESH_WRAP).format(pct=row["ctx_pct"])],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout).strip()[:200] or "herdr refused")
    label = "finishing its task, then handing over" if busy else "wrapping up"
    HANDOVERS[k] = {"phase": label, "label": label, "note": note, "t": time.time(),
                    "wait": 120 if busy else 20}
    event("action", f"Fresh start for {row['name'] or row['title']}: " + ("asked it to finish its task, then hand over."
          if busy else "asked it to save its work and hand over."), key=k)
    threading.Thread(target=_fresh, args=(dict(row),), daemon=True).start()
    return {"ok": True}


def _auto_fresh(row):
    try:
        fresh_start(row)
        event("action", f"Auto: {row['name'] or row['title'] or row['key']} reached {row['ctx_pct']}%, so Fresh start began by itself.",
              key=row["key"])
    except Exception as e:
        event("error", f"Auto Fresh start for {row['name'] or row['title'] or row['key']} didn't start: {e}", key=row["key"])


def act(p):
    with LOCK:
        row = next((r for r in STATE["rows"] if r["key"] == p.get("key")), None)
    if not row or not row["alive"]:
        raise ValueError("that session isn't running any more")
    if p.get("action") == "fresh":
        if row["status"] not in ("idle", "done", "working"):
            raise ValueError("it's waiting on a question in its tab. Answer that first")
        return fresh_start(row)
    if row["status"] not in ("idle", "done"):
        raise ValueError("it's busy. Try again when it's idle")
    text = {"wrap": WRAP.format(pct=row["ctx_pct"]), "compact": "/compact"}.get(p.get("action"))
    if not text:
        raise ValueError("unknown action")
    r = subprocess.run(["herdr", "--session", row["herdr_session"], "agent", "prompt", row["pane"], text],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout).strip()[:200] or "herdr refused")
    who = row["name"] or row["title"] or row["key"]
    event("action", f"Sent {'wrap-up request' if p['action'] == 'wrap' else '/compact'} to {who} (by hand).", key=row["key"])
    return {"ok": True}


# ---- Chat center (user, 2026-10-14: "I need a chat center in that as well"), like Daily Tasks': one running
# thread in data/chat.json. A message wakes the Claude session picked in "Goes to" (data/agent = "session/pane");
# that session marks it working and answers with chat.py.
CHAT, AGENT_FILE = DATA / "chat.json", DATA / "agent"
PORT = None


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def chat_load():
    return json.loads(CHAT.read_text()) if CHAT.exists() else []


def chat_save(items):
    tmp = CHAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    tmp.replace(CHAT)


def chosen():
    return AGENT_FILE.read_text().strip() if AGENT_FILE.exists() else None


def chat_send(text):
    text = text.strip()
    if not text:
        raise ValueError("write a message first")
    with LOCK:
        items = chat_load()
        items.append({"id": f"m{int(time.time() * 1000)}", "from": "you", "text": text, "t": now_iso(), "status": "sent"})
        chat_save(items)
    pick, tool = chosen(), HERE / "chat.py"
    woke = False
    if pick and "/" in pick:
        sess, pane = pick.split("/", 1)
        msg = (f'Bimal wrote in the Agent Monitor chat (page at http://localhost:{PORT}): "{text}". '
               f"First: python3 {tool} working. Live data on every Claude session (context, cost, plan use) is at "
               f"http://localhost:{PORT}/api/state; the monitor's code is {HERE}/monitor.py. Only if he asks you to act "
               f"on a session: POST http://localhost:{PORT}/api/action with {{key, action: wrap|compact}}. "
               f'Then answer in the chat, short and plain: python3 {tool} reply "your answer"')
        try:
            woke = subprocess.run(["herdr", "--session", sess, "agent", "prompt", pane, msg],
                                  capture_output=True, text=True, timeout=20).returncode == 0
        except Exception:
            woke = False
    return {"ok": True, "woke": woke, "chat": chat_load()}


def chat_mark(frm, to, reply=None):
    with LOCK:
        items = chat_load()
        for m in items:
            if m.get("from") == "you" and m.get("status") in frm:
                m["status"] = to
        if reply:
            items.append({"id": f"m{int(time.time() * 1000)}", "from": "claude", "text": reply.strip(), "t": now_iso()})
        chat_save(items)
    return {"ok": True}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
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
        if self.path == "/api/chat":
            return self._json({"chat": chat_load(), "version": CHAT.stat().st_mtime if CHAT.exists() else 0})
        if self.path == "/api/agents":
            with LOCK:
                rows = [r for r in STATE["rows"] if r["alive"]]
            return self._json({"chosen": chosen(), "agents": [{"id": f"{r['herdr_session']}/{r['pane']}", "session": r["herdr_session"],
                               "name": r["name"] or r["title"], "status": r["status"]} for r in rows]})
        if self.path == "/api/state":
            with LOCK:
                s = dict(STATE)
            hist = [h for h in tail(HISTORY, 600) if time.time() - h["t"] < 12 * 3600]
            return self._json({**s, "events": tail(EVENTS, 60)[::-1], "history": hist,
                               "policy": {"warn": thresholds()[0], "crit": thresholds()[1], "auto": auto_on()}})
        self.send_error(404)

    def do_POST(self):
        try:
            p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/action":
                return self._json(act(p))
            if self.path == "/api/close-tab":
                return self._json(close_tab(p))
            if self.path == "/api/chat":
                return self._json(chat_send(str(p.get("text", ""))))
            if self.path == "/api/settings":
                w, c = int(p["warn"]), int(p["crit"])
                if not 10 <= w < c <= 99:
                    raise ValueError("amber must be below red, both between 10 and 99")
                auto = bool(p.get("auto", auto_on()))
                SETTINGS.write_text(json.dumps({"warn": w, "crit": c, "auto": auto}))
                event("action", f"Settings changed: amber {w}%, red {c}%, auto Fresh start {'on' if auto else 'off'}.")
                gather()
                return self._json({"ok": True, "warn": w, "crit": c, "auto": auto})
            if self.path == "/api/chat/working":
                return self._json(chat_mark(("sent",), "working"))
            if self.path == "/api/chat/reply":
                if not str(p.get("text", "")).strip():
                    raise ValueError("empty reply")
                return self._json(chat_mark(("sent", "working"), "done", str(p["text"])))
            if self.path == "/api/agent":
                pick = str(p.get("id", ""))
                if not __import__("re").fullmatch(r"[\w.-]{1,40}/[\w:.-]{1,40}", pick):
                    raise ValueError("bad session/pane id")
                AGENT_FILE.write_text(pick)
                return self._json({"ok": True, "chosen": pick})
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 400)


def in_use(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    threading.Thread(target=loop, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    port = next(p for p in range(START_PORT, START_PORT + 50) if not in_use(p))
    httpd = socketserver.ThreadingTCPServer((os.environ.get("HOST", "127.0.0.1"), port), Handler)
    (HERE / ".port").write_text(str(port))
    PORT = port
    print(f"agent monitor at http://localhost:{port}")
    httpd.serve_forever()
