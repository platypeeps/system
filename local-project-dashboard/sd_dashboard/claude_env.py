"""The start of every dashboard `claude` line: the token from env.sh (sd:5791).

The dashboard under launchd has no token, and Claude Code strips it from a session's shell. env.sh holds it as
CUSTOM_CLAUDE_CODE_OAUTH_TOKEN, so a day-to-day claude keeps its own login (sd:5786). `SHELL_ENV` sources env.sh
quietly when it exists, then exports CLAUDE_CODE_OAUTH_TOKEN from that name when it is unset or empty: the mapping the
shell tools get from the repository's lib/. It names no path outside this package, so an installed copy maps it too,
and the token goes in no argv, file or log. A machine without env.sh runs on.
"""

SHELL_ENV = ('f="$HOME/.config/shell/env.sh"; [ ! -f "$f" ] || . "$f" >/dev/null 2>&1; '
             '[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -z "${CUSTOM_CLAUDE_CODE_OAUTH_TOKEN:-}" ] || '
             'export CLAUDE_CODE_OAUTH_TOKEN="$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"; ')
