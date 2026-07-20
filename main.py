## Import statements
from discord.ext import commands
from dataclasses import dataclass, field
import discord
import os # default module
from dotenv import load_dotenv
import argparse, asyncio, json, re, sys, urllib.parse
from prompt_toolkit import PromptSession
from services.webhook import run_webserver
from botlog import log, setup_logging, attach_discord
import config


def _log_unhandled_exception(exc_type, exc_value, exc_tb):
    """Last-resort hook: route any uncaught exception through the logger (so it
    lands in the log file) instead of a bare stderr traceback."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical("[Bot] Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))


def _loop_exception_handler(loop, context):
    """Log exceptions from background tasks that asyncio would otherwise swallow."""
    message = context.get("message", "unhandled exception in event loop")
    log.error("[Bot] Async task error: %s", message, exc_info=context.get("exception"))

class ChromesBot(discord.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cli_session = None  # Will be set after bot is ready
        self.OdooLoaded = False
        self.OdooURL = os.getenv("ODOO_URL", None)
        self.OdooDB = os.getenv("ODOO_DB", None)
        self.OdooUSERNAME = os.getenv("ODOO_USERNAME", None)
        self.OdooKEY = os.getenv("ODOO_API_KEY", None)
        if not all([self.OdooURL, self.OdooDB, self.OdooUSERNAME, self.OdooKEY]):
            log.warning("[Bot] Odoo configuration is incomplete; running without the Odoo integration.")
        else:
            self.OdooLoaded = True
            log.info("[Bot] Odoo configuration loaded successfully.")

# Log any uncaught exception (esp. to the log file) instead of a bare stderr dump.
sys.excepthook = _log_unhandled_exception

#load environment variables from .env file and other configurations
# On first run, create .env from .env.example so the bot can start.
ENV_CREATED = config.ensure_env_file()
load_dotenv()
# Configure logging now that .env is loaded (honors LOG_FILE / DEBUGGING /
# LOG_FILE_LEVEL) and before anything else that can fail, so startup errors --
# including cog import failures -- are captured in the log file.
setup_logging()
# Version + upgrade the .env (add new keys, rename/remove old ones), like the DB.
# Returns the keys added this run so startup can pause for review after an upgrade.
ENV_ADDED_KEYS = config.migrate_env()

# The webhook (server, admin cog, token) exists only for the Odoo/timecard
# integration, so everything webhook-related is gated on timetracking.
TIMETRACKING_ENABLED = os.getenv("ENABLE_TIMETRACKING", "false").lower() == "true"
if TIMETRACKING_ENABLED:
    # Auto-generate the webhook token if it isn't set yet (stored back to .env).
    config.ensure_webhook_token()

cogs_list = [
    'moderation',
]
# enabling and disabling cogs based on environment variables
if TIMETRACKING_ENABLED:
    cogs_list.insert(0, "timetracking")
    cogs_list.insert(0, "webhookadmin")  # webhook admin only when timetracking is on
if os.getenv("ENABLE_FUN", "false").lower() == "true":
    cogs_list.insert(0, "fun")
if os.getenv("ENABLE_FUNCTIONALITY", "false").lower() == "true":
    cogs_list.insert(0, "functionality")
# Test-only helper commands, loaded solely on the test bot.
if config.is_testing():
    cogs_list.insert(0, "testing")

# Global variables
bot = None
bot_ready_event = asyncio.Event()
session = None
bot_task = None  # module-level so cli_shutdown() can cancel it

async def on_ready():
    await synced()
    # Start Discord-channel logging now that the bot (and its channel cache) is up.
    attach_discord(bot)
    log.info("[Bot] Hello! Chromes Py-Bot is ready!")
    bot_ready_event.set()

async def synced():
    if bot.auto_sync_commands:
        await bot.sync_commands()
    log.info("[Bot] %s connected.", bot.user.name)

async def shutdown(ctx):
    # Fetch app info to ensure owner_id is populated
    app_info = await bot.application_info()
    if ctx.author.id != app_info.owner.id:
        await ctx.respond("You are not the owner!")
        return
    await ctx.respond("Shutting down the bot...")
    await cli_shutdown()

async def setup_bot():
    global bot
    # In testing, register commands to the test guild for INSTANT sync (global
    # commands take ~an hour to propagate).
    debug_guilds = None
    if config.is_testing() and config.testing_guild_id():
        debug_guilds = [config.testing_guild_id()]
        log.info("[Bot] TESTING mode: commands scoped to guild %s for instant sync.", debug_guilds[0])
    bot = ChromesBot(command_prefix="$", help_command=commands.DefaultHelpCommand(),
                     debug_guilds=debug_guilds)
    bot.cli_session = None  # Will be set after bot is ready

    failed = []
    for cog in cogs_list:
        log.info("[Bot] Loading cog %s", cog)
        try:
            bot.load_extension(f'cogs.{cog}')
        except Exception:
            # log.exception writes the full traceback to console + file (+ Discord
            # once attached), so a bad cog is recorded, not just dumped to stderr.
            log.exception("[Bot] Failed to load cog '%s'", cog)
            failed.append(cog)
    if failed:
        log.error("[Bot] %d cog(s) failed to load: %s. The bot will run without "
                  "them — see the traceback(s) above (also in the log file).",
                  len(failed), ", ".join(failed))

    bot.add_listener(on_ready, 'on_ready')
    bot.slash_command(description="Shutdown the bot. [Only BOT owner can use this command]")(shutdown)

async def cli_shutdown():
    global bot_task
    if bot_task and not bot_task.done():
        bot_task.cancel()
    if bot and not bot.is_closed():
        await bot.close()

async def cli_input_loop():
    global session
    session = PromptSession()
    bot.cli_session = session  # Store on bot instance for access by cogs
    await bot_ready_event.wait()
    session.output.write("[CLI] running interactive local console. Type 'shutdown' or '/shutdown' to stop the bot.\n")
    while bot and not bot.is_closed():
        try:
            user_input = await session.prompt_async("> ")
            cmd = user_input.strip().lower()
            if cmd in ("shutdown", "/shutdown", "exit", "quit"):
                session.output.write("[CLI] shutting down bot from command line\n")
                await cli_shutdown()
                break
            elif cmd in ("status", "/status"):
                session.output.write("[CLI] bot is running\n")
            elif cmd == "":
                continue
            else:
                session.output.write(f"[CLI] unknown command '{user_input}'\n")
        except EOFError:
            break
        except KeyboardInterrupt:
            break

async def _halt_for_review_if_upgraded() -> bool:
    """Run pending migrations up front (env already done; DB now). If an upgrade or
    first-run creation happened, log what needs review and return True so the caller
    stops before the bot goes live — giving the admin a chance to set new config."""
    review = []
    if ENV_CREATED:
        review.append("a new .env was created from .env.example — fill in your settings (BOT_TOKEN, channel IDs, Odoo, etc.)")
    elif ENV_ADDED_KEYS:
        review.append(f"{len(ENV_ADDED_KEYS)} new .env setting(s) were added, may need values: {', '.join(ENV_ADDED_KEYS)}")

    if TIMETRACKING_ENABLED:
        tt = bot.get_cog("TimeTracking")
        if tt is not None:
            try:
                await tt._open_db()  # back up + migrate now; workers start later
            except Exception:
                log.exception("[Bot] Database initialization/upgrade failed — not starting.")
                return True
            if tt.db_upgraded:
                review.append("the timecard database was upgraded (a timestamped backup was saved next to it)")

    if review:
        log.warning(
            "[Bot] Startup PAUSED after an upgrade so you can review configuration:\n  - %s\n"
            "[Bot] Review the items above (and your .env), then start the bot again.",
            "\n  - ".join(review),
        )
        return True
    return False


async def main():
    global bot_task
    # Catch exceptions from tasks that aren't awaited (webhook server, etc.).
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    await setup_bot()

    # After an upgrade (or first-run .env creation), pause so the admin can review
    # new settings / the migrated database before the bot connects.
    if await _halt_for_review_if_upgraded():
        if bot and not bot.is_closed():
            await bot.close()
        return

    # The webhook server only serves the Odoo/timecard integration.
    if TIMETRACKING_ENABLED:
        bot.loop.create_task(run_webserver(bot))
    else:
        log.info("[Bot] Timetracking disabled — webhook server not started.")
    bot_task = asyncio.create_task(bot.start(os.getenv("BOT_TOKEN")))
    cli_task = asyncio.create_task(cli_input_loop())

    done, pending = await asyncio.wait([bot_task, cli_task], return_when=asyncio.FIRST_COMPLETED)

    for task in done:
        if task == bot_task:
            try:
                await task
            except asyncio.CancelledError:
                log.info("[CLI] Bot task was cancelled")
            except Exception as e:
                log.exception("[CLI] Bot task exception: %s", e)

    for task in pending:
        task.cancel()

    if bot and not bot.is_closed():
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("[Bot] Interrupted by user; shutting down.")
    # Any other uncaught exception is logged by sys.excepthook (_log_unhandled_exception).