"""Host-loopback tunnel to Chromium's container-local debugging endpoint.

The listener never leaves host loopback.  Each accepted TCP connection gets a
separate ``docker exec`` stdio relay because Playwright discovers CDP over HTTP
and then opens a distinct WebSocket connection.
"""
from __future__ import annotations

import itertools
import os
import secrets
import socket
import subprocess
import threading
from typing import Any, Callable


_RELAY_CODE = r"""
import os
import selectors
import socket
import sys

upstream = socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=5)
upstream.settimeout(None)
selector = selectors.DefaultSelector()
selector.register(sys.stdin.buffer, selectors.EVENT_READ, "stdin")
selector.register(upstream, selectors.EVENT_READ, "upstream")

while True:
    for key, _ in selector.select():
        if key.data == "stdin":
            data = os.read(sys.stdin.fileno(), 65536)
            if data:
                upstream.sendall(data)
            else:
                selector.unregister(sys.stdin.buffer)
                upstream.shutdown(socket.SHUT_WR)
        else:
            data = upstream.recv(65536)
            if not data:
                raise SystemExit(0)
            os.write(sys.stdout.fileno(), data)
"""


class SandboxCDPTunnel:
    """Proxy host-loopback TCP connections through container-local stdio."""

    def __init__(
        self,
        container: str,
        *,
        target_port: int = 9223,
        docker: str = "docker",
        popen_factory: Callable[..., Any] = subprocess.Popen,
        run_factory: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._container = container
        self._target_port = int(target_port)
        self._docker = docker
        self._popen = popen_factory
        self._run = run_factory
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._children: set[Any] = set()
        self._clients: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._connection_ids = itertools.count()
        self._tag = f"operator-cdp-relay-{os.getpid()}-{secrets.token_hex(8)}"

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        host, port = listener.getsockname()[:2]
        self.address = (str(host), int(port))
        self.url = f"http://{self.address[0]}:{self.address[1]}"
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="sandbox-cdp-tunnel",
        )
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._closed.is_set():
                client.close()
                break
            worker = threading.Thread(
                target=self._serve_client,
                args=(client, f"{self._tag}-{next(self._connection_ids)}"),
                daemon=True,
                name="sandbox-cdp-connection",
            )
            with self._lock:
                self._clients.add(client)
                self._workers.add(worker)
            worker.start()

    def _serve_client(self, client: socket.socket, relay_tag: str) -> None:
        process = None
        upload = None
        try:
            process = self._popen(
                [self._docker, "exec", "-i", "-u", "opuser", self._container,
                 "python3", "-c", _RELAY_CODE, relay_tag,
                 str(self._target_port)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            with self._lock:
                if self._closed.is_set():
                    self._terminate(process)
                    return
                self._children.add(process)
            upload = threading.Thread(
                target=self._client_to_relay,
                args=(client, process),
                daemon=True,
                name="sandbox-cdp-upload",
            )
            upload.start()
            while not self._closed.is_set():
                data = process.stdout.read(65536)
                if not data:
                    break
                client.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
            if process is not None:
                try:
                    if process.stdin:
                        process.stdin.close()
                except OSError:
                    pass
                self._terminate(process)
                self._kill_container_relay(relay_tag)
            if upload is not None and upload is not threading.current_thread():
                upload.join(timeout=0.5)
            with self._lock:
                self._clients.discard(client)
                if process is not None:
                    self._children.discard(process)
                self._workers.discard(threading.current_thread())

    def _client_to_relay(self, client: socket.socket, process: Any) -> None:
        try:
            while not self._closed.is_set():
                data = client.recv(65536)
                if not data:
                    break
                process.stdin.write(data)
                process.stdin.flush()
        except (OSError, ValueError, BrokenPipeError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _terminate(process: Any) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def _kill_container_relay(self, relay_tag: str) -> None:
        # Bracketing the first character prevents pkill from matching its own
        # command line while retaining a literal match on the relay argv tag.
        pattern = "[o]" + relay_tag[1:]
        try:
            self._run(
                [self._docker, "exec", "-u", "opuser", self._container,
                 "pkill", "-9", "-f", pattern],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def close(self) -> None:
        """Close the listener and reap every host and in-container helper."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._listener.close()
        with self._lock:
            clients = list(self._clients)
            children = list(self._children)
            workers = list(self._workers)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        for process in children:
            self._terminate(process)
        self._kill_container_relay(self._tag)
        self._accept_thread.join(timeout=1)
        for worker in workers:
            worker.join(timeout=1)

    def __enter__(self) -> "SandboxCDPTunnel":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
