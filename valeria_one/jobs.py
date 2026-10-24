"""Background jobs, run by a timer (systemd, cron, Kubernetes CronJob…):

    python -m valeria_one.jobs calendar      # push appointment changes to connected Google Calendars
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
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
