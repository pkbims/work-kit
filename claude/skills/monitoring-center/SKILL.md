---
name: monitoring-center
description: One local page (localhost:7700) listing every running server on this Mac (the Planner, the Agent Monitor, html-worker review pages and anything else listening) with an Open link, a Restart button and a Stop button that also closes the server's herdr pane when it's in servers-tab; "Show stopped" reveals known servers with a Start button. Use when the user asks what's running, where a page is, which port something is on, to open, stop or restart a page/server, or mentions the monitoring center / servers hub — even if they don't name the skill.
---

# Monitoring center

Many small local servers run at once (html-worker review pages, the Planner, the Agent Monitor),
each on whatever port was free. This page is the one address to remember:
**http://localhost:7700/** (it takes the first free port from 7700 and writes it to
`~/.claude/skills/monitoring-center/.port`).

## Start it

```bash
~/.claude/scripts/server-pane.sh ~/work-tools "python3 ~/.claude/skills/monitoring-center/server.py"
```

Outside herdr, `sh ~/work-tools/start.sh` starts it with the other pages. It goes in the herdr
`servers-tab` like every other server, never as a split of the user's pane. Check the title is
`Monitoring Center` before giving the URL.

## What it shows

Refreshes every 10 s (paused while a Stop is awaiting confirmation). Grouped: Daily · Review pages · Other. **Only running servers are shown by default**,
because the user always wants that view. "Show stopped" (remembered per browser) adds the
stopped known servers with their Start buttons.

- **Live scan.** `lsof` lists every TCP port listening under this user. It keeps only
  python/bun/node/deno processes, which leaves out macOS services such as AirPlay on 5000
  and 7000, then reads each one's `<title>`, command and working folder. Nothing needs
  registering.
- **Known servers**, matched to live ones:
  - `KNOWN` in `server.py` lists the Planner and the Agent Monitor, matched by command line.
  - Every html-worker page folder (`server.py` + `index.html` side by side, anywhere under
    `~/work-tools` except `node_modules`) is matched by the process's working folder.
- **Running listed servers get ↻ Restart** (added 2026-09-26): stop, wait for the port to
  close, then Start the same command — so a server picks up code changes and comes back on
  the same port. Only servers on the list (known + html-worker pages); "Other" rows have no
  command to rerun. Servers must probe ports with `SO_REUSEADDR`, or a just-freed port looks
  taken and they drift to the next one (the visualizer did, 7710 → 7711).
- **Stopped known servers get Start.** It runs `~/.claude/scripts/server-pane.sh`, so the
  server opens in servers-tab. Only servers on the known list can be started.
- **Anything else listening goes under Other**, with no Start button, so nothing is hidden.

## Rules

- **Stop** asks to confirm on the page, then sends SIGTERM to the process listening on that
  port (`POST /api/stop {pid, port}`). It first re-checks that this pid still holds that
  port, since pids get reused, and it refuses to stop the monitoring center itself.
- **Stop closes the pane too, but only in servers-tab.** Every process started in a herdr
  pane inherits `HERDR_PANE_ID`, which `ps eww` reads (`pane_of()`). The pane's *current*
  tab is looked up live, because the inherited `HERDR_TAB_ID` goes stale when a pane is
  moved. If that tab is labelled `servers-tab`, the pane is closed after the process exits.
  A pane anywhere else (the user's session, an agent's tab, an older html-worker pane) is
  left open, and the result says so. The confirm text says "and close its pane" only when
  it will.
- Each card shows only the tab name (`servers-tab tab`). The user doesn't want pane ids
  like `w6:p7` in the UI or its messages. It comes from one
  `herdr workspace/tab/pane list` pass per scan across all workspaces.
- `PLACE` in `server.py` is the *default* placement for a page folder, and names pages whose
  `<title>` isn't one (e.g. a timer whose title is its countdown). Anything the user
  arranges on the page beats it.
- **The user arranges the page by dragging, and it sticks.** `layout.json` beside `server.py`
  holds three things, all written through `POST /api/layout`:
  - `places`: row key → group id. A card dragged into a category. The key is stable across a
    rescan, a restart and a new port (`known:<match>`, `page:<folder>`, `other:<cwd>`), so the
    placement follows the server, not today's pid. Dropping a card back where the scan would
    put it (`home`) deletes the override rather than pinning it.
  - `labels`: group id → display name. Renaming never changes the id, so ordering, `PLACE`
    and existing placements keep working; only what's drawn changes.
  - `groups`: categories the user made, in creation order. They sort after the derived ones,
    are always drawn even when empty (a drop target has to exist), and are the only ones that
    can be deleted — deleting one sends its cards back where the scan puts them.
  Derived groups (Daily, Review pages, Other) can be renamed but not deleted;
  they reappear from the scan regardless.
- **Dragging uses pointer events, not HTML5 drag-and-drop.** A card is picked up after the
  pointer moves 5 px (so a click still opens links and buttons), a fixed-position clone
  follows the cursor, and `elementFromPoint` under the clone — which is `pointer-events:none`
  — finds the grid being hovered. HTML5 `draggable` was tried first and silently refused to
  start a drag; pointer events behave the same everywhere and work with a finger too.
- **A drag must never show a category that only holds stopped servers.** The user does not
  want stopped things on this page at all, so an empty derived category stays hidden even
  mid-drag. The single exception is the dragged card's own `home`, revealed while it is in
  the air so it can always be put back; `state()` therefore keeps every row's `home` in the
  group list even when that category is empty.
- Categories are rendered once and empty ones hidden with CSS, never added on dragstart:
  re-rendering mid-drag would rebuild the card being dragged and drop it.
- The 10 s rescan pauses while a drag or a rename is in progress, so it can't clobber either.
- Add a new non-html-worker server to `KNOWN` (name, what, group, `match`, cwd, cmd) when
  a skill introduces one. html-worker pages appear automatically.
- An old html-worker folder may hold a `server.py` from before ports were auto-picked. If
  Start seems to do nothing, check its pane in servers-tab and run `sync.py` on that folder.
