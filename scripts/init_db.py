"""Manual wrapper over the same schema setup the configured store runs at startup.

With no flag it only applies the schema. --samples is the one deliberate way
to get the sample boxes back once a database has been initialised.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import samples  # noqa: E402
from app.stores import get_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

parser = argparse.ArgumentParser()
parser.add_argument("--force", action="store_true", help="re-apply even if version matches")
parser.add_argument(
    "--samples",
    action="store_true",
    help="add the six sample boxes on tags 1 to 6, leaving any of those tags that is in use alone",
)
args = parser.parse_args()

store = get_store()
applied = store.apply_schema(force=args.force)
print("applied" if applied else "already up to date")

if args.samples:
    added = samples.add(store)
    skipped = [sample[0] for sample in samples.SAMPLES if sample[0] not in added]
    print(f"sample boxes added on tags: {', '.join(map(str, added)) or 'none'}")
    if skipped:
        print(f"tags already in use, left alone: {', '.join(map(str, skipped))}")
