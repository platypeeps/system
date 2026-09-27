#!/bin/sh
# Recreate the Thunderbolt Bridge virtual interface and give it a fixed IP.
# Needed after exo's io.exo.networksetup daemon ran: its disable_bridge.sh
# deleted :VirtualNetworkInterfaces:Bridge:bridge0 from the system
# configuration, which is global, not per network location.
#
# Usage (root):
#   sudo ./thunderbolt-bridge.sh <name>  # a `bridge` record in the hosts file
#   sudo ./thunderbolt-bridge.sh <ip>    # any other address, /24
#
# Members are every "Thunderbolt N" hardware port. IPv4 is manual, /24, no
# reachable router, so the LAN default route is untouched. Safe to re-run.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"

# Run under sudo, HOME may be root's; resolve the config directory against
# the invoking user's home instead, unless one is set explicitly.
if [ -n "${SUDO_USER:-}" ] && [ -z "${SYSTEM_TOOLS_CONFIG:-}" ] && [ -z "${XDG_CONFIG_HOME:-}" ]; then
  SUDO_HOME="$(eval echo "~$SUDO_USER")"
  SYSTEM_TOOLS_CONFIG="$SUDO_HOME/.config/system"
fi
# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"

# Host presets live in <config>/network-testing/hosts.conf (lib/config.sh;
# copy hosts.conf.example there), or the file NETWORK_TESTING_HOSTS names. hosts_lookup <kind> <name> prints the
# record's remaining fields; hosts_names <kind> lists the names of one kind.
HOSTS_FILE="${NETWORK_TESTING_HOSTS:-$(st_config_dir network-testing)/hosts.conf}"
require_hosts() {
  [ -r "$HOSTS_FILE" ] && return 0
  echo "missing host presets: $HOSTS_FILE" >&2
  echo "copy network-testing/hosts.conf.example to $HOSTS_FILE and fill it in," >&2
  echo "or export NETWORK_TESTING_HOSTS=<file>" >&2
  exit 1
}
hosts_lookup() {
  require_hosts
  awk -v k="$1" -v n="$2" '{ sub(/#.*/, "") } $1 == k && $2 == n {
    $1 = ""; $2 = ""; sub(/^ +/, ""); print; found = 1; exit }
    END { exit !found }' "$HOSTS_FILE"
}
hosts_names() {
  require_hosts
  awk -v k="$1" '{ sub(/#.*/, "") } $1 == k { printf "%s%s", sep, $2; sep = " " }
    END { print "" }' "$HOSTS_FILE"
}

case "${1:-}" in
  "") echo "usage: $0 <name>|<ip>" >&2; exit 1 ;;
  *.*.*.*) IP="$1" ;;
  *) require_hosts
     IP="$(hosts_lookup bridge "$1")" || {
       echo "usage: $0 <name>|<ip> — no bridge record '$1' in $HOSTS_FILE" >&2
       exit 1
     } ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# exo's daemon deletes bridge0 again once it runs, so a rebuild while it is
# loaded is undone. Refuse before changing anything.
if launchctl print system/io.exo.networksetup >/dev/null 2>&1; then
  echo "io.exo.networksetup is loaded and would undo this; unload it first:" >&2
  echo "  sudo launchctl bootout system/io.exo.networksetup" >&2
  exit 1
fi

PREFS=/Library/Preferences/SystemConfiguration/preferences.plist
PB=/usr/libexec/PlistBuddy
SERVICE="Thunderbolt Bridge"

MEMBERS="$(networksetup -listallhardwareports \
  | awk '/^Hardware Port: Thunderbolt [0-9]+$/ {getline; print $2}')"
[ -n "$MEMBERS" ] || { echo "no Thunderbolt ports found" >&2; exit 1; }

if ifconfig bridge0 >/dev/null 2>&1; then
  echo "bridge0 exists; leaving the interface definition alone"
else
  cp "$PREFS" "$PREFS.bak.$(date +%Y%m%d%H%M%S)"

  $PB -c 'Delete :VirtualNetworkInterfaces:Bridge:bridge0' "$PREFS" 2>/dev/null || true
  $PB -c 'Add :VirtualNetworkInterfaces:Bridge dict' "$PREFS" 2>/dev/null || true
  $PB -c 'Add :VirtualNetworkInterfaces:Bridge:bridge0 dict' "$PREFS"
  $PB -c 'Add :VirtualNetworkInterfaces:Bridge:bridge0:SCNetworkInterfaceType string Bridge' "$PREFS"
  $PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0:UserDefinedName string '$SERVICE'" "$PREFS"
  $PB -c 'Add :VirtualNetworkInterfaces:Bridge:bridge0:Interfaces array' "$PREFS"
  for m in $MEMBERS; do
    $PB -c "Add :VirtualNetworkInterfaces:Bridge:bridge0:Interfaces: string $m" "$PREFS"
  done

  # configd re-reads the file and materialises bridge0.
  killall -HUP configd 2>/dev/null || true
  i=0
  until ifconfig bridge0 >/dev/null 2>&1 || [ $i -ge 20 ]; do sleep 1; i=$((i+1)); done
  ifconfig bridge0 >/dev/null 2>&1 || { echo "bridge0 did not appear; reboot and re-run" >&2; exit 1; }
fi

# exo also left a location named "exo" with per-port "EXO Thunderbolt N"
# services, which claim en1..en3 so the bridge gets no members. Leave it
# BEFORE creating the service: networksetup edits the current location, and a
# service created inside "exo" is deleted along with it.
if [ "$(networksetup -getcurrentlocation)" = exo ]; then
  networksetup -switchtolocation Automatic >/dev/null
  sleep 2
fi
networksetup -listlocations | grep -qx exo && networksetup -deletelocation exo >/dev/null || true

# A disabled service is listed with a leading "*"; strip it, or the exact
# match misses it and a second service with the same name is created.
networksetup -listallnetworkservices | sed 's/^\*//' | grep -qx "$SERVICE" \
  || networksetup -createnetworkservice "$SERVICE" "$SERVICE"   # hardware port = the bridge name
networksetup -setnetworkserviceenabled "$SERVICE" on

# Manual /24. -setmanual is the only networksetup form that takes a mask, and
# it insists on a router; -setmanualwithdhcprouter leaves INFORM and, with no
# DHCP on the link, a /32 that cannot reach the peer. Editing the service's
# IPv4 dict in preferences.plist does not stick: configd writes its in-memory
# copy back over the file. So: a router that does not exist. It yields a
# scoped default route on bridge0 only (UGScIg); the LAN default is untouched.
networksetup -setmanual "$SERVICE" "$IP" 255.255.255.0 "${IP%.*}.1"

# Reload configd every run, not only when bridge0 was just created. When
# bridge0 already existed, nothing made configd load the bridge's member list
# after the location switch, so bridge0 kept its IP with no members.
killall -HUP configd 2>/dev/null || true
missing_members() {
  attached="$(ifconfig bridge0 2>/dev/null | awk '/member:/ {print $2}')"
  for m in $MEMBERS; do
    printf '%s\n' "$attached" | grep -qx "$m" || printf '%s ' "$m"
  done
}
i=0
until [ -z "$(missing_members)" ] || [ $i -ge 20 ]; do sleep 1; i=$((i+1)); done
MISSING="$(missing_members)"
[ -z "$MISSING" ] \
  || echo "bridge0 is missing members: $MISSING; replug the Thunderbolt cable or reboot" >&2

echo
ifconfig bridge0 | grep -E 'inet |member'
