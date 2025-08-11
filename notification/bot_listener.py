from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from common.constants import TELEGRAM_TOKEN
from  common.logging_util import logger
import common.shared as shared

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, please talk to me!")


async def update_enctoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zerodha_ticker_manager = shared.app_ctx.zd_ticker_manager
    if zerodha_ticker_manager is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Zerodha Ticker Manager is not initialized.")
        return

    if zerodha_ticker_manager.connected:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Zerodha Ticker Manager is already connected.")
        return


    if len(context.args) < 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Please provide the 2FA code.")
        return
    enc_token = context.args[0]

    # import pdb; pdb.set_trace()  # Debugging breakpoint
    
    # if not zerodha_ticker_manager.refresh_enctoken(two_fa_code):
    #     await context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to refresh encToken. Please check your credentials and try again.")
    #     return
    zerodha_ticker_manager.update_enctoken(enc_token)
    
    if not zerodha_ticker_manager.connect():
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to connect to Zerodha Ticker")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Connected to Zerodha Ticker")


def init_telegram_bot():
    logger.info("Initializing Telegram Bot...")
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    start_handler = CommandHandler('start', start)
    caps_handler = CommandHandler('enctoken', update_enctoken)
    application.add_handler(start_handler)
    application.add_handler(caps_handler)
    
    application.run_polling()

