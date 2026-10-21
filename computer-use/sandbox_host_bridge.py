#!/usr/bin/env python3
"""sandbox_host_bridge.py — expose a host-loopback service to the operator
sandbox container.

The sandbox runs on Docker's default bridge; host services that bind
127.0.0.1 (fraggames :8091, etc.) are invisible from inside it, and the
tailnet is Windows-side so ts.net URLs don't route either (found 2026-07-21
wiring Pokémon Emerald into the game harness). This is a stdlib TCP proxy
that listens ONLY on the docker gateway address (172.17.0.1 — reachable from
containers and the host, never the LAN) and forwards to a 127.0.0.1 port.

Two deployments of the same script:
  - HOST (fraggames-sandbox-bridge unit): listen 172.17.0.1:8091 ->
    127.0.0.1:8091, so containers can reach the loopback-bound service.
  - IN-CONTAINER (started by sandbox_container.ensure): listen
    127.0.0.1:8091 -> 172.17.0.1:8091, so the sandbox browser can use
    http://localhost:8091 — a SECURE context. EmulatorJS needs
    SharedArrayBuffer => crossOriginIsolated => https or localhost; plain
    http://172.17.0.1 got "Failed to start game".

A third use, --allow-container NAME, locks a host bridge to one container.
The target sees every bridged connection as a 127.0.0.1 peer, and a raw TCP
client can forge any Host header, so an open bridge hands every container on
docker0 whatever the target trusts loopback with: vecgrep's tokenless admin
REST, ibkr-mcp's /mcp. The owui-bridge-* units use it so only open-webui gets
through and operator-sandbox does not (2026-09-25). The container's IP is
looked up with `docker inspect` and looked up again when a connection comes
from an unknown IP, so a recreated container is admitted without a restart.
A failed lookup refuses.

Usage: sandbox_host_bridge.py <listen_port> <target_port> [listen_ip] [target_ip]
                              [--allow-container NAME]
"""
import argparse
import asyncio
import subprocess
import time
from typing import Callable

# Lookups triggered by unknown peers are spaced at least this far apart, so a
# container hammering the port cannot turn each connection into a docker call.
MIN_RELOOKUP_S = 2.0


def docker_container_ips(name: str) -> set[str]:
    out = subprocess.run(
        ["docker", "inspect", "-f",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", name],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout
    return {ip for ip in out.split() if ip}


class ContainerPeers:
    """Admit connections only from the named container's current IPs."""

    def __init__(self, name: str,
                 resolve: Callable[[str], set[str]] = docker_container_ips,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._resolve = resolve
        self._clock = clock
        self._ips: set[str] = set()
        self._looked_up_at: float | None = None

    def allows(self, peer_ip: str) -> bool:
        if peer_ip in self._ips:
            return True
        now = self._clock()
        if self._looked_up_at is not None and now - self._looked_up_at < MIN_RELOOKUP_S:
            return False
        self._looked_up_at = now
        try:
            self._ips = set(self._resolve(self.name))
        except (OSError, subprocess.SubprocessError, ValueError):
            self._ips = set()
        return peer_ip in self._ips


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass


def make_handler(target_ip: str, target_port: int, peers: ContainerPeers | None = None,
                 source_ip: str | None = None):
    """Relay each admitted connection to the target.

    `source_ip` binds the outbound dial to that address, so the target can
    tell bridged traffic from its own loopback clients. Without it every
    bridged connection arrives as 127.0.0.1 and inherits whatever the target
    trusts loopback with; with 127.0.0.2 the target sees the bridge (found
    2026-09-29: host-app's vault answered a forged same-origin request
    from inside open-webui, because the peer looked local).
    """
    local_addr = (source_ip, 0) if source_ip else None

    async def _handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        if peers is not None:
            peer = cw.get_extra_info("peername")
            peer_ip = peer[0] if peer else ""
            # docker inspect blocks; keep it off the event loop
            if not await asyncio.to_thread(peers.allows, peer_ip):
                print(f"bridge: refused {peer_ip} (not {peers.name})", flush=True)
                cw.close()
                return
        try:
            tr, tw = await asyncio.open_connection(target_ip, target_port, local_addr=local_addr)
        except OSError:
            cw.close()
            return
        await asyncio.gather(_pump(cr, tw), _pump(tr, cw))

    return _handle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Expose a loopback port on the docker gateway.")
    ap.add_argument("listen_port", type=int, nargs="?", default=8091)
    ap.add_argument("target_port", type=int, nargs="?", default=8091)
    ap.add_argument("listen_ip", nargs="?", default="172.17.0.1")
    ap.add_argument("target_ip", nargs="?", default="127.0.0.1")
    ap.add_argument("--source-ip", metavar="IP", default=None,
                    help="bind outbound dials to this address so the target can tell "
                         "bridged peers from its own loopback clients (e.g. 127.0.0.2)")
    ap.add_argument("--allow-container", metavar="NAME",
                    help="admit only connections from this container's IPs")
    return ap.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    peers = ContainerPeers(args.allow_container) if args.allow_container else None
    handler = make_handler(args.target_ip, args.target_port, peers, source_ip=args.source_ip)
    server = await asyncio.start_server(handler, args.listen_ip, args.listen_port)
    lock = f" (only {args.allow_container})" if peers else ""
    print(f"bridge: {args.listen_ip}:{args.listen_port} -> "
          f"{args.target_ip}:{args.target_port}{lock}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
