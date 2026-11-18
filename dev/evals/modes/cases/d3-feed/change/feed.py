from datetime import datetime, timedelta


def is_stale(entry):
    published = datetime.fromisoformat(entry["published"])
    return datetime.now() - published > timedelta(days=7)
