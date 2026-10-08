# local-machine-setup

Provision and maintain several machines from a named profile. Answers three
questions: *set up a new machine*, *bring this machine up to date*, and *push a
change I made on this machine back into its profile*.

## Usage

```sh
./machine-setup.sh setup laptop        # provision a new machine (DRY RUN)
./machine-setup.sh setup laptop --apply
./machine-setup.sh update              # re-run stages for the recorded profile
./machine-setup.sh update brew --apply # one stage only
./machine-setup.sh capture --apply     # machine state -> profile
./machine-setup.sh status              # drift: profile wants vs machine has
./machine-setup.sh compare laptop desktop  # two profiles side by side
./machine-setup.sh triage brew --apply     # file unassigned items interactively
./machine-setup.sh candidates              # what this machine COULD run, unlisted
./machine-setup.sh candidates service      # one kind: service, cron or agent
./machine-setup.sh apps                # standalone /Applications report
./machine-setup.sh adopt --apply       # brew adopts all cask-covered standalone apps
./machine-setup.sh report [profile]    # readable overview of profile contents
./machine-setup.sh profiles            # list the supported profile names
./machine-setup.sh doctor              # sanity check: CLT, brew, docker, fnm,
                                       # keys, loaded agents, unfilled .env files,
                                       # firewall, useLS, credential dir modes,
                                       # shell, rtk hook, iTerm2; with an sd agent in
                                       # the profile, also the database, runner
                                       # heartbeat, dashboard route and HTTPS answer,
                                       # the sd_db build in each virtualenv, and
                                       # every enabled provider's variables (names
                                       # only) in the environment the runner sources;
                                       # no sqlite3 on PATH, Funnel on the
                                       # dashboard's :8443 origin, :8443 held by
                                       # another route, a service config naming
                                       # another database, or an env.sh that fails
                                       # when sourced, is a FAIL
./machine-setup.sh doctor sd           # those sd checks alone
./machine-setup.sh test                # unittest suite in tests/ (CI runs it)
./machine-setup.sh upgrade --apply     # brew update/upgrade/cleanup + mas upgrade
                                       # each step is logged as it starts and
                                       # stopped at its bound (300-1800 s, or
                                       # MACHINE_SETUP_STEP_TIMEOUT seconds)
./machine-setup.sh upgrade-report      # upgrade + email summary (cron: Sundays 04:00)
./machine-setup.sh checklist           # manual new-machine steps
./machine-setup.sh decommission        # retire machine: dirty-repo scan, job/agent
                                       # removal with --apply (asks you to type
                                       # "decommission" first), manual sign-off list
```

**Everything is a dry run without `--apply`.** That is the default on purpose:
this script installs packages, loads launch agents and starts containers, so an
unfamiliar machine should be inspected before it is touched.

The chosen profile is recorded in `~/.config/machine-setup/profile`, so `update`
and `capture` need no argument after the first `setup`.

## Configuration

Nothing machine-specific is committed here. The profiles, dotfiles, `.env`
templates and LaunchAgent templates live in the operator's config folder:

```
$SYSTEM_TOOLS_CONFIG/machine-setup/     # default ~/.config/system/machine-setup
  profiles/        common.<kind> and <profile>.<kind> manifests
  dotfiles/        common/ and <profile>/ trees, copied into $HOME
  envs/            common/ and <profile>/ <folder>.env templates
  launchagents/    <label>.plist templates
  machine-setup.env    optional, sourced first
  shell-env.example    optional, doctor's list of shell variable names
  checklist.txt, decommission.txt   optional, appended to those verbs
```

`SYSTEM_TOOLS_CONFIG` defaults to `${XDG_CONFIG_HOME:-~/.config}/system`.
`MACHINE_SETUP_PROFILE_DIR`, `MACHINE_SETUP_DOTFILE_DIR`,
`MACHINE_SETUP_ENV_DIR` and `MACHINE_SETUP_AGENT_DIR` override one folder each.
A verb that needs the profiles and finds no profile directory exits 1, naming
the path it looked in.

Start from the shipped examples:

```sh
mkdir -p ~/.config/system/machine-setup
cp -R local-machine-setup/examples/. ~/.config/system/machine-setup/
```

`machine-setup.env.example` lists the optional settings: `OBSIDIAN_VAULT`,
`MACHINE_SETUP_SSH_KEYS`, `MACHINE_SETUP_AGENT_GLOBS`,
`MACHINE_SETUP_PLACEHOLDER_EXPECTED` and `MACHINE_SETUP_PROFILES`. Copy it to
`machine-setup.env` in the config folder.

The config folder can be a git checkout of its own. `capture` then refuses to
write from a checkout behind its upstream. Without git, it simply writes the
files. `profile-autocapture.sh` never commits either way.

LaunchAgent labels start with `$SYSTEM_TOOLS_LABEL_PREFIX` (default
`local.system-tools`). A template in `launchagents/` may use `@LABEL@`,
`@HOME@` and `@ROOT@` (this checkout's root); the `agents` stage replaces them
when it installs the plist, and `capture` compares against the rendered form.

## Profiles

A profile is any `<name>` with a `<name>.<kind>` file in `profiles/`;
`profiles` lists them. `MACHINE_SETUP_PROFILES` names them explicitly instead.
They layer — `common.<kind>` applies to every machine, `<profile>.<kind>` adds
to it. A profile with only one file is therefore almost exactly `common`.

| Kind | Contents |
| --- | --- |
| `.tap` | Homebrew taps, applied before formulae resolve |
| `.brew` | formulae |
| `.cask` | casks |
| `.mas` | Mac App Store apps, as `<id> <name>` |
| `.macos` | `defaults` settings, as `<domain> <key> <type> <value>` |
| `.app` | standalone apps (informational — nothing installs these) |
| `.service` | `local-*` folders whose docker service should be running |
| `.cron` | `local-cron-jobs` job names to install; this host's own jobs need no entry |
| `.spotlight` | Spotlight privacy exclusions: absolute paths, a leading `~` is the account's home |
| `.satellite` | the sd hub this machine reaches, as `host` or `host:port` (default 8769); one line, on a satellite only. One machine runs each profile: `personal` is the hub, and a profile with this file (`work.satellite`, `terra.satellite`) is a satellite; the hub's own is refused |

Plain lists, one per line, `#` comments and blank lines ignored — the same
format as `local-repo-sync`.

A new machine's profile is best captured on that machine; guessing it from
another machine would be fiction.

## Stages

Run in order. Pass one as the second argument to run it alone.

| Stage | What it does | Delegates to |
| --- | --- | --- |
| `brew` | taps, formulae, casks | — |
| `appstore` | Mac App Store apps. An app that `mas list` shows under another id with the same name, such as a beta under id 0, counts as installed, and `capture` keeps its profile entry. A failed `mas install` prints `FAILED` and the run goes on | `mas` |
| `bin` | symlink CLI tools onto PATH | `local-bin-links` |
| `dotfiles` | install `.zshrc`, `.bash_aliases`, `.gitconfig`, `.gitignore_global`, `.ssh/config`, `.config/gh/config.yml`, `.prism/.env`, `.gito/.env`, `.aws/config`, `.vale.ini` from `dotfiles/<profile>/`, falling back to `dotfiles/common/` | — |
| `envs` | install the `.env` of each folder listed in `ENVS` into `$SYSTEM_TOOLS_CONFIG/<tool>/.env` from `envs/<profile>/<folder>.env`, falling back to `envs/common/`, always 0600. Templates hold non-secret defaults only | — |
| `prompts` | shared agent system prompt into each tool's global instructions | `local-agent-prompt` |
| `repos` | clone/pull the repo fleet | `local-repo-sync` |
| `cron` | install launch agents: the profile's `.cron` jobs plus every job in this host's own folder (`cron-jobs/jobs/<host>/`). A `CRON_JOBS_EXTRA_DIRS` job runs only when the profile names it. Any other job that `local-cron-jobs` installed is `EXTRA` and is uninstalled. An agent under `<prefix>.cron.` that `local-cron-jobs` did not install is `FOREIGN` and stays in place, even when the profile names it; one whose plist cannot be read is `UNKNOWN` and stays too. The stage knows its own plists by their label and their `cron-jobs.sh exec <job>` command, which every installed job carries. When the host folder cannot be read, the stage prints `MISSING host job list` and uninstalls nothing | `local-cron-jobs` |
| `sd` | the workflow database (`sd-db.sh init`) and the dashboard's private `tailscale serve` route on 8443 to 127.0.0.1:8767, on a profile whose `.agent` lists `<prefix>.sd-dashboard` or `<prefix>.sd-runner`. Runs before `agents`, because both agents open the database at startup. A :8443 that already serves something else is `DIFFERS` and left alone, and so is a `~/.config/sd/runner.json` or `dashboard.json` naming a `database` other than `~/.local/share/sd/sd.db`. On the hub, a profile without `<prefix>.sd-serve` or a config folder without its template is `MISSING`, and a `~/.config/sd/hub.json` is `EXTRA` and stays | `local-sd-db`, `tailscale` |
| `satellite` | on a profile with a `.satellite`: writes `~/.config/sd/hub.json`, checks that the pack's installed `sd_db` is the hub's build (`DIFFERS` names both values; `--apply` installs the hub's build from `origin/main` of this checkout when its digest is the hub's, `SD_DB_SOURCE_CHECKOUT` overrides the checkout), and installs the hub's `providers.yaml`. A local `sd.db` is `EXTRA` and stops the stage; a hub that does not answer is `SKIP`. On a satellite (a `.satellite` or a `hub.json`, and no hub agent in the profile), each hub-only job in `SD_HUB_ONLY_AGENTS` that is installed or loaded is `EXTRA` and stays. Where `jev` is on `PATH`, a missing jev shadow file is `MISSING`, and `--apply` runs `jev shadow on` (sd:2838). Installs no LaunchAgent | `sd_db.satellite` under `SD_DB_PYTHON` (default: the pack's virtualenv) |
| `agents` | install captured LaunchAgent plists, rendering `@LABEL@`, `@HOME@` and `@ROOT@`. While `~/.config/sd/hub.json` exists, each hub-only label in `SD_HUB_ONLY_AGENTS` is `SKIP` | — |
| `services` | start docker services | each `local-*/<name>.sh start` |
| `macos` | apply `defaults` settings | — |
| `tooling` | fnm node versions, rtk hooks, the Claude Code HUD (claude-hud plugin plus `local-statusline` as `statusLine`), the `claude_settings.py` baseline merged into `~/.claude/settings.json` (adds missing entries only, after a backup), Chrome as mailto handler | `fnm`, `rtk`, `claude`, `duti` |
| `system` | useLS, sudo grace period, firewall + stealth (needs sudo once), 700 on the credential dirs, Spotlight privacy exclusions from `<profile>.spotlight` | `plutil`, `PlistBuddy`, `launchctl` |
| `obsidian` | report vault plugins vs `<profile>.obsidian` (report-only) | `python3` |
| `iterm2` | point iTerm2 at the synced prefs folder in `<profile>.iterm2` | — |

Most stages delegate to tools that already existed. This folder is mostly an
ordering and profile layer over them, not a reimplementation.

### Obsidian plugins

The vault gitignores `.obsidian/` (it holds the Local REST API TLS private
key), so installed community plugins do not travel with the vault.
`<profile>.obsidian` lists the expected plugin folder names; the `obsidian`
stage compares that against `$OBSIDIAN_VAULT/.obsidian/plugins` (unset skips
the stage) and reports `MISSING` and `extra` — it never
installs or removes, because community plugins are managed inside Obsidian
(Settings → Community plugins). The listing runs through PATH `python3`
(`MACHINE_SETUP_PYTHON` overrides) — Homebrew's, which holds Full Disk Access
on this machine where the plain shell does not. Not `/usr/bin/python3`: that
pin looked upgrade-proof, but macOS ignores user FDA grants for Xcode's
python under launchd (verified 2026-08-29 — bundle, inner binary, toggle,
TCC reset; all fail). The cost of Homebrew is that an upgrade moves the
Cellar path and silently drops the grant; the stage then reports
`SKIP vault not found or unreadable` and the fix is one FDA re-grant.

### iTerm2

iTerm2 keeps every setting — profiles, keymaps, colors — in one nested plist,
so per-key `defaults write` gets you nothing. iTerm2's own answer is a custom
prefs folder: point it at a synced directory and the whole plist travels with
the machine. `<profile>.iterm2` holds that single path; it is profile-scoped
because the Drive mount differs per machine, and `capture` refreshes it.

The stage writes only `PrefsCustomFolder` and `LoadPrefsFromCustomFolder`, and
refuses to write either unless the folder **already contains**
`com.googlecode.iterm2.plist` — repointing iTerm2 at an empty folder makes it
write fresh defaults there, and the synced settings are what get lost. On a new
machine that means waiting for Drive to sync before running the stage.

**Quit iTerm2 before `--apply`.** A running instance rewrites its plist on
quit and would overwrite both keys. The stage prints that reminder whenever it
is about to change something.

### Spotlight privacy exclusions

`<profile>.spotlight` lists the paths Spotlight must not index, one per line.
A leading `~` means this account's home, so the file serves any user name.
The `system` stage compares it with the live list and prints `ok`, `MISSING`
or `EXTRA` per path, so `status` counts the drift.

The live list is the `Exclusions` array in
`/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist`. System
Settings > Spotlight > Search Privacy edits the same list. Apple ships no CLI
for it, so the stage reads and edits the plist directly:

- Reading needs sudo. Without `--apply` the stage uses `sudo -n` and never
  prompts, so the nightly `status` run cannot hang on a password.
- Root also needs Full Disk Access for the calling terminal. Without it, sudo
  gets "Operation not permitted".
- When the read fails, the stage prints one `DEFER` line with the cause and
  the remedy, and the run goes on. `DEFER` is not a drift word, so a
  `status` run without a sudo ticket counts no Spotlight drift.
- `--apply` adds each missing path with `PlistBuddy`, then restarts mds once
  (`launchctl kickstart -k system/com.apple.metadata.mds`). mds reads the list
  at start, so an edit without the restart waits for the next boot. The stage
  then reads the list again and reports a path that is still absent.
- The stage never removes a path. A path the profile does not list is `EXTRA`;
  `capture` writes it into the profile, or you remove it in System Settings.
- A path with a quote or a backslash stays `MISSING` with a `SKIP` line:
  PlistBuddy parses its command string and mangles those characters. Add it
  in System Settings instead.
- The stage compares paths as written. `/tmp/x` does not match
  `/private/tmp/x`; write the form the live list holds.

`capture` writes the live list into `<profile>.spotlight`, with paths under
`$HOME` written as `~/...`. It leaves out entries `common.spotlight` holds.
`capture --additive` skips it: reading the list needs sudo, which the
unattended run does not have.
`MACHINE_SETUP_SPOTLIGHT_PLIST` points the stage at another plist. It exists
for the tests only.

### Dotfiles and tool configs

Dotfiles are **profile-scoped**, the same way every manifest is:
`dotfiles/<profile>/` wins, `dotfiles/common/` is the fallback for files that
are genuinely identical on every machine — `.gitignore_global`, for example.
`capture` writes into
`<profile>/`, and if the captured file turns out
to match `common/` byte for byte it drops the profile copy instead of leaving a
duplicate to go stale.

The directory used to be flat, which made `capture` a cross-machine hazard: one
machine's 158-line `.zshrc` would have overwritten another's 31-line one, and
nothing in the output would have said so.

`DOTFILES` also carries `~/.prism/.env` and `~/.gito/.env`, the provider
configuration for the two code-review wrappers (`local-prism`, `local-gito`).
They are `.env` files by name only: every key is a `${VAR}` reference into
`~/.config/shell/env.sh`, never a literal, so a new machine gets the model and
endpoint choices while the credentials stay in the one place they belong.

Two things had to give way for that:

- `capture_dotfiles`' credential scan counted `OPENAI_API_KEY=` as a hit
  regardless of the value. It now requires the value to start with something
  other than `$`, so an indirection passes and a literal is still refused.
  Verified both ways before the files were captured.
- A `.env` ignore rule in the config checkout, if it has one, shadows the
  captured copies silently. Re-include `dotfiles/**/.env` there.

`DOTFILES_PRIVATE` (`.prism/.env`, `.gito/.env`, `.ssh/config`, `.zshenv`,
`.aws/config`) installs 0600 instead of taking `cp`'s default mode — not
because they hold secrets, but because they sit where a secret would, and
`~/.gito/.env` was found world-readable once already.

`~/.aws/config` is in `dotfiles/common/`, profiles only. It is the one dotfile
a tool writes into: `aws login --profile <name>` adds a `login_session = <arn>`
line under the profile it signed in, naming the account and user the browser
picked. `dotfile_session_re` names those lines, and the stage compares,
records and captures the file without them, then carries them back under
their own sections when it overwrites. Without that, every login read as
DIFFERS and `--force` signed the machine out of every profile.
`~/.aws/credentials` is never tracked; a new machine signs in with
`aws login` and gets agent-profile keys by hand.

### Launch agents

`capture` copies this machine's own LaunchAgent plists (`<prefix>.*`, plus any
label glob in `MACHINE_SETUP_AGENT_GLOBS`, excluding `<prefix>.cron.*` which
`local-cron-jobs` owns) into `launchagents/` and lists their labels in
`<profile>.agent`; the `agents` stage installs and `launchctl bootstrap`s them
on a new machine. `<prefix>` is `$SYSTEM_TOOLS_LABEL_PREFIX`, default
`local.system-tools`. Each plist passes the same credential scan as dotfiles
before capture — one with a credential-shaped value is refused. A captured
plist embeds absolute paths; replace the home and checkout paths with `@HOME@`
and `@ROOT@` by hand so it transplants between machines, and the stage renders
them back.

A label that carries the machine's user name once hid a machine's agents from
a narrower glob, so `candidates agent` reported `0 on this machine` while two
were running. One `owned_agent_plists` helper now answers for both `capture`
and `candidates`, so they cannot drift apart.

### The sd hub and its satellites

One machine runs each profile.
`personal` is the sd hub: it holds the workflow database and serves it.
A profile with a `.satellite` file is a satellite of that hub.
`local-sd-db/README.md` describes the server, its refusals and `hub.json`.

On the hub, `local.system-tools.sd-serve` runs `local-sd-db/sd-db.sh serve`, the tailnet listener on port 8769.
It admits only the untagged nodes of the node's owner, by `tailscale whois`, and carries no token.

1. Copy `examples/launchagents/local.system-tools.sd-serve.plist` to the config folder's `launchagents/`.
2. List `local.system-tools.sd-serve` in `personal.agent`.
3. Run `machine-setup.sh update agents --apply`. The agent loads in the operator's `gui/<uid>` domain.

- The plist names an absolute path to `local-sd-db/sd-db.sh`; rename that folder or file, and reload the plist.
- The plist sets `PATH`, because launchd's own `PATH` does not find `tailscale`.
- The server never creates a database. Under the wrong home it exits and names the path; `KeepAlive` retries every 30 seconds.
- The `sd` stage reports the label or the template `MISSING`, and a `~/.config/sd/hub.json` on the hub `EXTRA`.

On a satellite, put the hub's name in `<profile>.satellite`, then run `machine-setup.sh update satellite --apply`.
The stage writes `~/.config/sd/hub.json`, checks the pack's `sd_db` build against the hub's, and installs the hub's `providers.yaml`.
The satellite runs none of the hub's agents and jobs; `SD_HUB_ONLY_AGENTS` in `machine-setup.sh` lists them.

### Drift alerts

`status` runs every reporting stage — bin, dotfiles, envs, prompts, repos,
appstore, cron, sd, agents, services, tooling, system, macos, obsidian, iterm2 — and folds
each one's divergence into a single count. It forces dry-run mode around those
stages, so `status --apply` stays read-only no matter what.

It used to run six of them, and the eight it skipped were exactly the ones
nothing else watched: for weeks all nine cron plists were stale and drift
reported 0. `repos` was the last one left out, for want of a read-only verb —
`list` printed the configured fleet without touching disk and `sync` clones.
`repo-sync check` was added for this, so every comparing stage is now in.

A stage reports divergence four ways: `DIFFERS`, `MISSING`, `STALE`, `ABSENT`,
a `defaults write` it would have made, or an `extra` line. A would-be action
alone does not count — `[dry-run] mas install <id>` looked like a note rather
than a finding, which is why the appstore and cron stages now name what is
missing before offering to fix it.

`status --fail-on-drift` exits 1 when anything drifted, and the
`machine-setup-drift` job in `local-cron-jobs` (listed in `common.cron`, so
every machine installs it) runs that nightly — drift triggers the cron failure
notification (banner + ntfy push). A clean machine stays silent.

### Additive capture, unattended

`capture` is a **mirror**: it rebuilds each roster from live machine state and
`write_manifest` ends with `mv "$wm_new" "$wm_target"`, so an entry that is no
longer on this machine is written away. That is right for a human running it —
uninstalling a cask on purpose should propagate — and wrong for anything
unattended, because "this machine no longer has it" and "the roster should no
longer have it" are the same observation to `capture` and different facts to
everybody else. A laptop that has not run `mas` since a reinstall would quietly
delete eighteen App Store entries from every other machine's profile.

`capture --additive` makes the two cases different. It writes a roster entry
that appeared and **refuses to write one away**: a removal is named on stdout,
the manifest is left alone, and the run exits `3`. It also skips the stages that
copy file *content* — dotfiles, macOS defaults, iTerm2, and the launch-agent
plist copy — where "additive" has no meaning; a file is replaced or it is not.
So additive mode is rosters only, by construction. It skips the Spotlight
list too, a roster that only sudo can read.

That is what makes an unattended write defensible. `profile-autocapture.sh`
wraps it:

    sh profile-autocapture.sh

It runs `capture --apply --additive` against the profile directory
(`$SYSTEM_TOOLS_CONFIG/machine-setup/profiles` by default) and exits with
capture's code. The rosters keep no git history: the job writes them and
stages, commits and pushes nothing, even when the config folder is a git
checkout.

Exit `3` means a removal was held. The nightly job lets that fire the failure
banner, which is the intended trade: an entry disappearing from a machine is
either a deliberate uninstall worth one `capture --apply` by hand, or a
machine that has broken in a way worth knowing about tonight.

## Comparing and filing

- `report [profile]` prints a readable overview: a counts matrix (kind x
  profile), then per-profile sections listing every entry of every kind,
  columnized. With a profile argument it shows just common plus that profile.
- `profiles` lists the supported profile names and marks the one recorded on
  this machine.
- `compare <a> <b>` shows each kind side by side: `=` for entries in both,
  `<a` / `>b` for entries only one profile has. It reads the raw profile files,
  not the layered view — `common` applies to both by definition.
- `triage [kind]` walks everything on this machine that **no** manifest lists
  and asks where each item belongs: `c`=common, a profile's name or its first
  letter when no other profile shares it, `s`=skip, `q`=quit. Kinds are `brew`, `cask`, `mas`, `service`
  and `cron` — the last two have no package manager behind them, so they reuse
  the discovery described under `candidates` below. Filing a service shows its
  ports while asking and warns afterwards when a service already in that
  profile wants one of them:

  ```
    local-clickhouse   8002 8123 9000   [c/<profile>/s/q] l
            -> laptop.service
            CLASH   port 8002 is also wanted by redis in this profile
  ```

  No `local-*` pair actually clashes today — the defaults were separated so all
  13 can run at once — so the warning above is illustrative. It still fires for
  real when a port override collides with something, which is the case it
  exists for.

  It warns rather than refuses — the right fix may be to drop the other one,
  and triage is not the place to decide that. `agent` is deliberately not a
  triage kind: `capture` rewrites `<profile>.agent` wholesale, so a hand-filed
  label would be gone on the next run. Dry-run by default like everything
  else; answers can also be piped (`printf 'p\ns\n' | ./machine-setup.sh
  triage brew --apply`), which is how it is tested.
- `candidates [kind]` covers the three kinds `triage` cannot: there is no
  package manager to ask what services, cron jobs or launch agents a machine
  *could* run, so each is discovered instead. `+` means the profile already
  lists it, `.` means it is available and unlisted. Read-only in every kind —
  `--apply` changes nothing, because which of these to run is a decision.

  | Kind | Discovered from | Filed by |
  | --- | --- | --- |
  | `service` | `local-*/` folders whose entrypoint has a `start)` arm that mentions docker | hand-editing `<profile>.service` |
  | `cron` | `local-cron-jobs/cron-jobs.sh list` | hand-editing `<profile>.cron` |
  | `agent` | `~/Library/LaunchAgents/<prefix>.*` and `MACHINE_SETUP_AGENT_GLOBS` (minus `<prefix>.cron.*`) | `capture --apply` |

  The `service` listing also prints each folder's published host ports and
  flags two kinds of trouble. `CLASH` is theory — two candidates want the same
  port, so at most one may be listed in a profile. `BUSY` is fact — something
  on this machine is already listening, so the container will fail to bind,
  and the holder is often nothing in this repo (OpenWhispr and Miyo each
  bundle a qdrant; other projects run postgres containers). Listeners come
  from one `lsof` scan for the whole table, and each port reads `LISTEN`,
  `CLEAR` or `UNKNOWN`: a missing `lsof`, or one that reports an error, clears
  no port. lsof shows only this user's sockets, so `netstat` is read too: a
  port it lists as listening that lsof did not name (root's launchd and
  tailscaled hold several) is `UNKNOWN`, and without a readable macOS
  `netstat` no port is `CLEAR`. Ports are read
  from `docker run -p` flags, compose `ports:` lists, and the defaults of
  `${..._PORT:-N}` overrides, with an exported override winning so `BUSY`
  reflects what would really be bound. Both the folder
  list and the port list are read from the files on every run rather than kept
  as tables in the script — a hand-maintained copy is what goes stale when a
  service is added. Container names that differ from the folder suffix
  (`local-milvus` → `zilliz`) come from `container_for()`, the same mapping the
  `services` stage uses. A service with several containers
  (`local-genai-traces`) counts as running only while all of them are up.

## macOS settings

The `macos` stage applies `defaults` writes. Only keys **listed in a manifest**
are ever touched or captured — whole-domain exports rot across macOS releases
and some domains (Safari, AddressBook) are TCC-blocked and read back empty, so
curating the key list by hand is the only honest version. `capture` refreshes
the values of listed keys and never invents new ones. Changes need the
affected app restarted; after a real write the stage prints the `killall` line.

## Standalone applications

`apps` reports what is in `/Applications` with **no App Store receipt and no
Homebrew management**, and which of those have a cask. Classification is
authoritative, not name-guessing: MAS receipts, the installed casks' artifact
lists, pkg-based casks by token, `com.apple.*` and Google Drive shortcut
bundles excluded. On this machine that is 21 apps, 15 of them cask-covered.

For an app that has a cask, `brew install --cask --adopt <token>` adopts the
existing install in place — no reinstall, and from then on `brew upgrade`
manages it. `adopt --apply` does that for every cask-covered standalone app
at once and files each token into `<profile>.cask` (dropping the name from
`<profile>.app`); pkg-based casks may prompt for sudo. The remaining
no-cask apps (own builds, betas, vendor installers) stay manual; `capture`
records them in `<profile>.app` so a new machine at least gets a checklist.
`MACHINE_SETUP_APPLICATIONS_DIR` points the scan at another folder. It exists
for the tests only.

## Secrets

The ~40 live API keys (`OPENAI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`,
`HF_TOKEN`, …) live in **`~/.config/shell/env.sh`**, mode 600, and are
**never captured**. Committing that file would publish all of them.
`~/.bash_profile` sources it and is denylisted too, for the same reason.

They used to sit inline in `~/.bash_profile`, which only login shells read.
launchd starts jobs from launchd, so every cron job ran with `PATH` and `HOME`
and nothing else, and any MCP server authenticating by `${VAR}` resolved empty
inside it — proven with a LaunchAgent probe: all four credential-bearing
servers reported `EMPTY` before the split and `SET` after.
`local-cron-jobs/cron-jobs.sh` sources the same file, so the two worlds cannot
drift.

`shell-env.example` tracks the variable **names** (values `change-me`), so a
new machine gets the list without the secrets. This folder ships a generic
list; `doctor` reads `$SYSTEM_TOOLS_CONFIG/machine-setup/shell-env.example`
instead when it exists, so the operator's own list stays private. `doctor` reports the file's
presence and mode, and which names in the example this machine has not set.
With an sd agent in the profile it also reads the provider registry through
the runner's own interpreter — the merged view, so an entry the dashboard
switched off is skipped — sources this file in a subshell the way
`local-sd-runner/runner.sh` does on `serve`, and prints a `FAIL … MISSING`
line naming each enabled entry whose `env:` variable is unset or empty there.
Names only, never a value.

**Outside that check, an empty value is fine and doctor stays quiet about
it.** A name an enabled provider entry reads through `env:` must carry a value,
as the paragraph above says; every other name may be empty. The names are
deliberately the same on every machine so the file diffs cleanly, so a machine
with no business holding a given credential keeps the name with nothing after
the `=`. That is a statement, not an unfinished edit: it needs no value, no
entry here, and no removal. The only thing `doctor` asks you to write down is a
name carrying a **real value** that `shell-env.example` does not list — the one
case where a rebuild would come up short.

Every captured dotfile is re-scanned for credential shapes immediately before
it is copied. A file that matches is refused with a count, so a key pasted into
`.zshrc` next year cannot reach the repo through `capture`.

The scan matches four shapes: keyword assignments (`API_KEY=…`), high-entropy
assignment values, **colon-shaped secret keys** (`"github_personal_access_token":
"…"`), and **known provider token prefixes** anywhere on the line (`ghp_`,
`github_pat_`, `xoxb-`, `sk-`, `AKIA`, `AIza`, `glpat-`). The last two were
added after the first two were found to be blind to JSON and YAML: they all
require an `=`, so a live `ghp_` token sitting in a JSON config scored zero.
`~/.config/zed/settings.json` is exactly that case and is why it is **not** in
the captured list — Zed stores an MCP token inline, so the scan refuses it.

Per-service secrets keep the repo's pattern: a committed `.env.example` in the
folder, and the filled `.env` in `$SYSTEM_TOOLS_CONFIG/<tool>/`. This script
does not manage them — `doctor` lists which folders still lack a filled `.env`
on this machine, and
`local-scan-for-secrets` handles auditing and rotation.

## Gotchas

- `dotfiles` **never overwrites** an existing file. If the machine and the repo
  disagree it prints `DIFFERS` and stops; decide which is right, then `capture`.
  A file that exists for no profile at all reports `not captured for <profile>
  yet` — `capture` on that machine is what fills it in.
- `prompts` **never overwrites** a managed block that was edited in place: it
  reports `DIFFERS` and skips, so a `/memory` edit to `~/.claude/CLAUDE.md`
  survives a provisioning run. Keep the edit with `agent-prompt.sh capture
  --apply`, or discard it with `refresh --force`.
- `.service` is not auto-captured. Which services a machine *should* run is a
  decision, not an observation — edit `<profile>.service` by hand.
  `candidates service` lists what there is to choose from.
- `candidates` reports a service as `stopped` whenever no container by the
  expected name is up. That is a statement about this machine right now, not
  about whether the profile is satisfied — `update services` is what starts
  the missing ones.
- `status` compares against `brew list --formula`, not `brew leaves`: a manifest
  entry can be installed as another formula's dependency (`ripgrep` is here) and
  `leaves` omits those, which reported them as missing when they were present.
  Extras are compared against `leaves`, since the full list would name every
  transitive dependency.
- Tap-qualified entries (`openclaw/tap/goplaces`) are listed by their bare name;
  both the install check and the drift report normalize before comparing.
- `capture` writes only what `common` does not already provide, so profile files
  stay small and the baseline stays in one place.
- **A copied file that differs is judged, not guessed at.** `dotfiles`, `envs`
  and `agents` copy a file into place, and `cmp` alone cannot say which side
  moved — so every one of them used to assume the machine had, and printed
  "review, then `capture`". That is backwards after a pull: #160 rewrote a
  comment in `.zshrc` and #161 added an alias to `.bash_aliases`, and capturing
  either would have written this machine's older text back over them.
  `machine-setup` now records the hash of what it installed, under
  `$MACHINE_SETUP_STATE/installed/`, and reads three states out of it:

  | marker | meaning | `update --apply` |
  |---|---|---|
  | `STALE` | machine still matches the record — the repo moved | refreshes it, keeping a `.bak-<stamp>` copy |
  | `DIFFERS` | machine no longer matches the record — edited here | leaves it; `capture --apply` to keep the edit, `--force` to discard it |
  | `DIFFERS` (no record) | nothing recorded says which moved | leaves it, and says so rather than guessing; `--force` overwrites it, keeping a `.bak-<stamp>` copy (`dotfiles` and `agents`) |

  A record is written whenever the file is installed **and** whenever the two
  already agree under `--apply`, so an existing machine seeds itself instead of
  sitting at "no record" forever. Dry runs never write state.

  `envs` is the exception that proves the rule: a live `.env` holds credentials
  the committed template omits, so it is never overwritten. The hash recorded
  there is the **template's**, `STALE` means the template gained or changed a
  key, and `--apply` appends only the keys the live file lacks — a key it
  already has keeps its value.
- `capture --apply` **refuses from a checkout behind its upstream**, listing the
  commits it is missing. It fetches first (the local `origin/main` ref is as
  stale as the checkout holding it; an unreachable remote says so and falls back
  to the ref on disk). `capture` is the only verb that writes tracked files from
  machine state, and it rewrites manifests wholesale — the commits a stale
  checkout has not seen are often the ones that changed those very files, which
  is how #141 and #142 happened. `--stale-ok` writes anyway; a dry run only
  warns.
- `capture` regenerates each manifest, but carries the **leading comment block**
  across: everything above the first entry survives, the generated header is
  reprinted. A profile's `.cron` file may hold no entries at all, only a note
  about common cron jobs that machine does not want, and a capture used to
  erase it. Comments *between* entries are still lost; once the
  entry list is regenerated there is nothing to anchor them to. Notes no machine
  can invalidate belong in `common.<kind>`, which `capture` never writes.
- App Store apps are matched on numeric id, never name: names carry spaces and
  the version column changes on every update. `capture` strips that column so
  routine app updates do not produce diff noise.
- `.mas` entries can be enormous — Xcode alone is multiple GB. `common.mas` is
  empty by design; put App Store apps in the profile that actually wants them.
- `mas` is in `common.brew`, since the `appstore` stage cannot run without it.
  The stage reports `mas MISSING` and continues rather than failing the run.
- Shell functions share globals. A stage must not use `want` or `s` as a local:
  those are `do_stages`' filter and loop variables, and clobbering `want` made
  running one stage alone execute every later stage too.
- Under `set -e`, a comment-only profile file made `grep -v` exit 1 and abort
  the piped loop that builds triage's assigned list — everything after the
  empty file was silently mislabeled unassigned. All profile reads go through
  `profile_entries()`, which ends in `|| :`.
- BSD sed has no `\|` alternation; the GNU-ism silently matches nothing.
