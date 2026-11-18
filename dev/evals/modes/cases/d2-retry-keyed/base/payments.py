from gateway import charge


def pay(order):
    return charge(order.card, order.total, idempotency_key=order.id)
