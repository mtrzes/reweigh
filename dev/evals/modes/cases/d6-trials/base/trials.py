from datetime import datetime, timedelta, timezone

TRIAL = timedelta(days=14)


def trial_expired(account, now=None):
    now = now or datetime.now(timezone.utc)
    created = account.created_at
    if created.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    return now >= created + TRIAL
