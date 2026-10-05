"""This Tailscale node and its TCP peers, as the local daemon names them.

Step 7 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`.
Two listeners admit a tailnet peer: `sd-db.sh serve` without `--loopback`,
and the dashboard's direct listener on 8768. Both read the rules here, so a
change to one is a change to both:

* The peer is the TCP socket's own address: a canonical IPv4 address in
  100.64.0.0/10 and a real port. Request headers and frames never name it.
* `tailscale whois --json --proto=tcp <ip:port>` names the node that owns
  the connection and the node's owner. A tagged node, an expired node and a
  node that does not own the address have no login.
* The caller compares that login with the operator's, exactly.

`whois` names a node's owner, not the local account that dialed. Any account
on an admitted node passes, so `serve` also refuses this node's own
addresses (design, Q3 = A): a second account on the hub would otherwise
reach the hub's file through its own Tailscale address.

An answer the daemon cannot give, or gives malformed, raises `TailnetError`.
No caller reads it as an admission.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass

from .errors import SdDbError

#: The CGNAT range Tailscale assigns IPv4 node addresses from.
TAILNET = ipaddress.IPv4Network("100.64.0.0/10")
#: Seconds for one `tailscale` call. A stalled daemon refuses a session; it
#: does not hold the listener's thread for a TCP timeout.
TIMEOUT = 10.0


class TailnetError(SdDbError):
    """The local Tailscale daemon could not name this node or a peer."""


def run(arguments: list[str]) -> str:
    """The standard output of one `tailscale` call, or `TailnetError`."""
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TailnetError(f"cannot run {arguments[0]}: {error}") from error
    if result.returncode:
        problem = (result.stderr or result.stdout).strip()[:500]
        raise TailnetError(f"{' '.join(arguments[:2])} failed: {problem}")
    return result.stdout


def valid_login(login) -> bool:
    return isinstance(login, str) and bool(login) and login == login.strip()


def peer_address(peer) -> tuple[ipaddress.IPv4Address, int] | None:
    """The socket peer as `(address, port)` when it is a tailnet IPv4 peer, else `None`."""
    try:
        if (not isinstance(peer, tuple) or len(peer) != 2 or not isinstance(peer[0], str)
                or type(peer[1]) is not int or not 1 <= peer[1] <= 65535):
            return None
        address = ipaddress.IPv4Address(peer[0])
    except ValueError:
        return None
    if str(address) != peer[0] or address not in TAILNET:
        return None
    return address, peer[1]


@dataclass(frozen=True)
class Identity:
    """What `tailscale whois` says about one TCP peer."""

    address: ipaddress.IPv4Address
    login: object
    tags: tuple[str, ...]
    expired: bool
    owns_address: bool

    @property
    def refusal(self) -> str | None:
        """Why this peer has no login, or `None` when its login stands."""
        if self.tags:
            return f"a tagged node ({', '.join(self.tags)})"
        if self.expired:
            return "a node whose key expired"
        if not self.owns_address:
            return f"a node that does not own {self.address}"
        if not valid_login(self.login):
            return "a node with no owner login"
        return None


def whois(peer, run=run) -> Identity | None:
    """The peer's identity, or `None` for a peer that is not a tailnet IPv4 peer.

    A non-tailnet peer never reaches the daemon.
    """
    found = peer_address(peer)
    if found is None:
        return None
    address, port = found
    try:
        result = json.loads(run(["tailscale", "whois", "--json", "--proto=tcp", f"{address}:{port}"]))
        node, user = result["Node"], result["UserProfile"]
        if not isinstance(node, dict) or not isinstance(user, dict):
            raise ValueError("missing Tailscale peer identity")
        addresses = {ipaddress.ip_interface(value).ip for value in node.get("Addresses", [])}
        tags = node.get("Tags")
        tags = tuple(str(tag) for tag in tags) if isinstance(tags, list) else ((str(tags),) if tags else ())
        return Identity(address, user.get("LoginName"), tags, bool(node.get("Expired")), address in addresses)
    except (KeyError, TypeError, ValueError) as error:
        raise TailnetError(f"cannot identify the Tailscale peer {address}:{port}") from error


def login(peer, run=run) -> str | None:
    """The peer node's owner login, or `None` when the peer has none."""
    identity = whois(peer, run)
    if identity is None or identity.refusal is not None:
        return None
    return identity.login


@dataclass(frozen=True)
class Node:
    """This machine's node: its Tailscale addresses and its owner's login."""

    addresses: frozenset
    login: str
    address: ipaddress.IPv4Address


def this_node(run=run) -> Node:
    """This node, from `tailscale status --json`; refuse one with no operator.

    A tagged node is owned by a tag, not a person, so no login could match.
    """
    try:
        state = json.loads(run(["tailscale", "status", "--json"]))
        own = state["Self"]
        user = state["User"][str(own["UserID"])]
        addresses = frozenset(ipaddress.ip_address(value) for value in own["TailscaleIPs"])
        owner = user["LoginName"]
        tags = own.get("Tags")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise TailnetError("cannot identify this Tailscale node and its owner") from error
    if state.get("BackendState") != "Running":
        raise TailnetError("Tailscale is not running")
    if tags:
        raise TailnetError("this node is tagged, so it has no operator login to admit")
    if not valid_login(owner):
        raise TailnetError("this node's owner has no login")
    ipv4 = sorted(value for value in addresses if value.version == 4 and value in TAILNET)
    if not ipv4:
        raise TailnetError("this node has no Tailscale IPv4 address")
    return Node(addresses, owner, ipv4[0])
