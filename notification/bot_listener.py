from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from common.constants import TELEGRAM_INTRADAY_TOKEN
from  common.logging_util import logger
import common.shared as shared
import os
import urllib.parse

def is_urlencoded(s: str) -> bool:
    decoded = urllib.parse.unquote(s)
    return decoded != s

def update_enctoken_in_env(new_enctoken):
    env_path = ".env"
    lines = []
    updated = False
    # Read existing .env file
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("ZERODHA_ENC_TOKEN="):
                    lines.append(f"ZERODHA_ENC_TOKEN={new_enctoken}\n")
                    updated = True
                else:
                    lines.append(line)
    # If not present, add it
    if not updated:
        lines.append(f"ZERODHA_ENC_TOKEN={new_enctoken}\n")
    # Write back to .env file
    with open(env_path, "w") as f:
        f.writelines(lines)
    # Update in current environment
    os.environ["ZERODHA_ENC_TOKEN"] = new_enctoken
    logger.info(f"Updated ZERODHA_ENC_TOKEN in .env and current environment to {new_enctoken}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, please talk to me!")

async def update_enctoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zerodha_ticker_manager = shared.app_ctx.zd_ticker_manager
    zerodha_kite_connect = shared.app_ctx.zd_kc
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
    
    # if not zerodha_ticker_manager.refresh_enctoken(two_fa_code):
    #     await context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to refresh encToken. Please check your credentials and try again.")
    #     return
    zerodha_ticker_manager.update_enctoken(enc_token)
    if is_urlencoded(enc_token):
        decoded_enctoken = urllib.parse.unquote(enc_token)
    else:
        decoded_enctoken = enc_token
    zerodha_kite_connect.update_enctoken(decoded_enctoken)
    
    if not zerodha_ticker_manager.connect():
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to connect to Zerodha Ticker")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Connected to Zerodha Ticker")
        update_enctoken_in_env(decoded_enctoken)


def init_telegram_bot():
    logger.info("Initializing Telegram Bot...")
    application = ApplicationBuilder().token(TELEGRAM_INTRADAY_TOKEN).build()
    
    start_handler = CommandHandler('start', start)
    caps_handler = CommandHandler('enctoken', update_enctoken)
    application.add_handler(start_handler)
    application.add_handler(caps_handler)
    
    application.run_polling()

