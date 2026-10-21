"""One registry-backed target resolver for cockpit and control surfaces."""
from __future__ import annotations
import asyncio
import os
from typing import Any
from operator_browser_jobs import tabs


def endpoint() -> str:
    """Match the cockpit instance override; MCP adapters pass this explicitly."""
    return os.environ.get("OPERATOR_DEMO_CDP") or os.environ.get("OPERATOR_CDP") or "http://127.0.0.1:9222"


def owned_targets(conversation_id: str) -> list[str]:
    if not conversation_id:
        return []
    registry = tabs()
    return list(registry._read_registry(registry.DEFAULT_REGISTRY).get(conversation_id, []))


async def resolve_owned_page(browser: Any, conversation_id: str, current: Any = None,
                             target_ids: dict[Any, str] | None = None) -> Any:
    """Never adopt an unowned visible page. Closed roots may use an owned popup."""
    owned = owned_targets(conversation_id)
    if not owned:
        raise RuntimeError("no owned browser target for this conversation")
    ids = target_ids if target_ids is not None else {}
    live = [page for context in browser.contexts for page in context.pages if not page.is_closed()]
    for page in list(ids):
        if page not in live:
            ids.pop(page, None)
    if current in live and ids.get(current) in owned:
        return current
    matches = {}
    for page in live:
        target = ids.get(page)
        if not target:
            session = None
            try:
                session = await asyncio.wait_for(page.context.new_cdp_session(page), .8)
                info = await asyncio.wait_for(session.send("Target.getTargetInfo"), .8)
                target = info.get("targetInfo", {}).get("targetId", "")
                ids[page] = target
            except Exception:
                continue
            finally:
                if session is not None:
                    try: await asyncio.wait_for(session.detach(), .8)
                    except Exception: pass
        if target in owned:
            if target == owned[0]:
                return page
            matches[target] = page
    if current in live and ids.get(current) in owned:
        return current
    for target in owned:
        if target in matches:
            return matches[target]
    raise RuntimeError("owned browser target is unavailable")
