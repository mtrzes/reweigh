from db import execute, scoped


def delete_invoice(tenant_id, invoice_id):
    sql, params = scoped("DELETE FROM invoices WHERE id = ?", [invoice_id], tenant_id)
    return execute(sql, params)
