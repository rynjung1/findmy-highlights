#!/usr/bin/env python3
"""One-time migration: storage moved from uploads/<batch_id>/ to the
org-scoped uploads/<org_id>/<batch_id>/ layout (see backend/storage.py).
Existing batches sitting directly under uploads_root predate that and
need to move under a real org, or backend.jobs/backend.app simply won't
find them (jobs.find_active_job and _batch_dir both assume the org
level now exists).

Dry-run by default — prints the org that would be created and every
batch_id that would move, touches nothing. Pass --apply to actually
create the org/user and move the directories.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import auth, storage


def _existing_batch_dirs(uploads_root: Path) -> list[Path]:
    """Top-level directories under uploads_root that look like a
    pre-migration batch (contain files.json) — anything else there
    (e.g. an org_id directory from a previous partial run of this
    script) is left alone."""
    if not uploads_root.exists():
        return []
    return sorted(
        p for p in uploads_root.iterdir()
        if p.is_dir() and (p / "files.json").exists()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-name", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--uploads-root", default=None,
        help="defaults to backend.storage.DEFAULT_UPLOADS_ROOT (FMH_UPLOADS_ROOT or ./uploads)")
    parser.add_argument(
        "--auth-root", default=None,
        help="defaults to backend.auth.DEFAULT_AUTH_ROOT (FMH_AUTH_ROOT or ./auth)")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually create the org/user and move the directories "
             "(default: print the plan only, touch nothing)")
    args = parser.parse_args()

    uploads_root = Path(args.uploads_root or storage.DEFAULT_UPLOADS_ROOT)
    auth_root = args.auth_root or auth.DEFAULT_AUTH_ROOT

    batch_dirs = _existing_batch_dirs(uploads_root)
    if not batch_dirs:
        print(f"no pre-migration batches found under {uploads_root} — nothing to do")
        return

    print(f"will create org {args.org_name!r} and user {args.username!r}, "
         f"then move {len(batch_dirs)} batch(es):")
    for bdir in batch_dirs:
        print(f"  {bdir.name}")

    if not args.apply:
        print("\ndry run only — rerun with --apply to actually do this")
        return

    password = getpass.getpass("password for new user: ")
    confirm = getpass.getpass("confirm password: ")
    if password != confirm or not password:
        print("passwords did not match or were empty", file=sys.stderr)
        sys.exit(1)

    org = auth.create_org(auth_root, args.org_name)
    user = auth.create_user(auth_root, org["org_id"], args.username, password)
    print(f"created org {org['org_id']!r}, user {user['username']!r}")

    org_root = storage.org_dir(uploads_root, org["org_id"])
    org_root.mkdir(parents=True, exist_ok=True)
    moved = []
    for bdir in batch_dirs:
        dest = org_root / bdir.name
        bdir.rename(dest)
        moved.append(bdir.name)

    print(f"moved {len(moved)} batch(es) into {org_root}:")
    for batch_id in moved:
        print(f"  {batch_id}")


if __name__ == "__main__":
    main()
