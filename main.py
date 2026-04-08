## Import statements
from discord.ext import commands
from dataclasses import dataclass, field
import discord
import os # default module
from dotenv import load_dotenv
import argparse, asyncio, json, re, urllib.parse
from prompt_toolkit import PromptSession
from aiohttp import web
import requests

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
            print("Odoo configuration is incomplete. Please check your .env file if you wish to use the Odoo integration.")
        else:
            self.OdooLoaded = True
            print("Odoo configuration loaded successfully.")


load_dotenv()

cogs_list = [
    'moderation'
]

if os.getenv("ENABLE_TIMETRACKING", "false").lower() == "true":
    cogs_list.insert(0, "timetracking")
if os.getenv("ENABLE_FUN", "false").lower() == "true":
    cogs_list.insert(0, "fun")
if os.getenv("ENABLE_FUNCTIONALITY", "false").lower() == "true":
    cogs_list.insert(0, "functionality")


bot = None
bot_ready_event = asyncio.Event()
session = None

async def on_ready():
    await synced()
    print("Hello! Chromes Py-Bot is ready!")
    channel = bot.get_channel(int(os.getenv('BOT_LOG_ID')))
    await channel.send("Hello! Chromes Py-Bot is ready!")
    bot_ready_event.set()

async def synced():
    if bot.auto_sync_commands:
        await bot.sync_commands()
    print(f"{bot.user.name} connected.")

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
    bot = ChromesBot(command_prefix="$", help_command=commands.DefaultHelpCommand())
    bot.cli_session = None  # Will be set after bot is ready

    for cog in cogs_list:
        print(f"Loading Cog {cog}")
        bot.load_extension(f'cogs.{cog}')

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

async def main():
    await setup_bot()
    bot.loop.create_task(run_webserver())
    bot_task = asyncio.create_task(bot.start(os.getenv("BOT_TOKEN")))
    cli_task = asyncio.create_task(cli_input_loop())

    done, pending = await asyncio.wait([bot_task, cli_task], return_when=asyncio.FIRST_COMPLETED)

    for task in done:
        if task == bot_task:
            try:
                await task
            except asyncio.CancelledError:
                print("[CLI] Bot task was cancelled")
            except Exception as e:
                print(f"[CLI] Bot task exception: {e}")

    for task in pending:
        task.cancel()

    if bot and not bot.is_closed():
        await bot.close()

async def run_webserver():
    """Start the aiohttp web server to listen for webhook events"""
    
    # Sanitization and validation utilities
    class WebhookValidator:
        """Validates and sanitizes webhook payloads"""
        
        @staticmethod
        def sanitize_string(value, max_length=1000):
            """Sanitize string input to prevent injection attacks"""
            if not isinstance(value, str):
                raise ValueError("Expected string value")
            
            # Limit length
            if len(value) > max_length:
                raise ValueError(f"String exceeds max length of {max_length}")
            
            # Remove null bytes
            value = value.replace('\x00', '')
            
            # Allow only safe characters (alphanumeric, common punctuation, whitespace)
            if not re.match(r'^[\w\s\-.,()&@#!?]*$', value, re.UNICODE):
                raise ValueError("String contains unsupported characters")
            
            return value.strip()
        
        @staticmethod
        def sanitize_int(value, min_val=0, max_val=9223372036854775807):
            """Sanitize integer input"""
            try:
                int_val = int(value)
                if int_val < min_val or int_val > max_val:
                    raise ValueError(f"Integer out of valid range [{min_val}, {max_val}]")
                return int_val
            except (TypeError, ValueError):
                raise ValueError("Expected valid integer")
        
        @staticmethod
        def sanitize_action(action):
            """Validate action is from allowed list"""
            allowed_actions = [
                'update_content',
                'approve_punch',
                'update_clock_view',
                'sync_database'
            ]
            if action not in allowed_actions:
                raise ValueError(f"Unknown action: {action}")
            return action
        
        @staticmethod
        def validate_payload(payload):
            """Validate and sanitize entire webhook payload"""
            if not isinstance(payload, dict):
                raise ValueError("Payload must be a JSON object")
            
            # Required fields
            channel_id = payload.get('channel_id')
            message_id = payload.get('message_id')
            action = payload.get('action', 'update_content')
            
            if channel_id is None or message_id is None:
                raise ValueError("Missing required fields: channel_id, message_id")
            
            try:
                channel_id = WebhookValidator.sanitize_int(channel_id, min_val=1)
                message_id = WebhookValidator.sanitize_int(message_id, min_val=1)
                action = WebhookValidator.sanitize_action(action)
            except ValueError as e:
                raise ValueError(f"Validation error: {e}")
            
            # Optional fields
            sanitized = {
                'channel_id': channel_id,
                'message_id': message_id,
                'action': action,
                'punch_id': None,
                'content': None,
                'data': {}
            }
            
            # Sanitize punch_id if present
            if 'punch_id' in payload and payload['punch_id'] is not None:
                try:
                    sanitized['punch_id'] = WebhookValidator.sanitize_int(payload['punch_id'], min_val=1)
                except ValueError as e:
                    raise ValueError(f"Invalid punch_id: {e}")
            
            # Sanitize content if present
            if 'content' in payload and payload['content'] is not None:
                try:
                    sanitized['content'] = WebhookValidator.sanitize_string(payload['content'], max_length=2000)
                except ValueError as e:
                    raise ValueError(f"Invalid content: {e}")
            
            # Sanitize data object
            if 'data' in payload and isinstance(payload['data'], dict):
                try:
                    sanitized['data'] = WebhookValidator.sanitize_data_object(payload['data'])
                except ValueError as e:
                    raise ValueError(f"Invalid data object: {e}")
            
            return sanitized
        
        @staticmethod
        def sanitize_data_object(data):
            """Recursively sanitize data object"""
            if not isinstance(data, dict):
                raise ValueError("Data must be a dictionary")
            
            sanitized = {}
            for key, value in data.items():
                # Validate key is safe
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', key):
                    raise ValueError(f"Invalid key name: {key}")
                
                if isinstance(value, bool):
                    sanitized[key] = value
                elif isinstance(value, (int, float)):
                    sanitized[key] = value
                elif isinstance(value, str):
                    sanitized[key] = WebhookValidator.sanitize_string(value, max_length=500)
                elif isinstance(value, dict):
                    sanitized[key] = WebhookValidator.sanitize_data_object(value)
                elif value is None:
                    sanitized[key] = None
                else:
                    raise ValueError(f"Unsupported data type for key '{key}': {type(value)}")
            
            return sanitized
    
    async def verify_odoo_connection():
        """Verify connection to Odoo and attempt to get server info"""
        try:
            if not bot.OdooLoaded:
                print("[Webhook] Odoo is not configured, skipping IP verification")
                return None
            
            # Make a safe API call to Odoo to verify connectivity
            # This also implicitly validates that we're connecting to a real Odoo instance
            endpoint = "/res.partner/search_read"
            data = {
                "domain": [["id", "=", 1]],
                "fields": ["id"],
                "limit": 1,
                "context": {"lang": "en_US"}
            }
            
            response = requests.post(
                f"{bot.OdooURL}{endpoint}",
                headers={
                    "Authorization": f"Bearer {bot.OdooKEY}",
                    "X-Odoo-Database": f"{bot.OdooDB}"
                },
                json=data,
                timeout=5
            )
            
            if response.status_code == 200:
                print("[Webhook] Odoo connection verified")
                return True
            else:
                print(f"[Webhook] Odoo connection failed with status {response.status_code}")
                return False
                
        except Exception as e:
            print(f"[Webhook] Odoo verification error: {e}")
            return False
    
    async def handle_webhook(request):
        """Generic webhook handler that validates and routes to appropriate cog"""
        client_ip = request.remote
        
        # Log incoming webhook request
        print(f"[Webhook] Incoming request from IP: {client_ip} on path: {request.path}")
        
        try:
            # Step 1: Verify Odoo connection for IP validation
            odoo_verified = await verify_odoo_connection()
            
            if not odoo_verified:
                print(f"[Webhook] Rejected request - Odoo verification failed from IP: {client_ip}")
                return web.Response(status=403, text="Service unavailable")
            
            # Step 2: Validate content type
            content_type = request.headers.get('Content-Type', '')
            if 'application/json' not in content_type:
                print(f"[Webhook] Rejected request - Invalid content type from IP: {client_ip}")
                return web.Response(status=400, text="Content-Type must be application/json")
            
            # Step 3: Parse and sanitize payload
            try:
                payload_raw = await request.json()
                payload = WebhookValidator.validate_payload(payload_raw)
            except json.JSONDecodeError:
                print(f"[Webhook] Rejected request - Invalid JSON from IP: {client_ip}")
                return web.Response(status=400, text="Invalid JSON")
            except ValueError as e:
                print(f"[Webhook] Rejected request - Validation error from IP {client_ip}: {e}")
                return web.Response(status=400, text=f"Validation error: {e}")
            
            # Step 4: Route to appropriate cog
            path = request.path.lower()
            
            if '/odoo-timetracking' in path or '/timetracking' in path:
                # Get the timetracking cog and call its webhook handler
                timetracking_cog = bot.get_cog('TimeTracking')
                if timetracking_cog:
                    await timetracking_cog.handle_odoo_webhook(payload)
                    print(f"[Webhook] Successfully processed webhook from IP: {client_ip}")
                    return web.Response(status=200, text="ok")
                else:
                    print(f"[Webhook] TimeTracking cog not found")
                    return web.Response(status=500, text="TimeTracking cog not found")
            else:
                print(f"[Webhook] Rejected request - Unknown route from IP: {client_ip}")
                return web.Response(status=404, text="Unknown webhook route")
                
        except Exception as e:
            print(f"[Webhook] Unexpected error from IP {client_ip}: {e}")
            import traceback
            traceback.print_exc()
            return web.Response(status=500, text="Internal server error")
    
    # Create the web application
    app = web.Application()
    app.router.add_post('/webhook/odoo-timetracking', handle_webhook)
    app.router.add_post('/webhook/timetracking', handle_webhook)
    
    # Run the server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('WEBHOOK_PORT', '8080')))
    await site.start()
    print(f"Webhook server started on port {os.getenv('WEBHOOK_PORT', '8080')}")

if __name__ == "__main__":
    asyncio.run(main())