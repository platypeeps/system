"""Relay Home Assistant's host:port to a loopback port, under Apple's python.

macOS's Local Network privacy refuses node a same-link peer: Homebrew's
node and nodejs.org's notarized build both got EHOSTUNREACH from Home
Assistant, from a terminal too, while Apple's binaries (/usr/bin/python3,
curl, nc, ssh) reached it (sd:10894). Loopback is never gated. So ha-mcp.sh
runs this under /usr/bin/python3 and points mcp-remote at 127.0.0.1.

    relay.py <url> <parent pid>

Binds an ephemeral 127.0.0.1 port, prints <url> with its host:port
replaced by that port as one line on stdout, then copies each connection
to the url's host:port. It exits once its parent is gone: ha-mcp.sh
execs the bridge in its own process, so the bridge is that parent.
A connection it cannot open upstream prints one `cannot reach` line on
stderr. Standard library only; Python 3.9 (the CommandLineTools python).

http only: TLS checks the certificate against the host name, which the
rewrite replaces, so ha-mcp.sh sends an https url to the bridge as it is.
"""

import asyncio
import os
import sys
from urllib.parse import urlsplit, urlunsplit



def target(url):
    """(host, port) an http url names; port 80 when it names none."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"want an http:// url with a host, got {url!r}")
    return parts.hostname, parts.port or 80


def rewrite(url, port):
    """The url with its host and port replaced by 127.0.0.1:port."""
    return urlunsplit(urlsplit(url)._replace(netloc=f"127.0.0.1:{port}"))


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def serve(url, parent):
    host, port = target(url)

    async def handle(client_reader, client_writer):
        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError as err:
            print(f"ha-mcp relay: cannot reach {host}:{port}: {err}", file=sys.stderr, flush=True)
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer),
                             return_exceptions=True)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    print(rewrite(url, server.sockets[0].getsockname()[1]), flush=True)
    async with server:
        while os.getppid() == parent:
            await asyncio.sleep(1)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: relay.py <url> <parent pid>")
    url, parent = sys.argv[1], int(sys.argv[2])
    try:
        target(url)
    except ValueError as err:
        sys.exit(f"ha-mcp relay: {err}")
    if os.getppid() != parent:
        sys.exit("ha-mcp relay: parent already gone")
    asyncio.run(serve(url, parent))


if __name__ == "__main__":
    main()
