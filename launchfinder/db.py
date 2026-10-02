from __future__ import annotations

import asyncio
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

# All ingest paths (poll loop, backfill, websocket, webhooks) share one event
# loop but use synchronous DB sessions. If two open transactions contend for a
# row lock, the blocked sync call freezes the loop and the lock holder can
# never resume to release it. Hold this lock for the duration of any write
# transaction so they never overlap.
ingest_lock = asyncio.Lock()


def sqlalchemy_database_url(url: str) -> str:
    """Normalize Railway/libpq URLs for SQLAlchemy + psycopg3."""
    raw = (url or "").strip()
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://") :]
    return raw


_db_url = sqlalchemy_database_url(settings.database_url)
_is_sqlite = _db_url.startswith("sqlite")

connect_args = {}
if _is_sqlite:
    connect_args["check_same_thread"] = False
else:
    # Backstop: error out instead of freezing the event loop if a row lock is
    # ever contended despite the ingest lock.
    connect_args["options"] = "-c lock_timeout=15000"

engine = create_engine(
    _db_url,
    future=True,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=not _is_sqlite,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _widen_varchar(table: str, column: str, length: int) -> None:
    """Postgres enforces VARCHAR(n); SQLite does not. EVM pool ids are 66 chars."""
    if _is_sqlite:
        return
    insp = inspect(engine)
    if table not in set(insp.get_table_names()):
        return
    cols = {c["name"]: c for c in insp.get_columns(table)}
    col = cols.get(column)
    if not col:
        return
    current = getattr(col["type"], "length", None)
    if current is not None and current >= length:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({length})"))


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    insp = inspect(engine)
    if table not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def ensure_schema() -> None:
    """create_all does not ALTER existing Railway tables. Add chain columns."""
    _add_column_if_missing("tokens", "chain", "chain VARCHAR(16) DEFAULT 'sol'")
    _add_column_if_missing("model_state", "chain", "chain VARCHAR(16) DEFAULT 'sol'")
    _add_column_if_missing("outcomes", "last_mcap", "last_mcap FLOAT DEFAULT 0")
    _add_column_if_missing("early_wallet_hits", "sol_spent", "sol_spent FLOAT DEFAULT 0")
    _add_column_if_missing("early_wallet_hits", "token_amount", "token_amount FLOAT DEFAULT 0")
    _add_column_if_missing("early_wallets", "n_profitable", "n_profitable INTEGER DEFAULT 0")
    _add_column_if_missing("early_wallets", "n_sized", "n_sized INTEGER DEFAULT 0")
    _add_column_if_missing("early_wallets", "sum_sol_spent", "sum_sol_spent FLOAT DEFAULT 0")
    _add_column_if_missing("early_wallets", "fastest_buy_s", "fastest_buy_s FLOAT DEFAULT 0")
    _add_column_if_missing("early_wallets", "fomo_hits", "fomo_hits INTEGER DEFAULT 0")
    _add_column_if_missing("fomo_wallets", "n_wins", "n_wins INTEGER DEFAULT 0")
    _add_column_if_missing("fomo_wallets", "n_rugs", "n_rugs INTEGER DEFAULT 0")
    _add_column_if_missing("fomo_wallet_hits", "is_win", "is_win BOOLEAN DEFAULT TRUE")
    _add_column_if_missing("research", "preview_p", "preview_p FLOAT DEFAULT 0")
    # stack-v76: which model wrote the frozen Entry, so desk lines are per scorer.
    _add_column_if_missing("research", "scorer", "scorer VARCHAR(16) DEFAULT 'legacy'")
    _add_column_if_missing("decisions", "scorer", "scorer VARCHAR(16) DEFAULT 'legacy'")
    _add_column_if_missing("hunt_cards", "scorer", "scorer VARCHAR(16) DEFAULT 'legacy'")
    _add_column_if_missing("paper_fills", "open_via", "open_via VARCHAR(16)")
    _add_column_if_missing(
        "fomo_alert_events",
        "trader_wallet",
        "trader_wallet VARCHAR(64) DEFAULT ''",
    )
    _widen_varchar("tokens", "pool_address", 128)
    insp = inspect(engine)
    if "tokens" in set(insp.get_table_names()):
        indexes = {idx["name"] for idx in insp.get_indexes("tokens")}
        if "ix_tokens_chain" not in indexes:
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tokens_chain ON tokens (chain)"))


def init_db() -> None:
    from . import models  # noqa: F401

    models.Base.metadata.create_all(bind=engine)
    ensure_schema()


def apply_report_guards(session: Session, *, timeout_ms: int = 45_000) -> None:
    """Bound Learn/report SQL so a stampede cannot blow Postgres shm.

    Statement timeout + no parallel gather. Does not shrink training fits.
    SQLite / missing dialect is a no-op.
    """
    try:
        bind = session.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return
        ms = max(1_000, int(timeout_ms))
        session.execute(text(f"SET LOCAL statement_timeout = {ms}"))
        session.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
        session.execute(text("SET LOCAL work_mem = '16MB'"))
    except Exception:
        return


@contextmanager
def session_scope() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
