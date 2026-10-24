"""Shared test setup: an isolated SQLite file per test, and a logged-in client."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import db as db_module


@pytest.fixture()
def db_file(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def client(db_file, monkeypatch):
    """A TestClient whose database is a throwaway file, never test.db."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "async_session_maker", async_sessionmaker(engine, expire_on_commit=False)
    )

    from app.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(client):
    """Register + log in, and hand back the Bearer header."""
    client.post("/auth/register", json={"email": "ana@example.com", "password": "secret123"})
    response = client.post(
        "/auth/login", json={"email": "ana@example.com", "password": "secret123"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def expenses(db_file):
    """Read the expenses table directly — the truth behind the API response."""

    def read() -> list[sqlite3.Row]:
        with sqlite3.connect(db_file) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM expenses").fetchall()

    return read
