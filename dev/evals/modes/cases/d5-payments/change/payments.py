import time

from gateway import GatewayTimeout, charge


def pay(order, attempts=3):
    for attempt in range(attempts):
        try:
            return charge(order.card, order.total, merchant_ref=order.id)
        except GatewayTimeout:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
