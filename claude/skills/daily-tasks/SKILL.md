---
name: daily-tasks
description: The user's daily to-do page — they speak or type their plan for the day, Claude turns it into a few task cards with small tickable steps, one list per calendar date. Use when the user says today's tasks, task list, to-do, "plan my day", "what am I doing today", or wants to add, tick or see tasks — even if they don't name the skill.
---

# Daily tasks

A to-do list the user keeps open all day. They dictate the plan (Fn twice in any text
box), press **Make my task list**, and get 2–5 task cards, each with 2–4 steps, a progress
bar and a count. Later in the day, **+ Add task** takes one dictated task and adds it as
one new card with its own steps. Ticking, adding steps by hand and removing steps and
tasks happen on the page and don't involve Claude. So does editing: click any goal, task title, "why" line or step to edit it in place (Enter or
clicking away saves, Esc cancels). So does dragging: the ⠿ grip (shown on
hover) moves a card among cards, or a step within its card or into another card. It uses the
browser's built-in drag and drop, not a library, so the page stays dependency-free and works
offline; the cost is no touch support, which is fine for a desktop page.

Each card also has a **→** button (next to ×, shown on hover) that moves it to another
day: one click for the days either side and for today, or any date in the date box.
The card keeps its steps and its ticks. `POST /api/move {date, to, index}` does it, writing
the destination first and the source second, so a crash mid-move leaves a duplicate to
delete rather than a lost card. A day with no file yet is created and joins the switcher.

## Files

- `server.py` serves the page, stores lists, calls `claude -p` for generation.
- `page.html` has the date switcher, the speak box and the task cards.
- Data: `~/work-tools/tasks/YYYY-MM-DD.json` (set `TASKS=<dir>` to keep it elsewhere).

## Start it

```bash
python3 ~/.claude/skills/daily-tasks/server.py
```

In herdr, start it with `~/.claude/scripts/server-pane.sh ~/work-tools "python3 ~/.claude/skills/daily-tasks/server.py"` (the `servers-tab` tab). It takes the first free port from 7800 and writes it
to `~/work-tools/tasks/.port`. Check the title is `Daily Tasks` before giving the URL:
`curl -s localhost:$(cat ~/work-tools/tasks/.port)/ | grep -o '<title>[^<]*'`.

## Today's goals

A 🎯 card above the tasks: a few tickable lines the user types by hand, stored as `goals`
in the same day file via `POST /api/goals`. Goals are **always hand-typed** — the user asked
for that explicitly (2026-09-23). Never generate them, never send them to Claude, and don't
fill them in from chat unless the user dictates the exact words.

## How generation works

`POST /api/generate {date, text}` sends the spoken plan plus the existing list to
`claude -p` (`--tools ""`, `--no-session-persistence`, model from `TASKS_MODEL`, default
sonnet) with the rules in `PROMPT`. A step whose text comes back unchanged stays ticked,
so saying more later adds to the list without losing progress. Takes ~5–10 s; the button
shows "Writing your tasks…".

`POST /api/add {date, text}` is for one new task: `ADD_PROMPT` turns just that text into
one card, which is appended. The existing list is never sent to Claude, so a later
addition can't reword a card or untick a step. The tradeoff: it won't notice the new
item belongs inside an existing card. The user chose a separate card they can delete
over a quiet rewrite they might not notice (2026-09-22).

If the user gives their plan in chat instead, call `/api/generate` (a whole plan) or `/api/add` (one
task) rather than writing
the JSON yourself, so the format stays the same.

## Chat center

A panel on the right (a 💬 button on narrow screens) for talking to a Claude session from the
page, one thread per day, stored under `chat` in that day's file. "Goes to" picks the session:
any Claude in any running herdr session, saved in `~/work-tools/tasks/.agent` as `session/pane`. Pane ids like `w1:p1` repeat across herdr sessions, and this server may run in a different session from the one it wakes, so the session is always named (`herdr --session S agent prompt P`). Sending wakes
that session with `herdr agent prompt`; it marks the message working, reads or changes the day
(only through the page server, e.g. `/api/add`; never today's goals), and answers with:

    python3 ~/.claude/skills/daily-tasks/chat.py working YYYY-MM-DD
    python3 ~/.claude/skills/daily-tasks/chat.py reply YYYY-MM-DD "short plain answer"

Statuses: sent → working → done. If the day's file changes (Claude added a card), the task list
reloads unless the user is typing in it. Added at the user's request (2026-10-09).

## Format

Each task: one emoji, a title of up to 6 words, a "why" of up to 8 words, and steps of up
to 7 words each. The user asked for parent tasks with sub-steps that are visual and easy
to take in at a glance, and said they were "going too much in depth". Keep lists short;
merge small items rather than adding cards.

## Planner app and Thoughts (user, 2026-10-13: "transform this into an app like how I did with pomodoro")

- **Dock app:** `~/Applications/Planner.app` runs `open.sh`: it starts the server if nothing answers on
  `~/work-tools/tasks/.port` (with `~/.local/bin` on PATH, since the Dock gives a bare one and the server needs `claude` and
  `herdr`; log in `~/Library/Logs/daily-tasks-server.log`), then opens the page as a chromeless Chrome window at
  `/#app`, or focuses the window whose address ends in `#app` (a normal tab showing Daily Tasks is left alone).
  Rebuild (the kit's install.sh does this): `osacompile -o ~/Applications/Planner.app -e 'do shell script "<abs>/open.sh > /dev/null 2>&1"'`,
  copy `Planner.icns` to `Contents/Resources/applet.icns`, delete `Resources/Assets.car` and the
  `CFBundleIconName` key, `codesign --force --sign -`. The icon's source is `icon-dock.svg` (render with
  `qlmanage -t -s 1024`, cut the corners transparent with PIL, `iconutil`).
- **Tabs:** Today (date picker, board, goals) and 💭 Thoughts. Remembered per browser (`dt-tab`).
- **Thoughts:** one running list across all days in `~/work-tools/tasks/thoughts.json`, newest first, grouped by day;
  ⌘+Enter saves, click to edit, two clicks to delete. Hand-typed only, never sent to Claude.
- **Hide chat:** the chat's ✕ hides it on a wide window (`dt-chat-off`); 💬 Chat brings it back.

## Look and Focus mode (user, 2026-10-13: "clean and futuristic, like Motion … easy to focus")

- **Look v2:** cool neutrals with one indigo accent, Geist for text and Geist Mono for every time and count, a
  frosted header, borderless columns with soft-shadow cards. It's a block of overrides at the end of `<style>`
  ("Look v2"), with tokens in the three `:root` blocks (light, OS dark, toggle dark). The header reads "Planner";
  the `<title>` stays `Daily Tasks` because `open.sh` and the checks look for it.
- **Themes** (◐ menu in the header, `dt-skin`): Auto (Motion, follows the Mac), Motion Light, Motion Dark, Paper
  (the original warm look), Graphite (dark, mint), Nord (blue-grey). Motion was studied from usemotion.com: white,
  a faint lavender wash, indigo-blue buttons with a soft lift, Inter, purple→blue gradient. Motion is the base
  tokens; the others are `:root[data-skin=…]` blocks placed after the light/dark blocks so they win.
- **Simple / Detailed** (header button, default Simple, `dt-view`): Simple hides the why line, step counts,
  progress bars, helper text and the progress summary, shrinks the plan box to one line and puts goals on one line.
  Detailed shows everything. Text is 16px with darker greys in both (user: "too much wording … not easy on the eyes").
- **Focus mode:** ◎ Focus in the header, the ◎ on a started card, or **F**. Full screen: the task, a ring and
  countdown (mm:ss; "+" and red when over), its steps (tickable), and ▶ Start / ✓ Done. Opens on the most recently
  started task, else the first planned one; ‹ › or the arrow keys walk the open tasks; Esc leaves.

## Board (user, 2026-10-13: "like Motion")

The tasks show as a board: **Planned → Started → Completed**. Each card has a time limit (`minutes`,
Claude's estimate, or the speaker's own limit when given; click the ⏱ chip for a picker of 15-minute steps up to 3h, or type another, e.g. "4h").
`stage`, `started_at` and `done_at` live on the card and are set by the page: ▶ Start begins a countdown
(red "N over" past the limit), ✓ Done or ticking the last step completes it and shows how long it took, and
dragging a card to a column sets its stage. Cards from before the board have no `stage`: all steps ticked
reads as Completed, otherwise Planned. Regenerating the list keeps stage, timer and time limit for any task
whose title survives. The – on a column header folds it to a thin strip (click the strip to bring it back);
which columns are folded is remembered per browser (`dt-hidden` in localStorage).

## Rules

- Only the server writes the list files, under a lock, atomically.
- The date switcher always includes today, even before today's file exists.
- After editing `page.html`, check the script before telling the user it works: one syntax error
  leaves the page stuck on "Loading…". `bun build` on the extracted `<script>` catches it;
  loading the page in a browser catches runtime errors too.
