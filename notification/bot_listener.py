from telegram.ext import ApplicationBuilder
from common.constants import TELEGRAM_INTRADAY_TOKEN
from common.logging_util import logger
from notification.commands import register_all
from notification.commands.system import job_llm_budget_alert

# ── Re-exports for backward compatibility (tests and external callers) ────────
from notification.commands.account import (  # noqa: F401
    _subscribe_registered_options,
    _is_urlencoded as is_urlencoded,
    _update_enctoken_in_env as update_enctoken_in_env,
)
from notification.commands._helpers import (  # noqa: F401
    find_stock_by_symbol as _find_stock_by_symbol,
    build_gainers_losers as _build_gainers_losers,
)
from notification.commands.market import (  # noqa: F401
    cmd_ltp, cmd_gainers, cmd_losers, cmd_watchlist, cmd_holidays,
    cmd_straddle, cmd_walls,
)
from notification.commands.system import cmd_help, cmd_status  # noqa: F401

# Module-level reference so the analysis thread can stop the bot cleanly
_application = None


def init_telegram_bot():
    global _application
    logger.info("Initializing Telegram Bot...")
    _application = ApplicationBuilder().token(TELEGRAM_INTRADAY_TOKEN).build()

    # Register all commands via the router
    register_all(_application)

    # Schedule the LLM budget alert job every 15 minutes
    job_queue = _application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            job_llm_budget_alert,
            interval=900,   # 15 minutes
            first=300,      # first check 5 minutes after startup
        )
        logger.info("LLM budget alert job scheduled (every 15 min)")

    _application.run_polling()


def stop_telegram_bot():
    """Stop the bot cleanly from the analysis thread once work is done.

    `stop_running()` is thread-safe — it signals run_polling()'s event loop
    to exit, allowing the main thread (and thus the process) to terminate.
    """
    global _application
    if _application is not None:
        logger.info("[bot] Requesting Telegram bot shutdown...")
        _application.stop_running()

