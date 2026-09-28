# `make check` runs every native suite: the local merge gate runs it through
# sd-check. tests/check.sh builds its own environment in .ci/ (gitignored).
.PHONY: check
check:
	sh tests/check.sh run
