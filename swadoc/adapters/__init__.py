"""
Framework adapters for route discovery, annotation handling, and spec generation.

This package contains:
- protocol.py: AdapterProtocol definition and AdapterOperationError
- registry.py: AdapterRegistry for adapter registration and selection
- laravel.py: Laravel (PHP) adapter
- adonisjs.py: AdonisJS (Node.js) adapter
- express.py: Express (Node.js) adapter
"""

from swadoc.adapters.protocol import AdapterOperationError, AdapterProtocol
from swadoc.adapters.registry import (
    AdapterRegistrationError,
    AdapterRegistry,
    AdapterSelectionError,
)

__all__ = [
    "AdapterProtocol",
    "AdapterOperationError",
    "AdapterRegistry",
    "AdapterRegistrationError",
    "AdapterSelectionError",
]
