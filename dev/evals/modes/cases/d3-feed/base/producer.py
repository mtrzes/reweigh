from datetime import datetime, timezone


def make_entry(title):
    now = datetime.now(timezone.utc)
    return {"title": title, "published": now.isoformat(), "published_epoch": now.timestamp()}
