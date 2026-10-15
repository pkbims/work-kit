#!/bin/sh
# Work kit installer. Safe to run again: anything it would replace is backed up first.
#   sh install.sh
set -e
KIT="$(cd "$(dirname "$0")" && pwd)"
C="$HOME/.claude"; T="$HOME/work-tools"
BK="$C/backups/work-kit-$(date +%Y%m%d-%H%M%S)"
say() { printf '\033[1m%s\033[0m\n' "$*"; }
backup() { [ -e "$1" ] && { mkdir -p "$BK"; cp -R "$1" "$BK/" && echo "  backed up $(basename "$1") → $BK"; } || true; }

command -v python3 >/dev/null || { echo "python3 is missing. Install Xcode command line tools: xcode-select --install"; exit 1; }
command -v claude  >/dev/null || echo "note: 'claude' isn't on PATH yet. The Planner's 'Make my task list' needs it."
command -v herdr   >/dev/null || echo "note: herdr isn't installed yet (see README). Html-worker wake-ups, chats and the Agent Monitor need it."

say "1. Skills → $C/skills"
mkdir -p "$C/skills" "$C/scripts"
for s in html-worker daily-tasks monitoring-center herdr; do
  backup "$C/skills/$s"
  # keep the Monitoring Center layout you arranged by dragging
  keep=""; [ -e "$C/skills/$s/layout.json" ] && { keep=$(mktemp); cp "$C/skills/$s/layout.json" "$keep"; }
  rm -rf "$C/skills/$s"; cp -R "$KIT/claude/skills/$s" "$C/skills/"
  [ -n "$keep" ] && mv "$keep" "$C/skills/$s/layout.json"
done

say "2. Helper scripts and status line"
for f in server-pane.sh start-agent.sh; do backup "$C/scripts/$f"; cp "$KIT/claude/scripts/$f" "$C/scripts/"; done
backup "$C/statusline.py"; cp "$KIT/claude/statusline.py" "$C/"
chmod +x "$C/scripts/"*.sh "$C/statusline.py" "$C/skills/daily-tasks/open.sh" "$C/skills/html-worker/reply.py"

say "3. Settings (merged, nothing removed)"
backup "$C/settings.json"
python3 - "$C/settings.json" "$KIT/claude/settings-to-merge.json" <<'PY'
import json, os, sys
path, frag = sys.argv[1], json.load(open(sys.argv[2]))
cur = json.load(open(path)) if os.path.exists(path) else {}
cur.setdefault("model", frag["model"])
cur.setdefault("statusLine", frag["statusLine"])
allow = cur.setdefault("permissions", {}).setdefault("allow", [])
added = [a for a in frag["permissions"]["allow"] if a not in allow]
allow += added
json.dump(cur, open(path, "w"), indent=2)
print(f"  added {len(added)} allowed commands")
PY

say "4. Working style → $C/CLAUDE.md"
if [ -e "$C/CLAUDE.md" ]; then cp "$KIT/claude/CLAUDE.md" "$C/CLAUDE.md.from-kit"; echo "  you already have one: the kit's copy is CLAUDE.md.from-kit, merge by hand"
else cp "$KIT/claude/CLAUDE.md" "$C/CLAUDE.md"; fi

say "5. Agent Monitor and task folder → $T"
mkdir -p "$T/tasks" "$T/pages" "$T/agent-monitor"
cp "$KIT/work-tools/agent-monitor/"* "$T/agent-monitor/"
cp "$KIT/work-tools/start.sh" "$T/"; chmod +x "$T/start.sh"

say "6. herdr settings"
mkdir -p "$HOME/.config/herdr"
if [ -e "$HOME/.config/herdr/config.toml" ]; then echo "  kept your existing config.toml"
else cp "$KIT/herdr/config.toml" "$HOME/.config/herdr/"; fi

say "7. Planner app in the Dock (optional)"
APP="$HOME/Applications/Planner.app"
if mkdir -p "$HOME/Applications" && rm -rf "$APP" &&
   osacompile -o "$APP" -e "do shell script \"'$C/skills/daily-tasks/open.sh' > /dev/null 2>&1\"" 2>/dev/null; then
  cp "$C/skills/daily-tasks/Planner.icns" "$APP/Contents/Resources/applet.icns"
  rm -f "$APP/Contents/Resources/Assets.car"
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true
  codesign --force --sign - "$APP" 2>/dev/null || true
  echo "  built $APP. Drag it to the Dock."
else
  echo "  skipped: this Mac wouldn't build it. Use: sh ~/work-tools/start.sh"
fi

say "Done."
echo "Start the Planner, Agent Monitor and Monitoring Center:  sh ~/work-tools/start.sh"
echo "Restart any open Claude sessions so they pick up the new skills."
