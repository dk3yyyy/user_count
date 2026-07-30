import asyncio
from types import SimpleNamespace

import pytest

import main
from tests.test_milestones import FakeBot


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id: int, first_name: str) -> None:
        self.effective_user = SimpleNamespace(id=user_id, first_name=first_name)
        self.message = FakeMessage()


@pytest.mark.asyncio
async def test_start_claims_and_delivers_reached_milestone(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "start.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setattr(main, "ADMIN_ID", 99)
    monkeypatch.setattr(main, "NOTIFICATION_INTERVAL", 2)
    monkeypatch.setattr(main, "redis_client", None)
    await main.init_database()

    bot = FakeBot()
    context = SimpleNamespace(bot=bot)
    first = FakeUpdate(1, "Ada")
    second = FakeUpdate(2, "Lin")

    try:
        await main.start(first, context)
        await main.start(second, context)
        async with main.AsyncSessionLocal() as session:
            milestone = await session.get(main.MilestoneNotification, 2)
    finally:
        await main.close_database()

    assert first.message.replies == ["Welcome Ada! 👋\nYou are user #1"]
    assert second.message.replies == ["Welcome Lin! 👋\nYou are user #2"]
    assert milestone is not None and milestone.status == "sent"
    assert bot.events == [("send", 2)]


@pytest.mark.asyncio
async def test_start_rolls_back_registration_when_milestone_claim_fails(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "rollback.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setattr(main, "ADMIN_ID", 99)
    monkeypatch.setattr(main, "NOTIFICATION_INTERVAL", 1)
    monkeypatch.setattr(main, "redis_client", None)
    await main.init_database()

    async def fail_claim(*args, **kwargs):
        raise RuntimeError("simulated claim failure")

    monkeypatch.setattr(main, "claim_due_milestones", fail_claim)
    update = FakeUpdate(1, "Ada")

    try:
        await main.start(update, SimpleNamespace(bot=FakeBot()))

        async with main.AsyncSessionLocal() as session:
            user = await session.get(main.User, 1)
    finally:
        await main.close_database()

    assert user is None
    assert update.message.replies == ["💔 Something went wrong. Please try again."]


@pytest.mark.asyncio
async def test_startup_backfills_reached_unclaimed_milestones(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "startup.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setattr(main, "ADMIN_ID", 99)
    monkeypatch.setattr(main, "NOTIFICATION_INTERVAL", 2)
    monkeypatch.setattr(main, "REDIS_URL", None)
    await main.init_database()

    try:
        async with main.AsyncSessionLocal() as session:
            session.add_all([main.User(id=1), main.User(id=2)])
            await session.commit()

        bot = FakeBot()
        await main.startup(SimpleNamespace(bot=bot))

        async with main.AsyncSessionLocal() as session:
            milestone = await session.get(main.MilestoneNotification, 2)
    finally:
        await main.close_database()

    assert milestone is not None and milestone.status == "sent"
    assert bot.events == [("send", 2)]


@pytest.mark.asyncio
async def test_concurrent_starts_do_not_miss_milestone_boundary(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "concurrent.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setattr(main, "ADMIN_ID", 99)
    monkeypatch.setattr(main, "NOTIFICATION_INTERVAL", 2)
    monkeypatch.setattr(main, "redis_client", None)
    await main.init_database()

    bot = FakeBot()
    context = SimpleNamespace(bot=bot)

    try:
        await asyncio.gather(
            main.start(FakeUpdate(1, "Ada"), context),
            main.start(FakeUpdate(2, "Lin"), context),
        )

        async with main.AsyncSessionLocal() as session:
            milestone = await session.get(main.MilestoneNotification, 2)
            user_count = await main.get_user_count(session)
    finally:
        await main.close_database()

    assert user_count == 2
    assert milestone is not None and milestone.status == "sent"
    assert bot.events == [("send", 2)]
