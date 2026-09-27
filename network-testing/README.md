# network-testing

iperf3 throughput and ping latency testing between the machines here (LAN,
WiFi, and the Thunderbolt bridge). All output lands in `./logs` (gitignored).

## Usage

```sh
./network-testing.sh server [port]         # iperf3 server (e.g. 5202 for a second one)
./network-testing.sh server-tb <name>      # server bound to a `bridge` record's address
./network-testing.sh client <preset>       # a `client` record from the hosts file
./network-testing.sh results               # throughput column from last run
./network-testing.sh issues                # intervals that dropped below ~300 Mbit
./network-testing.sh ping <name>|<ip>      # a `ping` record (e.g. gateway, dns) or an IP
./network-testing.sh ping-issues           # non-clean lines from all ping logs
./network-testing.sh test -v               # thunderbolt-bridge.sh suite, stubbed tools
```

## Host map

Presets live in `~/.config/system/network-testing/hosts.conf` (`$SYSTEM_TOOLS_CONFIG/network-testing/` when that is set),
outside the checkout. Copy `hosts.conf.example` there and replace its
documentation addresses, or point
`NETWORK_TESTING_HOSTS` at another file. Three record kinds:

| Record | Fields | Used by |
|---|---|---|
| `client` | `<preset> <ip> [<bind-ip>]` | `client <preset>` |
| `bridge` | `<name> <ip>` | `server-tb <name>`, `thunderbolt-bridge.sh <name>` |
| `ping` | `<name> <ip>` | `ping <name>` |

A command that needs a preset fails naming the missing file and both remedies.

## Gotchas

- Client runs are near-endless (`--time 85000` ≈ 23.6 h) — Ctrl-C when done.
- `ping` uses macOS `--apple-time`; runs until interrupted.
- The "issues" filter is a crude grep: it flags any interval whose Mbit value
  doesn't have 3 digits starting 3-9.

## Thunderbolt bridge repair

exo's `io.exo.networksetup` daemon deleted the `bridge0` definition from the
system configuration (global, not per location). `thunderbolt-bridge.sh`
recreates it with every Thunderbolt port as a member and a manual /24
address and a router that does not exist (a scoped route on `bridge0` only,
the LAN default route stays). Re-runnable; it
also deletes the leftover `exo` network location:

```sh
sudo ./thunderbolt-bridge.sh <name>  # a `bridge` record in the hosts file
sudo ./thunderbolt-bridge.sh <ip>    # any address, /24
```

`sudo` drops `NETWORK_TESTING_HOSTS` and may reset `HOME`, so under `sudo`
(with neither `SYSTEM_TOOLS_CONFIG` nor `XDG_CONFIG_HOME` set) the script reads
`~$SUDO_USER/.config/system/network-testing/hosts.conf`.

It backs up `preferences.plist` next to itself before editing.
It refuses to run while `io.exo.networksetup` is loaded, because that daemon
deletes `bridge0` again; unload it with
`sudo launchctl bootout system/io.exo.networksetup` first.
