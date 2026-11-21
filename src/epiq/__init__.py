"""Epiq: an epistemic database for agent research."""

from .api import Database, open_database
from .ledger_api import LedgerDatabase, open_ledger

__version__ = "0.1.0"

# `open` is intentionally the friendly public spelling, despite shadowing the built-in.
open = open_database

__all__ = [
    "Database",
    "LedgerDatabase",
    "open",
    "open_database",
    "open_ledger",
    "__version__",
]
