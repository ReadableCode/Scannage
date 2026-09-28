-- Core schema. Idempotent and additive only; applied by app/bootstrap.py at
-- startup (version-gated via scannage.deploy_meta). The scannage_user role is
-- created by the bootstrap before this file runs.

CREATE SCHEMA IF NOT EXISTS scannage;

-- One row per claimed tag. The printed tag only ever holds tag_id, so the
-- contents behind a label can change without reprinting it.
CREATE TABLE IF NOT EXISTS scannage.boxes (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id     integer UNIQUE NOT NULL,
    name       text NOT NULL DEFAULT '',
    location   text NOT NULL DEFAULT '',
    notes      text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scannage.items (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    box_id     uuid NOT NULL REFERENCES scannage.boxes (id) ON DELETE CASCADE,
    name       text NOT NULL,
    qty        integer NOT NULL DEFAULT 1 CHECK (qty >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS items_box_id_idx ON scannage.items (box_id);

-- Small key/value table for one-time flags such as sample seeding.
CREATE TABLE IF NOT EXISTS scannage.app_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- The inventory is shared by everyone who can reach the app, so there is no
-- users table and no row level security. Nothing is granted to web_anon.
GRANT USAGE ON SCHEMA scannage TO scannage_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON scannage.boxes TO scannage_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON scannage.items TO scannage_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON scannage.app_meta TO scannage_user;
