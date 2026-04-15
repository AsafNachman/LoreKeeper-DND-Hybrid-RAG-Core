"""
Optional Arize Phoenix / OpenTelemetry tracing for LangChain.

Enable by setting PHOENIX_COLLECTOR_ENDPOINT (e.g. http://phoenix:6006/v1/traces in Docker).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def setup_phoenix_tracing(project_name: str = "dnd-lorekeeper") -> None:
    """
    Register OTLP export to Phoenix and instrument LangChain (once per process).
    Safe to call multiple times; only the first successful init applies.
    """
    global _initialized
    if _initialized:
        return

    endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
    if not endpoint:
        logger.debug("PHOENIX_COLLECTOR_ENDPOINT not set; skipping Phoenix tracing.")
        return

    try:
        from phoenix.otel import register
        from openinference.instrumentation.langchain import LangChainInstrumentor

        tracer_provider = register(
            project_name=os.getenv("PHOENIX_PROJECT_NAME", project_name),
            endpoint=endpoint,
            batch=True,
            auto_instrument=False,
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        _initialized = True
        logger.info("Phoenix tracing enabled → %s", endpoint)
    except Exception as exc:
        logger.warning("Phoenix instrumentation failed (%s); continuing without tracing.", exc)

