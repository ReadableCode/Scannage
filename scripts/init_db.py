"""Manual wrapper over the same startup bootstrap the postgrest store runs."""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bootstrap  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

parser = argparse.ArgumentParser()
parser.add_argument("--force", action="store_true", help="re-apply even if version matches")
args = parser.parse_args()

applied = bootstrap.apply_schema(force=args.force)
print("applied" if applied else "already up to date")
