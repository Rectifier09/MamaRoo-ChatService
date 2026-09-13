"""
Auth for browser-facing traffic.

Important: since end-user browsers call this API directly, the API key is a PUBLIC
identifier, not a secret — anyone can read it out of the page's network tab or JS
bundle, the same way a Stripe publishable key or a Google Maps browser key works.
Real protection comes from two other layers, both applied here: restricting which
origins a key is allowed to be used from, and rate-limiting per key (see rate_limit.py).
This does not stop a non-browser client from replaying a leaked key with a forged
Origin header — there's no way around that for a purely client-side integration. If
that risk matters for you, put a thin backend in front of this service for that
product and use a real server-side secret instead.
"""
from fastapi import Header, HTTPException, Request

import db


def get_product(request: Request, x_api_key: str = Header(...)) -> dict:
    product = db.fetchone("SELECT * FROM products WHERE api_key = %s", (x_api_key,))
    if not product:
        raise HTTPException(status_code=401, detail="Invalid API key")

    allowed = product["allowed_origins"]
    if allowed and "*" not in allowed:
        origin = request.headers.get("origin") or request.headers.get("referer", "")
        if not origin or not any(origin.startswith(o) for o in allowed):
            raise HTTPException(status_code=403, detail="Origin not allowed for this API key")

    return product
