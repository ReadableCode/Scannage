"""The parts of the PostgREST store and the settings that need no server.

Nothing here opens a connection: building the store and minting its token
are both local.
"""

import importlib
import time

import jwt
import pytest

from app import config
from app.stores import postgrest
from app.stores.postgrest import PostgrestStore

SECRET = "local-test-secret-that-is-long-enough-for-hs256"


@pytest.fixture()
def store():
    return PostgrestStore(url="http://postgrest.invalid:3000/", secret=SECRET, schema="scannage")


def test_token_carries_the_schema_role_and_expires(store):
    claims = jwt.decode(store._bearer(), SECRET, algorithms=["HS256"])

    assert set(claims) == {"role", "exp"}
    assert claims["role"] == "scannage_user"
    assert 0 < claims["exp"] - time.time() <= config.JWT_TTL_SECONDS


def test_token_is_cached_then_reminted_before_expiry(store):
    first = store._bearer()
    assert store._bearer() == first

    # inside the refresh margin counts as expired
    store._token_expires = time.time() + postgrest.JWT_REFRESH_MARGIN_SECONDS - 1
    store._token = "stale"
    fresh = store._bearer()

    assert fresh != "stale"
    assert jwt.decode(fresh, SECRET, algorithms=["HS256"])["role"] == "scannage_user"


def test_headers_pin_the_schema(store):
    read = store._headers()
    assert read["Accept-Profile"] == "scannage"
    assert read["Authorization"] == f"Bearer {store._bearer()}"
    assert "Content-Profile" not in read

    write = store._headers(write=True, prefer="return=representation")
    assert write["Accept-Profile"] == "scannage"
    assert write["Content-Profile"] == "scannage"
    assert write["Prefer"] == "return=representation"


def test_url_is_normalised(store):
    assert store.url == "http://postgrest.invalid:3000"


@pytest.mark.parametrize(
    ("url", "secret", "named"),
    [
        ("", SECRET, "POSTGREST_URL"),
        ("http://postgrest.invalid:3000", "", "POSTGREST_JWT_SECRET"),
        ("", "", "POSTGREST_URL and POSTGREST_JWT_SECRET"),
    ],
)
def test_misconfigured_store_fails_loudly(url, secret, named):
    with pytest.raises(RuntimeError, match=named):
        PostgrestStore(url=url, secret=secret, schema="scannage")


@pytest.fixture()
def reload_config(monkeypatch):
    """Re-read the settings under a patched environment, then put them back."""

    def _reload(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)

    yield _reload
    monkeypatch.undo()
    importlib.reload(config)


def test_unknown_store_raises(reload_config):
    with pytest.raises(ValueError, match="SCANNAGE_STORE"):
        reload_config(SCANNAGE_STORE="mysql")


def test_store_setting_is_case_insensitive(reload_config):
    assert reload_config(SCANNAGE_STORE="PostgREST").STORE == "postgrest"


def test_settings_defaults_and_alternate_secret_name(reload_config):
    settings = reload_config(
        SCANNAGE_STORE="",
        SCANNAGE_SQLITE_PATH="",
        SCANNAGE_SEED_SAMPLES="true",
        SCANNAGE_BASE_URL="https://boxes.example.com/",
        APP_SCHEMA="",
        POSTGRES_DB="",
        POSTGRES_PORT="",
        POSTGREST_JWT_SECRET="",
        JWT_SECRET=SECRET,
    )

    assert settings.STORE == "sqlite"
    assert settings.SQLITE_PATH == settings.REPO_ROOT / "data" / "scannage.db"
    assert settings.SEED_SAMPLES is True
    assert settings.BASE_URL == "https://boxes.example.com"
    assert settings.APP_SCHEMA == "scannage"
    assert settings.POSTGRES_DB == "apps"
    assert settings.POSTGRES_PORT == "5432"
    assert settings.JWT_SECRET == SECRET
    assert (settings.DICTIONARY, settings.TAG_COUNT, settings.PORT) == ("ARUCO_MIP_36h12", 250, 8791)
