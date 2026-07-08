"""operator.control — the fast hands of the planner/controller split.

Surfaces (browser CDP / Xvfb sandbox / real Windows desktop) expose one small
capture+inject interface; the macro controller executes multi-step macros
against a surface with local perception between steps (zero LLM round-trips);
mcp_server.py exposes it all to the agent as MCP tools (perceive / game_macro /
desktop computer actions)."""
