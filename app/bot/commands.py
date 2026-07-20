import logging
from typing import Protocol

from aiogram.types import (
    BotCommand,
    BotCommandScope,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from app.config import Settings
from app.messages import message


logger = logging.getLogger(__name__)


USER_COMMANDS = [
    BotCommand(command="start", description=message("command.start")),
]

ADMIN_COMMANDS = [
    *USER_COMMANDS,
    BotCommand(command="tariff_set", description=message("command.tariff_set")),
    BotCommand(command="grant_access", description=message("command.grant_access")),
]


class BotCommandsSetter(Protocol):
    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: BotCommandScope,
    ) -> object:
        ...


async def setup_bot_commands(bot: BotCommandsSetter, settings: Settings) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    logger.info("Installed private chat bot commands: count=%s", len(USER_COMMANDS))

    await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands([], scope=BotCommandScopeAllChatAdministrators())
    logger.info("Cleared group bot command scopes")

    for admin_id in settings.admin_ids:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
    logger.info("Installed admin bot command scopes: admin_id_count=%s", len(settings.admin_ids))
