"""Sample boxes, so a new database has something to scan.

They belong to a brand new database: each store's bootstrap seeds them once,
and the app_meta flag is what makes it once. After it is set, deleting every
box does not bring them back. Only scripts/init_db.py --samples adds them
again, and only on purpose.
"""

from __future__ import annotations

import logging

from . import history
from .stores.base import Store, utc_now

log = logging.getLogger("scannage.samples")

META_KEY = "samples_seeded"
# Who history says added them, so they never read as something a person did.
ACTOR = "samples"

# (tag_id, name, location, [(item name, qty), ...])
SAMPLES: tuple[tuple[int, str, str, tuple[tuple[str, int], ...]], ...] = (
    (
        1,
        "Camping",
        "Shelf A, top",
        (
            ("Tent, 4 person", 1),
            ("Sleeping bag", 2),
            ("Camp stove", 1),
            ("Lantern", 2),
            ("Tarp", 1),
            ("Tent stakes", 12),
        ),
    ),
    (
        2,
        "Christmas",
        "Shelf A, top",
        (("String lights", 6), ("Tree stand", 1), ("Ornaments", 1), ("Wreath", 1)),
    ),
    (
        3,
        "Paint supplies",
        "Shelf A, top",
        (("Rollers", 3), ("Drop cloth", 1), ("Painters tape", 4), ("Brushes", 5), ("Paint tray", 2)),
    ),
    (
        4,
        "Power tools",
        "Shelf A, bottom",
        (("Drill", 1), ("Drill bits", 20), ("Circular saw", 1), ("Sander", 1)),
    ),
    (
        5,
        "Car care",
        "Shelf A, bottom",
        (("Wax", 1), ("Microfiber towels", 10), ("Jump starter", 1)),
    ),
    (
        6,
        "Cables",
        "Shelf A, bottom",
        (
            ("HDMI cable", 5),
            ("Ethernet cable", 8),
            ("Power strip", 2),
            ("Extension cord", 3),
            ("Lantern batteries", 4),
        ),
    ),
)


def add(store: Store) -> list[int]:
    """Adds each sample whose tag is unclaimed and never touches a box that exists. Returns the tags it used."""
    added = []
    for tag_id, name, location, items in SAMPLES:
        if store.get_box(tag_id) is not None:
            continue
        history.put_box(store, tag_id, {"name": name, "location": location}, ACTOR)
        for item_name, qty in items:
            history.add_item(store, tag_id, item_name, qty, ACTOR)
        added.append(tag_id)
    store.set_meta(META_KEY, utc_now())
    return added


def seed(store: Store) -> bool:
    """Returns True when the sample boxes were inserted."""
    if store.get_meta(META_KEY) is not None:
        return False
    if store.list_boxes():
        # an inventory that is already in use is never topped up with samples
        store.set_meta(META_KEY, utc_now())
        return False
    log.info("seeded sample boxes on tags %s", add(store))
    return True


def seed_best_effort(store: Store) -> None:
    try:
        seed(store)
    except Exception:
        log.exception("sample seeding failed, serving anyway, will retry next boot")
