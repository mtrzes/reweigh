"""Client for the card gateway.

A GatewayTimeout can arrive after the gateway has already recorded the charge.
"""
import itertools

_ids = itertools.count(1)
LEDGER = []


class GatewayTimeout(Exception):
    pass


def charge(card, amount, idempotency_key=None):
    """Record a charge. The gateway deduplicates only charges that carry an idempotency_key."""
    if idempotency_key is not None:
        for existing in LEDGER:
            if existing["key"] == idempotency_key:
                return existing
    txn = {"id": next(_ids), "card": card, "amount": amount, "key": idempotency_key}
    LEDGER.append(txn)
    return txn
