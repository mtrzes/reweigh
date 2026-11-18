import sqlite3

CONN = sqlite3.connect(":memory:")


def execute(sql, params):
    return CONN.execute(sql, params).rowcount


def scoped(sql, params, tenant_id):
    """Every tenant query goes through here: it appends the tenant filter."""
    return sql + " AND tenant_id = ?", params + [tenant_id]
