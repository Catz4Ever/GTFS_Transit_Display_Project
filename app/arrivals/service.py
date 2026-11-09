#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import signal
import sys

from transit.config import load_settings
from transit.services.stop_arrivals import StopArrivalsService


def main():
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    service = StopArrivalsService()
    signal.signal(signal.SIGINT, service.stop)
    signal.signal(signal.SIGTERM, service.stop)

    try:
        service.run()
    except Exception as e:
        logging.exception("Stop arrivals worker failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
