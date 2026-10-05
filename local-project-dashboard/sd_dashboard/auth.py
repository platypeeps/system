"""The configured Tailscale Serve boundary; forwarded headers never select it.

Serve removes client-supplied Tailscale identity headers and replaces them
with the authenticated identity. This trust applies only to the configured
HTTPS authority, with a verified private Serve route and a loopback socket.
Local processes already trusted by this machine remain inside that boundary.
An optional IP listener authenticates its socket peer through Tailscale WhoIs;
request headers never provide that listener's identity.
"""

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit

from sd_db import tailnet


@dataclass(frozen=True)
class DirectAccess:
    origin: str
    authority: str
    address: str
    port: int


@dataclass(frozen=True)
class FrontDoor:
    origin: str
    operator_login: str
    authority: str
    serve_authority: str
    direct: DirectAccess | None = None


@dataclass(frozen=True)
class Context:
    origin: str
    principal: str
    secure: bool
    remote: bool = False

    @property
    def scope(self):
        return f"{self.origin}\n{self.principal}"


def parse_direct_origin(config):
    """Allow one explicit IPv4 address, with identity owned by the HTTPS config."""
    if not isinstance(config, dict):
        raise ValueError("Dashboard configuration must be an object.")
    if "ip_origin" not in config:
        return None
    origin, operator = config.get("origin"), config.get("operator_login")
    if (not isinstance(origin, str) or not isinstance(operator, str)
            or operator != operator.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+", operator)):
        raise ValueError("IP access requires the existing HTTPS origin and exact operator_login.")
    try:
        https = urlsplit(origin)
        https_port = 443 if https.port is None else https.port
        if (https.scheme != "https" or not https.hostname or not https.hostname.endswith(".ts.net")
                or not re.fullmatch(r"[a-z0-9.-]+", https.hostname)
                or https.username or https.password or https.path not in ("", "/")
                or https.query or https.fragment or not 1 <= https_port <= 65535):
            raise ValueError("IP access requires a valid HTTPS Tailscale origin.")
        value = config["ip_origin"]
        if not isinstance(value, str):
            raise ValueError("ip_origin must be an explicit HTTP Tailscale IPv4 origin.")
        parsed = urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname or "")
        authority = f"{address}:8768"
        if (parsed.scheme != "http" or parsed.netloc != authority
                or address not in ipaddress.IPv4Network("100.64.0.0/10")
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or config.get("port", 8767) == 8768
                or value not in (f"http://{authority}", f"http://{authority}/")):
            raise ValueError("Use a canonical HTTP Tailscale IPv4 origin on port 8768, separate from the backend.")
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid ip_origin: use http://100.x.y.z:8768 with a distinct backend port.") from error
    return DirectAccess(f"http://{authority}", authority, str(address), 8768)


def validate_frontdoor(config, serve_status, backend_port):
    """Reject a mismatched, public, or ambiguous proxy before serving requests."""
    if not isinstance(config, dict):
        raise ValueError("Dashboard configuration must be an object.")
    origin, operator = config.get("origin"), config.get("operator_login")
    if not origin and not operator:
        parse_direct_origin(config)
        return None
    if not isinstance(origin, str) or not isinstance(operator, str):
        raise ValueError("The dashboard requires both HTTPS origin and operator_login.")
    try:
        parsed = urlsplit(origin)
        port = 443 if parsed.port is None else parsed.port
    except ValueError as error:
        raise ValueError("The dashboard origin is invalid.") from error
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".ts.net")
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not 1 <= port <= 65535):
        raise ValueError("Use an explicit HTTPS Tailscale origin without a path, query or credentials.")
    if operator != operator.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+", operator):
        raise ValueError("operator_login must be the exact Tailscale login.")
    if config.get("port", backend_port) != backend_port:
        raise ValueError("The configured dashboard port does not match the listener.")
    authority = parsed.hostname + (f":{port}" if port != 443 else "")
    serve_authority = f"{parsed.hostname}:{port}"
    if not isinstance(serve_status, dict):
        raise ValueError("Tailscale Serve status must be an object.")
    for key in ("TCP", "Web", "AllowFunnel"):
        if not isinstance(serve_status.get(key, {}), dict):
            raise ValueError("Tailscale Serve status has an invalid configuration section.")
    tcp = serve_status.get("TCP", {}).get(str(port), {})
    web = serve_status.get("Web", {}).get(serve_authority, {})
    if not isinstance(tcp, dict) or not isinstance(web, dict):
        raise ValueError("Tailscale Serve listener configuration is invalid.")
    handlers = web.get("Handlers") or {}
    if tcp.get("HTTPS") is not True:
        raise ValueError(f"Tailscale Serve has no HTTPS listener for {serve_authority}.")
    if (serve_status.get("AllowFunnel") or {}).get(serve_authority):
        raise ValueError(f"Refusing public Funnel on the dashboard origin {serve_authority}.")
    target = f"http://127.0.0.1:{backend_port}"
    if (not isinstance(handlers, dict) or set(handlers) != {"/"}
            or not isinstance(handlers["/"], dict) or handlers["/"].get("Proxy") != target):
        raise ValueError(f"The dashboard origin must proxy only / to {target}.")
    return FrontDoor(f"https://{authority}", operator, authority, serve_authority,
                     parse_direct_origin(config))


def access_context(headers, peer, port, frontdoor=None):
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return None
    except ValueError:
        return None
    hosts = headers.get_all("Host", [])
    if len(hosts) != 1:
        return None
    host = hosts[0]
    if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        # A forwarded identity must never downgrade itself to the local path.
        if any(name.lower().startswith(("tailscale-", "x-forwarded-", "x-auth-"))
               or name.lower() == "forwarded" for name in headers):
            return None
        return Context(f"http://{host}", "local", False)
    if frontdoor is None or host != frontdoor.authority:
        return None
    if headers.get_all("Tailscale-User-Login", []) != [frontdoor.operator_login]:
        return None
    return Context(frontdoor.origin, frontdoor.operator_login, True, True)


def direct_context(headers, peer, frontdoor, lookup):
    """Authenticate the exact socket peer, never forwarded request identity."""
    if frontdoor is None or frontdoor.direct is None or not callable(lookup):
        return None
    if headers.get_all("Host", []) != [frontdoor.direct.authority]:
        return None
    if any(name.lower().startswith(("tailscale-", "x-forwarded-", "x-auth-"))
           or name.lower() in ("forwarded", "x-real-ip", "remote-user") for name in headers):
        return None
    # The socket peer rule `sd-db.sh serve` applies too, in one place.
    if tailnet.peer_address(peer) is None:
        return None
    if lookup(peer) != frontdoor.operator_login:
        return None
    return Context(frontdoor.direct.origin, frontdoor.operator_login, False, True)
