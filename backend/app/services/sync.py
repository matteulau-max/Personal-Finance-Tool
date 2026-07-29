"""The transaction sync engine.

===========================================================================
How Plaid's incremental sync works
===========================================================================

`/transactions/sync` is a cursor-based feed. You send the cursor you last
saw; Plaid returns everything that changed since then, in three buckets:

    added     transactions you have not seen before
    modified  transactions whose details changed (an amount finalized, a
              merchant name enriched, a pending charge posting)
    removed   transactions that no longer exist (a declined authorization,
              a reversed charge)

...plus a `next_cursor` and `has_more`. You keep calling until `has_more` is
false, then store the final cursor for next time.

===========================================================================
The four rules this engine is built around
===========================================================================

**1. The cursor advances only when the data it covers is committed.**

    Both are written in a single transaction. If the process dies mid-sync,
    PostgreSQL rolls back the page AND the cursor together, and the next run
    re-fetches it. Saving the cursor separately -- or first -- would mean a
    crash silently skips transactions forever, with no error and no way to
    notice until someone audits their statement by hand.

**2. Writes are upserts, so replaying a page is harmless.**

    We use INSERT ... ON CONFLICT DO UPDATE against the unique constraint
    from Milestone 2. Re-running a sync, retrying after a timeout, or two
    syncs racing each other cannot create a duplicate: the database arbitrates.
    This is what "idempotent" means -- doing it twice equals doing it once.

**3. An upsert only ever touches `raw_*` columns.**

    The ON CONFLICT clause names them explicitly. A user's category, notes,
    or corrected amount are never overwritten by a sync, no matter what
    the bank sends. This is the three-layer model from Milestone 2 being
    enforced at the point where it would otherwise be violated.

**4. Nothing is hard-deleted.**

    A removed transaction becomes status=REMOVED with a timestamp. If a sync
    bug removes four hundred transactions, recovery is one UPDATE. With a
    DELETE, they are gone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.crypto import decrypt
from app.models import (
    Account,
    AccountBalance,
    AccountType,
    AuditAction,
    AuditActor,
    AuditLog,
    Institution,
    PlaidItem,
    PlaidItemStatus,
    SyncHistory,
    SyncStatus,
    SyncTrigger,
    Transaction,
    TransactionSource,
    TransactionStatus,
    User,
)
from app.services.enrichment import enrich_transactions
from app.services.plaid_gateway import (
    PlaidAccount,
    PlaidApiError,
    PlaidGateway,
    PlaidTransaction,
)

logger = logging.getLogger(__name__)

# Plaid's `type` values mapped onto our AccountType enum. Anything
# unrecognized becomes OTHER rather than raising -- Plaid adds new types, and
# a new account type appearing must never break syncing for everything else.
ACCOUNT_TYPE_MAP = {
    "depository": AccountType.DEPOSITORY,
    "credit": AccountType.CREDIT,
    "loan": AccountType.LOAN,
    "investment": AccountType.INVESTMENT,
    "brokerage": AccountType.INVESTMENT,
}

# Raw fields a sync is allowed to write. Everything absent from this list --
# every user_* column -- is untouchable by automation.
SYNCABLE_FIELDS = (
    "raw_name",
    "raw_merchant_name",
    "raw_amount",
    "raw_currency_code",
    "raw_date",
    "raw_authorized_date",
    "raw_category",
    "raw_payload",
    "status",
    "replaces_plaid_transaction_id",
)

# Money columns are Numeric(18, 4); audit values are normalized to the same
# scale so "before" and "after" are always directly comparable.
MONEY_SCALE = Decimal("0.0001")

# A sync should never loop forever. If Plaid keeps saying has_more, something
# is wrong and we want a bounded failure rather than an infinite one.
MAX_PAGES = 200


@dataclass
class SyncCounts:
    added: int = 0
    modified: int = 0
    removed: int = 0
    skipped: int = 0
    accounts_updated: int = 0
    # Counted for the sync log, so "why is everything uncategorized?" has an
    # answer without digging through rows.
    categorized: int = 0


class SyncEngine:
    """Synchronizes one Plaid Item into our database."""

    def __init__(self, db: Session, gateway: PlaidGateway) -> None:
        self.db = db
        self.gateway = gateway

    # -----------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------

    def sync_item(self, item: PlaidItem, trigger: SyncTrigger) -> SyncHistory:
        """Run a full sync for one Item and return the SyncHistory record."""
        run = SyncHistory(
            plaid_item_id=item.id,
            trigger=trigger,
            status=SyncStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            cursor_before=item.transactions_cursor,
        )
        self.db.add(run)
        # Committed immediately and deliberately: if the process is killed
        # mid-sync, this row survives as evidence that a run started and never
        # finished. A sync that leaves no trace when it crashes is impossible
        # to debug after the fact.
        self.db.commit()

        counts = SyncCounts()

        try:
            access_token = decrypt(item.access_token_encrypted)
            self._run_pages(item, access_token, counts, run)
        except PlaidApiError as exc:
            self._record_failure(item, run, exc)
            return run
        except Exception as exc:  # noqa: BLE001 - we must record ANY failure
            logger.exception("Unexpected error syncing item %s", item.id)
            self.db.rollback()
            run.status = SyncStatus.FAILED
            run.finished_at = datetime.now(timezone.utc)
            run.error_code = "INTERNAL_ERROR"
            run.error_message = type(exc).__name__
            self.db.add(run)
            self.db.commit()
            return run

        self._record_success(item, run, counts)
        return run

    # -----------------------------------------------------------------
    # Paging
    # -----------------------------------------------------------------

    def _run_pages(
        self,
        item: PlaidItem,
        access_token: str,
        counts: SyncCounts,
        run: SyncHistory,
    ) -> None:
        cursor = item.transactions_cursor
        pages = 0

        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise PlaidApiError(
                    "TOO_MANY_PAGES",
                    f"Sync exceeded {MAX_PAGES} pages; aborting to avoid a loop",
                )

            page = self.gateway.sync_transactions(
                access_token=access_token, cursor=cursor
            )

            if page.accounts:
                counts.accounts_updated += self._upsert_accounts(item, page.accounts)

            accounts_by_plaid_id = self._account_map(item)

            self._apply_transactions(
                item, page.added, accounts_by_plaid_id, counts, is_added=True
            )
            self._apply_transactions(
                item, page.modified, accounts_by_plaid_id, counts, is_added=False
            )
            self._apply_removals(item, page.removed_ids, counts)
            self._reconcile_pending(item, page.added + page.modified)

            # Enrich what this page touched: assign merchants, categories, and
            # any tags the user's rules call for.
            #
            # Done inside the page's transaction so enrichment commits
            # atomically with the transactions it describes. If it ran
            # afterwards, a crash in between would leave rows imported but
            # uncategorized, with nothing recording that they still needed
            # work.
            self._enrich_page(item, page.added + page.modified, counts)

            cursor = page.next_cursor

            # THE CRITICAL LINE. The cursor moves forward in the same
            # transaction as the data it describes, so the two can never
            # disagree. See rule 1 in the module docstring.
            item.transactions_cursor = cursor
            run.cursor_after = cursor
            self.db.commit()

            if not page.has_more:
                break

    # -----------------------------------------------------------------
    # Accounts
    # -----------------------------------------------------------------

    def _upsert_accounts(
        self, item: PlaidItem, plaid_accounts: list[PlaidAccount]
    ) -> int:
        """Create or refresh accounts, and snapshot their balances.

        The balance snapshot is what makes net-worth-over-time possible. It
        costs one small row per account per sync, and it is the only chance we
        will ever get to record today's balance -- no bank API will tell us
        later what it was.
        """
        updated = 0
        now = datetime.now(timezone.utc)

        for plaid_account in plaid_accounts:
            account = self.db.execute(
                select(Account).where(
                    Account.plaid_account_id == plaid_account.account_id
                )
            ).scalar_one_or_none()

            if account is None:
                account = Account(
                    user_id=item.user_id,
                    plaid_item_id=item.id,
                    plaid_account_id=plaid_account.account_id,
                    name=plaid_account.name,
                    type=ACCOUNT_TYPE_MAP.get(plaid_account.type, AccountType.OTHER),
                )
                self.db.add(account)

            # Bank-provided fields are refreshed. `user_display_name` is NOT
            # touched -- if the user renamed the account, that name stands.
            account.official_name = plaid_account.official_name
            account.mask = plaid_account.mask
            account.subtype = plaid_account.subtype
            account.currency_code = plaid_account.currency_code
            account.current_balance = plaid_account.current_balance
            account.available_balance = plaid_account.available_balance
            account.credit_limit = plaid_account.credit_limit
            account.balance_updated_at = now
            self.db.flush()

            self._snapshot_balance(account, plaid_account, now)
            updated += 1

        return updated

    def _snapshot_balance(
        self, account: Account, plaid_account: PlaidAccount, as_of: datetime
    ) -> None:
        """Append to balance history, idempotently.

        The unique constraint on (account_id, as_of) means a retried sync at
        the same instant cannot double-write. `ON CONFLICT DO NOTHING` turns
        that rejection into a no-op instead of an exception.
        """
        statement = (
            insert(AccountBalance)
            .values(
                account_id=account.id,
                as_of=as_of,
                current_balance=plaid_account.current_balance,
                available_balance=plaid_account.available_balance,
                credit_limit=plaid_account.credit_limit,
                currency_code=plaid_account.currency_code,
            )
            .on_conflict_do_nothing(constraint="uq_account_balances_account_id_as_of")
        )
        self.db.execute(statement)

    def _account_map(self, item: PlaidItem) -> dict[str, Account]:
        accounts = (
            self.db.execute(select(Account).where(Account.plaid_item_id == item.id))
            .scalars()
            .all()
        )
        return {a.plaid_account_id: a for a in accounts if a.plaid_account_id}

    # -----------------------------------------------------------------
    # Transactions
    # -----------------------------------------------------------------

    def _apply_transactions(
        self,
        item: PlaidItem,
        transactions: list[PlaidTransaction],
        accounts: dict[str, Account],
        counts: SyncCounts,
        *,
        is_added: bool,
    ) -> None:
        if not transactions:
            return

        existing = self._load_existing(item, [t.transaction_id for t in transactions])

        for plaid_txn in transactions:
            account = accounts.get(plaid_txn.account_id)
            if account is None:
                # A transaction for an account we do not have. Skipping is
                # correct: the alternative is inventing an account row with no
                # name, type, or balance, which would corrupt every total on
                # the dashboard. Plaid sends accounts alongside transactions,
                # so this should not happen -- log it if it does.
                logger.warning(
                    "Skipping transaction %s for unknown account %s",
                    plaid_txn.transaction_id,
                    plaid_txn.account_id,
                )
                continue

            previous = existing.get(plaid_txn.transaction_id)

            if previous is not None and is_added:
                # Plaid re-sent something we already have. This is the
                # deduplication guarantee doing its job, and a healthy number
                # here proves it is actually working rather than silently
                # doing nothing.
                counts.skipped += 1

            self._upsert_transaction(item, account, plaid_txn)

            if previous is None:
                if is_added:
                    counts.added += 1
                else:
                    # A "modified" transaction we have never seen. Rare, but it
                    # happens when a sync failed partway. Counting it as added
                    # keeps the numbers honest.
                    counts.added += 1
            else:
                counts.modified += 1
                self._audit_change(item, previous, plaid_txn)

    def _load_existing(
        self, item: PlaidItem, plaid_ids: list[str]
    ) -> dict[str, dict]:
        """Snapshot the current raw values, for change auditing.

        Returned as plain dicts rather than ORM objects: once the upsert runs,
        the ORM instances would reflect the NEW values, and we would have
        nothing to compare against.
        """
        rows = self.db.execute(
            select(Transaction)
            .where(
                Transaction.user_id == item.user_id,
                Transaction.plaid_transaction_id.in_(plaid_ids),
            )
        ).scalars().all()

        return {
            row.plaid_transaction_id: {
                "raw_amount": row.raw_amount,
                "raw_name": row.raw_name,
                "raw_date": row.raw_date,
                "status": row.status,
            }
            for row in rows
        }

    def _upsert_transaction(
        self, item: PlaidItem, account: Account, plaid_txn: PlaidTransaction
    ) -> None:
        """Insert, or update if we have seen this transaction before.

        The `set_` clause is the important part: it lists ONLY raw_* columns
        and lifecycle fields. No user_* column appears, so a sync physically
        cannot overwrite a human's correction -- not by oversight, not by a
        future refactor, not by a bank restating the record.
        """
        values = {
            "user_id": item.user_id,
            "account_id": account.id,
            "plaid_transaction_id": plaid_txn.transaction_id,
            "source": TransactionSource.PLAID,
            "status": (
                TransactionStatus.PENDING
                if plaid_txn.pending
                else TransactionStatus.POSTED
            ),
            "raw_name": plaid_txn.name,
            "raw_merchant_name": plaid_txn.merchant_name,
            "raw_amount": plaid_txn.amount,
            "raw_currency_code": plaid_txn.currency_code,
            "raw_date": plaid_txn.date,
            "raw_authorized_date": plaid_txn.authorized_date,
            "raw_category": plaid_txn.category,
            "raw_payload": plaid_txn.raw or None,
            "replaces_plaid_transaction_id": plaid_txn.pending_transaction_id,
        }

        statement = insert(Transaction).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_transactions_account_id_plaid_transaction_id",
            set_={field: statement.excluded[field] for field in SYNCABLE_FIELDS},
        )
        self.db.execute(statement)

    def _apply_removals(
        self, item: PlaidItem, removed_ids: list[str], counts: SyncCounts
    ) -> None:
        """Soft-delete transactions Plaid says no longer exist."""
        if not removed_ids:
            return

        rows = (
            self.db.execute(
                select(Transaction).where(
                    Transaction.user_id == item.user_id,
                    Transaction.plaid_transaction_id.in_(removed_ids),
                    Transaction.status != TransactionStatus.REMOVED,
                )
            )
            .scalars()
            .all()
        )

        now = datetime.now(timezone.utc)
        for row in rows:
            row.status = TransactionStatus.REMOVED
            row.removed_at = now
            counts.removed += 1

            self.db.add(
                AuditLog(
                    user_id=item.user_id,
                    entity_type="transaction",
                    entity_id=row.id,
                    action=AuditAction.DELETE,
                    actor=AuditActor.SYNC,
                    before_values={"status": TransactionStatus.POSTED.value},
                    after_values={"status": TransactionStatus.REMOVED.value},
                    reason="removed by Plaid sync",
                )
            )

    def _reconcile_pending(
        self, item: PlaidItem, transactions: list[PlaidTransaction]
    ) -> None:
        """Retire pending transactions that have now posted.

        THE DUPLICATE THAT ISN'T A DUPLICATE
        ------------------------------------
        When a pending charge posts, Plaid does not update it. It issues a
        BRAND NEW transaction with a different id, and sets
        `pending_transaction_id` on it pointing at the old one.

        So for a moment you legitimately hold two rows for one coffee. Neither
        unique constraint catches it -- the ids genuinely differ. If we did
        nothing, every card purchase would appear twice and every total would
        be inflated.

        Plaid usually also sends the pending id in `removed`, but not always
        and not always in the same page. Handling it here as well makes the
        outcome correct either way, and doing it twice is harmless.
        """
        replaced_ids = [
            t.pending_transaction_id for t in transactions if t.pending_transaction_id
        ]
        if not replaced_ids:
            return

        rows = (
            self.db.execute(
                select(Transaction).where(
                    Transaction.user_id == item.user_id,
                    Transaction.plaid_transaction_id.in_(replaced_ids),
                    Transaction.status != TransactionStatus.REMOVED,
                )
            )
            .scalars()
            .all()
        )

        now = datetime.now(timezone.utc)
        for row in rows:
            row.status = TransactionStatus.REMOVED
            row.removed_at = now

            self.db.add(
                AuditLog(
                    user_id=item.user_id,
                    entity_type="transaction",
                    entity_id=row.id,
                    action=AuditAction.DELETE,
                    actor=AuditActor.SYNC,
                    before_values={"status": TransactionStatus.PENDING.value},
                    after_values={"status": TransactionStatus.REMOVED.value},
                    reason="pending transaction replaced by its posted version",
                )
            )

    def _enrich_page(
        self,
        item: PlaidItem,
        plaid_transactions: list[PlaidTransaction],
        counts: SyncCounts,
    ) -> None:
        """Run the enrichment pipeline over the rows this page wrote."""
        if not plaid_transactions:
            return

        user = self.db.get(User, item.user_id)
        if user is None:
            return

        plaid_ids = [t.transaction_id for t in plaid_transactions]
        rows = (
            self.db.execute(
                select(Transaction).where(
                    Transaction.user_id == item.user_id,
                    Transaction.plaid_transaction_id.in_(plaid_ids),
                    # A transaction that has just been retired as a duplicate
                    # pending charge needs no merchant or category.
                    Transaction.status != TransactionStatus.REMOVED,
                )
            )
            .scalars()
            .all()
        )

        result = enrich_transactions(self.db, user, rows)
        counts.categorized += result.categories_assigned

    def _audit_change(
        self, item: PlaidItem, previous: dict, plaid_txn: PlaidTransaction
    ) -> None:
        """Record what the bank changed, and to what.

        Only actual differences are logged. Plaid re-sends unchanged records
        routinely; writing an audit row for every one would bury the handful
        that matter under millions that do not.
        """
        new_status = (
            TransactionStatus.PENDING if plaid_txn.pending else TransactionStatus.POSTED
        )
        candidates = {
            "raw_amount": (previous["raw_amount"], plaid_txn.amount),
            "raw_name": (previous["raw_name"], plaid_txn.name),
            "raw_date": (previous["raw_date"], plaid_txn.date),
            "status": (previous["status"], new_status),
        }

        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for field_name, (old, new) in candidates.items():
            if old != new:
                before[field_name] = _stringify(old)
                after[field_name] = _stringify(new)

        if not before:
            return

        transaction_id = self.db.execute(
            select(Transaction.id).where(
                Transaction.user_id == item.user_id,
                Transaction.plaid_transaction_id == plaid_txn.transaction_id,
            )
        ).scalar_one()

        self.db.add(
            AuditLog(
                user_id=item.user_id,
                entity_type="transaction",
                entity_id=transaction_id,
                action=AuditAction.UPDATE,
                actor=AuditActor.SYNC,
                before_values=before,
                after_values=after,
                reason="modified by Plaid sync",
            )
        )

    # -----------------------------------------------------------------
    # Finishing
    # -----------------------------------------------------------------

    def _record_success(
        self, item: PlaidItem, run: SyncHistory, counts: SyncCounts
    ) -> None:
        now = datetime.now(timezone.utc)

        run.status = SyncStatus.SUCCESS
        run.finished_at = now
        run.transactions_added = counts.added
        run.transactions_modified = counts.modified
        run.transactions_removed = counts.removed
        run.transactions_skipped = counts.skipped
        run.accounts_updated = counts.accounts_updated

        item.last_successful_sync_at = now
        item.status = PlaidItemStatus.HEALTHY
        item.error_code = None
        item.error_message = None

        self.db.commit()

    def _record_failure(
        self, item: PlaidItem, run: SyncHistory, exc: PlaidApiError
    ) -> None:
        """Record the failure without losing the pages that already succeeded.

        `rollback()` discards only the page that was mid-flight. Earlier pages
        were committed as they completed, so a sync that fails on page 9 keeps
        pages 1-8 and resumes from the right place next time.
        """
        self.db.rollback()

        run.status = SyncStatus.FAILED
        run.finished_at = datetime.now(timezone.utc)
        run.error_code = exc.error_code
        run.error_message = exc.message

        if exc.requires_user_reauth:
            # The user must re-enter their bank credentials. Surfaced in the
            # UI as "reconnect"; retrying automatically would never work.
            item.status = PlaidItemStatus.LOGIN_REQUIRED
        elif not exc.is_transient:
            item.status = PlaidItemStatus.ERROR

        item.error_code = exc.error_code
        item.error_message = exc.message

        self.db.add(run)
        self.db.commit()

        logger.warning(
            "Sync failed for item %s: %s (%s)",
            item.id,
            exc.error_code,
            "reauth required" if exc.requires_user_reauth else "no reauth",
        )


def _stringify(value: object) -> str:
    """JSONB cannot hold Decimal, date, or an Enum. Normalize to text.

    Money is quantized to the column's scale first. Without that, the two
    halves of an audit entry come from different places and end up in
    different formats -- the "before" value is read back from a
    Numeric(18, 4) column as "4.5000", while the "after" value arrives from
    Plaid as "5.25". Same field, same currency, two spellings.

    That is not merely untidy: it makes before/after diffs impossible to
    compare programmatically, so any future "what changed?" report or
    anomaly check would see spurious differences on every unchanged field.
    Normalizing at the single point where audit values are written fixes it
    once for every caller.
    """
    if isinstance(value, Decimal):
        return str(value.quantize(MONEY_SCALE))
    if isinstance(value, (date, datetime)):
        return str(value)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


# ---------------------------------------------------------------------------
# Linking a new institution
# ---------------------------------------------------------------------------


def link_institution(
    db: Session,
    gateway: PlaidGateway,
    *,
    user_id,
    public_token: str,
) -> PlaidItem:
    """Exchange a public token and create the PlaidItem plus its accounts.

    The access token is encrypted before it is ever handed to SQLAlchemy, so
    the plaintext exists only as a local variable for the length of this
    function -- never in the ORM's identity map, never in a query log, never
    in a database backup.
    """
    from app.core.crypto import encrypt

    access_token, plaid_item_id = gateway.exchange_public_token(
        public_token=public_token
    )
    info = gateway.get_item(access_token=access_token)

    institution = None
    if info.institution_id:
        institution = _get_or_create_institution(db, gateway, info.institution_id)

    item = PlaidItem(
        user_id=user_id,
        institution_id=institution.id if institution else None,
        plaid_item_id=plaid_item_id,
        access_token_encrypted=encrypt(access_token),
        status=PlaidItemStatus.HEALTHY,
    )
    db.add(item)
    db.flush()

    engine = SyncEngine(db, gateway)
    engine._upsert_accounts(item, gateway.get_accounts(access_token=access_token))

    db.commit()
    return item


def _get_or_create_institution(
    db: Session, gateway: PlaidGateway, institution_id: str
) -> Institution | None:
    """Institutions are shared across users -- reuse the existing row."""
    existing = db.execute(
        select(Institution).where(Institution.plaid_institution_id == institution_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    details = gateway.get_institution(institution_id=institution_id)
    if details is None:
        return None

    institution = Institution(
        plaid_institution_id=details.institution_id,
        name=details.name,
        logo_url=details.logo_url,
        primary_color=details.primary_color,
        website_url=details.website_url,
    )
    db.add(institution)
    db.flush()
    return institution
