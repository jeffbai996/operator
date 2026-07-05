"""operator — the live browser/computer Operator cockpit.

A self-contained Flask blueprint + headless agent runner + UI. Currently mounted
into the host-app app (served at /squad/operator over Tailscale), but kept as
its own package so it can graduate to a standalone repo (see REPO_PLAN.md).

Exports: `bp` (Flask blueprint), `runner` (the AgentRunner singleton).
"""
from .operator_view import bp          # noqa: F401
from . import operator_agent           # noqa: F401
runner = operator_agent.runner
