"""Background jobs, run by a timer (systemd, cron, Kubernetes CronJob…) or by the Docker `jobs` service:

    python -m valeria_one.jobs calendar      # push appointment changes to connected Google Calendars
    python -m valeria_one.jobs webhooks      # deliver queued webhooks (with retries)
    python -m valeria_one.jobs followups     # reminders, follow-ups and campaigns
    python -m valeria_one.jobs all           # all of them — run every minute
    python -m valeria_one.jobs loop          # all of them, every minute, forever (the Docker install)

One job failing never stops the others; each round is recorded so the Server page can show it.
"""
from __future__ import annotations

import json
import logging
import sys
import time

log = logging.getLogger("valeria_one.jobs")


def _round() -> tuple[dict, dict]:
    from .services import billing, followups, gcal, instance, payments, webhooks
    steps = {"calendar": gcal.sync_all, "webhooks": webhooks.deliver_due, "campaigns": followups.dispatch_campaigns,
             "followups": followups.send_due, "payments": payments.reconcile, "billing": billing.notices,
             "countries": instance.country_alerts}
    results, errors = {}, {}
    for name, fn in steps.items():
        try:
            results[name] = fn()
        except Exception as e:                                  # noqa: BLE001 — logged, and the next job still runs
            log.exception("job %s failed", name)
            results[name] = errors[name] = f"{type(e).__name__}: {e}"[:300]
    try:
        from .services import instance
        instance.heartbeat(results, errors)
    except Exception:                                           # noqa: BLE001 — the record is nice to have, not vital
        log.exception("could not record the jobs heartbeat")
    return results, errors


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
        results, errors = _round()
        print(json.dumps(results, default=str))
        return 1 if errors else 0
    if job == "loop":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        every = int(argv[2]) if len(argv) > 2 else 60
        while True:
            started = time.monotonic()
            try:
                _round()
            except Exception:                                   # noqa: BLE001 — e.g. the database restarting
                log.exception("jobs round failed")
            time.sleep(max(1.0, every - (time.monotonic() - started)))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
