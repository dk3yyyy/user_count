import asyncio
import logging
import os
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from telegram import Message, Update
from telegram import User as TelegramUser
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

redis_module: Any | None = None
try:
    import redis.asyncio as redis_module

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Load environment variables
load_dotenv()


def _positive_int(
    values: Mapping[str, str], name: str, default: str | None = None
) -> int:
    raw_value = values.get(name, default)
    if raw_value is None:
        raise ValueError(f"{name} is required")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    database_url: str = "sqlite+aiosqlite:///users.db"
    redis_url: str | None = None
    notification_interval: int = 500
    rate_limit_requests: int = 100
    rate_limit_seconds: int = 60

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "Settings":
        bot_token = values.get("BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("BOT_TOKEN is required")
        admin_id = _positive_int(values, "ADMIN_ID")
        database_url = values.get(
            "DATABASE_URL", "sqlite+aiosqlite:///users.db"
        ).strip()
        if not database_url:
            raise ValueError("DATABASE_URL must not be empty")
        redis_url = values.get("REDIS_URL", "").strip() or None
        return cls(
            bot_token=bot_token,
            admin_id=admin_id,
            database_url=database_url,
            redis_url=redis_url,
            notification_interval=_positive_int(values, "NOTIFICATION_INTERVAL", "500"),
            rate_limit_requests=_positive_int(values, "RATE_LIMIT_REQUESTS", "100"),
            rate_limit_seconds=_positive_int(values, "RATE_LIMIT_SECONDS", "60"),
        )


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Runtime configuration is populated only after validation in main().
BOT_TOKEN: str | None = None
ADMIN_ID: int | None = None
NOTIFICATION_INTERVAL = 500
DATABASE_URL = "sqlite+aiosqlite:///users.db"
REDIS_URL: str | None = None
RATE_LIMIT_REQUESTS = 100
RATE_LIMIT_SECONDS = 60


def configure(settings: Settings) -> None:
    """Apply previously validated settings to runtime configuration."""
    global BOT_TOKEN, ADMIN_ID, NOTIFICATION_INTERVAL
    global DATABASE_URL, REDIS_URL, RATE_LIMIT_REQUESTS, RATE_LIMIT_SECONDS

    BOT_TOKEN = settings.bot_token
    ADMIN_ID = settings.admin_id
    DATABASE_URL = settings.database_url
    REDIS_URL = settings.redis_url
    NOTIFICATION_INTERVAL = settings.notification_interval
    RATE_LIMIT_REQUESTS = settings.rate_limit_requests
    RATE_LIMIT_SECONDS = settings.rate_limit_seconds


# Global objects
redis_client: Any | None = None
engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None
notification_lock = asyncio.Lock()


# Database models
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(nullable=True)
    registered_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class AdminMessage(Base):
    __tablename__ = "admin_messages"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    message_id: Mapped[int | None] = mapped_column(nullable=True)
    last_count: Mapped[int] = mapped_column(default=0)
    last_updated: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class MilestoneNotification(Base):
    __tablename__ = "milestone_notifications"

    milestone_count: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    message_id: Mapped[int | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)


async def init_database() -> None:
    """Initialize database engine and create tables."""
    global engine, AsyncSessionLocal

    # For SQLite, don't use connection pooling
    if DATABASE_URL.startswith("sqlite"):
        new_engine = create_async_engine(DATABASE_URL, echo=False)
    else:
        # For PostgreSQL, use connection pooling
        new_engine = create_async_engine(
            DATABASE_URL, echo=False, pool_size=20, max_overflow=40
        )
    engine = new_engine

    AsyncSessionLocal = async_sessionmaker(
        new_engine, class_=AsyncSession, expire_on_commit=False
    )

    async with new_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")


async def init_redis() -> None:
    """Initialize Redis connection."""
    global redis_client
    if not REDIS_URL:
        logger.info("Redis is not configured; rate limiting and caching are disabled")
        return
    if not REDIS_AVAILABLE:
        logger.warning("Redis not available. Caching and rate limiting disabled.")
        return

    if redis_module is None:
        logger.warning("Redis package is unavailable; rate limiting is disabled")
        return

    try:
        new_client = redis_module.from_url(REDIS_URL, decode_responses=True)
        await new_client.ping()
        redis_client = new_client
        logger.info("Redis connected")
    except Exception as exc:  # noqa: BLE001 - optional external service
        logger.warning(
            "Redis connection failed (%s). Continuing without cache.",
            type(exc).__name__,
        )
        redis_client = None


async def close_database() -> None:
    """Close database engine."""
    if engine:
        await engine.dispose()
        logger.info("Database closed")


async def close_redis() -> None:
    """Close Redis connection."""
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None
        logger.info("Redis closed")


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def _user_and_message(update: Update) -> tuple[TelegramUser, Message]:
    if update.effective_user is None or update.message is None:
        raise ValueError("This handler requires a user message update")
    return update.effective_user, update.message


def rate_limit(func: Handler) -> Handler:
    """Decorator for rate limiting per user."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = _user_and_message(update)
        user_id = user.id
        if redis_client:
            key = f"rate_limit:{user_id}"
            try:
                requests = await redis_client.incr(key)
                if requests == 1:
                    await redis_client.expire(key, RATE_LIMIT_SECONDS)
                if requests > RATE_LIMIT_REQUESTS:
                    await message.reply_text(
                        "⚠️ You're sending too many requests. Please slow down."
                    )
                    return
            except Exception as exc:  # noqa: BLE001 - optional external service
                logger.warning("Rate limit check failed (%s)", type(exc).__name__)
        await func(update, context)

    return wrapper


async def get_user_count(session: AsyncSession) -> int:
    """Get total user count from database."""
    result = await session.execute(select(func.count(User.id)))
    return result.scalar() or 0


async def claim_due_milestones(
    session: AsyncSession, user_count: int, interval: int
) -> list[int]:
    """Stage newly reached milestones; the caller owns the transaction."""
    if interval <= 0:
        raise ValueError("notification interval must be greater than zero")

    result = await session.execute(
        select(func.max(MilestoneNotification.milestone_count))
    )
    outbox_last_count = result.scalar() or 0
    legacy_admin_message = await session.get(AdminMessage, 1)
    legacy_last_count = (
        legacy_admin_message.last_count if legacy_admin_message is not None else 0
    )
    last_claimed = max(outbox_last_count, legacy_last_count)
    next_milestone = ((last_claimed // interval) + 1) * interval
    claimed: list[int] = []

    for milestone in range(next_milestone, user_count + 1, interval):
        try:
            async with session.begin_nested():
                session.add(MilestoneNotification(milestone_count=milestone))
                await session.flush()
            claimed.append(milestone)
        except IntegrityError:
            # Another concurrent transaction claimed this milestone first.
            continue

    return claimed


async def deliver_pending_notifications(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Any,
    admin_id: int,
) -> list[int]:
    """Deliver pending milestone rows and retain failures for a later retry."""
    delivered: list[int] = []
    async with session_factory() as session:
        result = await session.execute(
            select(MilestoneNotification)
            .where(MilestoneNotification.status == "pending")
            .order_by(MilestoneNotification.milestone_count)
        )
        notifications = result.scalars().all()

        for notification in notifications:
            admin_message = await session.get(AdminMessage, 1)
            if (
                admin_message is not None
                and notification.milestone_count <= admin_message.last_count
            ):
                notification.status = "superseded"
                notification.last_error = None
                await session.commit()
                continue

            notification.attempts += 1
            try:
                message = await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🎉 Milestone reached!\n"
                        f"User count: {notification.milestone_count:,}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - external API boundary
                notification.last_error = type(exc).__name__[:100]
                await session.commit()
                logger.warning(
                    "Milestone %s delivery failed (attempt %s)",
                    notification.milestone_count,
                    notification.attempts,
                )
                break

            previous_message_id = (
                admin_message.message_id if admin_message is not None else None
            )
            if admin_message is None:
                admin_message = AdminMessage(
                    id=1,
                    message_id=message.message_id,
                    last_count=notification.milestone_count,
                    last_updated=datetime.now(UTC),
                )
                session.add(admin_message)
            else:
                admin_message.message_id = message.message_id
                admin_message.last_count = notification.milestone_count
                admin_message.last_updated = datetime.now(UTC)

            notification.status = "sent"
            notification.message_id = message.message_id
            notification.last_error = None
            notification.sent_at = datetime.now(UTC)
            await session.commit()
            delivered.append(notification.milestone_count)

            if previous_message_id and previous_message_id != message.message_id:
                try:
                    await bot.delete_message(
                        chat_id=admin_id, message_id=previous_message_id
                    )
                except Exception:  # noqa: BLE001 - replacement was already sent
                    logger.warning(
                        "Could not delete previous admin message %s",
                        previous_message_id,
                    )

    return delivered


async def user_exists(session: AsyncSession, user_id: int) -> bool:
    """Check if user exists in database."""
    result = await session.execute(select(User).filter(User.id == user_id))
    return result.scalars().first() is not None


async def add_user(
    session: AsyncSession, user_id: int, username: str | None = None
) -> bool:
    """Stage a user insert; the caller owns the transaction."""
    dialect_name = session.get_bind().dialect.name
    values = {"id": user_id, "username": username}
    if dialect_name == "postgresql":
        statement = (
            postgresql_insert(User)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[User.id])
            .returning(User.id)
        )
    elif dialect_name == "sqlite":
        statement = (
            sqlite_insert(User)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[User.id])
            .returning(User.id)
        )
    else:
        raise RuntimeError(f"Unsupported database dialect: {dialect_name}")

    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None


@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register a user or welcome an existing user."""
    try:
        user, message = _user_and_message(update)
        user_id = user.id
        user_name = user.first_name or "User"

        if AsyncSessionLocal is None or ADMIN_ID is None:
            raise RuntimeError("Application resources are not initialized")

        async with notification_lock:
            async with AsyncSessionLocal() as session, session.begin():
                is_new = await add_user(session, user_id, user_name)
                user_count = await get_user_count(session)
                await claim_due_milestones(session, user_count, NOTIFICATION_INTERVAL)

            await deliver_pending_notifications(
                AsyncSessionLocal, context.bot, ADMIN_ID
            )

        if is_new:
            logger.info(
                "New user registered: %s (%s). Total: %s",
                user_id,
                user_name,
                user_count,
            )
            await message.reply_text(
                f"Welcome {user_name}! 👋\nYou are user #{user_count:,}"
            )
        else:
            await message.reply_text(f"Welcome back! 👋\nTotal users: {user_count:,}")
            logger.info("Returning user: %s", user_id)

    except Exception:
        logger.exception("Error in start handler")
        if update.message is not None:
            await update.message.reply_text(
                "💔 Something went wrong. Please try again."
            )


@rate_limit
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics to the configured admin."""
    user, message = _user_and_message(update)
    if user.id != ADMIN_ID:
        await message.reply_text("❌ This command is admin-only.")
        return

    try:
        cache_key = "stats_cache"
        if redis_client:
            cached = await redis_client.get(cache_key)
            if cached:
                await message.reply_text(cached)
                return

        if AsyncSessionLocal is None:
            raise RuntimeError("Database is not initialized")
        async with AsyncSessionLocal() as session:
            user_count = await get_user_count(session)
            result = await session.execute(
                select(AdminMessage).filter(AdminMessage.id == 1)
            )
            admin_msg = result.scalars().first()

            last_count = admin_msg.last_count if admin_msg else 0
            last_updated = (
                admin_msg.last_updated.strftime("%Y-%m-%d %H:%M:%S")
                if admin_msg and admin_msg.last_updated
                else "Never"
            )

            stats_text = f"""📊 Bot Statistics:

Total Users: {user_count:,}
Last Milestone: {last_count:,}
Last Updated: {last_updated}"""

            if redis_client:
                await redis_client.setex(cache_key, 60, stats_text)

            await message.reply_text(stats_text)
            logger.info("Sent stats to %s", user.id)
    except Exception:
        logger.exception("Error in stats handler")
        await message.reply_text("📊 Error retrieving statistics.")


@rate_limit
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command - show available commands."""
    help_text = """🤖 Bot Commands:

/start - Register as a user
/stats - View bot statistics (admin only)
/help - Show this help message"""

    user, message = _user_and_message(update)
    await message.reply_text(help_text)
    logger.info("Sent help to %s", user.id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors in the application."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)


async def startup(app: Any) -> None:
    """Initialize database and Redis on startup."""
    await init_database()
    await init_redis()
    if AsyncSessionLocal is None or ADMIN_ID is None:
        raise RuntimeError("Application resources are not initialized")
    async with AsyncSessionLocal() as session, session.begin():
        user_count = await get_user_count(session)
        await claim_due_milestones(session, user_count, NOTIFICATION_INTERVAL)
    async with notification_lock:
        await deliver_pending_notifications(AsyncSessionLocal, app.bot, ADMIN_ID)


async def shutdown(app: Any) -> None:
    """Clean up database and Redis on shutdown."""
    await close_database()
    await close_redis()


def main() -> None:
    """Start the bot."""
    logger.info("Starting Telegram bot...")

    try:
        settings = Settings.from_mapping(os.environ)
    except ValueError as exc:
        logger.error("Invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    configure(settings)

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(startup)
        .post_shutdown(shutdown)
        .build()
    )

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help", help_command))

    # Add error handler
    app.add_error_handler(error_handler)

    logger.info("Bot handlers registered. Starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
