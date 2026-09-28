"""One-time sample boxes, so a fresh install has something to scan.

Seeds through the normal store methods, once. The app_meta flag is what
makes it once: after it is set, deleting every box does not bring them back.
"""

from __future__ import annotations

import logging

from . import config
from .stores.base import Store, utc_now

log = logging.getLogger("scannage.samples")

META_KEY = "samples_seeded"
ACTOR = ""

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


def seed(store: Store) -> bool:
    """Returns True when the sample boxes were inserted."""
    if store.get_meta(META_KEY) is not None:
        return False
    if store.list_boxes():
        # an inventory that is already in use is never topped up with samples
        store.set_meta(META_KEY, utc_now())
        return False
    for tag_id, name, location, items in SAMPLES:
        store.upsert_box(tag_id, {"name": name, "location": location}, ACTOR)
        for item_name, qty in items:
            store.add_item(tag_id, item_name, qty, ACTOR)
    store.set_meta(META_KEY, utc_now())
    log.info("seeded %d sample boxes", len(SAMPLES))
    return True


def seed_best_effort(store: Store) -> None:
    if not config.SEED_SAMPLES:
        return
    try:
        seed(store)
    except Exception:
        log.exception("sample seeding failed, serving anyway, will retry next boot")
