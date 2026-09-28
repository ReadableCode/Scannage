# Scannage

A home inventory for storage boxes. Stick a printed tag on each box, point a
phone at the shelf, and the browser shows what is inside every box it can see.

- The tag holds a number, never the contents. Print a label once and change
  what is in the box as often as you like.
- Runs in the phone's browser. There is no app to install.
- Self hosted. Your inventory stays in your own database.
- Everything third party is open source and vendored in this repo. Nothing is
  loaded from a CDN and no video leaves the phone.

## How it works

Each label carries two codes:

| Code | Read by | Used for |
|---|---|---|
| Large ArUco tag | the live view in this app | finding boxes on a shelf, from a distance, many at once |
| Small QR code | any camera app | opening one box's page in a browser |

The live view reads tags from the camera in the browser, looks each number up
in the inventory it loaded from the server, and draws the box name and items
over the video. Tap a tag to edit the box. Type in the search field and only
the boxes holding a match light up.

## Run it

With Docker:

```bash
docker compose up -d
```

Without Docker (needs [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8791
```

Then open `http://localhost:8791`. With no configuration the inventory is a
SQLite file at `data/scannage.db`.

`http://localhost:8791/?demo` shows a drawn shelf in place of the camera, which
is the quickest way to see the live view working.

### Using a phone

Phone browsers only open the camera on an `https` page. To use the live view
from a phone, put the app behind anything that serves https: a reverse proxy
you already run, Caddy, or Tailscale Serve. The boxes list, the editor and the
label sheet work over plain http.

### Sign-in

The app has no accounts. Run it behind a reverse proxy that handles sign-in
if it is reachable by people who should not see it. When the proxy sends a
`Remote-User` header, the app records that name as who last changed a box.

## Print labels

Open `/labels`, choose the first tag number and how many, and print at 100%
scale on matte paper. Put a label on two neighbouring faces of each box so it
is visible from either side.

## Configuration

Set these in the environment or in a `.env` file in the repo root. See
`.env.example`.

| Setting | Default | Meaning |
|---|---|---|
| `SCANNAGE_STORE` | `sqlite` | `sqlite` or `postgrest` |
| `SCANNAGE_SQLITE_PATH` | `data/scannage.db` | where the SQLite file lives |
| `SCANNAGE_BASE_URL` | taken from the request | the address printed into QR codes |
| `SCANNAGE_SEED_SAMPLES` | off | `1` adds six sample boxes once, on an empty store |

### Postgres through PostgREST

Set `SCANNAGE_STORE=postgrest` to keep the inventory in Postgres. The app then
reads and writes only through [PostgREST](https://postgrest.org), in its own
schema, so it can share a database with other apps.

| Setting | Meaning |
|---|---|
| `APP_SCHEMA` | schema name, default `scannage` |
| `POSTGREST_URL` | address of PostgREST |
| `POSTGREST_JWT_SECRET` | the secret PostgREST verifies tokens with |
| `POSTGRES_URL`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | direct connection, used only to create the schema at startup |

At startup the app creates its schema, tables and the `scannage_user` role if
they are missing. It only ever adds. The schema also has to be listed in
PostgREST's `db-schemas` setting, and the roles `postgrest_authenticator` and
`web_anon` must already exist.

The choice of store is explicit. If `postgrest` is selected and cannot be
reached, the app reports it and does not switch to SQLite.

## Tests

```bash
uv run pytest tests/test_sqlite_store.py tests/test_api.py
```

`tests/test_db_real.py` and `tests/test_postgrest_real.py` run against a real
Postgres and PostgREST taken from the environment. They fail, rather than
skip, when those cannot be reached. They use negative tag numbers, which no
printed tag can have, and remove what they create.

## Layout

| Path | Contents |
|---|---|
| `app/main.py` | the API and page routes |
| `app/stores/` | the SQLite and PostgREST stores behind one interface |
| `app/bootstrap.py`, `deploy/02_schema.sql` | Postgres schema setup |
| `app/static/` | the frontend: plain HTML, CSS and JavaScript |
| `docs/api.md` | the API and data contract |
| `docs/open_decisions.md` | choices still to be made |
| `licenses/` | licenses of vendored code |

## License

MIT. Vendored third party code keeps its own license, see `licenses/`.
