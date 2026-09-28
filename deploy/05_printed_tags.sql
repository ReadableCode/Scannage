-- Printed labels. Idempotent and additive only; applied by app/bootstrap.py
-- at startup after 04_kept_photos.sql (schema version 4).

-- One row per tag that has had a label printed. tag_id is a plain column
-- with no foreign key: a label is printed before any box exists, and stays
-- printed when its box is deleted.
CREATE TABLE IF NOT EXISTS scannage.printed_tags (
    tag_id           integer PRIMARY KEY,
    first_printed_at timestamptz NOT NULL DEFAULT now(),
    last_printed_at  timestamptz NOT NULL DEFAULT now(),
    times            integer NOT NULL DEFAULT 1 CHECK (times >= 1),
    printed_by       text NOT NULL DEFAULT ''
);

-- UPDATE is for a reprint, DELETE for taking a print back. Nothing is
-- granted to web_anon.
GRANT SELECT, INSERT, UPDATE, DELETE ON scannage.printed_tags TO scannage_user;
