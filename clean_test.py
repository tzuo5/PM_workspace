# -*- coding: utf-8 -*-
"""Reset local PM Workplace test data.

Usage examples:
    python clean_test.py --all --yes
    python clean_test.py --attachments --yes
    python clean_test.py --db --yes
    python clean_test.py --dry-run

By default, with no target flags, this script cleans both attachments and the
current SQLite database after an interactive confirmation.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DATA_DIR = ROOT_DIR / "backend" / "data"
ATTACHMENT_DIRS = [
    BACKEND_DATA_DIR / "attachments",
    ROOT_DIR / "data" / "attachments",  # kept for compatibility if a local run created root/data
]
DB_PATHS = [
    BACKEND_DATA_DIR / "pm_tracker.db",
    BACKEND_DATA_DIR / "pm_tracker.db-wal",
    BACKEND_DATA_DIR / "pm_tracker.db-shm",
]


def remove_path(path: Path, dry_run: bool = False) -> None:
    if not path.exists():
        print(f"SKIP missing: {path}")
        return
    if dry_run:
        print(f"DRY-RUN would remove: {path}")
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    print(f"REMOVED: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean PM Workplace local test data.")
    parser.add_argument("--attachments", action="store_true", help="delete saved attachment files under backend/data/attachments")
    parser.add_argument("--db", action="store_true", help="delete the current backend/data/pm_tracker.db SQLite database")
    parser.add_argument("--all", action="store_true", help="delete both attachments and database")
    parser.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted without deleting")
    args = parser.parse_args()

    clean_attachments = args.all or args.attachments
    clean_db = args.all or args.db
    if not clean_attachments and not clean_db:
        clean_attachments = True
        clean_db = True

    targets = []
    if clean_attachments:
        targets.extend(ATTACHMENT_DIRS)
    if clean_db:
        targets.extend(DB_PATHS)

    existing_targets = [path for path in targets if path.exists()]
    print("Targets:")
    if existing_targets:
        for path in existing_targets:
            print(f"  - {path}")
    else:
        print("  - no existing target found")

    if not args.yes and not args.dry_run:
        answer = input("Delete these local test files? Type YES to continue: ").strip()
        if answer != "YES":
            print("Cancelled.")
            return 1

    for path in targets:
        remove_path(path, dry_run=args.dry_run)

    if clean_attachments and not args.dry_run:
        (BACKEND_DATA_DIR / "attachments").mkdir(parents=True, exist_ok=True)
        print(f"READY: {BACKEND_DATA_DIR / 'attachments'}")
    if not args.dry_run:
        BACKEND_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
