from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker,AsyncSession
from collections.abc import AsyncGenerator
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase

from app.config import DATABASE_URL, EXPENSE_CATEGORIES


class Base(DeclarativeBase):
    pass

def _engine_options(url: str) -> dict:
    """Pool settings that survive serverless hosting on top of Neon.

    Neon suspends an idle database and drops its connections; Vercel freezes a
    function between requests. Either way a pooled connection can be dead the
    moment it is handed out, so pre_ping tests it (one cheap round trip) and
    transparently replaces it instead of raising "connection was closed".
    max_overflow=0 keeps one instance inside Neon's connection limit, and
    recycle retires anything older than 30 minutes.
    """
    if url.startswith("sqlite"):
        return {}
    return {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 0,
        "pool_recycle": 1800,
        "connect_args": {"timeout": 10},
    }


engine = create_async_engine(DATABASE_URL, **_engine_options(DATABASE_URL))

async_session_maker=async_sessionmaker(engine,expire_on_commit=False)

def _add_missing_nullable_columns(connection) -> None:
    """Bring an existing table up to the current model without destroying data.

    create_all() only creates tables; it never alters one. So a field added to
    a model would break every insert on an existing table. Only nullable columns are added here: nothing is dropped, renamed or made
    required. Rows that predate the classification get "other" — the one
    truthful category for a bill nobody has classified — plus that category's
    icon, so every saved row stays renderable.
    """
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing or not column.nullable:
                continue
            column_type = column.type.compile(dialect=connection.dialect)
            connection.execute(
                text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}')
            )

    connection.execute(
        text(
            'UPDATE expenses SET category = :category, icon = :icon '
            'WHERE category IS NULL'
        ),
        {"category": "other", "icon": EXPENSE_CATEGORIES["other"][0]},
    )

async def create_db_and_table():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_nullable_columns)

async def get_async_session()->AsyncGenerator[AsyncSession,None]:
    async with async_session_maker() as session:
        yield session