# local-mac-utils

macOS maintenance one-liners, collected into one subcommand tool. Replaces six
loose scripts that used to sit in `~/bin/common` and `~/bin/darwin`.

## Usage

```sh
./mac-utils.sh flushdns              # flush DNS cache, restart mDNSResponder
./mac-utils.sh resetvideo            # kill VDCAssistant, freeing a wedged webcam
./mac-utils.sh unquarantine <path>.. # strip com.apple.quarantine
./mac-utils.sh net                   # processes holding network sockets
./mac-utils.sh addkey                # ssh-add the identity key; passphrase kept in the keychain
./mac-utils.sh brew-update           # brew update && upgrade && cleanup
mac-utils flushdns                   # symlink in ~/bin/common
```

## What it replaced

| Old script | Subcommand |
| --- | --- |
| `darwin/flushdns.sh` | `flushdns` |
| `darwin/resetvideo.sh` | `resetvideo` |
| `darwin/unquarantine.sh` | `unquarantine` |
| `common/usingnet.sh` | `net` |
| `common/addkey.sh` | `addkey` |
| `common/brew_update.sh` + `darwin/refreshbrew.sh` | `brew-update` |

The two brew scripts were near-duplicates — `refreshbrew.sh` was just
`brew_update.sh` without the `cleanup`. The merged subcommand keeps `cleanup`.

## Gotchas

- `flushdns` and `resetvideo` call `sudo` and will prompt.
- `unquarantine` with no path argument is an error rather than a no-op; the old
  script expanded `$*` to nothing and called `xattr -d` on the current directory.
- `addkey` reads whichever identity this machine has — `~/.ssh/identity_work`
  first, then `~/.ssh/identity_personal.pk`; override with `MAC_UTILS_SSH_KEY`.
- `addkey` passes `--apple-use-keychain`, so the passphrase survives a reboot.
  A job that starts before anyone logs in can then load the key with
  `ssh-add --apple-load-keychain`, as `repo-sync.sh nightly` does.
