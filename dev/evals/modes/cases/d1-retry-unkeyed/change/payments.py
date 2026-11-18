from gateway import GatewayTimeout, charge


def pay(order, attempts=3):
    for attempt in range(attempts):
        try:
            return charge(order.card, order.total)
        except GatewayTimeout:
            if attempt == attempts - 1:
                raise
