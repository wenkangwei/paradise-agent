"""Structured logging setup — JSON formatter when structlog available.

Phase 6 — Observability.

In prod (PARADISE_MODE=prod), emits JSON logs suitable for ELK/Loki ingestion.
In dev, uses default stdlib logging (human-readable).
"""
from __future__ import annotations

import logging
import os
import sys


def setup_structlog(level: str = "INFO") -> None:
    """Configure root logger.

    Prod (structlog installed): JSON formatter with timestamp, level, logger, msg.
    Dev: stdlib default format.

    Safe to call multiple times — re-configures handlers.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Remove existing handlers so re-config doesn't duplicate output
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)

    if os.getenv("PARADISE_MODE", "dev") == "prod":
        try:
            import structlog
            # Configure structlog to emit JSON
            structlog.configure(
                processors=[
                    structlog.contextvars.merge_contextvars,
                    structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.processors.StackInfoRenderer(),
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                wrapper_class=structlog.make_filtering_bound_logger(log_level),
                logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
                cache_logger_on_first_use=True,
            )
            # Bridge stdlib logging into structlog
            handler.setFormatter(structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=[
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.processors.add_log_level,
                ],
            ))
            logging.basicConfig(level=log_level, handlers=[handler])
            logging.getLogger("paradise").info("structlog configured (JSON output)")
            return
        except ImportError:
            pass

    # Dev mode (or structlog not installed): human-readable
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(log_level)
