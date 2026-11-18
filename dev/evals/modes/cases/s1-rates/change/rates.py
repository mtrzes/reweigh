"""Exchange rates for invoice conversion.

The pricing daemon rewrites rates.json every minute while the service runs,
so a rate read once at start-up goes stale within the hour.
"""
import json
from pathlib import Path

RATES_FILE = Path(__file__).with_name("rates.json")


def load_rates():
    return json.loads(RATES_FILE.read_text())


def convert(amount, currency):
    return round(amount * load_rates()[currency], 2)
