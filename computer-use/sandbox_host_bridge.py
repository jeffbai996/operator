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

Usage: sandbox_host_bridge.py <listen_port> <target_port> [listen_ip] [target_ip]
"""
import asyncio
import sys

LISTEN = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 8091
GATEWAY = sys.argv[3] if len(sys.argv) > 3 else "172.17.0.1"
TARGET_IP = sys.argv[4] if len(sys.argv) > 4 else "127.0.0.1"


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


async def _handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    try:
        tr, tw = await asyncio.open_connection(TARGET_IP, TARGET)
    except OSError:
        cw.close()
        return
    await asyncio.gather(_pump(cr, tw), _pump(tr, cw))


async def main() -> None:
    server = await asyncio.start_server(_handle, GATEWAY, LISTEN)
    print(f"bridge: {GATEWAY}:{LISTEN} -> 127.0.0.1:{TARGET}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
