"""A local-first, auditable council of heterogeneous language models."""

from .models import (
    ErrorCategory,
    ProviderConfig,
    ProviderResponse,
    RunPolicy,
    Usage,
)
from .version import PACKAGE_VERSION

__all__ = [
    "ErrorCategory",
    "ProviderConfig",
    "ProviderResponse",
    "RunPolicy",
    "Usage",
]

__version__ = PACKAGE_VERSION
