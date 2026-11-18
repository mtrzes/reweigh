from discounts import apply_discounts
from orders import validate


def checkout(order):
    validate(order)
    apply_discounts(order)
    validate(order)
    return order.total
