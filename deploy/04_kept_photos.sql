-- Kept photos. Idempotent and additive only; applied by app/bootstrap.py at
-- startup after 03_history_photos.sql (schema version 3).

-- The tag a photo is on rides on the photo itself. When a box is deleted its
-- photos go through ON DELETE CASCADE, and by then the box row is gone, so
-- the tag could no longer be looked up.
ALTER TABLE scannage.photos ADD COLUMN IF NOT EXISTS tag_id integer;

-- A photo that left the inventory: removed from its box or item, or gone with
-- it. Same columns as photos, plus when it left. It references nothing, so no
-- delete of a box or an item can reach it.
CREATE TABLE IF NOT EXISTS scannage.kept_photos (
    id         uuid PRIMARY KEY,
    box_id     uuid NOT NULL,
    item_id    uuid,
    width      integer NOT NULL,
    height     integer NOT NULL,
    size       integer NOT NULL,
    data       bytea NOT NULL,
    thumb      bytea NOT NULL,
    created_at timestamptz NOT NULL,
    created_by text NOT NULL DEFAULT '',
    tag_id     integer,
    removed_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION scannage.photos_fill_tag_id() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tag_id IS NULL THEN
        NEW.tag_id := (SELECT tag_id FROM scannage.boxes WHERE id = NEW.box_id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER photos_fill_tag_id
    BEFORE INSERT ON scannage.photos
    FOR EACH ROW EXECUTE FUNCTION scannage.photos_fill_tag_id();

-- Runs for a photo deleted on its own and for one deleted with its box or
-- item, inside the same transaction, so a photo cannot go without being kept.
CREATE OR REPLACE FUNCTION scannage.photos_keep() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO scannage.kept_photos (
        id, box_id, item_id, width, height, size, data, thumb, created_at, created_by, tag_id, removed_at
    ) VALUES (
        OLD.id, OLD.box_id, OLD.item_id, OLD.width, OLD.height, OLD.size, OLD.data, OLD.thumb,
        OLD.created_at, OLD.created_by, OLD.tag_id, now()
    );
    RETURN OLD;
END;
$$;

CREATE OR REPLACE TRIGGER photos_keep
    BEFORE DELETE ON scannage.photos
    FOR EACH ROW EXECUTE FUNCTION scannage.photos_keep();

-- Photos stored before the column existed. Only rows without a tag are touched.
UPDATE scannage.photos AS photo
SET tag_id = box.tag_id
FROM scannage.boxes AS box
WHERE box.id = photo.box_id AND photo.tag_id IS NULL;

-- The trigger functions run as the role doing the write, so that role needs
-- INSERT here. DELETE is for erasing a kept photo on purpose. There is no
-- UPDATE: a kept photo never changes. Nothing is granted to web_anon.
GRANT SELECT, INSERT, DELETE ON scannage.kept_photos TO scannage_user;
