"""Security and lifecycle contracts for the sandbox Chromium CDP tunnel."""
from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


_PATH = (Path(__file__).resolve().parents[1] /
         "computer-use" / "sandbox_cdp_tunnel.py")
_SPEC = importlib.util.spec_from_file_location("sandbox_cdp_tunnel", _PATH)
tunnel_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tunnel_mod)


def _no_cleanup(*args, **kwargs):
    return subprocess.CompletedProcess(args[0], 1)


def _local_echo_process(*args, **kwargs):
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys; data=sys.stdin.buffer.read(); "
         "sys.stdout.buffer.write(data); sys.stdout.buffer.flush()"],
        stdin=kwargs["stdin"], stdout=kwargs["stdout"],
        stderr=kwargs["stderr"], bufsize=kwargs["bufsize"],
    )


def _recv_all(sock: socket.socket) -> bytes:
    chunks = []
    while True:
        data = sock.recv(65536)
        if not data:
            return b"".join(chunks)
        chunks.append(data)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_tunnel_uses_exact_host_loopback_and_ephemeral_port():
    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", docker="docker-test",
        popen_factory=_local_echo_process, run_factory=_no_cleanup,
    )
    try:
        host, port = tunnel.address
        assert host == "127.0.0.1"
        assert port > 0
        assert tunnel.url == f"http://127.0.0.1:{port}"
    finally:
        tunnel.close()


def test_tunnel_carries_multiple_binary_connections_bidirectionally():
    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", docker="docker-test",
        popen_factory=_local_echo_process, run_factory=_no_cleanup,
    )
    try:
        for payload in (b"GET /json/version HTTP/1.1\r\n\r\n",
                        b"\x00websocket\xffpayload"):
            with socket.create_connection(tunnel.address, timeout=1) as client:
                client.settimeout(2)
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                assert _recv_all(client) == payload
    finally:
        tunnel.close()


def test_tunnel_carries_concurrent_connections_independently():
    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", docker="docker-test",
        popen_factory=_local_echo_process, run_factory=_no_cleanup,
    )
    payloads = [b"first" * 1000, b"second" * 1000]
    received = [None, None]

    def round_trip(index: int) -> None:
        with socket.create_connection(tunnel.address, timeout=1) as client:
            client.settimeout(2)
            client.sendall(payloads[index])
            client.shutdown(socket.SHUT_WR)
            received[index] = _recv_all(client)

    threads = [threading.Thread(target=round_trip, args=(index,))
               for index in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        assert not any(thread.is_alive() for thread in threads)
        assert received == payloads
    finally:
        tunnel.close()


def test_exec_command_targets_container_loopback_as_opuser():
    commands = []

    def factory(args, **kwargs):
        commands.append(args)
        return _local_echo_process(args, **kwargs)

    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", target_port=9123, docker="docker-test",
        popen_factory=factory, run_factory=_no_cleanup,
    )
    try:
        with socket.create_connection(tunnel.address, timeout=1) as client:
            client.sendall(b"probe")
            client.shutdown(socket.SHUT_WR)
            assert _recv_all(client) == b"probe"
        assert commands
        assert commands[0][:7] == [
            "docker-test", "exec", "-i", "-u", "opuser", "sandbox-test",
            "python3",
        ]
        assert commands[0][-1] == "9123"
        assert "operator-cdp-relay-" in commands[0][-2]
    finally:
        tunnel.close()


def test_container_stdio_relay_pumps_to_loopback_target():
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    payload = b"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:45678\r\n\r\n"
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    def serve() -> None:
        conn, _ = upstream.accept()
        with conn:
            assert conn.recv(65536) == payload
            conn.sendall(response)

    server = threading.Thread(target=serve)
    server.start()
    proc = subprocess.Popen(
        [sys.executable, "-c", tunnel_mod._RELAY_CODE, "relay-test",
         str(upstream.getsockname()[1])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(payload, timeout=3)
        assert proc.returncode == 0, stderr.decode(errors="replace")
        assert stdout == response
    finally:
        upstream.close()
        server.join(timeout=1)


def test_client_eof_reaps_its_exec_helper():
    children = []

    def factory(*args, **kwargs):
        proc = _local_echo_process(*args, **kwargs)
        children.append(proc)
        return proc

    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", docker="docker-test",
        popen_factory=factory, run_factory=_no_cleanup,
    )
    try:
        with socket.create_connection(tunnel.address, timeout=1) as client:
            client.sendall(b"done")
        _wait_until(lambda: children and children[0].poll() is not None)
    finally:
        tunnel.close()


def test_close_terminates_active_exec_helpers_and_listener():
    children = []

    def hanging_factory(*args, **kwargs):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=kwargs["stdin"], stdout=kwargs["stdout"],
            stderr=kwargs["stderr"], bufsize=kwargs["bufsize"],
        )
        children.append(proc)
        return proc

    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", docker="docker-test",
        popen_factory=hanging_factory, run_factory=_no_cleanup,
    )
    client = socket.create_connection(tunnel.address, timeout=1)
    _wait_until(lambda: bool(children))
    address = tunnel.address
    tunnel.close()
    client.close()

    _wait_until(lambda: children[0].poll() is not None)
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.1)


def test_failed_docker_exec_closes_connection_without_exposing_fallback():
    def broken_factory(*args, **kwargs):
        raise OSError("docker exec unavailable")

    tunnel = tunnel_mod.SandboxCDPTunnel(
        "sandbox-test", docker="docker-test",
        popen_factory=broken_factory, run_factory=_no_cleanup,
    )
    try:
        with socket.create_connection(tunnel.address, timeout=1) as client:
            client.settimeout(1)
            client.sendall(b"attacker-controlled bytes")
            assert client.recv(1) == b""
    finally:
        tunnel.close()
