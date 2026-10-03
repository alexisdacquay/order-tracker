# Incident Report: Server errors (5xx) on /api/orders/{order_id}

- **Alert fired:** 2026-10-03T19:16:50Z
- **Severity:** critical
- **Affected endpoint:** `GET /api/orders/{order_id}`

## What happened

Requests to `GET /api/orders/express-1002` returned HTTP 500. The order `express-1002` was seeded with `created_at` set to the last day of the previous month (2026-09-30).

## Root cause

`app/main.py:65` computed the estimated delivery date for express orders using:

```python
estimated_at = placed_at.replace(day=placed_at.day + 2)
```

When `placed_at.day + 2` exceeds the number of days in the month (e.g., Sept 30 + 2 = day 32), `datetime.replace()` raises `ValueError: day is out of range for month`. This crashed the request handler, producing a 500.

## Fix

Replaced `placed_at.replace(day=placed_at.day + 2)` with `placed_at + timedelta(days=2)`, which correctly rolls over into the next month.

## Verification

- `uv run pytest` — 4/4 tests pass, including a new regression test that creates an express order on Sept 30 and asserts `estimated_delivery == "2026-10-02"`.
- `docker compose up --build -d --wait app` — rebuilt and restarted the container.
- `curl http://localhost:8000/api/orders/express-1002` returned HTTP 200 with `"estimated_delivery": "2026-10-02"`.
