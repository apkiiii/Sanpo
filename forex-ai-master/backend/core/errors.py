"""Domain exceptions. Every error raised by the system derives from :class:`ForexAIError`."""

from __future__ import annotations


class ForexAIError(Exception):
    """Base class for all application errors."""


class ConfigurationError(ForexAIError):
    """Invalid or missing configuration."""


class DataError(ForexAIError):
    """Market or economic data is missing, stale or inconsistent."""


class InsufficientDataError(DataError):
    """Not enough data to produce a reliable analysis."""


class BrokerError(ForexAIError):
    """Generic broker failure."""


class BrokerConnectionError(BrokerError):
    """The broker is unreachable or the session was lost. Retry may be possible."""


class BrokerTimeoutError(BrokerConnectionError):
    """The broker did not answer in time. The order status is UNKNOWN."""


class OrderRejectedError(BrokerError):
    """The broker explicitly rejected an order. Never retry blindly."""


class RiskViolationError(ForexAIError):
    """An action would violate a risk rule."""


class SafetyLockError(ForexAIError):
    """The kill switch or safety mode blocks this action."""


class LiveModeNotAllowedError(ForexAIError):
    """LIVE mode was requested but the activation requirements are not met."""
