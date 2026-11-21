"""Epiq: an epistemic database for agent research."""

from .api import Database as LegacyDatabase
from .api import open_database as open_legacy_database
from .ledger_api import LedgerDatabase, open_ledger

__version__ = "0.1.0"

# Ledger projects are the public default. SQLite access remains explicit during removal.
Database = LedgerDatabase
open = open_ledger
open_database = open_legacy_database

__all__ = [
    "Database",
    "LedgerDatabase",
    "LegacyDatabase",
    "open",
    "open_database",
    "open_ledger",
    "open_legacy_database",
    "__version__",
]
