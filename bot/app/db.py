"""Engine and session.

SQLite for now. `DATABASE_URL` is a setting, so moving to Postgres later is a
config change — nothing in this package assumes SQLite beyond the pragmas here.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

def _normalise(url: str) -> str:
    """Accept the URL shapes hosts hand out.

    Neon and Render both give `postgres://…`, which SQLAlchemy 2 does not
    recognise; it wants an explicit driver. Rewriting here means the value can
    be pasted from the provider unchanged.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


DATABASE_URL = _normalise(settings.database_url)
_is_sqlite = DATABASE_URL.startswith("sqlite")


def _connect_args() -> dict:
    if _is_sqlite:
        # The bot loop and the API run in the same process on different threads.
        return {"check_same_thread": False}

    # psycopg starts server-side preparing a statement after it has been seen a
    # few times. Neon's pooled endpoint (`-pooler` in the host) is PgBouncer in
    # transaction mode, where the next execution can land on a different backend
    # that has never heard of that prepared statement — surfacing later, under
    # load, as "prepared statement ... does not exist". Turning preparation off
    # costs a little planning time and removes the whole failure mode.
    return {"prepare_threshold": None}


engine: Engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args=_connect_args(),
    # Neon suspends an idle database, and a free web service sleeps too, so a
    # pooled connection is routinely dead by the time it is next used.
    # pool_pre_ping costs one cheap round trip and turns a crash into a
    # transparent reconnect.
    pool_pre_ping=not _is_sqlite,
    # Well under Neon's own idle timeout, so we recycle before it disconnects us.
    pool_recycle=280 if not _is_sqlite else -1,
    pool_size=5 if not _is_sqlite else 5,
    max_overflow=5 if not _is_sqlite else 10,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # concurrent bot + API reads
        cur.execute("PRAGMA foreign_keys=ON")  # honour the CASCADE on query_keywords
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


#: Columns added after this database first existed. `create_all` only creates
#: whole tables, so a column added to an existing one has to be applied here.
#: Alembic is the real answer once the schema stops moving this fast.
_ADDED_COLUMNS = {
    "queries": {"transcript": "TEXT"},
    "channel_settings": {"product_terms": "TEXT DEFAULT ''"},
}


def _add_missing_columns() -> None:
    if not _is_sqlite:
        return
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            present = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not present:
                continue  # table does not exist yet; create_all will make it
            for name, ddl in columns.items():
                if name not in present:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


#: Columns holding ids that come from Telegram. A supergroup chat id such as
#: -1004369410993 is already far outside int32, and user ids are heading the
#: same way. SQLite ignores declared types so this only ever fails on Postgres,
#: at runtime, with "integer out of range".
_BIGINT_COLUMNS = {
    "channel_settings": ("group_chat_id", "admin_chat_id"),
    "queries": ("tg_chat_id", "tg_message_id", "tg_user_id"),
    "conversations": ("chat_id", "user_id"),
}


def _widen_id_columns() -> None:
    """Bring an existing Postgres schema up to BIGINT for Telegram ids."""
    if _is_sqlite:
        return
    with engine.begin() as conn:
        for table, columns in _BIGINT_COLUMNS.items():
            narrow = {
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{table}' AND data_type = 'integer'"
                )
            }
            for column in columns:
                if column in narrow:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT"
                    )


def init_db() -> None:
    """Create any missing tables and columns. Safe to call on every start."""
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _widen_id_columns()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on anything raised."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
