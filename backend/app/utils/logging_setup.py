"""
Structured logging configuration using structlog.
JSON-formatted output suitable for production log aggregation.
Log entries never include OTP codes, tokens, or full document content.
"""
import logging
import structlog
from app.utils.config import settings


def configure_logging():
    """Configure structlog for the application."""
    log_level = logging.DEBUG if settings.ENVIRONMENT == "development" else logging.INFO

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if settings.ENVIRONMENT != "development"
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
    )
    # Suppress noisy third-party loggers
    # HTTP/2's low-level debug output can include Authorization and API-key
    # header values. Keep the complete transport stack above DEBUG even in
    # local development so secrets never reach terminal or aggregated logs.
    for logger_name in ("httpx", "httpcore", "hpack", "h2", "supabase"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
