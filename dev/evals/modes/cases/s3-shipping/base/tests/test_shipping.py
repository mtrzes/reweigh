import unittest

from shipping import FLAT_RATE, shipping_for


class ShippingTest(unittest.TestCase):
    def test_over_threshold_ships_free(self):
        self.assertEqual(shipping_for(50.01), 0.0)

    def test_exact_threshold_pays_flat_rate(self):
        self.assertEqual(shipping_for(50.00), FLAT_RATE)
