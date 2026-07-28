"""Model registry.

Importing every model here serves a specific purpose: SQLAlchemy only knows
about a table once its class has been imported. Alembic's autogenerate
compares `Base.metadata` against the live database, so a model that was never
imported is invisible -- and Alembic will cheerfully generate a migration that
DROPS the table it cannot see.

So: every new model file must be added to this file. It is the one piece of
bookkeeping in the project that has teeth.
"""

from app.db.base import Base
from app.models.account import Account, AccountBalance
from app.models.audit_log import AuditLog
from app.models.category import Category
from app.models.enums import (
    AccountType,
    AuditAction,
    AuditActor,
    CategorySource,
    PlaidItemStatus,
    SyncStatus,
    SyncTrigger,
    TransactionSource,
    TransactionStatus,
)
from app.models.institution import Institution
from app.models.merchant import Merchant, MerchantAlias
from app.models.plaid_item import PlaidItem
from app.models.rule import Rule
from app.models.sync_history import SyncHistory
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction
from app.models.user import User

__all__ = [
    "Base",
    "Account",
    "AccountBalance",
    "AccountType",
    "AuditAction",
    "AuditActor",
    "AuditLog",
    "Category",
    "CategorySource",
    "Institution",
    "Merchant",
    "MerchantAlias",
    "PlaidItem",
    "PlaidItemStatus",
    "Rule",
    "SyncHistory",
    "SyncStatus",
    "SyncTrigger",
    "Tag",
    "Transaction",
    "TransactionSource",
    "TransactionStatus",
    "TransactionTag",
    "User",
]
