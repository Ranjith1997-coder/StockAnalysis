"""Command router — collects HANDLERS from all modules and registers them."""
from __future__ import annotations

from telegram.ext import Application, CommandHandler

from notification.commands import account, market, system, debug


def register_all(application: Application) -> None:
    """Register every command handler from all command modules."""
    for module in (account, market, system, debug):
        for command_name, handler_fn in module.HANDLERS:
            application.add_handler(CommandHandler(command_name, handler_fn))
