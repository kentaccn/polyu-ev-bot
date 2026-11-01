"""Continuously collect charger history independently of Telegram connectivity."""

import logging
import time

from .source import SourceClient, SourceError

LOG = logging.getLogger(__name__)


def collect_forever(history):
    source = SourceClient(history=history)
    LOG.info("Charger history collector started; interval 60 seconds.")
    while True:
        try:
            snapshot = source.get()
            LOG.info("Archived charger snapshot; source time %s.", snapshot.updated_at.isoformat())
        except SourceError:
            LOG.warning("PolyU collection failed; failure archived, retrying in 60 seconds.")
        # Always allow at least 60 seconds between fetch attempts.
        time.sleep(60)
