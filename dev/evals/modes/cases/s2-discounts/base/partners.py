from discounts import register


@register
def loyalty(order):
    if order.total > 100:
        order.total -= 5


@register
def stacked_coupons(order):
    # Each coupon in the basket takes 15 off the total.
    order.total -= 15 * sum(1 for item in order.items if item == "coupon")
