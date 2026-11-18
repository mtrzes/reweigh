from db import execute, scoped


def delete_invoice(tenant_id, invoice_id):
    sql, params = scoped("DELETE FROM invoices WHERE id = ?", [invoice_id], tenant_id)
    return execute(sql, params)


def bulk_delete(tenant_id, invoice_ids):
    marks = ",".join("?" * len(invoice_ids))
    sql, params = scoped(f"DELETE FROM invoices WHERE id IN ({marks})", list(invoice_ids), tenant_id)
    return execute(sql, params)
