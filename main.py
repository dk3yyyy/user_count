import logging
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import select, func, BigInteger
from sqlalchemy.exc import IntegrityError
from functools import wraps

try:
    import redis.asyncio as redis_module
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID')) if os.getenv('ADMIN_ID') else None
NOTIFICATION_INTERVAL = int(os.getenv('NOTIFICATION_INTERVAL', '500'))

# Database configuration
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///users.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# Rate limiting
RATE_LIMIT_REQUESTS = int(os.getenv('RATE_LIMIT_REQUESTS', '100'))
RATE_LIMIT_SECONDS = int(os.getenv('RATE_LIMIT_SECONDS', '60'))

# Global objects
redis_client = None
engine = None
AsyncSessionLocal = None

# Database models
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(nullable=True)
    registered_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))

class AdminMessage(Base):
    __tablename__ = 'admin_messages'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    message_id: Mapped[int] = mapped_column(nullable=True)
    last_count: Mapped[int] = mapped_column(default=0)
    last_updated: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))

async def init_database():
    """Initialize database engine and create tables."""
    global engine, AsyncSessionLocal
    
    # For SQLite, don't use connection pooling
    if DATABASE_URL.startswith('sqlite'):
        engine = create_async_engine(DATABASE_URL, echo=False)
    else:
        # For PostgreSQL, use connection pooling
        engine = create_async_engine(DATABASE_URL, echo=False, pool_size=20, max_overflow=40)
    
    AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")

async def init_redis():
    """Initialize Redis connection."""
    global redis_client
    if not REDIS_AVAILABLE:
        logger.warning("Redis not available. Caching and rate limiting disabled.")
        return
    
    try:
        redis_client = await redis_module.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}. Continuing without cache.")
        redis_client = None

async def close_database():
    """Close database engine."""
    if engine:
        await engine.dispose()
        logger.info("Database closed")

async def close_redis():
    """Close Redis connection."""
    if redis_client:
        await redis_client.close()
        logger.info("Redis closed")

def rate_limit(func):
    """Decorator for rate limiting per user."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if redis_client:
            key = f"rate_limit:{user_id}"
            try:
                requests = await redis_client.incr(key)
                if requests == 1:
                    await redis_client.expire(key, RATE_LIMIT_SECONDS)
                if requests > RATE_LIMIT_REQUESTS:
                    await update.message.reply_text("⚠️ You're sending too many requests. Please slow down.")
                    return
            except Exception as e:
                logger.warning(f"Rate limit check failed: {e}")
        return await func(update, context)
    return wrapper

async def get_user_count(session: AsyncSession) -> int:
    """Get total user count from database."""
    result = await session.execute(select(func.count(User.id)))
    return result.scalar() or 0

async def user_exists(session: AsyncSession, user_id: int) -> bool:
    """Check if user exists in database."""
    result = await session.execute(select(User).filter(User.id == user_id))
    return result.scalars().first() is not None

async def add_user(session: AsyncSession, user_id: int, username: str = None) -> bool:
    """Add new user to database. Returns True if new user was added."""
    try:
        session.add(User(id=user_id, username=username))
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False

async def get_or_create_admin_message(session: AsyncSession):
    """Get or create the singleton admin message record in the given session."""
    result = await session.execute(select(AdminMessage))
    msg = result.scalars().first()
    if not msg:
        msg = AdminMessage(id=1)
        session.add(msg)
    return msg

@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - register new users or welcome returning users."""
    try:
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name or "User"
        
        async with AsyncSessionLocal() as session:
            is_new = await add_user(session, user_id, user_name)
            user_count = await get_user_count(session)
            
            if is_new:
                logger.info(f"New user registered: {user_id} ({user_name}). Total: {user_count}")
                
                # Notify admin at intervals
                if user_count % NOTIFICATION_INTERVAL == 0:
                    try:
                        # Get admin message in the same session
                        admin_msg = await get_or_create_admin_message(session)
                        
                        # Delete previous message if exists
                        if admin_msg.message_id:
                            try:
                                await context.bot.delete_message(chat_id=ADMIN_ID, message_id=admin_msg.message_id)
                                logger.info(f"Deleted previous admin message: {admin_msg.message_id}")
                            except Exception as e:
                                logger.warning(f"Could not delete previous message: {e}")
                        
                        # Send new notification
                        msg = await context.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=f"🎉 Milestone reached!\nUser count: {user_count:,}"
                        )
                        # Update in the same session
                        admin_msg.message_id = msg.message_id
                        admin_msg.last_count = user_count
                        admin_msg.last_updated = datetime.now(timezone.utc)
                        await session.commit()
                        logger.info(f"Notified admin. Message ID: {msg.message_id}")
                    except Exception as e:
                        logger.error(f"Error sending admin notification: {e}")
                
                await update.message.reply_text(f"Welcome {user_name}! 👋\nYou are user #{user_count:,}")
            else:
                await update.message.reply_text(f"Welcome back! 👋\nTotal users: {user_count:,}")
                logger.info(f"Returning user: {user_id}")
    
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        await update.message.reply_text("💔 Something went wrong. Please try again.")

@rate_limit
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command - show bot statistics."""
    # Admin-only access
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ This command is admin-only.")
        return
    
    try:
        # Check cache first
        cache_key = "stats_cache"
        if redis_client:
            cached = await redis_client.get(cache_key)
            if cached:
                await update.message.reply_text(cached)
                return
        
        async with AsyncSessionLocal() as session:
            user_count = await get_user_count(session)
            admin_msg = await get_or_create_admin_message(session)
            
            stats_text = f"""📊 Bot Statistics:

Total Users: {user_count:,}
Last Milestone: {admin_msg.last_count:,}
Last Updated: {admin_msg.last_updated.strftime('%Y-%m-%d %H:%M:%S') if admin_msg.last_updated else 'Never'}"""
            
            # Cache stats for 60 seconds
            if redis_client:
                await redis_client.setex(cache_key, 60, stats_text)
            
            await update.message.reply_text(stats_text)
            logger.info(f"Sent stats to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error in stats handler: {e}")
        await update.message.reply_text("📊 Error retrieving statistics.")

@rate_limit
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command - show available commands."""
    help_text = """🤖 Bot Commands:

/start - Register as a user
/stats - View bot statistics (admin only)
/help - Show this help message"""
    
    await update.message.reply_text(help_text)
    logger.info(f"Sent help to {update.effective_user.id}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors in the application."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

async def startup(app):
    """Initialize database and Redis on startup."""
    await init_database()
    await init_redis()

async def shutdown(app):
    """Clean up database and Redis on shutdown."""
    await close_database()
    await close_redis()

def main():
    """Start the bot."""
    logger.info("Starting Telegram bot...")
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set")
        return
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(startup).post_shutdown(shutdown).build()
    
    # Add handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('help', help_command))
    
    # Add error handler
    app.add_error_handler(error_handler)
    
    logger.info("Bot handlers registered. Starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()