#!/bin/sh
# Planner Dock app (user, 2026-10-13): start the daily-tasks server if it isn't running, then open
# the page as its own window (a chromeless Chrome app window), or bring that window forward if it's
# already open. Same pattern as pomodoro/open.sh. Args: width height.
#
# The Dock starts apps with a bare PATH; the server needs `claude` (Make my task list) and `herdr`
# (the chat) from ~/.local/bin.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
W=${1:-1440} H=${2:-900}
SKILL="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/work-tools/tasks" && cd "$HOME/work-tools"   # the server keeps the lists in ./tasks

port() { cat tasks/.port 2>/dev/null || echo 7800; }
up() { curl -s -m 1 "http://localhost:$(port)/" | grep -q '<title>Daily Tasks'; }

if ! up; then
  nohup python3 "$SKILL/server.py" >> "$HOME/Library/Logs/daily-tasks-server.log" 2>&1 &
  for i in $(seq 1 25); do sleep 0.2; up && break; done
fi

# already open? bring it forward instead of opening a second one. The app window's address ends in
# #app, so a normal browser tab showing Daily Tasks isn't mistaken for it.
if osascript -e 'tell application "Google Chrome"
  repeat with w in windows
    if (URL of active tab of w) ends with "#app" then
      set index of w to 1
      activate
      return "focused"
    end if
  end repeat
  return "none"
end tell' 2>/dev/null | grep -q focused; then exit 0; fi

open -na "Google Chrome" --args --app="http://localhost:$(port)/#app" --window-size="$W,$H"
