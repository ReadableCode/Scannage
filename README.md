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
uv run python -m app
```

Then open `http://localhost:8791`. With no configuration the inventory is a
SQLite file at `data/scannage.db`.

`http://localhost:8791/?demo` shows a drawn shelf in place of the camera, which
is the quickest way to see the live view working.

A new database comes with six sample boxes on tags 1 to 6, so there is
something to scan. Delete them when you like: once a database has been set
up they never come back on their own. To get them back on purpose, run

```bash
uv run python scripts/init_db.py --samples
```

which adds each sample whose tag is free and leaves every existing box alone.
Without the flag the script only creates missing tables.

### Using a phone

Phone browsers only open the camera on an `https` page. The boxes list, the
editor and the label sheet work over plain http. For the live view from a
phone there are two ways:

- put the app behind anything that serves https: a reverse proxy you already
  run, Caddy, or Tailscale Serve
- or turn on the built in https

### Built in https

Set `SCANNAGE_HTTPS=1` and the app serves https itself, with no proxy. On the
first start it makes its own certificate and keeps it in `data/tls`.

```bash
SCANNAGE_HTTPS=1 uv run python -m app
```

Then open `https://<this machine's address>:8791` on the phone.

- The certificate is self signed, so the phone shows a warning the first
  time. Choose to continue. The start up log prints the certificate's SHA-256
  fingerprint, which you can compare with what the phone shows.
- While it is on the port speaks https only. `http://localhost:8791` stops
  working, use `https://localhost:8791`.
- The certificate covers `localhost`, `127.0.0.1`, the machine's hostname and
  its address on the local network. Add other names or addresses with
  `SCANNAGE_HTTPS_HOSTS`, comma separated. In a container the app only sees
  the container's own name and address, so list the one the phone opens.
- It is valid for 397 days and is replaced on a start within 30 days of its
  end, or when a name it should cover is missing. The phone then asks again.

Leave it off behind a reverse proxy that already serves https.

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
| `SCANNAGE_HTTPS` | off | `1` or `true` serves https with a self signed certificate |
| `SCANNAGE_HTTPS_HOSTS` | none | extra names or addresses for the certificate, comma separated |
| `SCANNAGE_TLS_DIR` | `data/tls` | where the certificate and its key are kept |

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
they are missing. It only ever adds, so a database from an earlier version is
brought up to date in place with its data untouched. The schema also has to
be listed in PostgREST's `db-schemas` setting, and the roles
`postgrest_authenticator` and `web_anon` must already exist.

The choice of store is explicit. If `postgrest` is selected and cannot be
reached, the app reports it and does not switch to SQLite.

## Photos

A box and each item in it can carry up to 12 photos. The app turns a photo
upright, scales it down to 1600 pixels on the long edge and stores it as a
JPEG inside the database, next to a small thumbnail. Everything else the
file carried is removed, the location it was taken at included. Photos go
when their box or item is deleted.

## History

Every change is recorded: what changed, from what to what, when, and who did
it when a sign-in proxy says so. Entries are kept for good and stay after
their box is deleted. Several saves of the same thing within two minutes
count as one change.

## Tests

```bash
uv run pytest tests/test_sqlite_store.py tests/test_api.py tests/test_tls.py tests/test_postgrest_config.py
```

`tests/test_db_real.py` and `tests/test_postgrest_real.py` run against a real
Postgres and PostgREST taken from the environment. They fail, rather than
skip, when those cannot be reached. They use negative tag numbers, which no
printed tag can have, and remove the boxes they create. The history entries
they write stay, as all history does, and the API never returns them.

## Layout

| Path | Contents |
|---|---|
| `app/main.py` | the API and page routes |
| `app/__main__.py`, `app/tls.py` | the `python -m app` start up and the built in https |
| `app/stores/` | the SQLite and PostgREST stores behind one interface |
| `app/history.py` | records each write, the same way on either store |
| `app/photos.py` | turns an upload into the stored JPEG and thumbnail |
| `app/samples.py`, `scripts/init_db.py` | the sample boxes and the manual schema and samples command |
| `app/bootstrap.py`, `deploy/*.sql` | Postgres schema setup |
| `app/static/` | the frontend: plain HTML, CSS and JavaScript |
| `docs/api.md` | the API and data contract |
| `docs/open_decisions.md` | choices still to be made |
| `licenses/` | licenses of vendored code |

## License

MIT. Vendored third party code keeps its own license, see `licenses/`.
