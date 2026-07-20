"""Unified logging for the whole bot.

One logger, three sinks: **console**, a **size-capped rotating file**, and a
**Discord channel**. Import it anywhere and use it the same way::

    from botlog import log
    log.info("[DB] migrated to v3")
    log.warning("[Sync] outbox #%s failed: %s", row_id, err)
    log.exception("[Webhook] handler blew up")   # inside an except: adds traceback

Setup is two calls, both made from ``main.py``:

* ``setup_logging()`` — once at startup: wires console + rotating file.
* ``attach_discord(bot)`` — once the bot is ready: adds the Discord handler and
  starts its background flush task.

Design notes:
* The Discord handler never blocks and never raises into caller code — records
  are queued and a background task batches them to the channel (Discord has a
  ~2000 char limit and rate limits, so we coalesce lines).
* ``emit`` only appends to a deque, so it is safe to call from worker threads
  (e.g. aiosqlite) as well as the event loop.
* Console/file capture DEBUG when ``DEBUGGING`` is set (verbose troubleshooting);
  the Discord channel defaults to INFO to stay readable.
"""

from __future__ import annotations

import collections
import logging
import os
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "bot"
TIMECARD_LOGGER_NAME = "bot.timecard"
_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Two loggers the codebase shares:
#   log          -> general bot/infra/webhook events  -> BOT_LOG_ID on Discord
#   timecard_log -> timecard activity (clock-ins, job starts, punch edits,
#                   Odoo sync/reconcile)              -> TIMECARD_LOG_ID on Discord
# timecard_log is a child of log, so console + file capture BOTH; only the
# Discord routing differs.
log = logging.getLogger(LOGGER_NAME)
timecard_log = logging.getLogger(TIMECARD_LOGGER_NAME)

_discord_handlers: list["DiscordChannelHandler"] = []
_configured = False


class _ExcludeTimecard(logging.Filter):
    """Keep timecard records out of the general (BOT_LOG_ID) Discord handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(TIMECARD_LOGGER_NAME)


def get_logger(component: str | None = None) -> logging.Logger:
    """Optional namespaced child logger (e.g. get_logger('sync'))."""
    return log.getChild(component) if component else log


def _level_from_env(var: str, default: int) -> int:
    name = os.getenv(var)
    if not name:
        return default
    return getattr(logging, name.upper(), default)


def setup_logging() -> logging.Logger:
    """Configure console + rotating file handlers. Idempotent."""
    global _configured
    if _configured:
        return log

    verbose = bool(os.getenv("DEBUGGING"))
    console_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(_FMT, _DATEFMT)

    log.setLevel(logging.DEBUG)  # capture everything; handlers decide what to show
    log.propagate = False

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    log.addHandler(console)

    log_file = os.getenv("LOG_FILE", "logs/bot.log")
    try:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),  # 5 MB
            backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)  # the file is the verbose record
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except Exception as e:  # noqa: BLE001 - never let logging setup crash the bot
        console.handle(logging.LogRecord(
            LOGGER_NAME, logging.WARNING, __file__, 0,
            "File logging disabled: %s", (e,), None))

    _configured = True
    log.info("[Log] Logging initialized (console=%s, file=%s).",
             logging.getLevelName(console_level), os.getenv("LOG_FILE", "logs/bot.log"))
    return log


class DiscordChannelHandler(logging.Handler):
    """Queues log lines and flushes them to a Discord channel in batches."""

    def __init__(self, level: int):
        super().__init__(level)
        self._queue: collections.deque[str] = collections.deque(maxlen=2000)
        self.bot = None
        self.channel_id: int | None = None
        self._task = None

    def emit(self, record: logging.LogRecord):
        # Only enqueue; the async flush task does the actual sending. Never raise.
        try:
            self._queue.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass

    def attach(self, bot, channel_id: int):
        self.bot = bot
        self.channel_id = channel_id

    async def run(self):
        import asyncio

        interval = float(os.getenv("LOG_DISCORD_FLUSH_SECONDS", "3"))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._flush()
            except asyncio.CancelledError:
                await self._flush()
                raise
            except Exception:  # noqa: BLE001 - keep the flusher alive no matter what
                pass

    async def _flush(self):
        if not self._queue or self.bot is None or self.channel_id is None:
            return
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            return  # channel not in cache yet; try again next tick

        # Coalesce lines into a single code block under Discord's 2000-char cap.
        lines, length = [], 0
        while self._queue and length < 1800:
            nxt = self._queue[0]
            if lines and length + len(nxt) + 1 > 1800:
                break
            self._queue.popleft()
            lines.append(nxt[:1800])
            length += len(nxt) + 1
        if not lines:
            return
        body = "\n".join(lines)
        try:
            await channel.send(f"```\n{body}\n```")
        except Exception:  # noqa: BLE001 - a failed send must not kill logging
            pass


def _parse_channel(*env_vars) -> int | None:
    for var in env_vars:
        raw = os.getenv(var)
        if raw:
            try:
                return int(raw)
            except ValueError:
                log.warning("[Log] %s is not a valid channel id; ignoring.", var)
    return None


def attach_discord(bot):
    """Add the Discord channel handlers and start their flush tasks. Call when ready.

    Routes general/infra logs to BOT_LOG_ID and timecard activity logs to
    TIMECARD_LOG_ID (falling back to the general channel).
    """
    global _discord_handlers
    if _discord_handlers:
        return _discord_handlers

    import asyncio

    level = _level_from_env("LOG_DISCORD_LEVEL", logging.INFO)
    fmt = logging.Formatter("[%(levelname)s] %(message)s")

    general_id = _parse_channel("BOT_LOG_ID")
    timecard_id = _parse_channel("TIMECARD_LOG_ID") or general_id

    # General handler on the root logger; excludes timecard records so they
    # don't double-post when the two channels differ.
    if general_id is not None:
        gh = DiscordChannelHandler(level)
        gh.setFormatter(fmt)
        gh.addFilter(_ExcludeTimecard())
        gh.attach(bot, general_id)
        log.addHandler(gh)
        gh._task = asyncio.create_task(gh.run())
        _discord_handlers.append(gh)
        log.info("[Log] Discord general logging -> channel %s.", general_id)

    # Timecard handler attached directly to the timecard logger.
    if timecard_id is not None:
        th = DiscordChannelHandler(level)
        th.setFormatter(fmt)
        th.attach(bot, timecard_id)
        timecard_log.addHandler(th)
        th._task = asyncio.create_task(th.run())
        _discord_handlers.append(th)
        log.info("[Log] Discord timecard logging -> channel %s.", timecard_id)

    if not _discord_handlers:
        log.info("[Log] No BOT_LOG_ID/TIMECARD_LOG_ID set; Discord logging disabled.")
    return _discord_handlers
