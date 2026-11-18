class InvalidOrder(Exception):
    pass


class Order:
    def __init__(self, total, items):
        self.total = total
        self.items = items


def validate(order):
    if not order.items:
        raise InvalidOrder("empty order")
    if order.total < 0:
        raise InvalidOrder("negative total")
