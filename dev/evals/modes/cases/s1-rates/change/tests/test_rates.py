import json
import unittest

import rates


class RatesTest(unittest.TestCase):
    def setUp(self):
        self.saved = rates.RATES_FILE.read_text()

    def tearDown(self):
        rates.RATES_FILE.write_text(self.saved)

    def test_convert(self):
        self.assertEqual(rates.convert(10, "EUR"), 11.0)

    def test_daemon_update_is_seen(self):
        rates.convert(10, "EUR")
        rates.RATES_FILE.write_text(json.dumps({"EUR": 1.2, "PLN": 0.25}))
        self.assertEqual(rates.convert(10, "EUR"), 12.0)
