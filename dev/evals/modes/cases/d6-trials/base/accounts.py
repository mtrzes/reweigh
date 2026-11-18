from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Account:
    email: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def sign_up(email):
    return Account(email=email)
