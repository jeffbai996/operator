"""loop — the Anthropic computer-use agentic loop over the Xvfb display.

Ties display.py + actions.py to the model: give it a task in natural language,
it screenshots the virtual display, sends the frame to Claude with the
`computer_20250124` tool, gets back tool_use actions (click/type/key/scroll/…),
executes them via xdotool, screenshots the result, and feeds it back — until the
model stops calling the tool (task done) or a step cap is hit.

This is the "general GUI use" capability (option B, 2026-06-25): the agent can
be asked to do something that needs a GUI, and it drives the sandboxed Linux
desktop to do it. The key is never hardcoded — it comes from ANTHROPIC_API_KEY.

Usage:
    from loop import run
    result = run("Open the calculator and compute 2+2", max_steps=15)
"""
from __future__ import annotations

import base64
import logging
import os
import time

log = logging.getLogger("computer_use.loop")

# Backend is pluggable: the loop only needs ensure() / screen_size() /
# screenshot(target, out_dir) / execute(action, target). "linux" (option B) drives
# an isolated Xvfb display via scrot+xdotool; "windows" (option A) drives the
# real desktop via PowerShell. Same vision loop, different screen — selected by
# COMPUTER_USE_BACKEND (default linux: safe + isolated).
def _load_backend():
    name = os.environ.get("COMPUTER_USE_BACKEND", "linux").lower()
    if name == "windows":
        import win_backend as _b
        return _b, _b, name
    import display as _display
    import actions as _actions
    return _display, _actions, name

# Computer-use is a beta tool; the model + tool-type version are a matched pair,
# and the pairing is tier-split (verified against the live API 2026-06-25, each
# model accepts EXACTLY ONE version):
#   - current gen (opus 4.6/4.7/4.8, sonnet 4.6) → computer_20251124
#       + beta header computer-use-2025-11-24
#   - older (sonnet 4.5, haiku 4.5, 3.5/3.7) → computer_20250124
#       + beta header computer-use-2025-01-24
# Passing the wrong version 400s with "does not support tool types: computer_…".
MODEL = os.environ.get("COMPUTER_USE_MODEL", "claude-opus-4-8")

_TOOL_NEW = ("computer_20251124", "computer-use-2025-11-24")
_TOOL_OLD = ("computer_20250124", "computer-use-2025-01-24")


def _tool_version_for(model: str) -> tuple[str, str]:
    """(tool_type, beta_flag) for `model`. Current-gen models take the newer
    computer_20251124; the older 4.5 / 3.x tier takes computer_20250124."""
    m = model.lower()
    old_tier = ("sonnet-4-5", "haiku-4-5", "3-5-sonnet", "3-7-sonnet",
                "3-5-haiku", "claude-3-")
    return _TOOL_OLD if any(t in m for t in old_tier) else _TOOL_NEW


DEFAULT_MAX_STEPS = int(os.environ.get("COMPUTER_USE_MAX_STEPS", "20"))


def _api_key() -> str:
    """Resolve the Anthropic key from the environment — never hardcoded."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (see .env.example)")
    return key


def _img_block(path: str) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    media = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return {"type": "image", "source": {"type": "base64",
            "media_type": media, "data": data}}


# How many of the most-recent screenshot frames to keep as actual images in the
# request. Older frames are swapped for a text stub — the model acts off the
# current frame, so stale ones just cost tokens (and eventually 413).
KEEP_SCREENSHOTS = int(os.environ.get("COMPUTER_USE_KEEP_SHOTS", "3"))


def _prune_screenshots(messages: list[dict], keep: int) -> None:
    """In-place: keep only the last `keep` image blocks across all messages,
    replacing earlier ones with a tiny '[screenshot omitted]' text block. Walks
    newest→oldest so the surviving images are the most recent state."""
    seen = 0
    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "image":
                seen += 1
                if seen > keep:
                    content[j] = {"type": "text",
                                  "text": "[earlier screenshot omitted]"}


def run(task: str, *, max_steps: int = DEFAULT_MAX_STEPS,
        display_id: str | None = None) -> dict:
    """Drive the virtual display to accomplish `task`. Returns a result dict:
    {"done": bool, "steps": int, "final_text": str, "last_screenshot": path}.

    Blocking + synchronous — a caller (CLI / MCP wrapper) runs it in a thread.
    """
    import anthropic  # local import: heavy, and keeps the module importable bare

    disp_mod, act_mod, backend = _load_backend()
    # Linux backend takes a display id (":99"); windows backend ignores it.
    if backend == "windows":
        disp = disp_mod.ensure()
    else:
        disp = disp_mod.ensure(display_id or disp_mod.DEFAULT_DISPLAY)
    width, height = disp_mod.screen_size()
    out_dir = os.environ.get(
        "COMPUTER_USE_OUTPUT_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "computer-use"),
    )
    # the backend's own error class — caught around action execution below
    backend_err = getattr(act_mod, "ActionError",
                          getattr(act_mod, "WinBackendError", Exception))

    client = anthropic.Anthropic(api_key=_api_key())
    tool_type, beta_flag = _tool_version_for(MODEL)
    _disp_num = (int(disp.lstrip(":")) if isinstance(disp, str)
                 and disp.lstrip(":").isdigit() else 1)
    tools = [{
        "type": tool_type, "name": "computer",
        "display_width_px": width, "display_height_px": height,
        "display_number": _disp_num,
    }]

    # Seed with the task + an initial screenshot so the model sees the start state.
    first_shot = act_mod.screenshot(disp, out_dir)
    messages: list[dict] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": task},
            _img_block(first_shot),
        ],
    }]

    last_shot = first_shot
    final_text = ""
    for step in range(1, max_steps + 1):
        # Prune old screenshots so the request doesn't grow unbounded — every step
        # appends a full-desktop image, and ~12 of them blow past the request-size
        # limit (the 413 that killed the first Steam run). The model only needs the
        # CURRENT state to act, so keep the last N image frames and swap older ones
        # for a tiny placeholder; the action/text history stays intact.
        _prune_screenshots(messages, keep=KEEP_SCREENSHOTS)
        try:
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=2048, tools=tools,
                messages=messages, betas=[beta_flag],
            )
        except Exception as e:  # noqa: BLE001 — network/API errors must not crash the bot
            log.error("anthropic call failed at step %d: %s", step, e)
            return {"done": False, "steps": step, "error": str(e),
                    "final_text": final_text, "last_screenshot": last_shot}

        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        texts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if texts:
            final_text = "\n".join(texts)

        if not tool_uses:
            # Model stopped calling the tool → it considers the task done.
            log.info("task complete in %d step(s)", step)
            return {"done": True, "steps": step, "final_text": final_text,
                    "last_screenshot": last_shot}

        # Execute each requested action, then return a fresh screenshot as the
        # tool_result so the model sees the consequence.
        results = []
        for tu in tool_uses:
            inp = tu.input or {}
            try:
                if inp.get("action") == "screenshot":
                    pass  # just (re)capture below
                elif inp.get("action") == "wait":
                    time.sleep(float(inp.get("duration", 1)))
                else:
                    act_mod.execute(inp, disp)
            except backend_err as e:
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": [{"type": "text", "text": f"action failed: {e}"}],
                                "is_error": True})
                continue
            time.sleep(0.4)  # let the UI settle before capturing
            last_shot = act_mod.screenshot(disp, out_dir)
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": [_img_block(last_shot)]})
        messages.append({"role": "user", "content": results})

    log.warning("hit max_steps=%d without the model finishing", max_steps)
    return {"done": False, "steps": max_steps, "final_text": final_text,
            "last_screenshot": last_shot, "error": "max_steps reached"}
