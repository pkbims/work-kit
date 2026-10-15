# Work kit

My Claude Code setup for the work laptop: html-worker, Daily Tasks (Planner), Agent Monitor, Monitoring Center, the herdr skill and helpers, the status line, and my working style.

Nothing personal is in here. No memory, no history, no side-project skills.

## Install (about 5 minutes)

1. **Install Claude Code** and sign in with your work-approved account.
2. **Install herdr** from its official releases page: https://github.com/herdrdev/herdr/releases (this kit was made with 0.9.x). Put it on your PATH (`~/.local/bin` works).
3. **Run the installer** from this folder:

       sh install.sh

4. **Open herdr**, start Claude inside it, and say "plan my day" or "start the planner".
   Or start all three pages yourself: `sh ~/work-tools/start.sh`

## What goes where

| What | Where it ends up |
|---|---|
| html-worker, daily-tasks, monitoring-center, herdr skills | `~/.claude/skills/` (works in every folder) |
| server-pane.sh, start-agent.sh | `~/.claude/scripts/` |
| Status line (also feeds the Agent Monitor) | `~/.claude/statusline.py` |
| Allowed commands, status line setting | merged into `~/.claude/settings.json` (backed up first) |
| My working style | `~/.claude/CLAUDE.md` |
| Daily task lists (this laptop's own) | `~/work-tools/tasks/` |
| Agent Monitor | `~/work-tools/agent-monitor/` |
| html-worker pages that aren't in a project | `~/work-tools/pages/` |
| Planner Dock app | `~/Applications/Planner.app` |

## Good to know

- **Each laptop has its own data.** Tasks, chats and the monitor's history stay on the laptop where they were made. Nothing syncs.
- **The Monitoring Center** (http://localhost:7700) lists every server running on the laptop, with Open, Restart and Stop. It knows the Planner, the Agent Monitor and html-worker pages under `~/work-tools`. Anything else running shows under Other.
- **The Agent Monitor needs herdr and the status line.** It shows every Claude session running in herdr. Sessions outside herdr show up without a pane.
- **"Make my task list" runs `claude -p`** (Sonnet by default, `TASKS_MODEL` changes it), so it uses your work Claude account.
- **Updating later:** improve things on the personal Mac, make a new kit, run `install.sh` again. It backs up what it replaces to `~/.claude/backups/`. Don't copy work-laptop changes back to the personal Mac.
- The pages load fonts from Google Fonts. If your network blocks that, they fall back to system fonts and still work.
