# local-leak-guard

A git pre-push hook that refuses a push whose new commits add a line matching
a private pattern: a hostname, an email address, a home path, an account id.
It exists because this repository is public and its tools run on private
machines.

## Patterns

The patterns live outside the checkout, in `<config>/privacy-patterns`.
`<config>` is `SYSTEM_TOOLS_CONFIG`, default `~/.config/system`. The file
holds one extended regular expression per line, as `grep -E -f` reads them.
Blank lines and `#` comments are ignored. `LEAK_GUARD_PATTERNS` names another
file.

```sh
cp local-leak-guard/privacy-patterns.example ~/.config/system/privacy-patterns
chmod 600 ~/.config/system/privacy-patterns   # then fill it in
```

Never commit the pattern file. It lists exactly what must stay out.

Without the file, the guard prints a warning and lets the push through.

## Usage

```sh
sh local-leak-guard/leak-guard.sh install          # this repository's pre-push hook
sh local-leak-guard/leak-guard.sh check            # commits no remote has yet
sh local-leak-guard/leak-guard.sh check --range origin/main..HEAD
sh local-leak-guard/leak-guard.sh remove
```

`install` writes the hook into `git rev-parse --git-path hooks`, so it honours
`core.hooksPath`. It refuses to replace a `pre-push` hook it did not write.

## What it checks

For each commit a push adds:

- every added line of the diff;
- the path of every file the commit adds or changes;
- the commit message.

A line added in one commit and removed in the next is still refused: both
commits reach the remote. Author and committer fields are not checked.

A hit names the commit, the kind of line and the file. It never prints the
matching text, and it withholds a path that itself matches.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | clean, or no pattern file |
| `1` | a pushed commit matches a pattern |
| `2` | usage or git error |

## CI

CI cannot run the guard against real patterns: they are not in the
repository. `leak-guard.sh test` runs the suite against synthetic patterns in
temporary repositories.
