#!/usr/bin/env python3
"""Tokens and busy time per agent, read off the Claude Code transcripts. No model, no tokens.

    usage.py <since-iso> [agent ...]        # prints the same JSON the ticker puts in status.json

A transcript belongs to an agent when it carries that agent's herdr name ("agentName"); a
subagent's transcript (<session>/subagents/*.jsonl) belongs to its parent's agent. So a side
session opened by hand in a worker's folder is not counted.

Tokens: every model call since <since>, counted once (a fork's copy of its parent's history is
skipped). Busy time: only while a turn is in progress — the model writing or a tool running.
Idle is left out: waiting for the next prompt, after a turn ends, and any silence over 30 min
inside a turn (the Mac asleep; no tool runs that long in the foreground).
"""
import glob, json, os, sys
from datetime import datetime

PROJECTS = os.path.expanduser("~/.claude/projects")
KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
SLEEP = 30 * 60
_cache = {}          # path -> (size, mtime, since, parsed)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _parse(path, since):
    agent, calls, events = None, {}, []
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        agent = agent or d.get("agentName")
        t, ty = d.get("timestamp"), d.get("type")
        if not t or ty not in ("user", "assistant", "system", "attachment"):
            continue
        t = _ts(t)
        if t < since:
            continue
        m = d.get("message") or {}
        if ty == "assistant" and m.get("usage") and m.get("model") != "<synthetic>":
            # one call is streamed over several lines that repeat its usage: keep the largest
            prev = calls.get(m.get("id") or d.get("uuid"), {})
            calls[m.get("id") or d.get("uuid")] = dict(
                {k: max(prev.get(k, 0), m["usage"].get(k) or 0) for k in KEYS}, model=m.get("model"))
        c = m.get("content")
        prompt = ty == "user" and not d.get("isMeta") and not (
            isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c))
        end = (ty == "assistant" and m.get("stop_reason") == "end_turn") or (
            ty == "system" and d.get("subtype") in ("turn_duration", "away_summary", "informational"))
        events.append((t, prompt, end))
    events.sort()
    busy = sum(b - a for (a, _, e), (b, p, _) in zip(events, events[1:])
               if not e and not p and b - a <= SLEEP)
    return {"agent": agent, "calls": calls, "busy": busy}


def _file(path, since):
    st = os.stat(path)
    hit = _cache.get(path)
    if hit and hit[:3] == (st.st_size, st.st_mtime, since):
        return hit[3]
    parsed = _parse(path, since)
    _cache[path] = (st.st_size, st.st_mtime, since, parsed)
    return parsed


def usage(agents, since_iso):
    since = _ts(since_iso) if since_iso else 0
    out = {a: dict({k: 0 for k in KEYS}, calls=0, busy_s=0, sessions=0, models=[]) for a in agents}
    seen = set()
    mains = sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")))
    for path in mains:
        if os.path.getmtime(path) < since:
            continue
        top = _file(path, since)
        a = top["agent"]
        if a not in out:
            continue
        subs = sorted(glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "*.jsonl")))
        row = out[a]
        row["sessions"] += 1
        for p in [path] + subs:
            f = top if p == path else _file(p, since)
            row["busy_s"] += round(f["busy"])
            for mid, u in f["calls"].items():
                if mid in seen:
                    continue
                seen.add(mid)
                row["calls"] += 1
                for k in KEYS:
                    row[k] += u[k]
                if u["model"] and u["model"] not in row["models"]:
                    row["models"].append(u["model"])
    for row in out.values():
        row["total"] = sum(row[k] for k in KEYS)
    return out


if __name__ == "__main__":
    print(json.dumps(usage(sys.argv[2:] or ["code-orchestrator", "backend", "ios"], sys.argv[1]), indent=1))
