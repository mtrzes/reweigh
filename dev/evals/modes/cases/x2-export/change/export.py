import csv
import json


def export_json(customers, out):
    json.dump([c.to_public() for c in customers], out)


def export_csv(customers, out):
    writer = None
    for customer in customers:
        row = vars(customer)
        if writer is None:
            writer = csv.DictWriter(out, fieldnames=list(row))
            writer.writeheader()
        writer.writerow(row)
