# Ledger-Based Wallet

Django/PostgreSQL implementation of the Tabdeal wallet assignment.

**Current stage: models, posting service and tests written; verification pending.**
PostgreSQL configuration and the API contract are in place. Wallet and
LedgerEntry now have model definitions and database constraints. Generate and
review the wallet migration, then run the checks below before committing.
The API, demo-user command and reconciliation remain implementation targets.
Service, rollback and PostgreSQL concurrency tests are written but have not been
executed as part of the service implementation.

## Local setup and verification

For a fresh checkout:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Keep an existing `.env`; do not overwrite it. Set `SECRET_KEY` and
`POSTGRES_PASSWORD` there. Compose and Django use the same PostgreSQL settings.
The database runs in Docker; Django runs on the host.

```bash
docker compose up -d --wait --wait-timeout 60 db
```

For this model-development stage, generate the migration once and inspect it:

```bash
venv/bin/python manage.py makemigrations wallet
git diff -- wallet/models.py wallet/tests/test_models.py README.md
```

The new migration may be untracked and absent from that diff; open it separately
and commit it with the models. After the migration is committed, normal setup
only needs `migrate`, not `makemigrations`.

```bash
venv/bin/python manage.py check --database default
venv/bin/python manage.py migrate
venv/bin/python manage.py test wallet.tests.test_models --verbosity 2 --noinput
venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python manage.py migrate --check
git diff --check
```

Tests use a separate database; the database role must be allowed to create it.
Model tests require PostgreSQL and inspect its constraint diagnostics.
No commands, migrations or tests were run as part of this implementation.

To verify the posting service, also run:

```bash
venv/bin/python manage.py test wallet.tests.test_services wallet.tests.test_service_transactions --verbosity 2 --noinput
```

## Assumptions

- Each Django user has at most one wallet, with one unspecified unit and initial
  balance zero. Multiple assets, conversions, transfers and external payments
  are out of scope.
- Authentication and ownership checks are omitted for trusted local evaluation.
  This is an applicant decision, not a company-approved exception. Anyone can
  select a user ID. Planned wallet views use `AllowAny` and
  `authentication_classes = []`; Django admin authentication remains separate.
- Registration is out of scope. A planned `create_demo_user --username NAME`
  command creates a user and zero-balance wallet atomically, prints the user ID
  and rejects an existing username. APIs do not create missing wallets implicitly.
- Ledger records represent successful changes only. Rejected requests return
  errors without financial entries; failed-attempt auditing is out of scope.
- Amounts use Decimal with 20 total digits and 8 decimal places. The minimum
  positive amount is `0.00000001`; maximum amount and balance are
  `999999999999.99999999`. A debit may leave exactly zero. The business maximum
  is explicit in the model; changing precision alone does not change the contract.

## Models

| Model | Fields |
| --- | --- |
| Wallet | id, user (one-to-one), balance, created_at, updated_at |
| LedgerEntry | id, wallet, entry_type, amount, balance_before, balance_after, idempotency_key (UUID), created_at |

The internal `entry_type` field will be exposed as `type` in the API.
LedgerEntry has no duplicate user field, status or updated_at.

Database constraints enforce nonnegative bounded balances, positive bounded
amounts, known entry types, credit/debit arithmetic and unique
`(wallet, idempotency_key)`. UUID keys are required and have no generated default.
History defaults to ascending ID order, with a `(wallet, id)` index.

Django `PROTECT` prevents deleting a user with a wallet or a wallet with entries
through the ORM. It does not make entries immutable. Application writes will
go through the posting service; no ledger edit/delete API or admin registration
is provided. Direct ORM/SQL edits remain possible for a trusted database
operator; database immutability triggers are not implemented.

These constraints validate individual rows. They do not maintain Wallet.balance
from the ledger, enforce a continuous history, provision users' wallets or make
two writes atomic. Model-test fixtures exercise rows independently of a complete
wallet history.

## Posting service and planned reconciliation

Validate input and resolve the existing user/wallet. Inside `transaction.atomic()`:

1. Lock the wallet with `select_for_update()`.
2. Look up the idempotency key before checking current funds.
3. Replay a matching entry or reject a conflicting payload.
4. Check funds and the balance ceiling.
5. Update the balance and insert the entry in the same transaction.

Any failure must roll back both writes. Include `updated_at` when using
`save(update_fields=...)`; `QuerySet.update()` does not update it automatically.

A planned `reconcile_wallet --user-id ID` command takes the same wallet lock,
compares stored balance with credits minus debits from zero, and validates the
ordered before/after chain. Discrepancies produce a nonzero exit status;
reconciliation does not silently repair data.

### Amount normalization and service decisions

**مسئولیت quantize کردن مقدار به دقیق ۸ رقم اعشار، قبل از فراخوانی سرویس، بر عهده‌ی caller (لایه‌ی API) است**

The API must validate the original input, including its fractional digit count,
before calling `quantize(Decimal("0.00000001"))`. Quantization must not round
an invalid input into an accepted amount. The service does not quantize; in
accordance with its tests, it accepts valid Decimal values with zero to eight
fractional places and rejects more than eight, including trailing zeros.
String parsing, UUID parsing and fixed-width response formatting belong to the API.

Review decisions:

- `post_entry` returns `PostingResult(entry, created)`; it does not return HTTP
  statuses. Expected rejections use `WalletError.code`; API status mapping is pending.
- Invalid input is rejected before locking or replay lookup. For valid input,
  replay/conflict is checked before current funds or the balance ceiling.
- `user_id` must be a positive integer (not a boolean); invalid values yield
  `invalid_request`. Missing users/wallets yield their respective not-found codes;
  posting never provisions either resource.
- The wallet row serializes postings to that wallet. Keys are caller-supplied
  UUID objects, scoped across credit/debit, and failed writes do not reserve them.
- Balance arithmetic uses a local Decimal precision of 21 for the current 20-digit
  fields, allowing an exact sum above the ceiling to be rejected before persistence.
- Unexpected database errors propagate after rollback; they are not converted
  into successful replays. The API must handle generic failures without exposing details.
- A successful call inside an outer transaction is provisional until the caller
  commits. Its lock remains held until that transaction ends; callers must not
  perform slow external work while holding it.

## Planned API contract

| Method | Path | Success |
| --- | --- | --- |
| POST | /api/users/{user_id}/wallet/credits/ | 201 created; 200 replay |
| POST | /api/users/{user_id}/wallet/debits/ | 201 created; 200 replay |
| GET | /api/users/{user_id}/wallet/ | Current balance |
| GET | /api/users/{user_id}/wallet/entries/ | Paginated history |

Wallet endpoints accept and return JSON. Write bodies contain only `amount`,
for example `{"amount": "10.00000000"}`, and require a UUID `Idempotency-Key` header.

Amounts must be strings of ASCII digits with an optional fractional part of
1–8 digits. Reject zero, negatives, signs, whitespace, scientific notation, JSON
numbers/booleans/null, NaN, infinity, unknown fields and values outside the range.
Reject excess decimal places before persistence; do not rely on database
rounding or convert amounts through float.

Keys are normalized as UUIDs and scoped to a wallet across both write endpoints:

- Same key, type and numeric amount: return the original entry with 200.
  Its balance_after is historical, not necessarily the current wallet balance.
- Same key with a different type or numeric amount: 409 idempotency_conflict.
- Another wallet may use the same key independently.
- An uncommitted/rejected operation does not reserve its key and may succeed
  later. Keys for committed entries do not expire.
- Concurrent identical requests must commit at most one entry. A client timeout
  does not prove failure; retry with the same key.

Entry responses contain `id`, `wallet_id`, `type`, `amount`, `balance_before`,
`balance_after`, `idempotency_key` and `created_at`. Decimal response values use
strings with eight fractional digits; timestamps use UTC.

History is wallet-scoped and ordered by ascending ID. Use `limit` (default 20,
range 1–100) and `offset` (default 0, nonnegative), with `count`, `next`, `previous`,
`results`. Invalid pagination returns 400; offsets beyond the end return empty
results. Pagination reflects live data rather than a fixed snapshot.

Errors use `{"error": {"code": "...", "message": "..."}}`:

| HTTP status | Codes |
| --- | --- |
| 400 | invalid_request, invalid_amount, invalid_idempotency_key, invalid_pagination |
| 404 | user_not_found, wallet_not_found |
| 409 | insufficient_funds, balance_limit_exceeded, idempotency_conflict |
| 405 / 406 / 415 | method_not_allowed / not_acceptable / unsupported_media_type |
| 500 | internal_error |

Unexpected errors must not expose implementation details.

## Remaining acceptance tests

Model tests cover individual constraints, relationships, UUID scoping, stored
decimal precision, boundary values and deterministic history ordering.
Service and transaction tests now cover the posting cases below; execution is
pending. Reconciliation and API coverage remain future work:

- Sequential credits/debits, exact arithmetic, debit to zero and balance ceiling.
- Rejections and injected failures between writes leave balance and history unchanged.
- Replay returns the original result; changed payloads conflict; rejected requests
  may retry successfully after circumstances change.
- Two simultaneous withdrawals of 80 from 100 yield one success, one rejection,
  balance 20 and one entry. Concurrent identical keys yield one entry.
- Reconciliation detects balance and history-chain corruption.
- API validation, errors, complete wallet-scoped history and pagination.

Concurrency and transaction-boundary tests must use PostgreSQL,
`TransactionTestCase` and separate database connections.
