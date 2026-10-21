"""One owned Codex app-server connection per Operator run.

No retry after turn/start is written: a timeout is an unknown dispatch, not
permission to execute the user's task twice. The owner retains the real Popen
and process group so existing Stop/handoff cancellation still applies.
"""
from __future__ import annotations

import json
import queue
import threading
import os
import signal
import subprocess


def close_process(process, timeout=2):
    """Boundedly reap this owned process group without changing turn outcome."""
    try:
        if process.stdin:
            process.stdin.close()
    except (OSError, ValueError):
        pass
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            except OSError:
                return False
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            continue
    return False


class TransportError(RuntimeError):
    pass


class AppServer:
    def __init__(self, process):
        self.process = process
        self.thread_id = self.turn_id = ""
        self.dispatched = False
        self.closed = threading.Event()
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._pending = {}
        self._sequence = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _send(self, message):
        with self._lock:
            if self.closed.is_set():
                raise TransportError("Codex connection closed")
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and "method" not in msg:
                    with self._lock:
                        target = self._pending.get(msg["id"])
                    if target:
                        target.put(msg)
                elif "id" in msg:
                    # Never auto-approve an unexpected server-side request.
                    self._send({"id": msg["id"], "error": {
                        "code": -32601, "message": "Operator cannot approve this request automatically"}})
                else:
                    self.events.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.closed.set()
            with self._lock:
                for target in self._pending.values():
                    target.put({"error": {"message": "Codex connection closed"}})
            self.events.put(None)

    def request(self, method, params, timeout=10):
        target = queue.Queue()
        with self._lock:
            self._sequence += 1
            ident = self._sequence
            self._pending[ident] = target
        try:
            self._send({"id": ident, "method": method, "params": params})
            try:
                reply = target.get(timeout=timeout)
            except queue.Empty as exc:
                raise TransportError(f"Codex {method} acknowledgement timed out") from exc
            if "error" in reply:
                raise TransportError(str(reply["error"].get("message", "Codex request failed"))[:400])
            return reply.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def begin(self, *, prompt, cwd, model, effort, resume_id="", service_tier=""):
        self.request("initialize", {"clientInfo": {
            "name": "operator", "title": "Operator", "version": "1.2.7"}})
        self._send({"method": "initialized", "params": {}})
        params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "danger-full-access"}
        if model:
            params["model"] = model
        if service_tier:
            params['serviceTier'] = service_tier
        if resume_id:
            params["threadId"] = resume_id
        reply = self.request("thread/resume" if resume_id else "thread/start", params, timeout=30)
        self.thread_id = reply["thread"]["id"]
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]}
        if effort:
            params["effort"] = effort
        if service_tier:
            params['serviceTier'] = service_tier
        self.dispatched = True
        reply = self.request("turn/start", params, timeout=30)
        self.turn_id = reply["turn"]["id"]

    def steer(self, text):
        if not self.turn_id:
            raise TransportError("Codex is still starting")
        reply = self.request("turn/steer", {"threadId": self.thread_id,
            "expectedTurnId": self.turn_id, "input": [{"type": "text", "text": text}]}, timeout=3)
        if reply.get("turnId") != self.turn_id:
            raise TransportError("Codex acknowledged a different turn")
        return reply

    def notifications(self):
        started = False
        while True:
            event = self.events.get()
            if event is None:
                raise TransportError("Codex disconnected before completing the turn")
            params = event.get("params", {})
            if params.get("threadId", self.thread_id) != self.thread_id:
                continue
            if params.get("turnId", params.get("turn", {}).get("id", self.turn_id)) != self.turn_id:
                continue
            if event.get('method') == 'turn/started':
                started = True
            if event.get('method') == 'thread/tokenUsage/updated' and not started:
                continue  # resume notifications describe the prior turn
            yield event
            if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == self.turn_id:
                return


def command(exec_plan):
    """Reuse the audited CLI config overrides, not a second browser config."""
    source = exec_plan.cmd
    out = [source[0], "app-server"]
    index = 2
    while index < len(source) - 1:
        if source[index] == "-c":
            out.extend(source[index:index + 2])
            index += 2
        elif source[index] == "-m":
            index += 2  # supplied explicitly to thread/start or resume
        else:
            index += 1
    return out


def cli_event(event):
    """Project native items onto the existing activity parser, without prose scraping."""
    method, params = event.get("method"), event.get("params", {})
    if method in ("item/started", "item/completed"):
        item = dict(params.get("item", {}))
        types = {"agentMessage": "agent_message", "commandExecution": "command_execution",
                 "mcpToolCall": "mcp_tool_call", "reasoning": "reasoning", "fileChange": "file_change"}
        item["type"] = types.get(item.get("type"), item.get("type"))
        if item["type"] == "reasoning":
            # Public summaries only. The raw reasoning content is not a UI feed.
            summary = item.get("summary", [])
            item["text"] = "\n".join(s for s in summary if isinstance(s, str)) if isinstance(summary, list) else ""
        if "aggregatedOutput" in item:
            item["aggregated_output"] = item["aggregatedOutput"]
        if "exitCode" in item:
            item["exit_code"] = item["exitCode"]
        return {"type": "item.started" if method.endswith("started") else "item.completed", "item": item}
    return None
