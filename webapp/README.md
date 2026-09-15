# MamaRoo-ChatService Test Webapp

A small, password-protected webapp for manually exercising the main
MamaRoo-ChatService. It serves a single chat page and proxies `POST /chat`
to the real service, injecting the real product API key server-side — the
browser never sees it. Intended as an internal testing tool, not a public
product surface.

## Environment variables

| Variable | Purpose |
|---|---|
| `WEBAPP_USERNAME` | HTTP Basic Auth username gating this webapp |
| `WEBAPP_PASSWORD` | HTTP Basic Auth password gating this webapp |
| `CHATSERVICE_URL` | Base URL of the real chat service this webapp proxies to |
| `CHATSERVICE_API_KEY` | Product API key sent to the real chat service (never exposed to the browser) |

For the deployed instance these are set as Railway service variables (on
the `webapp` service, alongside the main `app` service in the same
project). For local development, put them in `webapp/.env` (gitignored,
not committed).

If any of these are missing/empty, the webapp fails closed with a clean
`401`/`503` rather than silently authenticating everyone or proxying with a
broken key.

## Rotating the webapp login

Generate a fresh username/password pair and update the Railway service's
variables:

```bash
python3 -c "import secrets; print(secrets.token_hex(4)); print(secrets.token_urlsafe(18))"
```

The first line is a new `WEBAPP_USERNAME`, the second a new
`WEBAPP_PASSWORD`. Redeploy (or let Railway restart the service) after
updating the variables.

## Rotating/revoking the product API key

This webapp authenticates to the main chat service as a dedicated product
named `webapp-tester`, provisioned via the main service's
`POST /admin/products`. As of this writing, the main service has no
`DELETE`/regenerate endpoint for products — there is no API-level way to
revoke or rotate an existing key. Revoking the `webapp-tester` key today
means an operator has to intervene directly at the database level (e.g.
deactivating or deleting its row in the products table). Provisioning a
brand-new product (and pointing `CHATSERVICE_API_KEY` at its new key) is
the practical alternative to rotation.

## Further detail

See the design spec (`../docs/superpowers/specs/2026-09-15-test-webapp-design.md`)
and implementation plan (`../docs/superpowers/plans/2026-09-15-test-webapp.md`)
for the full design rationale and history.

## Known trade-off: global (not per-IP) lockout

The login lockout (5 failed attempts locks out all logins for 5 minutes)
is shared across every client, not scoped per-IP. This means anyone who
finds this webapp's URL can trigger repeated 5-minute lockouts against the
legitimate user just by submitting wrong credentials in a loop. This is a
deliberate, accepted trade-off, not an oversight: the shared password
itself is long and random, so the lockout isn't doing meaningful
brute-force protection to begin with — the password's own entropy is the
real protection. Worth keeping in mind before sharing this webapp's URL
more widely than intended.
