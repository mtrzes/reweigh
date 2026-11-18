from datetime import datetime, timedelta, timezone

TRIAL = timedelta(days=14)


def trial_expired(account, now=None):
    now = now or datetime.now(timezone.utc)
    created = account.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return now >= created + TRIAL
