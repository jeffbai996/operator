"""operator — the live browser/computer Operator cockpit.

A self-contained Flask blueprint + headless agent runner + UI. Currently mounted
mounted into a host Flask app as a blueprint, but kept as
its own self-contained package.

Exports: `bp` (Flask blueprint), `runner` (the AgentRunner singleton).
"""
from .operator_view import bp          # noqa: F401
from . import operator_agent           # noqa: F401
runner = operator_agent.runner
