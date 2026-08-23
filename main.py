## Import statements
from discord.ext import commands
from dataclasses import dataclass, field
import discord
import os # default module
from dotenv import load_dotenv
import argparse, asyncio, json, logging, re, sys, urllib.parse
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
# In TESTING mode, overlay TESTING_* channel ids onto the live env so the bot uses
# the test guild's channels without touching production's ids in the shared .env.
config.apply_testing_channel_overrides()
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

def _managed_guild():
    """The guild whose managed channels we resolve at startup: the test guild in
    test mode, else the configured primary guild, else the only/first guild."""
    gid = config.testing_guild_id() if config.is_testing() else config.primary_guild_id()
    if gid:
        g = bot.get_guild(gid)
        if g is not None:
            return g
    return bot.guilds[0] if bot.guilds else None


async def on_ready():
    await synced()
    # Resolve (and in test mode create) the managed channels BEFORE attaching the
    # Discord log handlers, which read BOT_LOG_ID/TIMECARD_LOG_ID. Runs once.
    if not getattr(bot, "_channels_resolved", False):
        bot._channels_resolved = True
        try:
            import channelsetup
            guild = _managed_guild()
            if guild is None:
                log.warning("[Setup] Bot isn't in any guild yet; skipping channel resolution.")
            elif not await channelsetup.resolve_channels(guild):
                log.critical("[Bot] Halting: a required channel is missing (see above).")
                await bot.close()
                return
        except Exception:
            # Never let channel setup crash startup — degrade to whatever's configured.
            log.exception("[Setup] Channel resolution failed; continuing with configured ids.")
    # Start Discord-channel logging now that the bot (and its channel cache) is up.
    attach_discord(bot)
    # Test-db bootstrap prompt: post the Yes/No to the (now-resolved) admin channel;
    # the console side + execution happen in the CLI loop (whichever answers wins).
    if getattr(bot, "_needs_test_bootstrap", False) and not getattr(bot, "_bootstrap_posted", False):
        bot._bootstrap_posted = True
        bot._bootstrap_guild = _managed_guild()
        bot._test_decision = asyncio.get_event_loop().create_future()
        try:
            import testboot
            aid = os.getenv("TIMECARD_ADMIN_CHANNEL_ID")
            ch = bot.get_channel(int(aid)) if aid and aid.isdigit() else None
            if ch is not None:
                await ch.send(
                    "**No test database found.** Pull data from **production** (copy + "
                    "sanitize) or start from **scratch**? You can also answer y/n in the "
                    "console. No answer in 5 minutes → scratch.",
                    view=testboot.TestBootstrapView(bot._test_decision))
        except Exception:
            log.exception("[Setup] Could not post the test-bootstrap prompt.")
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
    # Registering a listener also suppresses py-cord's default stderr-only handler,
    # so slash-command errors are logged (to the file) instead of just printed.
    bot.add_listener(on_application_command_error, 'on_application_command_error')
    bot.slash_command(description="Shutdown the bot. [Only BOT owner can use this command]")(shutdown)


async def on_application_command_error(ctx, error):
    """Central handler for slash-command errors: log through our logger (so it
    lands in the log file) and tell the user, instead of py-cord dumping it to
    stderr only."""
    cmd = ctx.command.qualified_name if getattr(ctx, "command", None) else "unknown"
    # Permission / check failures are expected, not crashes: a plain notice and a
    # single info line (no traceback), rather than the generic "something broke".
    # NOTE: py-cord raises discord.errors.CheckFailure when an application-command
    # check predicate returns False (e.g. our is_timecard_admin), which is a
    # DIFFERENT class from discord.ext.commands.CheckFailure (raised by
    # has_permissions et al.) — catch both.
    if isinstance(error, (commands.CheckFailure, discord.CheckFailure)):
        who = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        log.info("[Bot] %s was denied /%s (%s).", who, cmd, type(error).__name__)
        await _notify_command_error(ctx, "You don't have permission to use this command.")
        return
    log.error("[Bot] Error in command '/%s'", cmd, exc_info=error)
    await _notify_command_error(ctx, "Something went wrong running that command — it's been logged.")


async def _notify_command_error(ctx, message):
    """Best-effort ephemeral notice, whether or not the interaction was deferred."""
    try:
        await ctx.respond(message, ephemeral=True)
    except Exception:
        try:
            await ctx.followup.send(message, ephemeral=True)
        except Exception:
            pass

async def cli_shutdown():
    global bot_task
    if bot_task and not bot_task.done():
        bot_task.cancel()
    if bot and not bot.is_closed():
        await bot.close()

async def _run_test_bootstrap_if_needed(session):
    """When the test db is fresh, race a console y/n against the admin-channel
    buttons (and a 5-minute timeout → scratch), then run the chosen bootstrap."""
    if not getattr(bot, "_needs_test_bootstrap", False):
        return
    import testboot
    fut = getattr(bot, "_test_decision", None)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        bot._test_decision = fut
    session.output.write(
        "[Setup] No test database. Pull data from PRODUCTION? Answer y/n here, or use "
        "the buttons in the timecard-admin channel. No answer in 5 min → start from scratch.\n")
    console = asyncio.ensure_future(session.prompt_async("Pull test data from prod? [y/N]: "))
    pull = False
    try:
        done, pending = await asyncio.wait({console, fut}, timeout=300,
                                           return_when=asyncio.FIRST_COMPLETED)
        if console in done and not console.cancelled():
            pull = (console.result() or "").strip().lower() in ("y", "yes")
            if not fut.done():
                fut.set_result(pull)
        elif fut in done:
            pull = bool(fut.result())
        else:
            session.output.write("[Setup] No answer in 5 minutes — starting from scratch.\n")
            if not fut.done():
                fut.set_result(False)
        for t in pending:
            t.cancel()
    except Exception:
        log.exception("[Setup] Bootstrap prompt failed; starting from scratch.")
    bot._needs_test_bootstrap = False
    try:
        await testboot.apply_decision(bot, pull, getattr(bot, "_bootstrap_guild", None))
    except Exception:
        log.exception("[Setup] Test bootstrap execution failed.")


async def cli_input_loop():
    global session
    session = PromptSession()
    bot.cli_session = session  # Store on bot instance for access by cogs
    await bot_ready_event.wait()
    await _run_test_bootstrap_if_needed(session)
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


async def _shutdown_cleanup():
    """Release resources so the process can actually exit: stop the Odoo workers and
    close the db connection (frees the aiosqlite worker thread + the WAL lock), and
    shut down the webhook server. Without this the non-daemon db thread keeps the
    process alive after the loop ends -- the 'still running, still using the db' hang."""
    if bot is None:
        return
    tt = bot.get_cog("TimeTracking")
    if tt is not None:
        try:
            await tt.close_db()  # stops sync+inbox workers and closes the db
        except Exception:
            log.exception("[Bot] Error closing the timecard database during shutdown")
    runner = getattr(bot, "webhook_runner", None)
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception:
            log.exception("[Bot] Error cleaning up the webhook server during shutdown")


async def main():
    global bot_task
    # Catch exceptions from tasks that aren't awaited (webhook server, etc.).
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    try:
        await setup_bot()

        # Decide whether a test-db bootstrap is needed BEFORE the db is opened
        # (opening it would create an empty test db and hide the "fresh" state).
        import testboot
        bot._needs_test_bootstrap = config.is_testing() and not testboot.test_db_present()

        # After an upgrade (or first-run .env creation), pause so the admin can
        # review new settings / the migrated database before the bot connects.
        if await _halt_for_review_if_upgraded():
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
    finally:
        # Runs on every exit path (shutdown, halt-for-review, error).
        if bot and not bot.is_closed():
            await bot.close()
        await _shutdown_cleanup()
        log.info("[Bot] Shutdown complete.")


if __name__ == "__main__":
    _code = 0
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("[Bot] Interrupted by user; shutting down.")
    except Exception:
        log.exception("[Bot] Fatal error during run.")
        _code = 1
    finally:
        # Flush logs, then hard-exit past any lingering non-daemon thread (the
        # prompt_toolkit CLI reader on Windows, an in-flight Odoo request) so the
        # process actually terminates instead of hanging. The db was already closed
        # gracefully above, so this loses nothing.
        logging.shutdown()
        os._exit(_code)