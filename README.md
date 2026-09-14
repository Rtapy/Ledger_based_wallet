# Wallet Evaluation Project

Django and DRF foundation for the wallet evaluation. Wallet models, operations,
and behavior tests are still to be implemented.

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

## Assumptions

- Wallet API authentication is omitted as an implementation decision for local
  evaluation. The company has not approved this assumption. The current DRF
  permission is `AllowAny`: unauthenticated requests are allowed and wallet
  ownership is not enforced. This is a documented access-control limitation.
- PostgreSQL is the development and test database for exact decimal storage and
  row-lock testing. SQLite is not used as evidence for those guarantees.
- Financial precision remains 20 total digits, including 8 decimal places,
  as configured in `config/settings.py`.

Database setup references: [Django PostgreSQL support](https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes)
and [the official PostgreSQL image](https://hub.docker.com/_/postgres).
