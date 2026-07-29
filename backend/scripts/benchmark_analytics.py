"""Measure SQL aggregation against the Python equivalent.

Milestone 6 claims that aggregating in the database is much faster than
loading rows and summing them in Python. This script is the evidence, and it
is committed so the claim can be re-checked rather than taken on trust.

    python scripts/benchmark_analytics.py --rows 100000

It creates a throwaway database, generates synthetic transactions, runs both
implementations, and prints the comparison. Nothing touches your real data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import time
import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from app.core.config import get_settings
from app.models import (
    Account,
    AccountType,
    Category,
    Merchant,
    Transaction,
    TransactionStatus,
    User,
)
from app.services import analytics


def build_database(rows: int, seed: int = 7):
    """Create a scratch database and fill it with `rows` transactions."""
    url = make_url(get_settings().DATABASE_URL)
    bench_name = f"{url.database}_bench"
    bench_url = url.set(database=bench_name).render_as_string(hide_password=False)
    admin_url = url.set(database="postgres")

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{bench_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{bench_name}"'))
    admin.dispose()

    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy_url"] = bench_url
    command.upgrade(cfg, "head")

    engine = create_engine(bench_url)
    random.seed(seed)

    with Session(engine) as db:
        user = User(email=f"bench-{uuid.uuid4()}@example.com")
        db.add(user)
        db.flush()

        account = Account(
            user_id=user.id,
            name="Bench Checking",
            type=AccountType.DEPOSITORY,
            plaid_account_id=f"bench_{uuid.uuid4().hex[:10]}",
        )
        db.add(account)

        categories = db.execute(
            select(Category).where(Category.is_system.is_(True)).limit(20)
        ).scalars().all()

        merchants = [
            Merchant(
                user_id=None,
                normalized_name=f"merchant {i}",
                display_name=f"Merchant {i}",
            )
            for i in range(200)
        ]
        db.add_all(merchants)
        db.flush()

        start = dt.date.today() - dt.timedelta(days=365 * 3)

        # Bulk insert via Core rather than the ORM: building 100,000 ORM
        # objects is itself slow enough to distort the measurement, and the
        # thing under test is the aggregation, not the insert.
        batch: list[dict] = []
        for i in range(rows):
            batch.append(
                {
                    "id": uuid.uuid4(),
                    "user_id": user.id,
                    "account_id": account.id,
                    "plaid_transaction_id": f"bench_{i}",
                    "source": "plaid",
                    "status": TransactionStatus.POSTED.value,
                    "raw_name": f"MERCHANT {i % 200} STORE #{i % 900}",
                    "raw_amount": Decimal(random.randrange(100, 40000)) / 100,
                    "raw_currency_code": "USD",
                    "raw_date": start + dt.timedelta(days=random.randrange(0, 365 * 3)),
                    "auto_category_id": random.choice(categories).id,
                    "auto_category_source": "plaid",
                    "auto_merchant_id": random.choice(merchants).id,
                    "is_hidden": False,
                    "is_reviewed": False,
                }
            )
            if len(batch) >= 5000:
                db.execute(Transaction.__table__.insert(), batch)
                batch = []

        if batch:
            db.execute(Transaction.__table__.insert(), batch)

        db.commit()

        # ANALYZE so the planner has statistics. Without it PostgreSQL may
        # choose a bad plan and the benchmark measures the wrong thing.
        db.execute(text("ANALYZE transactions"))
        db.commit()

        user_id = user.id

    return engine, user_id, bench_name, admin_url


def python_aggregation(db: Session, user: User, start: dt.date, end: dt.date):
    """The naive version: load every row, sum in Python."""
    rows = (
        db.execute(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.status != TransactionStatus.REMOVED,
                Transaction.is_hidden.is_(False),
                Transaction.raw_date >= start,
                Transaction.raw_date <= end,
            )
        )
        .scalars()
        .unique()
        .all()
    )

    totals: dict = defaultdict(Decimal)
    for row in rows:
        amount = row.user_amount if row.user_amount is not None else row.raw_amount
        if amount > 0:
            key = row.user_category_id or row.auto_category_id
            totals[key] += amount

    return len(rows), totals


def timed(label: str, fn, repeats: int = 3):
    """Best of N. Reporting the best run rather than the average filters out
    interference from other processes, which is what you want when comparing
    two implementations rather than predicting production latency."""
    best = float("inf")
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - started)
    print(f"  {label:<34} {best * 1000:9.1f} ms")
    return best, result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--keep", action="store_true", help="keep the scratch database")
    args = parser.parse_args()

    print(f"Building a scratch database with {args.rows:,} transactions…")
    engine, user_id, bench_name, admin_url = build_database(args.rows)

    start = dt.date.today() - dt.timedelta(days=365 * 3)
    end = dt.date.today()

    try:
        with Session(engine) as db:
            user = db.get(User, user_id)

            total = db.execute(
                select(func.count()).select_from(Transaction)
            ).scalar_one()
            print(f"Rows in table: {total:,}\n")

            print("Spending by category, 3 years:")
            sql_time, sql_result = timed(
                "SQL (GROUP BY in PostgreSQL)",
                lambda: analytics.spending_by_category(
                    db, user, start=start, end=end, limit=100
                ),
            )
            py_time, py_result = timed(
                "Python (load rows, sum in memory)",
                lambda: python_aggregation(db, user, start, end),
            )

            print(f"\n  SQL returned {len(sql_result)} category rows")
            print(f"  Python loaded {py_result[0]:,} transaction rows")
            print(f"\n  Speed-up: {py_time / sql_time:.0f}x")

            print("\nMonthly summary, 3 years:")
            timed(
                "SQL (date_trunc + FILTER)",
                lambda: analytics.monthly_summary(db, user, start=start, end=end),
            )

            print("\nTop 20 merchants:")
            timed(
                "SQL (GROUP BY + AVG)",
                lambda: analytics.spending_by_merchant(
                    db, user, start=start, end=end, limit=20
                ),
            )
    finally:
        engine.dispose()
        if not args.keep:
            admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{bench_name}" WITH (FORCE)'))
            admin.dispose()
            print(f"\nDropped scratch database {bench_name}.")


if __name__ == "__main__":
    main()
