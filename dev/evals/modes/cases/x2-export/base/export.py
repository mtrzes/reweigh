import json


def export_json(customers, out):
    json.dump([c.to_public() for c in customers], out)
