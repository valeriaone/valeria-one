"""Server commands (the host's `valeria-one` script calls these inside the app container):

    python -m valeria_one.ops setup-link          # the one-time link to create the first account
    python -m valeria_one.ops make-admin EMAIL    # let an existing account run this server
    python -m valeria_one.ops status              # what the Server page shows, as JSON
    python -m valeria_one.ops partner-key NAME    # a key for a host's or agency's systems (shown once)
    python -m valeria_one.ops reset-link EMAIL    # a password link for someone, when this server sends no email
    python -m valeria_one.ops billing-account     # where transfers go: JSON {bank, holder, account, swift, currency} on stdin
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
    if cmd == "reset-link" and len(argv) > 2:
        from . import config
        from .services import users
        with db.tx() as conn:
            r = users.start_reset(conn, argv[2])
        if not r:
            print(f"No account with the email {argv[2]} (or 3 links were already made this hour)", file=sys.stderr)
            return 1
        print(f"{config.PUBLIC_URL()}/reset/{r[1]}")
        return 0
    if cmd == "billing-account":
        from .services import partner
        d = json.load(sys.stdin)
        a = {k: " ".join(str(d.get(k) or "").split())[:120] for k in ("bank", "holder", "account", "swift", "currency")}
        if not (a["bank"] and a["holder"] and a["account"]):
            print("bank, holder and account are needed", file=sys.stderr)
            return 1
        a["currency"] = (a["currency"] or "MXN").upper()[:3]
        with db.tx() as conn:
            instance.kv_set(conn, "billing_account", a)
            partner.audit(conn, "billing.account_changed", "host", {"bank": a["bank"], "last4": a["account"][-4:]})
        print(f"Transfers go to {a['bank']} · {a['holder']} · …{a['account'][-4:]} ({a['currency']})")
        return 0
    if cmd == "status":
        with db.tx() as conn:
            print(json.dumps(instance.status(conn), default=str, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
