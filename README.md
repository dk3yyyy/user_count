# 👥 Telegram User Counter Bot

A small async Telegram bot that registers unique users, reports counts to an admin, and sends durable milestone notifications.

The project favors verifiable behavior over benchmark claims: it has automated tests and CI, but it has **not** been load-tested for a specific throughput or user count.

## ✨ What it does

- Registers each Telegram user once with `/start`
- Returns the current unique-user count
- Sends admin notifications for every reached milestone
- Persists pending notifications so transient Telegram failures can be retried
- Restricts `/stats` to the configured admin
- Uses Redis for optional rate limiting and 60-second stats caching
- Supports SQLite for local use and PostgreSQL through Docker Compose
- Runs the application container as an unprivileged user on a read-only filesystem

## Commands

| Command | Access | Behavior |
|---|---|---|
| `/start` | Everyone | Register once or show the current count |
| `/stats` | Admin only | Show total users and the most recently delivered milestone |
| `/help` | Everyone | Show available commands |

## 🚀 Local setup

Requirements: Python 3.11 or newer.

```bash
git clone https://github.com/dk3yyyy/user_count.git
cd user_count
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set at least these values in `.env`:

```dotenv
BOT_TOKEN=replace_with_botfather_token
ADMIN_ID=replace_with_numeric_telegram_user_id
```

The default database is local SQLite. Redis is optional; leave `REDIS_URL` unset to run without distributed rate limiting and stats caching.

```bash
python main.py
```

Startup validates all required and numeric settings. Invalid configuration exits before polling begins.

## 🐳 Docker Compose

Compose runs one bot poller with PostgreSQL and authenticated Redis.

```bash
cp .env.example .env
```

Replace the Telegram credentials and service passwords in `.env`. Set each
`*_PASSWORD_URLENCODED` value to the percent-encoded form of its matching raw
password; this keeps characters such as `@`, `:`, `/`, and `#` valid inside
connection URLs. For example:

```bash
python -c "from urllib.parse import quote; print(quote('your password', safe=''))"
```

Then validate and start the stack:

```bash
docker compose config --quiet
docker compose up --build -d
docker compose logs -f bot
```

PostgreSQL and Redis have health checks. The bot starts only after both dependencies report healthy.

Stop the stack without deleting database data:

```bash
docker compose down
```

To also delete PostgreSQL and Redis volumes, explicitly run `docker compose down -v`. This is destructive.

## ⚙️ Configuration

| Variable | Default | Required | Purpose |
|---|---:|:---:|---|
| `BOT_TOKEN` | — | Yes | Token issued by BotFather |
| `ADMIN_ID` | — | Yes | Positive numeric Telegram user ID |
| `DATABASE_URL` | `sqlite+aiosqlite:///users.db` | No | SQLAlchemy async database URL |
| `REDIS_URL` | unset | No | Redis URL for rate limiting and stats caching |
| `NOTIFICATION_INTERVAL` | `500` | No | Positive milestone interval |
| `RATE_LIMIT_REQUESTS` | `100` | No | Positive requests allowed per window |
| `RATE_LIMIT_SECONDS` | `60` | No | Positive rate-limit window in seconds |
| `POSTGRES_USER` | `botuser` | Compose | PostgreSQL user |
| `POSTGRES_PASSWORD` | — | Compose | PostgreSQL password; no insecure default |
| `POSTGRES_PASSWORD_URLENCODED` | — | Compose | Percent-encoded form of the PostgreSQL password for the connection URL |
| `REDIS_PASSWORD` | — | Compose | Redis password; no insecure default |
| `REDIS_PASSWORD_URLENCODED` | — | Compose | Percent-encoded form of the Redis password for the connection URL |

Examples:

```dotenv
# Local SQLite
DATABASE_URL=sqlite+aiosqlite:///users.db

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/telegram_bot

# Optional Redis
REDIS_URL=redis://:password@host:6379/0
```

Do not commit `.env` or real credentials.

## 🧱 Data and failure behavior

### Tables

- `users`: unique Telegram ID, display name, registration time
- `admin_messages`: most recently delivered milestone message
- `milestone_notifications`: durable pending/sent/superseded milestone records and attempt counts

### Registration and milestones

1. A unique user and every newly reached milestone are committed in one database transaction.
2. Startup reconciles the current user count into any missing milestone rows before delivery.
3. Existing `admin_messages.last_count` state is used as the upgrade baseline, preventing historical milestones from being resent when the outbox table is introduced.
4. Pending notifications are sent in milestone order, stopping at the first failed send.
5. A successful notification is marked sent.
6. Stale pending rows at or below the delivered admin milestone are marked superseded instead of replacing newer state.
7. The previous admin message is deleted only after its replacement is delivered.

A Telegram send failure increments the attempt count and leaves the row pending. Pending rows are retried on application startup and after subsequent `/start` requests.

### Redis failure

Redis is optional. If it is unavailable, the bot logs a warning and continues without distributed rate limiting or stats caching. Database-backed registration and milestones continue to work.

### Database failure

The bot does **not** silently fall back to another database. Startup database failures stop initialization; handler-time failures are logged and return a generic user-facing error.

## 🧪 Development and verification

Install the exact development toolchain:

```bash
pip install -r requirements-dev.txt
```

Run the same gates used by CI:

```bash
python -m compileall -q main.py
ruff check .
ruff format --check .
mypy
pytest -q
bandit -q -r main.py
pip-audit --requirement requirements.txt
```

CI runs these checks on Python 3.11, 3.12, and 3.13. Dependabot checks Python, Docker, and GitHub Actions dependencies weekly.

The test suite covers:

- `/stats` before any milestone exists
- Required and numeric configuration validation
- Gap-safe, idempotent milestone claims
- Atomic registration and milestone claims
- Startup reconciliation and legacy milestone baselines
- Ordered delivery that stops after the first failed milestone
- Failed notification retention and retry
- Redis password escaping, config permissions, and argv secrecy
- End-to-end registration and milestone delivery

## 📐 Deployment boundaries

- Run **one long-polling process per bot token**. Multiple `getUpdates` pollers for the same token are not a supported scaling strategy.
- The in-process notification lock prevents duplicate sends inside one process. The durable primary key prevents duplicate milestone claims in the database.
- Telegram delivery cannot be exactly-once: a process crash after Telegram accepts a message but before the database commit can cause a retry and duplicate message.
- There is no bot HTTP health endpoint, Prometheus integration, or published performance benchmark.
- For horizontal scaling, first move from long polling to a webhook architecture with centralized idempotency and distributed delivery locking, then benchmark the actual target workload.

## Project files

```text
.github/workflows/ci.yml  Automated quality and security gates
.github/dependabot.yml    Dependency update policy
main.py                   Bot, persistence, and handlers
tests/                    Regression and behavior tests
requirements.txt          Runtime dependencies
requirements-dev.txt      Test and quality dependencies
Dockerfile                Non-root application image
docker-compose.yml        Bot, PostgreSQL, and Redis stack
docker/redis-entrypoint.sh Safely escaped Redis password configuration
.env.example              Safe configuration template
LICENSE                   MIT license
```

## License

[MIT](LICENSE) © 2026 Joshua Nwachinemere
