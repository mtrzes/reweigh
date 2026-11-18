from gateway import charge


def pay(order):
    return charge(order.card, order.total, merchant_ref=order.id)
