from aiogram.types import (
    BotCommand,
    BotCommandScope,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from app.bot.commands import ADMIN_COMMANDS, USER_COMMANDS, setup_bot_commands


def _command_names(commands: list[BotCommand]) -> list[str]:
    return [command.command for command in commands]


def test_user_and_admin_command_lists() -> None:
    user_names = _command_names(USER_COMMANDS)
    admin_names = _command_names(ADMIN_COMMANDS)

    assert user_names == ["start"]
    assert admin_names[: len(user_names)] == user_names
    assert admin_names[len(user_names) :] == ["tariff_set", "grant_access"]


def test_tariff_set_command_description_shows_short_format() -> None:
    tariff_set_command = next(command for command in ADMIN_COMMANDS if command.command == "tariff_set")

    assert tariff_set_command.description == 'Создать тариф: код "Название" цена дни'
    assert len(tariff_set_command.description) <= 256


async def test_setup_bot_commands_sets_private_group_and_admin_scopes() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[tuple[list[BotCommand], BotCommandScope]] = []

        async def set_my_commands(
            self,
            commands: list[BotCommand],
            scope: BotCommandScope,
        ) -> None:
            self.calls.append((commands, scope))

    class FakeSettings:
        admin_ids = [111, 222]

    bot = FakeBot()

    await setup_bot_commands(bot, FakeSettings())  # type: ignore[arg-type]

    assert len(bot.calls) == 5
    assert isinstance(bot.calls[0][1], BotCommandScopeAllPrivateChats)
    assert _command_names(bot.calls[0][0]) == _command_names(USER_COMMANDS)
    assert isinstance(bot.calls[1][1], BotCommandScopeAllGroupChats)
    assert bot.calls[1][0] == []
    assert isinstance(bot.calls[2][1], BotCommandScopeAllChatAdministrators)
    assert bot.calls[2][0] == []
    assert [call[1].chat_id for call in bot.calls[3:]] == [111, 222]
    assert all(_command_names(call[0]) == _command_names(ADMIN_COMMANDS) for call in bot.calls[3:])


async def test_setup_bot_commands_allows_empty_admin_ids() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[tuple[list[BotCommand], BotCommandScope]] = []

        async def set_my_commands(
            self,
            commands: list[BotCommand],
            scope: BotCommandScope,
        ) -> None:
            self.calls.append((commands, scope))

    class FakeSettings:
        admin_ids: list[int] = []

    bot = FakeBot()

    await setup_bot_commands(bot, FakeSettings())  # type: ignore[arg-type]

    assert len(bot.calls) == 3
    assert isinstance(bot.calls[0][1], BotCommandScopeAllPrivateChats)
    assert _command_names(bot.calls[0][0]) == _command_names(USER_COMMANDS)
    assert isinstance(bot.calls[1][1], BotCommandScopeAllGroupChats)
    assert bot.calls[1][0] == []
    assert isinstance(bot.calls[2][1], BotCommandScopeAllChatAdministrators)
    assert bot.calls[2][0] == []
