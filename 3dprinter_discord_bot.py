"""
Makerspace 3D Printer Discord Bot
Monitors Prusa (via PrusaLink HTTP) and Bambu Lab (via MQTT) printers
and displays real-time status in Discord.

Features:
    - Status message that auto-updates (interval configurable in printers.json)
    - Supports both Prusa and Bambu Lab printers from one config file
    - Notification messages when a print completes or fails, including print duration
    - Mentions the Discord user if their username is in the filename
      (format: filename_@username.gcode)
    - Custom status title and refresh interval configurable in printers.json

Requirements:
    pip install discord.py requests python-dotenv bambulabs-api

Setup:
    1. Create a .env file with your DISCORD_TOKEN and channel IDs
    2. Edit printers.json to add your printers (type: "prusa" or "bambu")
    3. Run: python3 3dprinter_discord_bot.py
"""

import os
import json
import time as time_module
from datetime import datetime, timezone

import discord
from discord.ext import tasks
import requests
from dotenv import load_dotenv

try:
    import bambulabs_api as bl
    BAMBU_AVAILABLE = True
except ImportError:
    BAMBU_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", 0))
NOTIFICATION_CHANNEL_ID = int(os.getenv("NOTIFICATION_CHANNEL_ID", 0))

# How often to poll printers (in seconds)
POLL_INTERVAL = 30

# Path to the printer config file
PRINTERS_CONFIG = "printers.json"


def load_config():
    """
    Load configuration from printers.json config file.
    Supports two formats:
      - Legacy: a plain list of printer dicts (for backwards compatibility)
      - New: a dict with top-level settings and a "printers" key

    Returns (printers, poll_interval, status_title).
    """
    try:
        with open(PRINTERS_CONFIG, "r") as f:
            data = json.load(f)

        # Support legacy format (plain list)
        if isinstance(data, list):
            printers = data
            poll_interval = 30
            status_title = "3D Printer Status"
        else:
            printers = data.get("printers", [])
            poll_interval = data.get("refresh_interval_seconds", 30)
            status_title = data.get("status_title", "3D Printer Status")

        print(f"Loaded {len(printers)} printer(s) from {PRINTERS_CONFIG}")
        print(f"Refresh interval: {poll_interval}s | Status title: \"{status_title}\"")
        return printers, poll_interval, status_title

    except FileNotFoundError:
        print(f"Error: {PRINTERS_CONFIG} not found. Create it with your printer info.")
        print('Example format:')
        print('{')
        print('  "status_title": "My Makerspace 3D Printer Status",')
        print('  "refresh_interval_seconds": 30,')
        print('  "printers": [')
        print('    {"name": "Printer Name", "type": "prusa/bambu", "ip": "YOUR_IP", "api_key": "YOUR_KEY"}')
        print('  ]')
        print('}')
        exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: {PRINTERS_CONFIG} contains invalid JSON: {e}")
        exit(1)


# ---------------------------------------------------------------------------
# Bambu Lab MQTT Connections
# ---------------------------------------------------------------------------

# Stores connected Bambu printer instances: {"printer_name": bl.Printer}
bambu_connections = {}


def connect_bambu_printers(printers):
    """
    Connect to all Bambu printers in the config.
    Each connection runs MQTT in the background, caching status updates.
    """
    if not BAMBU_AVAILABLE:
        bambu_printers = [p for p in printers if p.get("type") == "bambu"]
        if bambu_printers:
            print("WARNING: bambulabs-api is not installed. Bambu printers will show as offline.")
            print("Install it with: pip install bambulabs-api")
        return

    for printer in printers:
        if printer.get("type") != "bambu":
            continue

        name = printer["name"]
        ip = printer["ip"]
        serial = printer["serial"]
        access_code = printer["access_code"]

        print(f"Connecting to Bambu printer: {name} at {ip}...")
        try:
            client = bl.Printer(ip, access_code, serial)
            client.mqtt_start()
            bambu_connections[name] = client
            print(f"  Connected to {name}")
        except Exception as e:
            print(f"  Failed to connect to {name}: {e}")

    if bambu_connections:
        # Wait for initial MQTT data to arrive
        print("Waiting for Bambu MQTT data...")
        time_module.sleep(3)


def disconnect_bambu_printers():
    """Disconnect all Bambu printer MQTT connections."""
    for name, client in bambu_connections.items():
        try:
            client.mqtt_stop()
            print(f"Disconnected from {name}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PrusaLink API Functions
# ---------------------------------------------------------------------------


def get_printer_status(ip, api_key, auth_type="api_key", username="maker"):
    """
    Query a printer's PrusaLink API for its current status.
    Returns a dictionary with the printer's state and job info.

    auth_type: "api_key" uses X-Api-Key header (Prusa Mini/Mini+)
               "digest"  uses HTTP Digest auth (MK3S with RPi Zero)
    username:  Username for digest auth (default: "maker")
    """
    base_url = f"http://{ip}"

    # Set up authentication based on printer type
    if auth_type == "digest":
        from requests.auth import HTTPDigestAuth
        auth = HTTPDigestAuth(username, api_key)
        headers = {}
    else:
        auth = None
        headers = {"X-Api-Key": api_key}

    try:
        # Get combined status (printer state + job progress)
        status_response = requests.get(
            f"{base_url}/api/v1/status", headers=headers, auth=auth, timeout=10
        )
        status_response.raise_for_status()
        status_data = status_response.json()

        printer_state = status_data.get("printer", {}).get("state", "UNKNOWN")
        job_data = status_data.get("job", {})

        result = {
            "state": printer_state,
            "progress": job_data.get("progress"),
            "time_remaining": job_data.get("time_remaining"),
            "time_printing": job_data.get("time_printing"),
        }

        # If printing, also grab the job endpoint for the file name
        if printer_state == "PRINTING":
            try:
                job_response = requests.get(
                    f"{base_url}/api/v1/job", headers=headers, auth=auth, timeout=10
                )
                job_response.raise_for_status()
                result["job_data"] = job_response.json()
            except Exception:
                result["job_data"] = None

        return result

    except requests.exceptions.ConnectionError:
        return {"state": "OFFLINE"}
    except requests.exceptions.Timeout:
        return {"state": "OFFLINE"}
    except Exception as e:
        print(f"Error polling {ip}: {e}")
        return {"state": "UNKNOWN"}


# ---------------------------------------------------------------------------
# Bambu Lab Status Function
# ---------------------------------------------------------------------------


def get_bambu_status(printer_name):
    """
    Read the cached status from a connected Bambu printer.
    Returns a dictionary in the same format as get_printer_status().
    """
    client = bambu_connections.get(printer_name)
    if client is None:
        return {"state": "OFFLINE"}

    try:
        state = client.get_state()
        percentage = client.get_percentage()
        time_remaining_min = client.get_time()
        file_name = client.subtask_name() or client.get_file_name()
        current_layer = client.current_layer_num()
        total_layers = client.total_layer_num()

        # Normalize Bambu states to match Prusa conventions
        state_map = {
            "IDLE": "IDLE",
            "RUNNING": "PRINTING",
            "PREPARE": "PRINTING",
            "PAUSE": "PAUSED",
            "FINISH": "IDLE",       # Treat FINISH as IDLE for display
            "FAILED": "ERROR",
            "UNKNOWN": "UNKNOWN",
        }

        state_str = str(state).split(".")[-1] if state else "UNKNOWN"
        normalized_state = state_map.get(state_str, state_str)

        # Check for FINISH -> send notification but display as IDLE
        # We pass the raw state so the notification logic can detect completion
        raw_state = state_str

        # Convert time from minutes to seconds for format_time()
        time_remaining_sec = (time_remaining_min * 60) if time_remaining_min is not None else None

        result = {
            "state": normalized_state,
            "raw_bambu_state": raw_state,
            "progress": percentage,
            "time_remaining": time_remaining_sec,
        }

        # If printing, include file info in job_data format to match Prusa
        if normalized_state == "PRINTING" and file_name:
            result["job_data"] = {
                "file": {"display_name": file_name}
            }
            result["layers"] = f"{current_layer}/{total_layers}"

        return result

    except Exception as e:
        print(f"Error reading Bambu printer {printer_name}: {e}")
        return {"state": "UNKNOWN"}


# ---------------------------------------------------------------------------
# Unified Printer Polling
# ---------------------------------------------------------------------------


def poll_printer(printer):
    """
    Poll a single printer regardless of type.
    Returns a status dictionary in a common format.
    """
    printer_type = printer.get("type", "prusa")

    if printer_type == "bambu":
        return get_bambu_status(printer["name"])
    else:
        # Support both "api_key" and "password" fields in config
        key = printer.get("api_key") or printer.get("password", "")
        return get_printer_status(
            printer["ip"],
            key,
            auth_type=printer.get("auth_type", "api_key"),
            username=printer.get("username", "maker"),
        )


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def format_time(seconds):
    """Convert seconds into a human-friendly time string."""
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_status_emoji(state):
    """Return an emoji representing the printer state."""
    state_emojis = {
        "IDLE": "🟢",
        "READY": "🟢",
        "PRINTING": "🟡",
        "BUSY": "🟡",
        "PAUSED": "🟠",
        "FINISHED": "🔵",
        "STOPPED": "🔴",
        "ERROR": "🔴",
        "ATTENTION": "🟠",
        "OFFLINE": "⚫",
        "UNKNOWN": "⚪",
    }
    return state_emojis.get(state, "⚪")


def get_status_label(state):
    """Return a human-friendly label for the printer state."""
    state_labels = {
        "IDLE": "Available",
        "READY": "Available",
        "PRINTING": "Printing",
        "BUSY": "Busy",
        "PAUSED": "Paused",
        "FINISHED": "Print Finished",
        "STOPPED": "Stopped",
        "ERROR": "Error",
        "ATTENTION": "Needs Attention",
        "OFFLINE": "Offline",
        "UNKNOWN": "Unknown",
    }
    return state_labels.get(state, state)


def get_file_name(status):
    """Extract the file name from job data."""
    if status.get("job_data") and isinstance(status["job_data"], dict):
        return (
            status["job_data"].get("file", {}).get("display_name")
            or status["job_data"].get("file", {}).get("name")
            or "Unknown file"
        )
    return "Unknown file"


def parse_username_from_filename(file_name):
    """
    Extract a Discord username from a filename.
    Looks for the pattern _@username before the file extension.
    Example: 'bracket_v3_@bob.smith.gcode' returns 'bob.smith'
    Returns None if no username is found.
    """
    if "_@" not in file_name:
        return None

    # Get everything after the last _@
    after_at = file_name.split("_@")[-1]

    # Remove the file extension (.gcode, .bgcode, etc.)
    # Find the last dot that's part of the extension
    # We need to be careful because usernames can have dots (e.g. riley.smith)
    # So we strip known gcode extensions from the end
    for ext in [".gcode.3mf", ".bgcode", ".gcode", ".gco", ".3mf"]:
        if after_at.lower().endswith(ext):
            return after_at[: -len(ext)]

    return after_at


async def find_member_by_username(guild, username):
    """
    Search guild members for a matching Discord username.
    Returns the Member object if found, None otherwise.
    Case-insensitive search.
    """
    if username is None:
        return None

    username_lower = username.lower()

    for member in guild.members:
        if member.name.lower() == username_lower:
            return member

    return None


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Required to look up members by username
bot = discord.Client(intents=intents)

# Store the message ID so we can edit the same pinned message each time
status_message_id = None

# Track previous printer states to detect completed/failed prints
# Key: printer name, Value: dict with state and file info from last poll
previous_states = {}

# Track when each printer started its current print job (in-memory only)
# Key: printer name, Value: datetime when printing state was first detected
# If the bot restarts mid-print, this will be empty and duration will be omitted.
print_start_times = {}

# Load printers and global config from config file
PRINTERS, POLL_INTERVAL, STATUS_TITLE = load_config()

# Connect to Bambu printers (MQTT background connections)
connect_bambu_printers(PRINTERS)


def build_status_embed(printer_statuses):
    """
    Build a Discord embed with all printer statuses.
    printer_statuses is a list of (printer, status, file_name, username) tuples.
    Returns a discord.Embed object.
    """
    embed = discord.Embed(
        title=STATUS_TITLE,
        color=0x4A90D9,
        timestamp=datetime.now(timezone.utc),
    )

    for printer, status, file_name, username in printer_statuses:
        emoji = get_status_emoji(status["state"])
        label = get_status_label(status["state"])

        if status["state"] == "PRINTING":
            progress = status.get("progress", 0) or 0
            time_left = format_time(status.get("time_remaining"))
            user_display = f"@{username}" if username else "Unknown User"

            value = (
                f"`{file_name}`\n"
                f"{user_display}\n"
                f"{progress:.0f}% complete | ~{time_left} remaining"
            )
        elif status["state"] == "OFFLINE":
            value = "Printer is not reachable"
        elif status["state"] in ("ERROR", "ATTENTION"):
            value = "Check printer"
        else:
            value = "\u200b"  # Invisible character so Discord doesn't complain about empty value

        embed.add_field(
            name=f"{emoji} {printer['name']} - {label}",
            value=value,
            inline=False,
        )

    embed.set_footer(text=f"Updates every {POLL_INTERVAL} seconds")

    return embed


@bot.event
async def on_ready():
    """Called when the bot connects to Discord."""
    print(f"Bot connected as {bot.user}")
    print(f"Monitoring {len(PRINTERS)} printer(s)")
    update_status.start()


@tasks.loop(seconds=POLL_INTERVAL)
async def update_status():
    """Periodically poll printers and update the status message in Discord."""
    global status_message_id, previous_states

    status_channel = bot.get_channel(STATUS_CHANNEL_ID)
    notification_channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)

    if status_channel is None:
        print(f"Error: Could not find status channel with ID {STATUS_CHANNEL_ID}")
        return

    if notification_channel is None:
        print(f"Error: Could not find notification channel with ID {NOTIFICATION_CHANNEL_ID}")
        return

    # On first run, clear the status channel so it's clean
    if status_message_id is None:
        await status_channel.purge()

    guild = status_channel.guild

    # Poll all printers and collect statuses
    printer_statuses = []
    for printer in PRINTERS:
        status = poll_printer(printer)
        file_name = get_file_name(status) if status["state"] == "PRINTING" else None
        username = parse_username_from_filename(file_name) if file_name else None
        printer_statuses.append((printer, status, file_name, username))

    # --- Check for completed or failed prints (send to notification channel) ---
    for printer, status, file_name, username in printer_statuses:
        printer_name = printer["name"]
        prev = previous_states.get(printer_name)

        # Skip UNKNOWN states (Bambu printers may return this on first poll
        # before MQTT data arrives). Don't save it or check notifications.
        if status["state"] == "UNKNOWN":
            continue

        # Record start time when a printer transitions into PRINTING
        if status["state"] == "PRINTING" and (not prev or prev["state"] != "PRINTING"):
            print_start_times[printer_name] = datetime.now()

        if prev and prev["state"] == "PRINTING":
            # Printer was printing last poll, check if it finished or failed
            # For Bambu printers, raw_bambu_state == "FINISH" means completed
            bambu_finished = status.get("raw_bambu_state") == "FINISH"

            # Calculate duration:
            # 1. Use printer-reported time_printing if available (Prusa)
            # 2. Fall back to in-memory start time tracker (Bambu, or if API didn't report it)
            reported_time = prev.get("time_printing")
            start_time = print_start_times.pop(printer_name, None)

            if reported_time:
                duration_str = format_time(int(reported_time))
            elif start_time:
                elapsed = datetime.now() - start_time
                duration_str = format_time(int(elapsed.total_seconds()))
            else:
                duration_str = None  # Bot restarted mid-print; omit duration

            if status["state"] in ("IDLE", "READY", "FINISHED") or bambu_finished:
                # Print completed!
                member = await find_member_by_username(guild, prev.get("username"))
                mention = member.mention if member else (f"@{prev['username']}" if prev.get("username") else "Unknown user")
                prev_file = prev.get("file_name", "Unknown file")

                duration_line = f"Print time: {duration_str}\n" if duration_str else ""
                await notification_channel.send(
                    f"**Print Complete** on **{printer_name}**\n"
                    f"`{prev_file}`\n"
                    f"{mention}\n"
                    f"{duration_line}"
                    f"Your print is ready for pickup!"
                )

            elif status["state"] in ("ERROR", "STOPPED"):
                # Print failed!
                member = await find_member_by_username(guild, prev.get("username"))
                mention = member.mention if member else (f"@{prev['username']}" if prev.get("username") else "Unknown user")
                prev_file = prev.get("file_name", "Unknown file")

                duration_line = f"Print time: {duration_str}\n" if duration_str else ""
                await notification_channel.send(
                    f"**Print Failed** on **{printer_name}**\n"
                    f"`{prev_file}`\n"
                    f"{mention}\n"
                    f"{duration_line}"
                    f"Please check the printer."
                )

        # Save current state for next poll comparison
        previous_states[printer_name] = {
            "state": status["state"],
            "file_name": file_name,
            "username": username,
            "time_printing": status.get("time_printing"),
        }

    # --- Update the status embed (in status channel) ---
    embed = build_status_embed(printer_statuses)

    if status_message_id is not None:
        try:
            message = await status_channel.fetch_message(status_message_id)
            await message.edit(embed=embed)
        except discord.NotFound:
            message = await status_channel.send(embed=embed)
            status_message_id = message.id
    else:
        message = await status_channel.send(embed=embed)
        status_message_id = message.id


@update_status.before_loop
async def before_update_status():
    """Wait until the bot is ready before starting the loop."""
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Run the bot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not found in .env file")
        print("Create a .env file with: DISCORD_TOKEN=your_token_here")
        exit(1)

    if STATUS_CHANNEL_ID == 0:
        print("Error: STATUS_CHANNEL_ID not found in .env file")
        print("Add to .env file: STATUS_CHANNEL_ID=your_channel_id_here")
        exit(1)

    if NOTIFICATION_CHANNEL_ID == 0:
        print("Error: NOTIFICATION_CHANNEL_ID not found in .env file")
        print("Add to .env file: NOTIFICATION_CHANNEL_ID=your_channel_id_here")
        exit(1)

    try:
        bot.run(DISCORD_TOKEN)
    finally:
        disconnect_bambu_printers()
