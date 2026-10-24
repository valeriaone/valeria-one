"""Server commands (the host's `valeria-one` script calls these inside the app container):

    python -m valeria_one.ops setup-link          # the one-time link to create the first account
    python -m valeria_one.ops make-admin EMAIL    # let an existing account run this server
    python -m valeria_one.ops status              # what the Server page shows, as JSON
    python -m valeria_one.ops partner-key NAME    # a key for a host's or agency's systems (shown once)
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    from . import db
    from .services import instance
    if cmd == "setup-link":
        with db.tx() as conn:
            if not instance.needs_setup(conn):
                print("This server is already set up. Sign in at the usual address.", file=sys.stderr)
                return 1
        print(instance.setup_link())
        return 0
    if cmd == "make-admin" and len(argv) > 2:
        from .services import NotFound
        try:
            with db.tx() as conn:
                print(json.dumps(instance.make_admin(conn, argv[2]), default=str))
        except NotFound:
            print(f"No account with the email {argv[2]}", file=sys.stderr)
            return 1
        return 0
    if cmd == "partner-key" and len(argv) > 2:
        from .services import partner
        with db.tx() as conn:
            key, row = partner.new_key(conn, " ".join(argv[2:]), "host")
        print(key)
        print(f"Partner key {row['prefix']}… for {row['name']}: shown once, keep it secret.", file=sys.stderr)
        return 0
    if cmd == "status":
        with db.tx() as conn:
            print(json.dumps(instance.status(conn), default=str, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
