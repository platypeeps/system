# local-config-check

Checks each tool's private config against the example the tool commits.

Private values live outside the checkout, under
`${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}/<tool>/`
(see `lib/config.sh`). The repository keeps only the `*.example` files. This
tool reads those examples as the manifest, so there is no tool list to edit:
a new folder with a `.env.example` is checked on the next run.

## Usage

```sh
./config-check.sh check              # every tool with a committed *.example
./config-check.sh check notify jev   # only these (tool or folder name)
./config-check.sh status             # one line; exit 0, 3 or 1
./config-check.sh list               # each example and the config path it maps to
./config-check.sh test -v            # this folder's suite
```

## What it checks

Each top-level `<folder>/<file>.example` maps to `<config>/<tool>/<file>`.
`<tool>` is the folder name minus `local-`; other folders keep their name.
`st_config_file` in `lib/config.sh` resolves the path, the same function the
tools read through; `privacy-patterns` sits at `<config>/privacy-patterns`.

For `.env.example`:

- whether `<config>/<tool>/.env` exists;
- required variables (uncommented in the example) that are unset or empty
  after sourcing the `.env` the way `st_source_env` does, so an exported
  value counts;
- values still holding a placeholder: `change-me`, `/path/to/...`,
  `example.test`;
- for names ending in `_DIR`, `_REPO`, `_FILE`, `_SRC`, `_SOURCE`,
  `_DESTINATION`: whether the path exists (a leading `~/` expands);
- names ending in `_MARKER_DIR` are exempt from the path check: a marker
  exists on one machine only, so its absence is a state, not a fault;
- for names ending in `_SD_KEY`: whether `sd config get <value>` succeeds.
  Without `sd` on `PATH`, the check prints a note and skips it.

Placeholder, path and key checks apply to optional variables too, when set.
Other example files (`hosts.conf.example`, `repos.*.conf.example`) are
reported present or absent; their content is not checked.

Output names tools, variables and files. It never prints a value.

## States

| State | Meaning |
| --- | --- |
| `ok` | configured, and no finding |
| `broken` | configured, with at least one finding |
| `unconfigured` | no config file present, and the example has required variables or no `.env.example` |
| `optional` | no `.env`, and every example variable is commented out |

A tool is configured when its `.env` or another mapped file exists.
`check` exits 1 when a tool is broken, else 0.
`status` exits 0 when every configured tool is healthy, 3 when none is
configured, and 1 when one is broken. `local-health-check` reads these codes.

`CONFIG_CHECK_ROOT` points the scan at another tree; the tests use it.
