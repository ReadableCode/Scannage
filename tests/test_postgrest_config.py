"""The parts of the PostgREST store and the settings that need no server.

Nothing here opens a connection: building the store and minting its token
are both local.
"""

import importlib
import time
import uuid

import httpx
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
        SCANNAGE_HTTPS="",
        SCANNAGE_HTTPS_HOSTS="",
        SCANNAGE_TLS_DIR="",
        SCANNAGE_BASE_URL="https://boxes.example.com/",
        APP_SCHEMA="",
        POSTGRES_DB="",
        POSTGRES_PORT="",
        POSTGREST_JWT_SECRET="",
        JWT_SECRET=SECRET,
    )

    assert settings.STORE == "sqlite"
    assert settings.SQLITE_PATH == settings.REPO_ROOT / "data" / "scannage.db"
    assert settings.HTTPS is False
    assert settings.HTTPS_HOSTS == ()
    assert settings.TLS_DIR == settings.REPO_ROOT / "data" / "tls"
    assert settings.BASE_URL == "https://boxes.example.com"
    assert settings.APP_SCHEMA == "scannage"
    assert settings.POSTGRES_DB == "apps"
    assert settings.POSTGRES_PORT == "5432"
    assert settings.JWT_SECRET == SECRET
    assert (settings.DICTIONARY, settings.TAG_COUNT, settings.PORT) == ("ARUCO_MIP_36h12", 250, 8791)


@pytest.mark.parametrize("value", ["1", "true", "TRUE"])
def test_https_settings(reload_config, tmp_path, value):
    settings = reload_config(
        SCANNAGE_HTTPS=value,
        SCANNAGE_HTTPS_HOSTS=" boxes.example.com , 192.0.2.10,, ",
        SCANNAGE_TLS_DIR=str(tmp_path / "tls"),
    )

    assert settings.HTTPS is True
    assert settings.HTTPS_HOSTS == ("boxes.example.com", "192.0.2.10")
    assert settings.TLS_DIR == tmp_path / "tls"


def test_samples_are_not_a_setting(reload_config):
    # they belong to a new database, so nothing in the environment can ask for them
    assert not hasattr(reload_config(SCANNAGE_STORE="sqlite"), "SEED_SAMPLES")


# --- photos and history, as far as they go without a server -------------------


def test_bytea_travels_as_postgres_hex():
    raw = bytes(range(256))

    sent = postgrest.to_bytea(raw)

    assert sent.startswith("\\x")
    assert sent[2:] == raw.hex()
    assert postgrest.from_bytea(sent) == raw
    assert postgrest.from_bytea("\\x") == b""


@pytest.mark.parametrize("value", ["", "ffd8ff", "\\xnot-hex", "\\377\\330"])
def test_bytea_in_another_format_is_a_store_error(value):
    with pytest.raises(postgrest.StoreError):
        postgrest.from_bytea(value)


def test_items_embed_names_its_foreign_key():
    assert postgrest.BOX_SELECT["select"] == "*,items!items_box_id_fkey(*)"
    assert postgrest.BOX_SELECT["items.order"] == "created_at.asc,id.asc"


def test_photo_lists_never_select_the_bytes():
    columns = postgrest.PHOTO_SELECT["select"].split(",")

    assert columns == ["id", "box_id", "item_id", "width", "height", "size", "created_at", "created_by"]
    assert postgrest.PHOTO_SELECT["order"] == "created_at.asc,id.asc"


def test_kept_photos_are_never_selected_with_their_bytes():
    columns = postgrest.KEPT_PHOTO_SELECT["select"].split(",")

    assert columns == [*postgrest.PHOTO_SELECT["select"].split(","), "tag_id", "removed_at"]
    assert not {"data", "thumb"} & set(columns)


def test_a_set_of_ids_is_split_so_no_address_grows_too_long(store):
    ids = [str(uuid.uuid4()) for _ in range(2 * postgrest.ID_CHUNK + 5)]

    filters = postgrest.id_filters(ids)

    assert [len(value.split(",")) for value in filters] == [postgrest.ID_CHUNK, postgrest.ID_CHUNK, 5]
    assert all(value.startswith("in.(") and value.endswith(")") for value in filters)
    assert ",".join(value[4:-1] for value in filters) == ",".join(ids)
    assert postgrest.id_filters([]) == []
    for table in ("photos", postgrest.KEPT_PHOTOS):
        for value in filters:
            request = httpx.Request("GET", f"{store.url}/{table}", params={**postgrest.PHOTO_SELECT, "id": value})
            assert len(str(request.url)) < 4000


def test_no_ids_to_look_up_means_no_request(store):
    # the address does not resolve, so reaching for the network would raise
    assert store.list_photos_by_ids([]) == []
    assert store.list_photos_by_ids(["not-a-uuid", "", None]) == []
    assert store.get_kept_photo("not-a-uuid") is None
    assert store.erase_photo("not-a-uuid") is False


def test_history_is_selected_newest_first():
    assert postgrest.HISTORY_SELECT["order"] == "at.desc,id.desc"


def test_negative_tag_history_is_refused_before_any_request(store):
    # the address does not resolve, so reaching for the network would raise
    assert store.list_history(-9001, 50, None) == []
