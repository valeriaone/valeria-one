"""Background jobs, run by a timer (systemd, cron, Kubernetes CronJob…) or by the Docker `jobs` service:

    python -m valeria_one.jobs calendar      # push appointment changes to connected Google Calendars
    python -m valeria_one.jobs webhooks      # deliver queued webhooks (with retries)
    python -m valeria_one.jobs followups     # reminders, follow-ups and campaigns
    python -m valeria_one.jobs grow          # today's win-backs (once a day per business, in its own time zone)
    python -m valeria_one.jobs posts         # publish the posts that are due (Facebook, Instagram, Google)
    python -m valeria_one.jobs google        # Google Business Profile reviews (each profile at most every 30 minutes)
    python -m valeria_one.jobs tiktok        # renew TikTok tokens before they run out
    python -m valeria_one.jobs assistant     # Valeria: approved actions out, her questions, 08:00 summary, Monday proof,
                                             # slow days, holidays, style learning (each business in its own time zone)
    python -m valeria_one.jobs recipes       # "when this, do that": no reply for N hours; deleted fields gone after 30 days
    python -m valeria_one.jobs workflows     # v0.18: workflow waits that are over; birthdays, no visit in N days, no reply
    python -m valeria_one.jobs sequences     # v0.18: drip steps whose wait is over go into the outbox
    python -m valeria_one.jobs invoices      # v0.18: subscription invoices, overdue invoices, reminders (Valeria asks)
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
    from .services import (billing, followups, gbp, gcal, growth, instance, invoices, owner, payments, posts, recipes, sequences,
                           studio, tiktok, voice_agent, webhooks, workflows)
    steps = {"studio": studio.coordinator_round, "calendar": gcal.sync_all, "webhooks": webhooks.deliver_due, "grow": growth.daily,
             "posts": posts.publish_due, "google": gbp.sync_all, "tiktok": tiktok.refresh_due,
             "recipes": recipes.round_,                     # before Valeria: a "tell me" it queues goes out this round
             "workflows": workflows.round_,                 # v0.18: waits that are over, birthdays, "no visit in N days"
             "assistant": owner.round_,                     # before the outbox: an offer she queues goes out this round
             "sequences": sequences.round_,                 # v0.18: drip steps whose wait is over, into the outbox
             "campaigns": followups.dispatch_campaigns,
             "followups": followups.send_due, "payments": payments.reconcile,
             "invoices": invoices.round_,                   # v0.18: each subscription's invoice, overdue, reminders (asked)
             "calls": voice_agent.sweep,                    # v0.18: phone-agent calls the caller hung up on, summed up
             "billing": billing.notices, "countries": instance.country_alerts}
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
        from .services import sequences
        print(json.dumps({"sequences": sequences.round_(), "campaigns": followups.dispatch_campaigns(), "messages": followups.send_due()}))
        return 0
    if job == "grow":
        from .services import growth
        print(json.dumps(growth.daily()))
        return 0
    if job == "recipes":
        from .services import recipes
        print(json.dumps(recipes.round_(), default=str))
        return 0
    if job in ("workflows", "sequences", "invoices"):
        from .services import invoices, sequences, workflows
        print(json.dumps({"workflows": workflows.round_, "sequences": sequences.round_, "invoices": invoices.round_}[job](),
                         default=str))
        return 0
    if job == "assistant":
        from .services import owner
        print(json.dumps(owner.round_(), default=str))
        return 0
    if job in ("posts", "google", "tiktok"):
        from .services import gbp, posts, tiktok
        print(json.dumps({"posts": posts.publish_due, "google": gbp.sync_all, "tiktok": tiktok.refresh_due}[job](), default=str))
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
