"""Discount rules, run in registration order.

Partner integrations register rules at import time through register().
"""
RULES = []


def register(rule):
    RULES.append(rule)
    return rule


def apply_discounts(order):
    for rule in RULES:
        rule(order)
