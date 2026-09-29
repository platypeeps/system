#!/bin/sh
# Claude Code statusline: runs claude-hud, then adds what the plugin cannot
# render on its own.
# Usage: statusline.sh render|install
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CONFIG_DIR/settings.json"

# Separator between wrapper-added segments (email, allowance warning,
# folded compactions): U+2502, the same box-drawing bar claude-hud joins merged
# segments with. Octal escapes keep the source 7-bit ASCII.
SEP="$(printf ' \342\224\202 ')"

render() {
  # claude-hud sizes itself to COLUMNS; the statusline is indented by Claude
  # Code, so hand it a slightly narrower terminal than the real one.
  cols="${COLUMNS:-}"
  case "$cols" in ""|*[!0-9]*) cols=$(stty size </dev/tty 2>/dev/null | awk '{print $2}') ;; esac
  case "$cols" in ""|*[!0-9]*) cols=120 ;; esac
  COLUMNS=$(( cols > 4 ? cols - 4 : 1 ))
  export COLUMNS

  # Highest installed claude-hud version, by numeric version sort.
  plugin_dir=$(ls -d "$CONFIG_DIR"/plugins/cache/*/claude-hud/*/ 2>/dev/null \
    | awk -F/ '{ print $(NF-1) "\t" $0 }' \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+[[:space:]]' \
    | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -1 | cut -f2-)
  if [ -z "$plugin_dir" ]; then
    echo "statusline.sh: claude-hud not found under $CONFIG_DIR/plugins/cache" >&2
    exit 1
  fi

  # claude-hud only ever renders the email local part (auth.ts strips the
  # domain), so the full address is appended here instead of forking the plugin.
  email=$(CLAUDE_CONFIG_DIR="$CONFIG_DIR" python3 -c 'import json,os,sys
try:
    p=os.environ["CLAUDE_CONFIG_DIR"].rstrip("/")+".json"
    p=p if os.path.exists(p) else os.path.expanduser("~/.claude.json")
    sys.stdout.write(json.load(open(p))["oauthAccount"]["emailAddress"])
except Exception:
    pass' 2>/dev/null)

  # System load: the 1, 5 and 15 minute averages. The 1 minute value is
  # yellow at or above the logical CPU count and red at twice that. Empty
  # where sysctl has no vm.loadavg (Linux), so the segment is left out.
  load=$(sysctl -n vm.loadavg hw.logicalcpu 2>/dev/null | tr -d '{}' | awk '
    NR == 1 { a = $1; b = $2; c = $3 }
    NR == 2 { n = $1 + 0 }
    END {
      if (a == "") exit
      s = sprintf("load %.1f %.1f %.1f", a, b, c)
      if (n > 0 && a + 0 >= 2 * n) s = "\033[1;38;5;196m" s "\033[0m"
      else if (n > 0 && a + 0 >= n) s = "\033[38;5;214m" s "\033[0m"
      print s
    }' || true)

  # Model-scoped weekly allowance (e.g. Fable) from the statusline JSON on
  # stdin: warn in red once remaining drops below FABLE_WARN_PCT (default 35).
  # stdin is teed to a file because the HUD needs the same bytes afterwards.
  stdin_file=$(mktemp)
  trap 'rm -f "$stdin_file"' EXIT INT TERM
  cat > "$stdin_file"
  warn=$(python3 -c 'import json,os,sys
try:
    d=json.load(open(sys.argv[1]))
    thr=float(os.environ.get("FABLE_WARN_PCT","35"))
    rl=d.get("rate_limits") or {}
    outs=[]
    # Model-scoped weekly windows (e.g. Fable) when the client sends them.
    for e in rl.get("model_scoped") or []:
        u=e.get("utilization")
        if u is None: continue
        rem=100.0-float(u)
        if rem<thr:
            name="".join(ch for ch in str(e.get("display_name") or "model")
                         if ch.isalnum() or ch in " -_")[:20].strip() or "model"
            # The Fable weekly window shows as bare F (5H/W cover the
            # plan windows); other model windows keep a shortened name.
            name="F" if "fable" in name.lower() else \
                name.replace("weekly","W").replace("Weekly","W").strip()
            outs.append("%s %.0f%%" % (name, rem))
    # Plan windows — the only allowance data most clients actually send.
    for key,label in (("five_hour","5H"),("seven_day","W")):
        w=rl.get(key) or {}
        u=w.get("used_percentage")
        if u is None: continue
        rem=100.0-float(u)
        if rem<thr:
            outs.append("%s %.0f%%" % (label, rem))
    sys.stdout.write(" \u00b7 ".join(outs))
except Exception: pass' "$stdin_file" 2>/dev/null || true)
  if [ -n "$warn" ]; then
    warn=$(printf '\033[1;38;5;196m[%s]\033[0m' "$warn")
  fi

  # Label that identifies the compactions line, matched against claude-hud's
  # configured language so the fold still works if that changes.
  case "$(hud_language)" in
    zh-Hans) COMPACTIONS_LABEL='\u538b\u7f29\u6b21\u6570' ;;
    zh-Hant) COMPACTIONS_LABEL='\u58d3\u7e2e\u6b21\u6578' ;;
    *)       COMPACTIONS_LABEL='Compactions' ;;
  esac
  COMPACTIONS_LABEL=$(printf '%b' "$COMPACTIONS_LABEL")

  bun_bin="${BUN_BIN:-$HOME/.bun/bin/bun}"
  if [ ! -x "$bun_bin" ]; then
    bun_bin=$(command -v bun 2>/dev/null || true)
  fi
  if [ -z "$bun_bin" ]; then
    echo "statusline.sh: bun not found (set BUN_BIN)" >&2
    exit 1
  fi

  # Three edits to the HUD output, in order:
  #
  #   1. The compactions line is appended last by claude-hud and sits outside
  #      its element/mergeGroups system, so it cannot be merged from config the
  #      way the Cache line can. It is folded onto the preceding line here.
  #      It only exists once a session has compacted at least once.
  #   2. The full login email, then the system load, join line 1.
  #   3. The allowance warning joins line 2 (the context bar), just left of
  #      the Cache segment that mergeGroups moved onto that line. Line 1 is
  #      already the widest line in the HUD, hence not there.
  #
  # Folding happens first so the email and warning see the final line count.
  # Falling back to line 1 keeps the warning visible if the HUD renders a
  # single line.
  "$bun_bin" --env-file /dev/null "${plugin_dir}src/index.ts" < "$stdin_file" \
    | awk -v e="$email" -v l="$load" -v w="$warn" -v sep="$SEP" -v clabel="$COMPACTIONS_LABEL" '
        # claude-hud hardcodes the "Weekly" usage label (i18n has no user
        # override); shorten it, and the "Fable weekly" window name, to W.
        { gsub(/[Ww]eekly/, "W"); line[NR] = $0 }
        END {
          n = NR
          if (n == 0) exit

          if (n >= 2) {
            bare = line[n]
            gsub(/\033\[[0-9;]*m/, "", bare)
            if (bare ~ ("^" clabel ":")) {
              line[n - 1] = line[n - 1] sep line[n]
              n--
            }
          }

          if (e != "") line[1] = line[1] sep e
          if (l != "") line[1] = line[1] sep l
          # The allowance warning goes just left of the Cache segment so it
          # sits with the other budget numbers; if that label is absent
          # (translated HUD, no Cache line) it falls back to the line end.
          if (w != "") {
            t = (n >= 2) ? 2 : 1
            if (!sub(/(\033\[[0-9;]*m)*Cache/, w sep "&", line[t]))
              line[t] = line[t] sep w
          }
          for (i = 1; i <= n; i++) print line[i]
        }'
}

hud_language() {
  lang_file="$CONFIG_DIR/plugins/claude-hud/config.json"
  if [ -f "$lang_file" ]; then
    HUD_CFG="$lang_file" python3 -c 'import json,os,sys
try:
    sys.stdout.write(json.load(open(os.environ["HUD_CFG"])).get("language","en"))
except Exception:
    sys.stdout.write("en")' 2>/dev/null || echo en
  else
    echo en
  fi
}

install_statusline() {
  if [ ! -f "$SETTINGS" ]; then
    echo "statusline.sh: $SETTINGS not found" >&2
    exit 1
  fi
  # Timestamped so a second install cannot destroy the original backup.
  bak="$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
  cp "$SETTINGS" "$bak"
  SL_CMD="sh $DIR/statusline.sh render" SL_FILE="$SETTINGS" python3 -c '
import json, os
path = os.environ["SL_FILE"]
with open(path) as fh:
    settings = json.load(fh)
settings["statusLine"] = {
    "type": "command",
    "command": os.environ["SL_CMD"],
    "refreshInterval": 5,
}
with open(path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
'
  echo "statusLine now runs: sh $DIR/statusline.sh render"
  echo "previous settings.json saved to $bak"
}

case "$1" in
  render)
    render
    ;;
  install)
    install_statusline
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: statusline.sh render|install

  render     print the status line: claude-hud output, with the full login
             email and the system load appended to line 1, the allowance
             warning to line 2, and the compactions line folded onto the
             line above it
  install    point statusLine in ~/.claude/settings.json at this script
             (backs the file up to settings.json.bak.<timestamp> first)

environment:
  CLAUDE_CONFIG_DIR            Claude config dir (default ~/.claude)
  BUN_BIN                      bun binary (default ~/.bun/bin/bun, then PATH)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") render|install" >&2
    exit 1
    ;;
esac
