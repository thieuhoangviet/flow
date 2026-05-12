"""Database wrapper for Flow2API

This module maintains backward compatibility by exporting the Database class
from the refactored db modular structure.
"""

from .db.manager import Database

__all__ = ["Database"]
