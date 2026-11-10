"""The pasted Postgres URL is rewritten into something asyncpg accepts."""

from urllib.parse import parse_qsl, urlsplit

from app.config import _normalise_database_url

NEON_POOLED = (
    "postgresql://neondb_owner:secret@ep-still-night-bb5rzmim-pooler.c-15.us-east-1.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)
NEON_DIRECT = NEON_POOLED.replace("-pooler", "")


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


def test_async_driver_is_added():
    assert _normalise_database_url(NEON_POOLED).startswith("postgresql+asyncpg://")


def test_sslmode_is_translated_to_ssl():
    query = _query(_normalise_database_url(NEON_POOLED))
    assert query["ssl"] == "require"
    assert "sslmode" not in query


def test_libpq_only_option_is_dropped():
    assert "channel_binding" not in _query(_normalise_database_url(NEON_POOLED))


def test_pooled_host_disables_the_prepared_statement_cache():
    assert _query(_normalise_database_url(NEON_POOLED))["prepared_statement_cache_size"] == "0"


def test_no_option_reaches_the_driver_as_an_untyped_string():
    # asyncpg compares this to an int inside connect(); the string "0" would raise.
    assert "statement_cache_size" not in _query(_normalise_database_url(NEON_POOLED))


def test_direct_host_keeps_the_statement_cache():
    url = _normalise_database_url(NEON_DIRECT)
    assert "prepared_statement_cache_size" not in _query(url)


def test_credentials_host_and_path_survive():
    parts = urlsplit(_normalise_database_url(NEON_POOLED))
    assert parts.username == "neondb_owner"
    assert parts.password == "secret"
    assert parts.path == "/neondb"
    assert parts.hostname == "ep-still-night-bb5rzmim-pooler.c-15.us-east-1.aws.neon.tech"


def test_sqlite_is_left_alone():
    url = "sqlite+aiosqlite:///./test.db"
    assert _normalise_database_url(url) == url
