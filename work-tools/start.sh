#!/bin/sh
# Start the Planner (daily tasks), the Agent Monitor and the Monitoring Center if they aren't already running, then print their links.
# Inside herdr, they go in the "servers-tab" tab. Outside herdr, they run in the background with logs in ~/Library/Logs.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
T="$HOME/work-tools"; mkdir -p "$T/tasks"
up() { curl -s -m 1 "http://localhost:$(cat "$1" 2>/dev/null || echo 1)/" | grep -q "<title>$2"; }
run() {  # <dir> <command> <log name>
  if [ "${HERDR_ENV:-}" = 1 ]; then "$HOME/.claude/scripts/server-pane.sh" "$1" "$2" >/dev/null
  else (cd "$1" && exec nohup sh -c "exec $2" >> "$HOME/Library/Logs/$3.log" 2>&1) </dev/null >/dev/null 2>&1 & fi
}
up "$T/tasks/.port" "Daily Tasks" || run "$T" "python3 $HOME/.claude/skills/daily-tasks/server.py" daily-tasks-server
up "$T/agent-monitor/.port" "Agent Monitor" || run "$T/agent-monitor" "python3 monitor.py" agent-monitor
MC="$HOME/.claude/skills/monitoring-center"
up "$MC/.port" "Monitoring Center" || run "$T" "python3 $MC/server.py" monitoring-center
sleep 2
echo "Planner:       http://localhost:$(cat "$T/tasks/.port" 2>/dev/null)/"
echo "Agent Monitor: http://localhost:$(cat "$T/agent-monitor/.port" 2>/dev/null)/"
echo "Monitoring Center (every running server): http://localhost:$(cat "$MC/.port" 2>/dev/null)/"
