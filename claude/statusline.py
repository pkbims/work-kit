#!/usr/bin/env python3
"""Claude Code status line: plan usage, context and session cost, always on screen.

Claude Code pipes the session JSON in on stdin; whatever this prints becomes the
line under the prompt. Keep it fast — it runs on every render.
A sample payload is saved at /tmp/claude-statusline-payload.json.
"""
import json
import sys
import time

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"


def hue(pct):
    """Green while there's room, amber when it matters, red when it's nearly gone."""
    return "\033[32m" if pct < 60 else "\033[33m" if pct < 85 else "\033[1;31m"


def left(ts):
    secs = int(ts - time.time())
    if secs <= 0:
        return "now"
    hours, mins = divmod(secs // 60, 60)
    return "%dh%02dm" % (hours, mins) if hours else "%dm" % mins


def record(d):
    """Agent monitor (user, 2026-10-14): save this session's numbers, keyed by the herdr pane it runs in, so
    ~/work-tools/agent-monitor sees every Claude session's context, cost and plan use without
    reading screens or spending tokens. Herdr's env (HERDR_PANE_ID, HERDR_SOCKET_PATH) reaches us from the pane."""
    import os
    pane = os.environ.get("HERDR_PANE_ID", "")
    sock = os.environ.get("HERDR_SOCKET_PATH", "")
    sess = os.path.basename(os.path.dirname(sock)) if "/sessions/" in sock else ("default" if pane else "")
    key = ("%s__%s" % (sess, pane) if pane else "nopane__" + str(d.get("session_id", "x"))).replace(":", "_").replace("/", "_")
    out = os.path.expanduser("~/.claude/agent-monitor/live")
    os.makedirs(out, exist_ok=True)
    tmp = os.path.join(out, "." + key + ".tmp")
    with open(tmp, "w") as f:
        json.dump({"t": time.time(), "herdr_session": sess, "pane": pane,
                   "workspace": os.environ.get("HERDR_WORKSPACE_ID", ""), "tab": os.environ.get("HERDR_TAB_ID", ""),
                   "payload": d}, f)
    os.replace(tmp, os.path.join(out, key + ".json"))


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return ""
    try:
        record(d)
    except Exception:
        pass  # the monitor is a nice-to-have; never break the status line
    parts = []

    model = d.get("model", {}).get("display_name", "?")
    effort = d.get("effort", {}).get("level")
    parts.append(BOLD + model + RESET + (DIM + "·" + effort + RESET if effort else ""))

    cw = d.get("context_window") or {}
    pct = cw.get("used_percentage")
    if pct is not None:
        tok = cw.get("total_input_tokens", 0) / 1000.0
        parts.append("%sctx%s %s%d%%%s%s/%.0fk%s" % (DIM, RESET, hue(pct), pct, RESET, DIM, tok, RESET))

    # The plan windows — what "usage" means on a Claude subscription.
    rl = d.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "wk")):
        w = rl.get(key) or {}
        pct = w.get("used_percentage")
        if pct is None:
            continue
        s = "%s%s%s %s%d%%%s" % (DIM, label, RESET, hue(pct), pct, RESET)
        if w.get("resets_at"):
            s += "%s↻%s%s" % (DIM, left(w["resets_at"]), RESET)
        parts.append(s)

    cost = (d.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        parts.append("%s$%.2f%s" % (DIM, cost, RESET))

    if (d.get("prompt_cache") or {}).get("warm") is False:
        parts.append(DIM + "cold cache" + RESET)

    return "  ".join(parts)


if __name__ == "__main__":
    try:
        print(main())
    except Exception as e:  # never leave the line blank on a bad payload
        print("statusline: %s" % str(e)[:60])
