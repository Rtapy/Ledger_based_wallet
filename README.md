# Wallet Evaluation Project

Django and DRF foundation for the wallet evaluation. PostgreSQL setup is
implemented. Wallet models, operations, endpoints, and behavior tests are not
implemented yet. The design and API contract below define the implementation
target; the examples are illustrative, not results from a running wallet API.

## Setup

Requirements: Python 3.10+ and Docker with Docker Compose. Django runs in the
local virtual environment; Compose runs PostgreSQL 18 only.

For a fresh checkout, run these commands from the project directory:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

If `.env` already exists, keep its values and add any missing variables from
`.env.example`. Set your own `SECRET_KEY` and `POSTGRES_PASSWORD` in `.env`.
Keep `SECRET_KEY` single-quoted so Compose treats any `$` characters literally.
The same PostgreSQL credentials are used by Django and Compose. The database
port is published on `127.0.0.1` only; change `POSTGRES_PORT` if needed.

```bash
docker compose up -d --wait db
python manage.py migrate
python manage.py check --database default
python manage.py runserver
```

PostgreSQL data is kept in a named Docker volume. `docker compose stop db` stops
the database without deleting its data. PostgreSQL initialization variables only
apply when this volume is first initialized; editing `.env` afterward does not
change the existing database user or password.

Existing SQLite files are preserved locally and ignored by Git. Django now uses
PostgreSQL; SQLite data is not automatically copied to it.

## Verification

With the virtual environment active and PostgreSQL running:

```bash
python -m pip check
python manage.py check --database default
python manage.py makemigrations --check --dry-run
python manage.py test wallet
```

The wallet test package is currently empty. A run discovering zero tests does
not validate wallet behavior, rollback, or concurrent requests. Future locking
tests must use PostgreSQL with `TransactionTestCase` and independent connections.

## Scope and design decisions

These are implementation decisions, not additional requirements or assumptions
approved by the company.

- Wallet API authentication is omitted as a local evaluation decision. The
  current permission is `AllowAny`; planned wallet views will also explicitly
  disable DRF authentication (`authentication_classes = []`). Callers can select
  any user's wallet: `user_id` does not prove identity or ownership. This is for
  a trusted local demonstration; Django admin keeps its own authentication.
- Each provisioned user has one wallet in a single unspecified unit. Multiple
  assets, exchange rates, external payments, transfers, registration, and password
  recovery are outside this exercise's scope. Credit and debit are internal
  balance adjustments.
- Use Django's built-in user model. A planned `create_demo_user --username NAME`
  management command will create a user and a zero-balance wallet atomically and
  print the user ID. It will reject an existing username. API requests will not
  create users or wallets implicitly. An unknown user returns `user_not_found`;
  an existing user without a wallet returns `wallet_not_found`.
- PostgreSQL is the development and test database for exact decimal storage and
  row-lock testing. Financial precision is 20 total digits, including 8 decimal
  places, as configured in `config/settings.py`.
- Ledger entries record successful, committed balance changes only. Rejected
  attempts return an error and do not produce financial entries. A separate,
  searchable history of failed attempts is outside scope.

### Data and consistency

| Model | Planned fields and rules |
| --- | --- |
| `Wallet` | `id`, unique one-to-one `user`, `balance`, `created_at`, `updated_at`; initial balance is zero. |
| `LedgerEntry` | `id`, `wallet`, `type` (`credit` or `debit`), positive `amount`, `balance_before`, `balance_after`, UUID `idempotency_key`, `created_at`. |

All monetary fields use the same decimal precision. The ledger's user is derived
from its wallet; there is no duplicate user field or pending/failed status.
Foreign keys protect referenced users and wallets from cascading deletion.
Ledger entries have no application/API/admin edit or delete operation. Direct
database access is trusted; database-level immutability triggers are not part of
this baseline.

Planned database constraints enforce nonnegative balances, positive entry amounts,
valid entry types, the per-entry before/after arithmetic, and uniqueness of
`(wallet, idempotency_key)`. The arithmetic is `after = before + amount` for a
credit and `after = before - amount` for a debit.

`Wallet.balance` is a stored balance maintained together with the ledger. Starting
from zero, it must equal the sum of credits minus the sum of debits. Before/after
snapshots assist inspection; they do not replace recomputing that sum.

Each financial operation follows this sequence:

1. Validate the request and resolve the existing user and wallet.
2. Enter `transaction.atomic()` and fetch the wallet using `select_for_update()`.
3. Check for a previously committed entry with the same idempotency key.
4. For a new operation, check the locked balance and calculate the new balance.
5. Update the wallet and insert exactly one ledger entry in the same transaction.
6. Commit before returning success. Exceptions before commit roll back both writes.

All financial writers must use this service and locking order. A planned
`reconcile_wallet --user-id ID` command will lock the same wallet in a transaction,
recompute the ledger sum, and check consecutive before/after snapshots against
the stored balance. It will report discrepancies and exit nonzero without
repairing data. Locking during comparison avoids a false mismatch caused by a
concurrent posting.

## API contract (to implement)

`user_id` is the Django user ID, supplied in the URL. There is no user or wallet
selector in the request body. The documented routes have trailing slashes.
Wallet views will use JSON-only parsing and rendering.

| Method | Path | Success |
| --- | --- | --- |
| `POST` | `/api/users/{user_id}/wallet/credits/` | `201` for a new credit; `200` for replay. |
| `POST` | `/api/users/{user_id}/wallet/debits/` | `201` for a new debit; `200` for replay. |
| `GET` | `/api/users/{user_id}/wallet/` | `200`, current balance. |
| `GET` | `/api/users/{user_id}/wallet/entries/` | `200`, paginated financial history. |

### Amount validation

- POST requests use `Content-Type: application/json` and a JSON object containing
  only `amount`. Missing or additional fields are rejected.
- `amount` must be a plain decimal **string**, for example `"10"` or `"10.25"`.
  Accept ASCII digits with an optional decimal point followed by 1 to 8 digits. Reject
  signs, surrounding whitespace, scientific notation, JSON numbers, booleans,
  `null`, `NaN`, and infinity. Decimal parsing must not pass through `float`.
- The supported positive range is `0.00000001` to `999999999999.99999999`, inclusive.
  Reject excess precision or values outside this range; do not silently round.
  These are representation limits, not additional business transaction limits.
- A debit may consume the entire balance but cannot exceed it. A credit cannot
  raise the balance above `999999999999.99999999`. All monetary response fields
  are strings formatted with exactly 8 decimal places.

### Credit, debit, and retries

Both POST endpoints require an `Idempotency-Key` header containing a valid UUID.
Use a new UUID for each new logical operation and reuse it for retries. Keys are
normalized to their lowercase, hyphenated UUID representation.

Example credit request:

```http
POST /api/users/1/wallet/credits/
Content-Type: application/json
Idempotency-Key: 9a0632b4-c863-42e7-9343-f10c125181c2
```

```json
{"amount": "10.25"}
```

New credit response (`201`), assuming an initial balance of zero:

```json
{
  "id": 1,
  "wallet_id": 1,
  "type": "credit",
  "amount": "10.25000000",
  "balance_before": "0.00000000",
  "balance_after": "10.25000000",
  "idempotency_key": "9a0632b4-c863-42e7-9343-f10c125181c2",
  "created_at": "2026-09-14T08:00:00Z"
}
```

Debit uses the same request and response shapes, with `type: "debit"` and the
corresponding subtraction. For example, a new debit of `"3.25"` after the credit
above returns `amount: "3.25000000"`, `balance_before: "10.25000000"`, and
`balance_after: "7.00000000"`, with its own entry ID, key, and timestamp.
IDs are integers and timestamps are UTC ISO 8601 strings.

Idempotency is scoped to the wallet, across both POST endpoints:

| Situation | Result |
| --- | --- |
| Same wallet, key, type, and numeric amount after success | `200` with the original entry; no new entry or balance change. `"10"` and `"10.00000000"` are the same amount. |
| Same wallet and committed key, different type or valid amount | `409 idempotency_conflict`. |
| Same key in another wallet | Independent operation. |
| Validation or business rejection without a commit | No entry or key reservation; a later retry is evaluated again and may succeed. |
| Concurrent requests using the same wallet and key | Serialized by the wallet lock; at most one financial effect. |

Validate input before comparing keys, but check an existing key before applying
current balance checks. Replay returns the original entry and its historical
`balance_after`, even if later operations changed the current balance. Read the
balance endpoint for the current value.

A client timeout or lost response does not prove that the transaction failed.
Retry with the same key: a committed operation is replayed; an uncommitted one
can be attempted again. Committed keys are retained with the ledger without a
separate expiration policy.

### Balance and history

Balance response (`200`), after the credit and debit examples above:

```json
{"wallet_id": 1, "user_id": 1, "balance": "7.00000000"}
```

History returns only the selected wallet's committed entries, ordered by entry
`id` ascending. Each item has the same shape as a POST response. Query parameters
are `limit` (default 20, integer 1–100) and `offset` (default 0, nonnegative
integer). Invalid supplied pagination values return `400 invalid_pagination`;
an offset beyond the end returns an empty `results` list.

The response contains `count` (the total entries for that wallet), `next` and
`previous` (page URLs or `null`), and `results`. For an empty wallet:

```json
{"count": 0, "next": null, "previous": null, "results": []}
```

All history is reachable through pagination. Pages are live reads, not a frozen
snapshot across requests; later committed entries can appear on later pages.

### Errors

Errors raised while handling these wallet endpoints use this shape. Clients
should depend on `code`, not the human-readable message:

```json
{
  "error": {
    "code": "insufficient_funds",
    "message": "The requested debit exceeds the available balance."
  }
}
```

| HTTP | Code | Meaning |
| --- | --- | --- |
| `400` | `invalid_request` | Malformed JSON, wrong body shape, or missing/additional body fields. |
| `400` | `invalid_amount` | Invalid amount type, format, precision, or range. |
| `400` | `invalid_idempotency_key` | Missing or invalid UUID header. |
| `400` | `invalid_pagination` | Invalid supplied limit or offset. |
| `404` | `user_not_found` | Selected user does not exist. |
| `404` | `wallet_not_found` | Selected user exists but has no provisioned wallet. |
| `409` | `insufficient_funds` | Debit exceeds the locked current balance. |
| `409` | `balance_limit_exceeded` | Credit would exceed the balance's numeric capacity. |
| `409` | `idempotency_conflict` | Committed key reused with a different type or amount. |
| `405` | `method_not_allowed` | Unsupported method on a documented endpoint. |
| `406` | `not_acceptable` | The requested response media type does not allow JSON. |
| `415` | `unsupported_media_type` | POST body is not sent as JSON. |
| `500` | `internal_error` | Unexpected failure; return a generic message without database details or a traceback. |

An error or disconnected client must never leave a partial financial operation.
If the outcome is uncertain, the caller uses the same idempotency key to resolve
it; a server error alone is not proof that no commit occurred.

## Acceptance checks (to implement)

| Requirement or decision | Evidence required |
| --- | --- |
| Consecutive credits/debits | Exact decimal balances, correct before/after values, one entry per successful operation, and an allowed debit down to zero. |
| Invalid requests / insufficient balance | Defined errors with unchanged balance and no new ledger entry. |
| Complete history | Correct wallet filtering, deterministic order, and traversal across pages without omitted existing entries. |
| Verifiable balance | Recomputed ledger balance agrees with the stored balance; intentional corruption is detected. |
| No partial writes | Inject failures after the wallet update and after the entry insert, before commit; assert both writes roll back and a retry can succeed. |
| Defined retries | Same-key replay, changed-payload conflict, wallet-scoped keys, retry after rejection, and replay after a response is lost. |
| Concurrent debits | On PostgreSQL, two debits of 80 from a balance of 100 yield one success, one insufficient-funds error, one debit entry, and balance 20. |
| Concurrent retries | Independent PostgreSQL connections using the same key create one financial effect. |

Write behavior tests alongside each implementation step. Concurrency tests must
exercise the actual posting service with overlapping transactions; checking a
backend capability flag or running requests sequentially is not sufficient.

Database setup references: [Django PostgreSQL support](https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes)
and [the official PostgreSQL image](https://hub.docker.com/_/postgres).
