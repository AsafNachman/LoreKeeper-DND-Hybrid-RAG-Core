"""Thin orchestrator module.

`app.py` imports `LoreKeeper` from `main.py`. The full implementation lives in
`core.lorekeeper` to keep the root module small and maintain a modular architecture.
"""

from core.lorekeeper import LoreKeeper, get_llm_engine, register_runtime_singletons

__all__ = ["LoreKeeper", "get_llm_engine", "register_runtime_singletons"]