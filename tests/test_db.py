from launchfinder.db import ensure_schema, init_db, sqlalchemy_database_url


def test_rewrites_railway_postgres_scheme():
    assert sqlalchemy_database_url(
        "postgres://user:pass@host:5432/railway"
    ) == "postgresql+psycopg://user:pass@host:5432/railway"


def test_rewrites_libpq_postgresql_scheme():
    assert sqlalchemy_database_url(
        "postgresql://user:pass@host:5432/railway?sslmode=require"
    ) == "postgresql+psycopg://user:pass@host:5432/railway?sslmode=require"


def test_leaves_sqlite_and_psycopg_urls():
    sqlite = "sqlite:////tmp/launchfinder.db"
    already = "postgresql+psycopg://user:pass@host/db"
    assert sqlalchemy_database_url(sqlite) == sqlite
    assert sqlalchemy_database_url(already) == already


def test_ensure_schema_is_idempotent():
    init_db()
    ensure_schema()
    ensure_schema()
