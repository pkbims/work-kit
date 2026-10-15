#!/usr/bin/env bash
# Run a long-lived command (a page server, a render) in the "servers-tab" herdr tab,
# never as a split of the user's session pane, so their session keeps the full screen.
#
#   ~/.claude/scripts/server-pane.sh <cwd> <command...>
#
# First call creates the tab; later calls split its right-most pane to the right.
# Prints the new pane id. Focus never moves.
set -euo pipefail
[ "${HERDR_ENV:-}" = 1 ] || { echo "not inside herdr" >&2; exit 1; }
# WS=<workspace id> puts the server in that workspace's servers-tab (an app's own workspace)
HERDR_WORKSPACE_ID=${WS:-$HERDR_WORKSPACE_ID}
cwd=$1; shift
label=servers-tab

tab=$(herdr tab list --workspace "$HERDR_WORKSPACE_ID" | python3 -c "
import json,sys
print(next((t['tab_id'] for t in json.load(sys.stdin)['result']['tabs'] if t.get('label')=='$label'),''))")

if [ -z "$tab" ]; then
  pane=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --label "$label" --cwd "$cwd" --no-focus |
    python3 -c "import json,sys;print(json.load(sys.stdin)['result']['root_pane']['pane_id'])")
else
  last=$(herdr pane list --workspace "$HERDR_WORKSPACE_ID" | python3 -c "
import json,sys
print([p['pane_id'] for p in json.load(sys.stdin)['result']['panes'] if p['tab_id']=='$tab'][-1])")
  pane=$(herdr pane split --pane "$last" --direction right --cwd "$cwd" --no-focus |
    python3 -c "import json,sys;print(json.load(sys.stdin)['result']['pane']['pane_id'])")
fi
herdr pane run "$pane" "$*" >/dev/null
echo "$pane"
