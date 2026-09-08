#!/usr/bin/env python3
"""CLI-only user creation, no self-service signup or password reset —
deliberate: this app's real user base is small volunteer/staff teams
per organization, an admin (you) creates each account by hand. Prompts
for the password via getpass so it's never in shell history or process
args.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import auth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", help="org_id to create this user under")
    parser.add_argument("--username")
    parser.add_argument(
        "--list-orgs", action="store_true",
        help="list existing orgs (org_id, name) and exit — use this to "
             "find the --org value instead of hand-copying it")
    parser.add_argument(
        "--auth-root", default=None,
        help="defaults to backend.auth.DEFAULT_AUTH_ROOT (FMH_AUTH_ROOT or ./auth)")
    args = parser.parse_args()

    auth_root = args.auth_root or auth.DEFAULT_AUTH_ROOT

    if args.list_orgs:
        orgs = auth.list_orgs(auth_root)
        if not orgs:
            print("no orgs yet — create one first: python scripts/create_org.py --name ...")
            return
        for org_id, info in orgs.items():
            print(f"{org_id}  {info['name']!r}  (created {info['created_at']})")
        return

    if not args.org or not args.username:
        parser.error("--org and --username are required (or pass --list-orgs)")

    password = getpass.getpass("password: ")
    confirm = getpass.getpass("confirm password: ")
    if password != confirm:
        print("passwords did not match", file=sys.stderr)
        sys.exit(1)
    if not password:
        print("password must not be empty", file=sys.stderr)
        sys.exit(1)

    try:
        user = auth.create_user(auth_root, args.org, args.username, password)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"created user {user['username']!r} in org {user['org_id']!r}")


if __name__ == "__main__":
    main()
