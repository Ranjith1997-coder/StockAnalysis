"""Command router — collects HANDLERS from all modules and registers them."""
from __future__ import annotations

from telegram.ext import Application, CommandHandler

from . import account, debug, loglevel, market, paper_trading_cmds, stats, sysstats, system


def register_all(application: Application) -> None:
    """Register every command handler from all command modules."""
    for module in (account, market, system, debug, paper_trading_cmds, stats, sysstats, loglevel):
        for command_name, handler_fn in module.HANDLERS:
            application.add_handler(CommandHandler(command_name, handler_fn))
