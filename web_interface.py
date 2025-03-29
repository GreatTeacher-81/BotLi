# -*- coding: utf-8 -*-
import eventlet
eventlet.monkey_patch() # MUST BE VERY FIRST after imports that don't conflict

# --- Now other imports can follow ---
import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time 
from enum import Enum
from typing import TypeVar, Any, Callable, Optional

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
# ...

# --- Import Existing BotLi Modules ---
# Ensure these imports work based on your project structure
try:
    from api import API
    from botli_dataclasses import Challenge_Request
    from config import Config
    from engine import Engine
    from enums import Challenge_Color, Perf_Type, Variant
    from event_handler import Event_Handler
    from game_manager import Game_Manager
    from logo import LOGO
except ImportError as e:
    print(f"Error importing BotLi modules: {e}")
    print("Make sure web_interface.py is in the correct directory relative to other BotLi files.")
    sys.exit(1)

# --- Flask & SocketIO Setup ---
app = Flask(__name__)
# Use a strong, random secret key in production, perhaps from environment variables
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', os.urandom(32))
# Use eventlet for async operations, generally robust for this kind of mixed workload
socketio = SocketIO(app, async_mode='eventlet', logger=False, engineio_logger=False)

# --- Global Bot State Dictionary ---
# Holds references to bot components and status flags
bot_context: dict[str, Any] = {
    "config_path": "config.yml", # Default config path
    "config": None,
    "api": None,
    "game_manager": None,
    "event_handler": None,
    "bot_thread": None,
    "bot_loop": None,
    "running": False,
    "username": "N/A",
    "is_matchmaking": False,
    "last_challenge_event": None,
    "allow_upgrade": False, # Bot upgrade handled via CLI usually
    "initial_tournament_id": None,
    "initial_tournament_team": None,
    "initial_tournament_password": None,
    "start_matchmaking_on_boot": False,
    "shutdown_requested": False,
}

# --- Logging Configuration ---
# Redirects Python's standard logging to the web UI via SocketIO
class SocketIOHandler(logging.Handler):
    """Custom logging handler to emit logs via SocketIO."""
    def emit(self, record):
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            # Map log levels to CSS classes/types for the frontend
            log_type_map = {
                'debug': 'debug',
                'info': 'info',
                'warning': 'warning',
                'error': 'error',
                'critical': 'error', # Treat critical as error for UI
                # Add specific types if needed, e.g., from Game Manager
                'game': 'game',
                'challenge': 'challenge',
            }
            # Use log_type attribute if set directly on record, else map level
            log_type = getattr(record, 'log_type', log_type_map.get(level, 'info'))

            # Emit to all connected clients in the main namespace
            # This needs to be thread-safe, socketio.emit handles this.
            socketio.emit('log_message', {'data': msg, 'type': log_type})
        except Exception:
            self.handleError(record) # Default error handling

# Configure the root logger
logger = logging.getLogger() # Get root logger
logger.setLevel(logging.INFO) # Set default level (can be overridden by args)
socketio_handler = SocketIOHandler()
# Consistent log format
formatter = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s', datefmt='%H:%M:%S')
socketio_handler.setFormatter(formatter)

# Prevent adding handler multiple times during development reloads
if not any(isinstance(h, SocketIOHandler) for h in logger.handlers):
    logger.addHandler(socketio_handler)
    # Keep console logging for server-side visibility
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# --- Bot Logic Integration ---

def log_bot_message(message: str, level: str = 'info'):
    """Callback function for bot components to log messages via SocketIO handler."""
    level_map = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR,
        'critical': logging.CRITICAL,
        'game': logging.INFO, # Map custom types to standard levels
        'challenge': logging.INFO,
    }
    log_level = level_map.get(level.lower(), logging.INFO)
    # Create a record manually or use logger directly
    # Using logger directly is simpler if no extra context needed
    # Add extra dict to pass custom type if needed
    extra_info = {'log_type': level.lower()} if level.lower() in ['game', 'challenge'] else {}
    logger.log(log_level, message, extra=extra_info)

def update_last_challenge(challenge_event: dict):
    """Callback to store the last received challenge event."""
    global bot_context
    bot_context["last_challenge_event"] = challenge_event
    challenger = challenge_event.get('challenger', {}).get('name', 'Unknown')
    log_bot_message(f"Stored last challenge details from {challenger}", "debug")

def update_matchmaking_status(is_running: bool):
     """Callback from Game Manager to update matchmaking status."""
     global bot_context
     if bot_context["is_matchmaking"] != is_running:
          bot_context["is_matchmaking"] = is_running
          log_bot_message(f"Matchmaking status changed: {'ON' if is_running else 'OFF'}", "info")
          socketio.emit('status_update', {'matchmaking_status': is_running})

EnumT = TypeVar('EnumT', bound=Enum)
def find_enum(name: str, enum_type: type[EnumT]) -> Optional[EnumT]:
    """Helper to find enum member by value, case-insensitive. Returns None if not found."""
    if not name:
        return None
    name_lower = name.lower()
    for enum_member in enum_type:
        if enum_member.value.lower() == name_lower:
            return enum_member
    return None

async def run_bot_async():
    """The main async function that sets up and runs the BotLi core components."""
    global bot_context
    config = bot_context["config"]

    try:
        # Initialize API within the bot's event loop
        api = API(config)
        await api.create_session()
        bot_context["api"] = api

        logger.info(f'{LOGO} {config.version}')
        # Get account info
        account = await api.get_account()
        username: str = account['username']
        api.append_user_agent(username)
        bot_context["username"] = username
        logger.info(f"Successfully logged in as: {username}")
        socketio.emit('status_update', {'username': username}) # Send username to UI

        # Verify token scope
        if 'bot:play' not in await api.get_token_scopes(config.token):
            logger.critical('CRITICAL: Token is missing the mandatory "bot:play" scope! Bot cannot function.')
            socketio.emit('log_message', {'data': 'ERROR: Token scope missing. Bot stopped.', 'type': 'error'})
            bot_context["running"] = False
            socketio.emit('status_update', {'bot_status': 'Error'})
            return # Stop the bot thread

        # Check BOT status (informational for web UI)
        if account.get('title') != 'BOT':
            logger.warning("Account is not registered as a BOT on Lichess. Some features might be restricted.")
            logger.warning("Upgrade to BOT account using the '--upgrade' flag with the CLI version if needed (irreversible!).")
            # Note: Upgrade functionality is not provided in this web interface.

        # Test engines
        logger.info("Testing configured engines...")
        engine_ok = True
        for engine_name, engine_config in config.engines.items():
            try:
                logger.info(f'Testing engine "{engine_name}"...')
                await Engine.test(engine_config)
                logger.info(f'Engine "{engine_name}" test PASSED.')
            except Exception as e:
                logger.error(f'Engine "{engine_name}" test FAILED: {e}')
                engine_ok = False # Mark failure but continue testing others
        if engine_ok:
             logger.info("All engine tests completed (or passed).")
        else:
             logger.warning("One or more engine tests failed. Check engine configuration.")
             # Decide if bot should stop if an engine fails (currently continues)

        # --- Initialize Core Bot Components ---
        logger.info("Initializing Game Manager and Event Handler...")
        # Pass the logging callback to the components
        game_manager = Game_Manager(api, config, username, log_callback=log_bot_message)
        event_handler = Event_Handler(api, config, username, game_manager, log_callback=log_bot_message)

        # Register callbacks for status updates
        game_manager.set_matchmaking_status_callback(update_matchmaking_status)
        event_handler.set_last_challenge_callback(update_last_challenge)

        bot_context["game_manager"] = game_manager
        bot_context["event_handler"] = event_handler

        # --- Handle Initial Actions from Command Line Args ---
        if bot_context.get("initial_tournament_id"):
            logger.info(f"Requesting to join tournament {bot_context['initial_tournament_id']}...")
            game_manager.request_tournament_joining(
                bot_context["initial_tournament_id"],
                bot_context["initial_tournament_team"],
                bot_context["initial_tournament_password"]
            )

        if bot_context.get("start_matchmaking_on_boot"):
            logger.info("Auto-starting matchmaking as requested...")
            game_manager.start_matchmaking()
            # Status update will be handled by the callback

        # --- Start the Main Async Loops ---
        logger.info("Starting main event loops for Game Manager and Event Handler...")
        # Store tasks to allow cancellation later
        gm_task = asyncio.create_task(game_manager.run())
        eh_task = asyncio.create_task(event_handler.run())
        bot_context["bot_tasks"] = [gm_task, eh_task]

        bot_context["running"] = True
        socketio.emit('status_update', {'bot_status': 'Running'})
        logger.info("BotLi core components initialized and running.")

        # Keep the bot running until tasks finish or are cancelled
        await asyncio.gather(*bot_context["bot_tasks"])

    except asyncio.CancelledError:
        logger.info("Bot async tasks were cancelled.")
    except Exception as e:
        # Log detailed exception traceback
        logger.exception(f"CRITICAL ERROR in bot async execution: {e}")
        socketio.emit('log_message', {'data': f'FATAL BOT ERROR: {e}. Bot stopped.', 'type': 'error'})
    finally:
        # --- Cleanup ---
        logger.info("Cleaning up bot resources...")
        bot_context["running"] = False
        socketio.emit('status_update', {'bot_status': 'Stopped'})
        if bot_context.get("api"):
            logger.info("Closing API session...")
            # Ensure session is closed within the correct loop
            try:
                await bot_context["api"].close_session()
            except Exception as close_err:
                logger.error(f"Error closing API session: {close_err}")
            bot_context["api"] = None
        bot_context["game_manager"] = None
        bot_context["event_handler"] = None
        logger.info("Bot async loop has finished.")


def bot_thread_target():
    """Target function for the bot's background thread."""
    global bot_context
    logger.info("Bot thread started.")

    # Create and set a new event loop for this thread
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    bot_context["bot_loop"] = bot_loop

    try:
        # Run the main async function until it completes or is cancelled
        bot_loop.run_until_complete(run_bot_async())
    finally:
        logger.info("Bot thread event loop closing...")
        # Close the event loop
        try:
            # Cancel any remaining tasks just in case
            tasks = asyncio.all_tasks(loop=bot_loop)
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Run loop briefly to allow cancellations to process
            # bot_loop.run_until_complete(asyncio.sleep(0.1)) # May not be needed if run_bot_async handles gather
            bot_loop.run_until_complete(bot_loop.shutdown_asyncgens())
        except Exception as e:
            logger.error(f"Error during bot loop shutdown: {e}")
        finally:
             bot_loop.close()
        bot_context["bot_loop"] = None
        logger.info("Bot thread event loop closed.")
        # Update status one last time in case of unexpected exit
        if not bot_context["shutdown_requested"]: # Avoid duplicate messages on clean quit
             bot_context["running"] = False
             socketio.emit('status_update', {'bot_status': 'Stopped'})


def start_bot():
    """Starts the BotLi logic in a background thread."""
    global bot_context
    if bot_context.get("running") or (bot_context.get("bot_thread") and bot_context["bot_thread"].is_alive()):
        logger.warning("Bot is already running or thread exists.")
        return

    logger.info("Attempting to start BotLi background thread...")
    # Load config before starting thread
    try:
        bot_context["config"] = Config.from_yaml(bot_context["config_path"])
        logger.info(f"Configuration loaded from {bot_context['config_path']}")
    except FileNotFoundError:
        logger.critical(f"CRITICAL ERROR: Config file not found at {bot_context['config_path']}!")
        socketio.emit('log_message', {'data': f'ERROR: Config file "{bot_context["config_path"]}" not found. Bot cannot start.', 'type': 'error'})
        socketio.emit('status_update', {'bot_status': 'Error: No Config'})
        return
    except Exception as e:
        logger.critical(f"CRITICAL ERROR: Failed to load or parse config file: {e}")
        socketio.emit('log_message', {'data': f'ERROR: Failed to load config: {e}. Bot cannot start.', 'type': 'error'})
        socketio.emit('status_update', {'bot_status': 'Error: Bad Config'})
        return

    # Start the bot logic in a separate thread
    bot_context["shutdown_requested"] = False
    thread = threading.Thread(target=bot_thread_target, daemon=True) # Daemon allows main thread to exit
    bot_context["bot_thread"] = thread
    thread.start()
    logger.info("BotLi background thread initiated.")


def stop_bot():
    """Stops the BotLi background thread gracefully."""
    global bot_context
    logger.info("Attempting to stop BotLi background thread...")
    bot_context["shutdown_requested"] = True # Signal intentional shutdown

    if not bot_context.get("running") and not (bot_context.get("bot_thread") and bot_context["bot_thread"].is_alive()):
        logger.info("Bot is already stopped.")
        socketio.emit('status_update', {'bot_status': 'Stopped'}) # Ensure UI reflects this
        return

    bot_loop = bot_context.get("bot_loop")
    if bot_loop and bot_loop.is_running():
        logger.info("Cancelling tasks in bot event loop...")
        # Schedule cancellation of tasks from the bot's own loop if possible
        tasks_to_cancel = bot_context.get("bot_tasks", [])
        if tasks_to_cancel:
            # Use run_coroutine_threadsafe to interact with the loop from another thread
            future = asyncio.run_coroutine_threadsafe(
                asyncio.gather(*[task.cancel() for task in tasks_to_cancel], return_exceptions=True),
                bot_loop
            )
            try:
                future.result(timeout=5) # Wait for cancellation to be scheduled/run
                logger.info("Cancellation requests submitted to bot tasks.")
            except TimeoutError:
                logger.warning("Timeout waiting for task cancellation confirmation.")
            except Exception as e:
                 logger.error(f"Error submitting task cancellations: {e}")
        else:
             logger.warning("No specific bot tasks found to cancel.")

        # Optionally, stop the loop itself if tasks don't exit cleanly
        # bot_loop.call_soon_threadsafe(bot_loop.stop) # This can be abrupt

    # Wait for the thread to finish
    thread = bot_context.get("bot_thread")
    if thread and thread.is_alive():
        logger.info("Waiting for bot thread to join...")
        thread.join(timeout=10) # Wait up to 10 seconds
        if thread.is_alive():
            logger.warning("Bot thread did not join gracefully after 10 seconds.")
        else:
            logger.info("Bot thread joined successfully.")

    # Final status update
    bot_context["running"] = False
    bot_context["game_manager"] = None
    bot_context["event_handler"] = None
    bot_context["bot_thread"] = None
    bot_context["bot_loop"] = None
    bot_context["api"] = None
    socketio.emit('status_update', {'bot_status': 'Stopped'})
    logger.info("Bot stop sequence complete.")


# --- Flask Routes ---
@app.route('/')
def index():
    """Serves the main web interface page."""
    # Renders the HTML template
    return render_template('index.html')

# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    """Called when a new client connects via Socket.IO."""
    sid = request.sid
    logger.info(f'Client connected: {sid}')
    emit('log_message', {'data': 'Welcome to the BotLi Web Interface!', 'type': 'success'})
    # Send current status to the newly connected client
    emit('status_update', {
        'bot_status': 'Running' if bot_context.get("running") else 'Stopped',
        'username': bot_context.get("username", "N/A"),
        'matchmaking_status': bot_context.get("is_matchmaking", False)
    })

@socketio.on('disconnect')
def handle_disconnect():
    """Called when a client disconnects."""
    sid = request.sid
    logger.info(f'Client disconnected: {sid}')

@socketio.on('request_initial_status')
def handle_request_initial_status():
     """Client explicitly requests status after connection."""
     emit('status_update', {
         'bot_status': 'Running' if bot_context.get("running") else 'Stopped',
         'username': bot_context.get("username", "N/A"),
         'matchmaking_status': bot_context.get("is_matchmaking", False)
     })

@socketio.on('submit_command')
def handle_command(data: dict):
    """Handles commands sent from the web UI."""
    if bot_context["shutdown_requested"]:
         log_bot_message("Shutdown in progress, ignoring command.", "warning")
         return

    command = data.get('command')
    params = data.get('params', {})
    gm = bot_context.get("game_manager")
    config = bot_context.get("config")
    api = bot_context.get("api") # Needed for some potential future commands

    if not command:
        log_bot_message("Received empty command from client.", "warning")
        return

    # Many commands require the bot to be running
    if not bot_context.get("running"):
        # Allow 'quit' even if stopped to try and exit server
        # Allow 'start' if implemented
        allowed_when_stopped = ['quit']
        if command not in allowed_when_stopped:
             log_bot_message(f"Bot is not running. Cannot process command: {command}", "warning")
             # Ensure UI shows stopped status
             emit('status_update', {'bot_status': 'Stopped'})
             return

    # Log command received for debugging/audit
    logger.info(f"Processing command '{command}' with params: {params} from {request.sid}")

    try:
        # --- Command Processing Logic ---
        match command:
            case 'blacklist':
                username = params.get('username', '').strip().lower()
                if username and config:
                    if username not in config.blacklist:
                        config.blacklist.append(username)
                        log_bot_message(f'Added "{username}" to temporary blacklist.', 'success')
                        # If user was whitelisted, remove them
                        if username in config.whitelist:
                             config.whitelist.remove(username)
                             log_bot_message(f'Removed "{username}" from temporary whitelist.', 'info')
                    else:
                        log_bot_message(f'"{username}" is already in the temporary blacklist.', 'info')
                else:
                    log_bot_message("Blacklist command requires a valid username.", "warning")

            case 'whitelist':
                username = params.get('username', '').strip().lower()
                if username and config:
                    if username not in config.whitelist:
                        config.whitelist.append(username)
                        log_bot_message(f'Added "{username}" to temporary whitelist.', 'success')
                        # If user was blacklisted, remove them
                        if username in config.blacklist:
                             config.blacklist.remove(username)
                             log_bot_message(f'Removed "{username}" from temporary blacklist.', 'info')
                    else:
                        log_bot_message(f'"{username}" is already in the temporary whitelist.', 'info')
                else:
                    log_bot_message("Whitelist command requires a valid username.", "warning")

            case 'challenge':
                if not gm: log_bot_message("Game Manager not available.", "error"); return
                try:
                    opponent_username = params['username'].strip()
                    if not opponent_username: raise ValueError("Opponent username cannot be empty")
                    time_control = params.get('timecontrol', '1+1').strip()
                    # Basic validation for time control format
                    if '+' not in time_control: raise ValueError("Invalid time control format (e.g., '5+3')")
                    initial_time_str, increment_str = time_control.split('+', 1)
                    initial_time = int(float(initial_time_str) * 60)
                    increment = int(increment_str)
                    if initial_time <= 0 or increment < 0: raise ValueError("Time/increment must be non-negative, initial time > 0")

                    color_str = params.get('color', 'random')
                    color = find_enum(color_str, Challenge_Color)
                    if color is None: raise ValueError(f"Invalid color: {color_str}")

                    rated = params.get('rated', True) # Defaults to rated

                    variant_str = params.get('variant', 'standard')
                    variant = find_enum(variant_str, Variant)
                    if variant is None: raise ValueError(f"Invalid variant: {variant_str}")

                    challenge_request = Challenge_Request(opponent_username, initial_time, increment, rated, color, variant, 30)
                    gm.request_challenge(challenge_request) # Add to the Game Manager's queue
                    log_bot_message(f'Challenge vs {opponent_username} ({time_control} {variant.value}, Rated: {rated}, Color: {color.value}) added to queue.', 'success', )

                except (KeyError, ValueError, AttributeError) as e:
                    log_bot_message(f'Invalid challenge parameters: {e}', 'error')
                except Exception as e:
                    logger.exception("Unexpected error processing challenge command")
                    log_bot_message(f'Error sending challenge: {e}', 'error')

            case 'create':
                if not gm: log_bot_message("Game Manager not available.", "error"); return
                try:
                    count = int(params['count'])
                    if count <= 0: raise ValueError("Number of pairs must be positive.")

                    opponent_username = params['username'].strip()
                    if not opponent_username: raise ValueError("Opponent username cannot be empty")

                    time_control = params.get('timecontrol', '1+1').strip()
                    if '+' not in time_control: raise ValueError("Invalid time control format (e.g., '1+1')")
                    initial_time_str, increment_str = time_control.split('+', 1)
                    initial_time = int(float(initial_time_str) * 60)
                    increment = int(increment_str)
                    if initial_time <= 0 or increment < 0: raise ValueError("Time/increment must be non-negative, initial time > 0")

                    rated = params.get('rated', True)

                    variant_str = params.get('variant', 'standard')
                    variant = find_enum(variant_str, Variant)
                    if variant is None: raise ValueError(f"Invalid variant: {variant_str}")

                    challenges: list[Challenge_Request] = []
                    for _ in range(count):
                        # Queue one challenge as white, one as black for each pair
                        challenges.append(Challenge_Request(opponent_username, initial_time,
                                          increment, rated, Challenge_Color.WHITE, variant, 30))
                        challenges.append(Challenge_Request(opponent_username, initial_time,
                                          increment, rated, Challenge_Color.BLACK, variant, 30))

                    gm.request_challenge(*challenges) # Add all generated challenges
                    log_bot_message(f'Queued {count} game pair(s) ({count*2} challenges) vs {opponent_username} ({time_control} {variant.value}, Rated: {rated}).', 'success')

                except (KeyError, ValueError, AttributeError) as e:
                    log_bot_message(f'Invalid create game pair parameters: {e}', 'error')
                except Exception as e:
                    logger.exception("Unexpected error processing create command")
                    log_bot_message(f'Error creating game pairs: {e}', 'error')

            case 'leave':
                 if not gm: log_bot_message("Game Manager not available.", "error"); return
                 tournament_id = params.get('tournament_id', '').strip()
                 if tournament_id:
                     gm.request_tournament_leaving(tournament_id)
                     log_bot_message(f'Requested to leave tournament "{tournament_id}".', 'info')
                 else:
                     log_bot_message("Tournament ID is required to leave.", 'warning')

            case 'clear':
                if gm:
                    count = len(gm.challenge_requests)
                    gm.challenge_requests.clear()
                    log_bot_message(f'Challenge queue cleared ({count} challenges removed).', 'success')
                else:
                    log_bot_message("Game Manager not available.", "error")

            case 'quit':
                 log_bot_message('Quit command received. Initiating shutdown...', 'warning')
                 stop_bot() # Stop the bot thread first
                 # Request Flask-SocketIO server shutdown after a short delay
                 socketio.sleep(0.5) # Allow messages to flush
                 logger.info("Requesting server shutdown.")
                 # This is the recommended way for eventlet
                 # It might not work perfectly in all environments/deployments
                 pid = os.getpid()
                 logger.info(f"Sending SIGINT to process {pid} to stop server.")
                 os.kill(pid, signal.SIGINT)
                 # As a fallback, could use socketio.stop() but kill is often cleaner for eventlet

            case 'matchmaking':
                if not gm: log_bot_message("Game Manager not available.", "error"); return
                if not bot_context.get("is_matchmaking"):
                    log_bot_message('Requesting matchmaking start...', 'info')
                    gm.start_matchmaking()
                    # Status update relies on the callback from Game Manager
                else:
                    log_bot_message('Matchmaking is already running or starting.', 'info')
                    emit('status_update', {'matchmaking_status': True}) # Re-sync UI just in case

            case 'rechallenge':
                if not gm: log_bot_message("Game Manager not available.", "error"); return
                last_challenge = bot_context.get("last_challenge_event")
                if not last_challenge:
                    log_bot_message('No previous challenge received to rechallenge.', 'warning')
                    return

                try:
                    if last_challenge['speed'] == 'correspondence':
                        log_bot_message('Rechallenging correspondence games is not supported.', 'warning')
                        return

                    opponent_username: str = last_challenge['challenger']['id'] # Use ID for reliability
                    opponent_name: str = last_challenge['challenger']['name'] # For logging
                    initial_time: int = last_challenge['timeControl']['limit']
                    increment: int = last_challenge['timeControl']['increment']
                    rated: bool = last_challenge['rated']
                    variant_key: str = last_challenge['variant']['key']
                    variant = find_enum(variant_key, Variant)
                    if variant is None: raise ValueError(f"Invalid variant key in last challenge: {variant_key}")

                    # Determine the color for the rechallenge (opposite of received)
                    event_color: str = last_challenge['color']
                    if event_color == 'white':
                        color = Challenge_Color.BLACK
                    elif event_color == 'black':
                        color = Challenge_Color.WHITE
                    else: # random
                        color = Challenge_Color.RANDOM # Rechallenge with random color too

                    rechallenge_request = Challenge_Request(opponent_username, initial_time, increment, rated, color, variant, 30)
                    gm.request_challenge(rechallenge_request)
                    log_bot_message(f'Rechallenge vs {opponent_name} ({initial_time//60}+{increment} {variant.value}, Color: {color.value}) added to queue.', 'success')

                except (KeyError, ValueError, AttributeError) as e:
                     log_bot_message(f"Error processing last challenge data for rechallenge: {e}", "error")
                except Exception as e:
                     logger.exception("Unexpected error processing rechallenge command")
                     log_bot_message(f'Error sending rechallenge: {e}', 'error')


            case 'reset':
                if not gm: log_bot_message("Game Manager not available.", "error"); return
                perf_type_str = params.get('perf_type')
                perf_type = find_enum(perf_type_str, Perf_Type) if perf_type_str else None

                if perf_type:
                    # Access matchmaking opponents safely
                    if hasattr(gm, 'matchmaking') and hasattr(gm.matchmaking, 'opponents'):
                         gm.matchmaking.opponents.reset_release_time(perf_type)
                         log_bot_message(f'Matchmaking opponents list reset for {perf_type.value}.', 'success')
                    else:
                         log_bot_message(f'Matchmaking component or opponents list not found.', 'error')
                else:
                    log_bot_message(f'Invalid or missing performance type for reset: "{perf_type_str}"', 'warning')

            case 'stop':
                if not gm: log_bot_message("Game Manager not available.", "error"); return
                if bot_context.get("is_matchmaking"):
                     log_bot_message('Requesting matchmaking stop...', 'info')
                     # Assuming stop_matchmaking returns True if it was running
                     if gm.stop_matchmaking():
                          # Status update relies on callback
                          pass
                     else:
                          # Should ideally not happen if is_matchmaking was True
                          log_bot_message('Game Manager reported matchmaking was not stopped (unexpected).', 'warning')
                          bot_context["is_matchmaking"] = False # Force status update
                          emit('status_update', {'matchmaking_status': False})
                else:
                    log_bot_message('Matchmaking is not currently running.', 'info')
                    emit('status_update', {'matchmaking_status': False}) # Re-sync UI

            case 'tournament':
                 if not gm: log_bot_message("Game Manager not available.", "error"); return
                 tournament_id = params.get('tournament_id', '').strip()
                 tournament_team = params.get('team') # Optional
                 tournament_password = params.get('password') # Optional

                 if tournament_id:
                     log_bot_message(f'Requesting to join tournament "{tournament_id}"...'
                                     f'{f" with team {tournament_team}" if tournament_team else ""}'
                                     f'{f" using password" if tournament_password else ""}', 'info')
                     gm.request_tournament_joining(tournament_id, tournament_team, tournament_password)
                 else:
                     log_bot_message("Tournament ID is required to join.", 'warning')

            case _:
                log_bot_message(f"Unknown command received: {command}", "warning")

    except Exception as e:
         # Catch-all for unexpected errors during command processing
         logger.exception(f"Unhandled error processing command '{command}': {e}")
         log_bot_message(f"Internal server error processing command '{command}'. Check server logs.", "error")


# --- Main Execution ---
if __name__ == '__main__':
    # Argument parsing similar to the original script
    parser = argparse.ArgumentParser(description="BotLi Web Interface")
    parser.add_argument('--config', '-c', default='config.yml', type=str, help='Path to config.yml.')
    parser.add_argument('--host', default='0.0.0.0', type=str, help='Host to bind the web server to.')
    parser.add_argument('--port', default=7860, type=int, help='Port to run the web server on.')
    # Add args to control initial bot state if desired
    parser.add_argument('--matchmaking', '-m', action='store_true', help='Start matchmaking automatically when the bot starts.')
    parser.add_argument('--tournament', '-t', type=str, help='ID of a tournament to join automatically when the bot starts.')
    parser.add_argument('--team', type=str, help='The team ID for automatic tournament joining.')
    parser.add_argument('--password', type=str, help='The password for automatic tournament joining.')
    parser.add_argument('--debug', '-d', action='store_true', help='Enable debug logging.')
    args = parser.parse_args()

    # Set logging level based on debug flag
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger('socketio').setLevel(logging.DEBUG) # Enable SocketIO debug logging
        logging.getLogger('engineio').setLevel(logging.DEBUG) # Enable EngineIO debug logging
        logger.debug("Debug logging enabled.")
    else:
        logger.setLevel(logging.INFO)
        # Keep socketio/engineio logs quieter in non-debug mode
        logging.getLogger('socketio').setLevel(logging.WARNING)
        logging.getLogger('engineio').setLevel(logging.WARNING)


    # Store config path and initial actions in context
    bot_context["config_path"] = args.config
    bot_context["start_matchmaking_on_boot"] = args.matchmaking
    bot_context["initial_tournament_id"] = args.tournament
    bot_context["initial_tournament_team"] = args.team
    bot_context["initial_tournament_password"] = args.password

    # --- Start Bot and Server ---
    try:
        # Start the bot logic in its background thread *first*
        start_bot()

        # Start the Flask-SocketIO web server (using eventlet)
        logger.info(f"Starting Flask-SocketIO server on http://{args.host}:{args.port}")
        socketio.run(app, host=args.host, port=args.port, debug=False, use_reloader=False)
        # Note: Flask's internal reloader doesn't work well with background threads/eventlet.
        # Set debug=False for socketio.run even if Flask debug is conceptually on.

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down...")
    except Exception as main_err:
         logger.exception(f"Fatal error during server startup or execution: {main_err}")
    finally:
        # Ensure bot is stopped cleanly when server exits
        logger.info("Server shutdown initiated. Stopping bot thread...")
        stop_bot()
        logger.info("Shutdown complete.")
        print("BotLi Web Interface Exited.") # Final message to console
