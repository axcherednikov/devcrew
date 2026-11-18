def get_invoice(db, cache, tenant_id, invoice_id):
    return db.find_invoice(tenant_id, invoice_id)


def listing_limit(raw):
    limit = 25 if raw is None else raw
    if not 1 <= limit <= 100:
        raise ValueError("limit outside 1..100")
    return limit
