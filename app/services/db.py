from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker,AsyncSession
from collections.abc import AsyncGenerator
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = "sqlite+aiosqlite:///./test.db"


class Base(DeclarativeBase):
    pass

engine = create_async_engine(DATABASE_URL)

async_session_maker=async_sessionmaker(engine,expire_on_commit=False)

async def create_db_and_table():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_async_session()->AsyncGenerator[AsyncSession,None]:
    async with async_session_maker() as session:
        yield session