"""Sync engine tests.

The scenarios below are the ones that break sync engines in production. Each
is impossible to reproduce on demand against a real Plaid sandbox, which is
precisely why the gateway is a substitutable boundary.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.crypto import encrypt
from app.models import (
    Account,
    AccountBalance,
    AuditLog,
    Institution,
    PlaidItem,
    PlaidItemStatus,
    SyncHistory,
    SyncStatus,
    SyncTrigger,
    Transaction,
    TransactionStatus,
    User,
)
from app.services.plaid_gateway import PlaidApiError
from app.services.sync import SyncEngine, link_institution
from tests.fake_plaid import FakePlaidGateway, make_account, make_txn, page


def make_item(db: Session, user: User, *, cursor: str | None = None) -> PlaidItem:
    institution = Institution(
        plaid_institution_id=f"ins_{uuid.uuid4().hex[:8]}", name="Fake Bank"
    )
    db.add(institution)
    db.flush()

    item = PlaidItem(
        user_id=user.id,
        institution_id=institution.id,
        plaid_item_id=f"item_{uuid.uuid4().hex[:12]}",
        access_token_encrypted=encrypt("access-sandbox-fake"),
        transactions_cursor=cursor,
    )
    db.add(item)
    db.flush()
    return item


def transactions_for(db: Session, user: User) -> list[Transaction]:
    return (
        db.execute(
            select(Transaction)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.raw_date)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# The basics
# ---------------------------------------------------------------------------


def test_first_sync_imports_transactions_and_accounts(db: Session, user: User):
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[page(added=[make_txn("txn_1"), make_txn("txn_2", amount="99.00")])]
    )

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    assert run.status == SyncStatus.SUCCESS
    assert run.transactions_added == 2
    assert len(transactions_for(db, user)) == 2

    account = db.execute(
        select(Account).where(Account.plaid_account_id == "plaid_acct_1")
    ).scalar_one()
    assert account.current_balance == Decimal("1200.50")


def test_first_sync_sends_no_cursor(db: Session, user: User):
    """Plaid rejects an empty-string cursor.

    "Start from the beginning" is expressed by OMITTING the cursor, which is
    an easy detail to get wrong on the one call where it matters most -- the
    very first sync of a new connection.
    """
    item = make_item(db, user, cursor=None)
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_1")])])

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    assert gateway.cursors_seen[0] is None


def test_amounts_are_stored_as_exact_decimals(db: Session, user: User):
    """Plaid sends floats. They must not survive contact with our database."""
    item = make_item(db, user)
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_1", amount="0.10")])])

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.MANUAL)

    [txn] = transactions_for(db, user)
    assert txn.raw_amount == Decimal("0.10")
    assert isinstance(txn.raw_amount, Decimal)


def test_raw_payload_is_retained(db: Session, user: User):
    """The full original record, for fields we have not modeled yet."""
    item = make_item(db, user)
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_1")])])

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.MANUAL)

    [txn] = transactions_for(db, user)
    assert txn.raw_payload["transaction_id"] == "txn_1"


# ---------------------------------------------------------------------------
# Idempotency -- the guarantee everything else depends on
# ---------------------------------------------------------------------------


def test_resyncing_the_same_transactions_creates_no_duplicates(
    db: Session, user: User
):
    """Run the identical sync twice. The second must change nothing.

    This is what "idempotent" means, and it is what makes retries, overlapping
    webhooks, and crash recovery safe.
    """
    item = make_item(db, user)
    transactions = [make_txn("txn_1"), make_txn("txn_2", amount="20.00")]

    first = FakePlaidGateway(pages=[page(added=transactions)])
    SyncEngine(db, first).sync_item(item, SyncTrigger.INITIAL)

    second = FakePlaidGateway(pages=[page(added=transactions)])
    run = SyncEngine(db, second).sync_item(item, SyncTrigger.MANUAL)

    assert len(transactions_for(db, user)) == 2
    # Counted as skipped, which is the visible proof that deduplication ran
    # rather than the second sync simply having found nothing.
    assert run.transactions_skipped == 2


def test_a_sync_never_overwrites_a_user_correction(db: Session, user: User):
    """THE most important test in this file.

    A user recategorizes a transaction and renames it. The bank then restates
    the record. The bank's new values must land in the raw_* columns, and the
    user's choices must survive completely untouched.

    If this ever fails, every manual correction a user has ever made is one
    sync away from being silently erased.
    """
    item = make_item(db, user)
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_1", amount="12.34")])])
    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    [txn] = transactions_for(db, user)
    txn.user_description = "Coffee with Sam"
    txn.user_amount = Decimal("10.00")
    txn.user_notes = "reimbursable"
    txn.is_reviewed = True
    db.commit()

    # The bank restates the amount and the description.
    restated = FakePlaidGateway(
        pages=[
            page(
                modified=[
                    make_txn("txn_1", amount="13.99", name="STARBUCKS #12345 SEATTLE")
                ]
            )
        ]
    )
    SyncEngine(db, restated).sync_item(item, SyncTrigger.WEBHOOK)

    db.expire_all()
    [txn] = transactions_for(db, user)

    # Bank data updated...
    assert txn.raw_amount == Decimal("13.99")
    assert txn.raw_name == "STARBUCKS #12345 SEATTLE"
    # ...user data untouched.
    assert txn.user_description == "Coffee with Sam"
    assert txn.user_amount == Decimal("10.00")
    assert txn.user_notes == "reimbursable"
    assert txn.is_reviewed is True
    # And what the app displays is still the user's version.
    assert txn.effective_amount == Decimal("10.00")


# ---------------------------------------------------------------------------
# Pending -> posted: the duplicate that isn't a duplicate
# ---------------------------------------------------------------------------


def test_pending_transaction_is_retired_when_it_posts(db: Session, user: User):
    """The classic double-charge bug.

    When a pending charge posts, Plaid issues a NEW transaction id and points
    `pending_transaction_id` at the old one. Neither unique constraint catches
    it, because the ids genuinely differ. Without explicit reconciliation
    every card purchase would appear on the dashboard twice.
    """
    item = make_item(db, user)

    pending = FakePlaidGateway(
        pages=[page(added=[make_txn("txn_pending", amount="4.50", pending=True)])]
    )
    SyncEngine(db, pending).sync_item(item, SyncTrigger.INITIAL)

    [txn] = transactions_for(db, user)
    assert txn.status == TransactionStatus.PENDING

    # It posts -- with a tip added, so the amount differs too.
    posted = FakePlaidGateway(
        pages=[
            page(
                added=[
                    make_txn(
                        "txn_posted",
                        amount="5.25",
                        pending=False,
                        pending_transaction_id="txn_pending",
                    )
                ]
            )
        ]
    )
    SyncEngine(db, posted).sync_item(item, SyncTrigger.WEBHOOK)

    db.expire_all()
    rows = {t.plaid_transaction_id: t for t in transactions_for(db, user)}

    assert rows["txn_posted"].status == TransactionStatus.POSTED
    assert rows["txn_posted"].raw_amount == Decimal("5.25")
    # The pending row is retired, not deleted -- history is preserved.
    assert rows["txn_pending"].status == TransactionStatus.REMOVED
    assert rows["txn_pending"].removed_at is not None

    # Only one transaction counts toward spending.
    active_total = db.execute(
        select(func.sum(Transaction.effective_amount)).where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
        )
    ).scalar_one()
    assert active_total == Decimal("5.25")


def test_pending_and_posted_arriving_in_the_same_page_is_handled(
    db: Session, user: User
):
    """Plaid can deliver both halves at once, in either order."""
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[
            page(
                added=[
                    make_txn("txn_pending", amount="4.50", pending=True),
                    make_txn(
                        "txn_posted",
                        amount="4.50",
                        pending_transaction_id="txn_pending",
                    ),
                ]
            )
        ]
    )

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    db.expire_all()
    rows = {t.plaid_transaction_id: t for t in transactions_for(db, user)}
    assert rows["txn_pending"].status == TransactionStatus.REMOVED
    assert rows["txn_posted"].status == TransactionStatus.POSTED


# ---------------------------------------------------------------------------
# Removals
# ---------------------------------------------------------------------------


def test_removed_transactions_are_soft_deleted(db: Session, user: User):
    item = make_item(db, user)
    SyncEngine(db, FakePlaidGateway(pages=[page(added=[make_txn("txn_1")])])).sync_item(
        item, SyncTrigger.INITIAL
    )

    remover = FakePlaidGateway(pages=[page(removed_ids=["txn_1"])])
    run = SyncEngine(db, remover).sync_item(item, SyncTrigger.WEBHOOK)

    db.expire_all()
    [txn] = transactions_for(db, user)
    assert run.transactions_removed == 1
    assert txn.status == TransactionStatus.REMOVED
    # The row and its original data survive, so recovery is one UPDATE away.
    assert txn.raw_amount == Decimal("12.34")


def test_removal_is_recorded_in_the_audit_log(db: Session, user: User):
    item = make_item(db, user)
    SyncEngine(db, FakePlaidGateway(pages=[page(added=[make_txn("txn_1")])])).sync_item(
        item, SyncTrigger.INITIAL
    )

    SyncEngine(db, FakePlaidGateway(pages=[page(removed_ids=["txn_1"])])).sync_item(
        item, SyncTrigger.WEBHOOK
    )

    entries = (
        db.execute(select(AuditLog).where(AuditLog.user_id == user.id)).scalars().all()
    )
    assert any(e.reason == "removed by Plaid sync" for e in entries)


def test_removing_an_unknown_transaction_is_harmless(db: Session, user: User):
    """Plaid can send a removal for something we never saw -- if an earlier
    sync failed, say. It must not crash the run."""
    item = make_item(db, user)

    run = SyncEngine(
        db, FakePlaidGateway(pages=[page(removed_ids=["never_seen"])])
    ).sync_item(item, SyncTrigger.WEBHOOK)

    assert run.status == SyncStatus.SUCCESS
    assert run.transactions_removed == 0


# ---------------------------------------------------------------------------
# Modifications and the audit trail
# ---------------------------------------------------------------------------


def test_bank_modification_is_audited_with_before_and_after(db: Session, user: User):
    item = make_item(db, user)
    SyncEngine(
        db, FakePlaidGateway(pages=[page(added=[make_txn("txn_1", amount="4.50")])])
    ).sync_item(item, SyncTrigger.INITIAL)

    SyncEngine(
        db,
        FakePlaidGateway(pages=[page(modified=[make_txn("txn_1", amount="5.25")])]),
    ).sync_item(item, SyncTrigger.WEBHOOK)

    entry = db.execute(
        select(AuditLog).where(
            AuditLog.user_id == user.id, AuditLog.reason == "modified by Plaid sync"
        )
    ).scalar_one()

    assert entry.before_values["raw_amount"] == "4.5000"
    assert entry.after_values["raw_amount"] == "5.2500"


def test_unchanged_resend_writes_no_audit_row(db: Session, user: User):
    """Plaid re-sends unchanged records routinely.

    Logging every one would bury the handful that matter under millions that
    do not -- and would grow the fastest-growing table in the schema for no
    reason.
    """
    item = make_item(db, user)
    txn = make_txn("txn_1", amount="4.50")

    SyncEngine(db, FakePlaidGateway(pages=[page(added=[txn])])).sync_item(
        item, SyncTrigger.INITIAL
    )
    SyncEngine(db, FakePlaidGateway(pages=[page(modified=[txn])])).sync_item(
        item, SyncTrigger.WEBHOOK
    )

    count = db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.user_id == user.id, AuditLog.reason == "modified by Plaid sync")
    ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Paging and the cursor
# ---------------------------------------------------------------------------


def test_all_pages_are_fetched_and_the_cursor_advances(db: Session, user: User):
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[
            page(added=[make_txn("txn_1")], next_cursor="cursor-a", has_more=True),
            page(added=[make_txn("txn_2")], next_cursor="cursor-b", has_more=True),
            page(added=[make_txn("txn_3")], next_cursor="cursor-c", has_more=False),
        ]
    )

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    assert run.transactions_added == 3
    assert len(transactions_for(db, user)) == 3
    # Each request carried the cursor returned by the previous page.
    assert gateway.cursors_seen == [None, "cursor-a", "cursor-b"]
    assert item.transactions_cursor == "cursor-c"
    assert run.cursor_after == "cursor-c"


def test_the_next_sync_resumes_from_the_stored_cursor(db: Session, user: User):
    """The point of incremental sync: never re-download history."""
    item = make_item(db, user, cursor="cursor-from-last-time")
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_new")])])

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.SCHEDULED)

    assert gateway.cursors_seen == ["cursor-from-last-time"]


def test_a_failure_mid_sync_keeps_the_pages_that_succeeded(db: Session, user: User):
    """Partial progress must survive.

    Pages 1 and 2 committed; page 3 fails. Those two pages and their cursor
    are kept, so the retry resumes from the right place instead of re-fetching
    everything or -- far worse -- skipping it.
    """
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[
            page(added=[make_txn("txn_1")], next_cursor="cursor-a", has_more=True),
            page(added=[make_txn("txn_2")], next_cursor="cursor-b", has_more=True),
        ]
    )
    gateway.raise_on_sync_after_pages = 2

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.SCHEDULED)

    assert run.status == SyncStatus.FAILED
    assert run.error_code == "ITEM_LOGIN_REQUIRED"

    db.expire_all()
    # The two committed pages survived the failure.
    assert len(transactions_for(db, user)) == 2
    assert item.transactions_cursor == "cursor-b"


def test_the_cursor_does_not_advance_when_the_first_page_fails(
    db: Session, user: User
):
    """If nothing was imported, the cursor must not move.

    A cursor that advances past data we never stored means those transactions
    are lost permanently -- Plaid will never send them again, and nothing
    reports an error.
    """
    item = make_item(db, user, cursor="cursor-start")
    gateway = FakePlaidGateway()
    gateway.raise_on_sync = PlaidApiError("INTERNAL_SERVER_ERROR", "Plaid is down")

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.SCHEDULED)

    assert run.status == SyncStatus.FAILED
    db.expire_all()
    assert item.transactions_cursor == "cursor-start"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_login_required_marks_the_item_for_reconnection(db: Session, user: User):
    item = make_item(db, user)
    gateway = FakePlaidGateway()
    gateway.raise_on_sync = PlaidApiError("ITEM_LOGIN_REQUIRED", "reconnect needed")

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.SCHEDULED)

    db.expire_all()
    assert item.status == PlaidItemStatus.LOGIN_REQUIRED
    assert item.error_code == "ITEM_LOGIN_REQUIRED"


def test_a_transient_failure_leaves_the_item_healthy(db: Session, user: User):
    """Plaid being briefly down is not the user's problem.

    Marking the connection broken would tell them to go re-enter their bank
    credentials for no reason -- and they might well do it.
    """
    item = make_item(db, user)
    gateway = FakePlaidGateway()
    gateway.raise_on_sync = PlaidApiError("RATE_LIMIT_EXCEEDED", "slow down")

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.SCHEDULED)

    db.expire_all()
    assert run.status == SyncStatus.FAILED
    assert item.status == PlaidItemStatus.HEALTHY


def test_a_failed_run_is_recorded_in_sync_history(db: Session, user: User):
    """'My transactions are missing' must have an answer."""
    item = make_item(db, user)
    gateway = FakePlaidGateway()
    gateway.raise_on_sync = PlaidApiError("ITEM_LOGIN_REQUIRED", "reconnect needed")

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.SCHEDULED)

    runs = (
        db.execute(select(SyncHistory).where(SyncHistory.plaid_item_id == item.id))
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status == SyncStatus.FAILED
    assert runs[0].finished_at is not None
    assert runs[0].duration_seconds is not None


def test_a_successful_sync_clears_a_previous_error(db: Session, user: User):
    item = make_item(db, user)
    item.status = PlaidItemStatus.LOGIN_REQUIRED
    item.error_code = "ITEM_LOGIN_REQUIRED"
    db.flush()

    SyncEngine(db, FakePlaidGateway(pages=[page(added=[make_txn("txn_1")])])).sync_item(
        item, SyncTrigger.MANUAL
    )

    db.expire_all()
    assert item.status == PlaidItemStatus.HEALTHY
    assert item.error_code is None
    assert item.last_successful_sync_at is not None


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def test_every_sync_snapshots_balances(db: Session, user: User):
    """What makes net worth over time possible."""
    item = make_item(db, user)

    SyncEngine(
        db, FakePlaidGateway(pages=[page(accounts=[make_account(current_balance="100")])])
    ).sync_item(item, SyncTrigger.INITIAL)
    SyncEngine(
        db, FakePlaidGateway(pages=[page(accounts=[make_account(current_balance="250")])])
    ).sync_item(item, SyncTrigger.SCHEDULED)

    snapshots = (
        db.execute(select(AccountBalance).order_by(AccountBalance.as_of))
        .scalars()
        .all()
    )
    assert [s.current_balance for s in snapshots] == [
        Decimal("100.0000"),
        Decimal("250.0000"),
    ]


def test_credit_card_limits_are_captured(db: Session, user: User):
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[
            page(
                accounts=[
                    make_account(
                        "plaid_card_1",
                        name="Sapphire",
                        type="credit",
                        subtype="credit card",
                        current_balance="1500",
                        credit_limit="5000",
                    )
                ]
            )
        ]
    )

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    account = db.execute(
        select(Account).where(Account.plaid_account_id == "plaid_card_1")
    ).scalar_one()
    assert account.credit_limit == Decimal("5000.0000")
    assert account.utilization == Decimal("0.3")


def test_a_renamed_account_keeps_the_users_name(db: Session, user: User):
    """Same override principle as transactions, applied to accounts."""
    item = make_item(db, user)
    SyncEngine(db, FakePlaidGateway(pages=[page()])).sync_item(item, SyncTrigger.INITIAL)

    account = db.execute(
        select(Account).where(Account.plaid_account_id == "plaid_acct_1")
    ).scalar_one()
    account.user_display_name = "Joint Everyday"
    db.commit()

    SyncEngine(
        db, FakePlaidGateway(pages=[page(accounts=[make_account(name="Renamed By Bank")])])
    ).sync_item(item, SyncTrigger.SCHEDULED)

    db.expire_all()
    db.refresh(account)
    assert account.user_display_name == "Joint Everyday"
    assert account.display_name == "Joint Everyday"


def test_transactions_for_an_unknown_account_are_skipped_not_invented(
    db: Session, user: User
):
    """Inventing a nameless, typeless account would corrupt every total."""
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[
            page(
                added=[make_txn("txn_1", account_id="account_we_do_not_have")],
                accounts=[],
            )
        ]
    )

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    assert run.status == SyncStatus.SUCCESS
    assert transactions_for(db, user) == []
    assert db.execute(select(func.count()).select_from(Account)).scalar_one() == 0


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------


def test_linking_stores_the_access_token_encrypted(db: Session, user: User):
    """The plaintext token must never reach the database."""
    gateway = FakePlaidGateway()

    item = link_institution(
        db, gateway, user_id=user.id, public_token="public-sandbox-abc"
    )

    stored = db.execute(
        select(PlaidItem.access_token_encrypted).where(PlaidItem.id == item.id)
    ).scalar_one()

    assert "access-sandbox-secret-value" not in stored
    # ...but it round-trips for our own use.
    from app.core.crypto import decrypt

    assert decrypt(stored) == "access-sandbox-secret-value"


def test_linking_creates_the_institution_once(db: Session, user: User):
    """Institutions are shared. Two users at the same bank get one row."""
    gateway = FakePlaidGateway()

    link_institution(db, gateway, user_id=user.id, public_token="tok-1")

    other = User(email=f"other-{uuid.uuid4()}@example.com")
    db.add(other)
    db.flush()
    link_institution(db, gateway, user_id=other.id, public_token="tok-2")

    count = db.execute(
        select(func.count())
        .select_from(Institution)
        .where(Institution.plaid_institution_id == "ins_fake")
    ).scalar_one()
    assert count == 1


def test_linking_imports_accounts(db: Session, user: User):
    gateway = FakePlaidGateway(
        accounts=[make_account("a1", name="Checking"), make_account("a2", name="Savings")]
    )

    item = link_institution(db, gateway, user_id=user.id, public_token="tok")

    accounts = (
        db.execute(select(Account).where(Account.plaid_item_id == item.id))
        .scalars()
        .all()
    )
    assert {a.name for a in accounts} == {"Checking", "Savings"}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_transaction_dates_survive_the_round_trip(db: Session, user: User):
    """A leap day, because date handling is where off-by-one bugs live."""
    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[page(added=[make_txn("txn_1", date=dt.date(2028, 2, 29))])]
    )

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    [txn] = transactions_for(db, user)
    assert txn.raw_date == dt.date(2028, 2, 29)


# ---------------------------------------------------------------------------
# Enrichment integration (Milestone 5)
# ---------------------------------------------------------------------------


def test_synced_transactions_are_enriched(db: Session, user: User):
    """Sync and enrichment must be one atomic unit.

    If enrichment ran afterwards, a crash in between would leave rows imported
    but uncategorized, with nothing recording that they still needed work.
    """
    from sqlalchemy import select as _select

    from app.models import Category

    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[
            page(
                added=[
                    make_txn(
                        "txn_1",
                        name="STARBUCKS STORE 123",
                        category="FOOD_AND_DRINK",
                    )
                ]
            )
        ]
    )

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    [txn] = transactions_for(db, user)
    assert txn.auto_merchant_id is not None
    expected = db.execute(
        _select(Category.id).where(Category.slug == "food_and_drink")
    ).scalar_one()
    assert txn.auto_category_id == expected


def test_a_users_rule_applies_during_sync(db: Session, user: User):
    """Rules run as data arrives, not on a later pass."""
    from sqlalchemy import select as _select

    from app.models import Category, Rule

    coffee_id = db.execute(
        _select(Category.id).where(Category.slug == "food_and_drink.coffee")
    ).scalar_one()

    db.add(
        Rule(
            user_id=user.id,
            name="Starbucks is coffee",
            conditions={
                "conditions": [
                    {"field": "raw_name", "op": "contains", "value": "starbucks"}
                ]
            },
            actions={"set_category_id": str(coffee_id)},
        )
    )
    db.flush()

    item = make_item(db, user)
    gateway = FakePlaidGateway(
        pages=[page(added=[make_txn("txn_1", name="STARBUCKS STORE 123")])]
    )

    SyncEngine(db, gateway).sync_item(item, SyncTrigger.INITIAL)

    [txn] = transactions_for(db, user)
    assert txn.auto_category_id == coffee_id


def test_a_resync_does_not_undo_corrections_made_since(db: Session, user: User):
    """The end-to-end guarantee, across both engines.

    Import, correct, then sync again. Enrichment runs on the re-synced row and
    must still leave the user's category alone.
    """
    from sqlalchemy import select as _select

    from app.models import Category

    item = make_item(db, user)
    txn_payload = make_txn("txn_1", name="STARBUCKS STORE 123")

    SyncEngine(db, FakePlaidGateway(pages=[page(added=[txn_payload])])).sync_item(
        item, SyncTrigger.INITIAL
    )

    travel_id = db.execute(
        _select(Category.id).where(Category.slug == "travel")
    ).scalar_one()

    [txn] = transactions_for(db, user)
    txn.user_category_id = travel_id
    db.commit()

    SyncEngine(db, FakePlaidGateway(pages=[page(modified=[txn_payload])])).sync_item(
        item, SyncTrigger.WEBHOOK
    )

    db.expire_all()
    [txn] = transactions_for(db, user)
    assert txn.user_category_id == travel_id
    assert txn.effective_category_id == travel_id
