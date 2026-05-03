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


def init_telegram_bot():
    logger.info("Initializing Telegram Bot...")
    application = ApplicationBuilder().token(TELEGRAM_INTRADAY_TOKEN).build()

    # Register all commands via the router
    register_all(application)

    # Schedule the LLM budget alert job every 15 minutes
    job_queue = application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            job_llm_budget_alert,
            interval=900,   # 15 minutes
            first=300,      # first check 5 minutes after startup
        )
        logger.info("LLM budget alert job scheduled (every 15 min)")

    application.run_polling()

