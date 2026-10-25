"""Startup schema handling: an existing SQLite file must survive a new field.

`create_all()` never alters a table, so a column added to a model would leave
every older database unable to insert. This is the test for the fix.
"""

import asyncio
import sqlite3

from sqlalchemy.ext.asyncio import create_async_engine

from app.services import db as db_module

# The `expenses` table as it existed before category/icon were added.
OLD_EXPENSES = """
CREATE TABLE expenses (
    id INTEGER NOT NULL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    shop VARCHAR(255) NOT NULL,
    date DATE NOT NULL,
    currency VARCHAR(3) NOT NULL,
    total_minor INTEGER NOT NULL,
    items JSON NOT NULL,
    notes TEXT,
    prompt_version VARCHAR(16) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users (id)
)
"""


def test_startup_adds_new_columns_and_keeps_existing_rows(db_file, monkeypatch):
    with sqlite3.connect(db_file) as connection:
        connection.execute(OLD_EXPENSES)
        connection.execute("CREATE INDEX ix_expenses_user_id ON expenses (user_id)")
        connection.execute(
            "INSERT INTO expenses (user_id, shop, date, currency, total_minor, items, prompt_version)"
            " VALUES (1, 'Lulu', '2026-10-22', 'INR', 12500, '[]', 'v4')"
        )

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(db_module, "engine", engine)

    asyncio.run(db_module.create_db_and_table())

    with sqlite3.connect(db_file) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(expenses)")}
        rows = connection.execute(
            "SELECT shop, total_minor, category, icon FROM expenses"
        ).fetchall()

    assert {"category", "icon"} <= columns
    # The row survives, and an unclassified bill becomes "other" with a drawable icon.
    assert rows == [("Lulu", 12_500, "other", "strokeRoundedHelpCircle")]
