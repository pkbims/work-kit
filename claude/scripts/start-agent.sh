#!/usr/bin/env bash
# Start a named Claude session in its own herdr tab and hand it a first prompt.
#
#   ~/.claude/scripts/start-agent.sh <name> <cwd> <model> <effort> "<first prompt>"
#   WS=w3 … (another workspace)   PANE=w3:p1 … (an existing pane)
# Prints the pane id. Focus never moves; the user switches to the tab themselves.
set -euo pipefail
[ "${HERDR_ENV:-}" = 1 ] || { echo "not inside herdr" >&2; exit 1; }
name=$1 cwd=$2 model=$3 effort=$4 prompt=$5

if herdr agent get "$name" >/dev/null 2>&1; then
  echo "an agent named $name already exists" >&2; exit 1
fi
# WS=<workspace id> puts the tab in another workspace ;
# PANE=<pane id> starts the agent in an existing empty pane instead of a new tab.
ws=${WS:-$HERDR_WORKSPACE_ID}
if [ -n "${PANE:-}" ]; then
  pane=$PANE
  tab=$(herdr pane get "$pane" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['pane']['tab_id'])")
  herdr tab rename "$tab" "$name" >/dev/null
else
  pane=$(herdr tab create --workspace "$ws" --label "$name" --cwd "$cwd" --no-focus |
    python3 -c "import json,sys;print(json.load(sys.stdin)['result']['root_pane']['pane_id'])")
fi
herdr agent start "$name" --kind claude --pane "$pane" -- --model "$model" --effort "$effort" --permission-mode auto --name "$name" >/dev/null
herdr agent prompt "$name" "$prompt" >/dev/null
echo "$pane"
