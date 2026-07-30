from types import SimpleNamespace

import pytest
from sqlalchemy import select

import main


class FakeBot:
    def __init__(
        self,
        *,
        fail_send: bool = False,
        fail_milestones: set[int] | None = None,
    ) -> None:
        self.fail_send = fail_send
        self.fail_milestones = fail_milestones or set()
        self.events: list[tuple[str, int]] = []

    async def send_message(self, *, chat_id: int, text: str):
        milestone = int(text.rsplit(" ", 1)[-1].replace(",", ""))
        self.events.append(("send", milestone))
        if self.fail_send or milestone in self.fail_milestones:
            raise RuntimeError("temporary Telegram failure")
        return SimpleNamespace(message_id=milestone + 10)

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.events.append(("delete", message_id))


@pytest.mark.asyncio
async def test_claim_due_milestones_is_gap_safe_and_idempotent(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "milestones.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    await main.init_database()

    try:
        async with main.AsyncSessionLocal() as session:
            first = await main.claim_due_milestones(session, 1001, 500)
            second = await main.claim_due_milestones(session, 1001, 500)
            rows = (
                (
                    await session.execute(
                        select(main.MilestoneNotification).order_by(
                            main.MilestoneNotification.milestone_count
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await main.close_database()

    assert first == [500, 1000]
    assert second == []
    assert [row.milestone_count for row in rows] == [500, 1000]
    assert all(row.status == "pending" for row in rows)


@pytest.mark.asyncio
async def test_failed_notification_stays_pending_and_retries(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "retry.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    await main.init_database()

    try:
        async with main.AsyncSessionLocal() as session:
            session.add(main.AdminMessage(id=1, message_id=99, last_count=0))
            await session.commit()
            await main.claim_due_milestones(session, 500, 500)
            await session.commit()

        failing_bot = FakeBot(fail_send=True)
        first = await main.deliver_pending_notifications(
            main.AsyncSessionLocal, failing_bot, admin_id=1
        )

        async with main.AsyncSessionLocal() as session:
            pending = await session.get(main.MilestoneNotification, 500)
            assert pending is not None
            assert pending.status == "pending"
            assert pending.attempts == 1

        working_bot = FakeBot()
        second = await main.deliver_pending_notifications(
            main.AsyncSessionLocal, working_bot, admin_id=1
        )

        async with main.AsyncSessionLocal() as session:
            sent = await session.get(main.MilestoneNotification, 500)
            admin = await session.get(main.AdminMessage, 1)
    finally:
        await main.close_database()

    assert first == []
    assert second == [500]
    assert failing_bot.events == [("send", 500)]
    assert working_bot.events == [("send", 500), ("delete", 99)]
    assert sent is not None and sent.status == "sent"
    assert sent.message_id == 510
    assert admin is not None and admin.last_count == 500


@pytest.mark.asyncio
async def test_claim_uses_legacy_admin_milestone_as_baseline(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "legacy.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    await main.init_database()

    try:
        async with main.AsyncSessionLocal() as session:
            session.add(main.AdminMessage(id=1, message_id=10, last_count=1000))
            await session.commit()

            claimed = await main.claim_due_milestones(session, 1500, 500)
    finally:
        await main.close_database()

    assert claimed == [1500]


@pytest.mark.asyncio
async def test_delivery_stops_when_earliest_pending_milestone_fails(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "ordered.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    await main.init_database()

    try:
        async with main.AsyncSessionLocal() as session:
            await main.claim_due_milestones(session, 1000, 500)
            await session.commit()

        bot = FakeBot(fail_milestones={500})
        delivered = await main.deliver_pending_notifications(
            main.AsyncSessionLocal, bot, admin_id=1
        )

        async with main.AsyncSessionLocal() as session:
            first = await session.get(main.MilestoneNotification, 500)
            second = await session.get(main.MilestoneNotification, 1000)
            admin = await session.get(main.AdminMessage, 1)
    finally:
        await main.close_database()

    assert delivered == []
    assert bot.events == [("send", 500)]
    assert first is not None and first.status == "pending"
    assert second is not None and second.status == "pending"
    assert admin is None
