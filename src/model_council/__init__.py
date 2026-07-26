"""A local-first, auditable council of heterogeneous language models."""

from .models import (
    ErrorCategory,
    ProviderConfig,
    ProviderResponse,
    RunPolicy,
    Usage,
)

__all__ = [
    "ErrorCategory",
    "ProviderConfig",
    "ProviderResponse",
    "RunPolicy",
    "Usage",
]

__version__ = "0.2.0a1"
