import time


def is_stale(entry):
    return time.time() - entry["published_epoch"] > 7 * 86400
