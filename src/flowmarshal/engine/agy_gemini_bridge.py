"""Antigravity (`agy.exe`) stream-json bridge compatibility module.

Core implementation lives in `flowmarshal.engine.gemini_runtime` so that
installed wheel distributions include the bridge without needing developer modules.
"""

from __future__ import annotations

from .gemini_runtime import (
    AGY_CLI_VERSION,
    AGY_MODEL_MAP,
    DEFAULT_AGY_EXE,
    agy_bridge_main,
    create_agy_gemini_runtime,
    default_agy_gemini_model_catalog,
    default_agy_gemini_role_configuration,
    map_to_agy_model,
    resolve_agy_exe,
    write_agy_gemini_model_catalog,
)

main = agy_bridge_main

__all__ = [
    "AGY_CLI_VERSION",
    "AGY_MODEL_MAP",
    "DEFAULT_AGY_EXE",
    "agy_bridge_main",
    "create_agy_gemini_runtime",
    "default_agy_gemini_model_catalog",
    "default_agy_gemini_role_configuration",
    "main",
    "map_to_agy_model",
    "resolve_agy_exe",
    "write_agy_gemini_model_catalog",
]


if __name__ == "__main__":
    raise SystemExit(main())
