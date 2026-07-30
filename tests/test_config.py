import pytest

import main


def test_settings_reject_missing_bot_token() -> None:
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        main.Settings.from_mapping({"ADMIN_ID": "1"})


def test_settings_reject_empty_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        main.Settings.from_mapping(
            {"BOT_TOKEN": "token", "ADMIN_ID": "1", "DATABASE_URL": ""}
        )


def test_settings_apply_safe_defaults() -> None:
    settings = main.Settings.from_mapping({"BOT_TOKEN": "token", "ADMIN_ID": "123"})

    assert settings.admin_id == 123
    assert settings.database_url == "sqlite+aiosqlite:///users.db"
    assert settings.redis_url is None
    assert settings.notification_interval == 500
    assert settings.rate_limit_requests == 100
    assert settings.rate_limit_seconds == 60


def test_configure_applies_validated_settings() -> None:
    settings = main.Settings(
        bot_token="token",
        admin_id=42,
        database_url="sqlite+aiosqlite:///test.db",
        redis_url=None,
        notification_interval=25,
        rate_limit_requests=7,
        rate_limit_seconds=11,
    )

    main.configure(settings)

    assert main.BOT_TOKEN == "token"
    assert main.ADMIN_ID == 42
    assert main.DATABASE_URL == "sqlite+aiosqlite:///test.db"
    assert main.REDIS_URL is None
    assert main.NOTIFICATION_INTERVAL == 25
    assert main.RATE_LIMIT_REQUESTS == 7
    assert main.RATE_LIMIT_SECONDS == 11


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ADMIN_ID", "not-an-integer"),
        ("NOTIFICATION_INTERVAL", "0"),
        ("RATE_LIMIT_REQUESTS", "-1"),
        ("RATE_LIMIT_SECONDS", "0"),
    ],
)
def test_settings_reject_invalid_integer_values(name: str, value: str) -> None:
    values = {"BOT_TOKEN": "token", "ADMIN_ID": "123", name: value}

    with pytest.raises(ValueError, match=name):
        main.Settings.from_mapping(values)
