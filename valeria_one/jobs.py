"""Background jobs, run by a timer (systemd, cron, Kubernetes CronJob…):

    python -m valeria_one.jobs calendar      # push appointment changes to connected Google Calendars
    python -m valeria_one.jobs webhooks      # deliver queued webhooks (with retries)
    python -m valeria_one.jobs followups     # reminders, follow-ups and campaigns
    python -m valeria_one.jobs all           # all of them — run every minute
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    job = argv[1] if len(argv) > 1 else ""
    if job == "calendar":
        from .services import gcal
        print(json.dumps(gcal.sync_all()))
        return 0
    if job == "webhooks":
        from .services import webhooks
        print(json.dumps(webhooks.deliver_due()))
        return 0
    if job == "followups":
        from .services import followups
        print(json.dumps({"campaigns": followups.dispatch_campaigns(), "messages": followups.send_due()}))
        return 0
    if job == "all":
        from .services import followups, gcal, payments, webhooks
        print(json.dumps({"calendar": gcal.sync_all(), "webhooks": webhooks.deliver_due(),
                          "campaigns": followups.dispatch_campaigns(), "followups": followups.send_due(),
                          "payments": payments.reconcile()}))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
