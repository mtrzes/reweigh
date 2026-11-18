FREE_SHIP_MIN = 50.00
FLAT_RATE = 4.99


def shipping_for(subtotal):
    if subtotal > FREE_SHIP_MIN:
        return 0.0
    return FLAT_RATE
