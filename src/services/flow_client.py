"""
This file is a lightweight wrapper for backward compatibility.
The actual implementation has been refactored and split into the `flow` package.
"""

from .flow.client import FlowClient

__all__ = ["FlowClient"]
