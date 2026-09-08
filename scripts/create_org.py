#!/usr/bin/env python3
"""CLI-only org creation, no self-service equivalent (matches this
project's create_user.py pattern). Run once per real organization
(church) before creating any users for it — see
scripts/create_user.py --org.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import auth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="organization display name")
    parser.add_argument(
        "--auth-root", default=None,
        help="defaults to backend.auth.DEFAULT_AUTH_ROOT (FMH_AUTH_ROOT or ./auth)")
    args = parser.parse_args()

    auth_root = args.auth_root or auth.DEFAULT_AUTH_ROOT
    org = auth.create_org(auth_root, args.name)
    print(f"created org {org['org_id']!r} ({org['name']!r})")
    print(f"next: python scripts/create_user.py --org {org['org_id']} --username <name>")


if __name__ == "__main__":
    main()
