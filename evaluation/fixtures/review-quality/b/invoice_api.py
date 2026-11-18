def get_invoice(db, cache, tenant_id, invoice_id):
    key = (tenant_id, invoice_id)
    if key not in cache:
        cache[key] = db.find_invoice(tenant_id, invoice_id)
    return cache[key]


def listing_limit(raw):
    limit = 25 if raw is None else raw
    if limit < 1 or limit > 100:
        raise ValueError("limit outside 1..100")
    return limit
