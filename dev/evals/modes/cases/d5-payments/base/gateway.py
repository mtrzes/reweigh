"""HTTP client for the card gateway."""
import json
import urllib.request

GATEWAY_URL = "https://gateway.example/v1/charges"


class GatewayTimeout(Exception):
    pass


def charge(card, amount, merchant_ref):
    """POST one charge and return the gateway's charge record."""
    body = json.dumps({"card": card, "amount": amount, "merchant_ref": merchant_ref}).encode()
    request = urllib.request.Request(GATEWAY_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except TimeoutError as error:
        raise GatewayTimeout(str(error)) from error
