# `make check` runs every native suite: the local merge gate runs it through
# sd-check. tests/check.sh builds its own environment in .ci/ (gitignored).
.PHONY: check
check:
	sh tests/check.sh run

# `make test SUITE=<name>` runs one suite the way the gate runs it: the
# preflight, then that run_suite or macOS-only line, against the pack pin in
# .ci/pack, never the live checkout (sd:3534).
.PHONY: test
test:
	sh tests/check.sh suite "$(SUITE)"

# `make post-merge` runs in the hub checkout after the lane merges a PR
# here (pack sd:3566): move the checkout to the merge, rewrite stale agent
# prompt targets (it writes nothing current and refuses hand edits), then
# report service health so a stale build shows (sd:3568).
.PHONY: post-merge
post-merge:
	sh local-repo-sync/repo-sync.sh refresh "$(CURDIR)"
	sh local-agent-prompt/agent-prompt.sh refresh --apply
	sh local-project-dashboard/dashboard.sh health
