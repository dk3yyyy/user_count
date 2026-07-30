from types import SimpleNamespace

import pytest

import main


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_stats_works_before_first_milestone(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "stats.db"
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setattr(main, "ADMIN_ID", 1)
    monkeypatch.setattr(main, "redis_client", None)

    await main.init_database()
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace()

    try:
        await main.stats(update, context)
    finally:
        await main.close_database()

    assert len(message.replies) == 1
    assert "Total Users: 0" in message.replies[0]
    assert "Last Milestone: 0" in message.replies[0]
    assert "Last Updated: Never" in message.replies[0]
