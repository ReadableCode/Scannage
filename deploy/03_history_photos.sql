-- History and photos. Idempotent and additive only; applied by
-- app/bootstrap.py at startup after 02_schema.sql (schema version 2).

-- One row per change, kept for good. tag_id, box_id and item_id are plain
-- columns with no foreign keys, so an entry outlives the box it describes.
CREATE TABLE IF NOT EXISTS scannage.history (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    at        timestamptz NOT NULL DEFAULT now(),
    actor     text NOT NULL DEFAULT '',
    action    text NOT NULL,
    tag_id    integer NOT NULL,
    box_id    uuid NOT NULL,
    box_name  text NOT NULL DEFAULT '',
    item_id   uuid,
    item_name text NOT NULL DEFAULT '',
    changes   jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS history_tag_id_at_idx ON scannage.history (tag_id, at DESC);
CREATE INDEX IF NOT EXISTS history_at_idx ON scannage.history (at DESC);

-- A photo belongs to a box, and to one of its items when item_id is set. The
-- image is stored already processed: data is the full JPEG, thumb the small one.
CREATE TABLE IF NOT EXISTS scannage.photos (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    box_id     uuid NOT NULL,
    item_id    uuid,
    width      integer NOT NULL,
    height     integer NOT NULL,
    size       integer NOT NULL,
    data       bytea NOT NULL,
    thumb      bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT '',
    CONSTRAINT photos_box_id_fkey FOREIGN KEY (box_id) REFERENCES scannage.boxes (id) ON DELETE CASCADE,
    CONSTRAINT photos_item_id_fkey FOREIGN KEY (item_id) REFERENCES scannage.items (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS photos_box_id_idx ON scannage.photos (box_id);
CREATE INDEX IF NOT EXISTS photos_item_id_idx ON scannage.photos (item_id);

-- UPDATE and DELETE on history are for the merge rule only: the API has no
-- way to edit or remove an entry.
GRANT SELECT, INSERT, UPDATE, DELETE ON scannage.history TO scannage_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON scannage.photos TO scannage_user;
