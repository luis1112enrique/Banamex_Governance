"""Request-local chart units; never infer currency from financial labels."""

from contextvars import ContextVar


# Legacy console calls preserve their rendering. HTTP calls supply trusted units.
chart_units: ContextVar[tuple[bool, str | None]] = ContextVar("chart_units", default=(False, None))
