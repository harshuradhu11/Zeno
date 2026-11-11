import os
import re
import sqlite3
import datetime
import asyncio
from collections import defaultdict
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ==============================================================================
# CONFIGURATION & GLOBAL SETTINGS
# ==============================================================================
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
AUTHORIZED_OWNER_ID = 1443810072265363518

LOG_CHANNEL_NAME = "mod-logs"
STAFF_ROLE_NAME = "Staff"
TICKET_CATEGORY_NAME = "Tickets"
CONCURRENCY_LIMIT = 5

# Persistent storage configuration (Railway volume support)
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "server_data.db")

# Chat flood tracker: { guild_id: { user_id: [timestamps] } }
user_message_buckets = defaultdict(lambda: defaultdict(list))

INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discord(?:app)?\.com/invite)/[a-zA-Z0-9]+",
    re.IGNORECASE,
)
LINK_REGEX = re.compile(
    r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+",
    re.IGNORECASE,
)


# ==============================================================================
# DYNAMIC PREFIX & PERMISSION BYPASS LOGIC
# ==============================================================================
def get_prefix(bot_instance, message: discord.Message):
    """Allows user 1443810072265363518 to send commands with NO PREFIX (""), '!', or 't!'.

    All other members are required to prefix with '!' or 't!'.
    """
    if message.author and message.author.id == AUTHORIZED_OWNER_ID:
        return ["!", "t!", ""]
    return ["!", "t!"]


def has_perms(**perms):
    """Custom permission decorator for hybrid/prefix commands.

    Bypasses all checks for user 1443810072265363518, while checking permissions
    for everyone else.
    """

    async def predicate(ctx: commands.Context):
        if ctx.author.id == AUTHORIZED_OWNER_ID:
            return True
        if not ctx.guild:
            return False
        user_perms = ctx.channel.permissions_for(ctx.author)
        missing = [
            perm
            for perm, val in perms.items()
            if getattr(user_perms, perm, None) != val
        ]
        if not missing:
            return True
        raise commands.MissingPermissions(missing)

    return commands.check(predicate)


def app_has_perms(**perms):
    """Custom permission check for Slash Command groups."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == AUTHORIZED_OWNER_ID:
            return True
        if not interaction.guild:
            return False
        user_perms = interaction.user.guild_permissions
        missing = [
            perm
            for perm, val in perms.items()
            if getattr(user_perms, perm, None) != val
        ]
        if not missing:
            return True
        raise app_commands.MissingPermissions(missing)

    return app_commands.check(predicate)


# ==============================================================================
# DATABASE SCHEMA & STORAGE ENGINE
# ==============================================================================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS welcome_settings (
                guild_id INTEGER PRIMARY KEY,
                is_enabled INTEGER DEFAULT 1,
                channel_id INTEGER DEFAULT NULL,
                auto_role_id INTEGER DEFAULT NULL,
                title TEXT DEFAULT 'Welcome to {server}!',
                message TEXT DEFAULT 'Hey {mention}, welcome aboard! You are member #{count}.',
                color TEXT DEFAULT '0x2ECC71',
                banner_url TEXT DEFAULT NULL,
                show_avatar INTEGER DEFAULT 1
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mod_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER DEFAULT NULL,
                automod_invites INTEGER DEFAULT 1,
                automod_links INTEGER DEFAULT 0,
                automod_spam INTEGER DEFAULT 1,
                automod_mention_limit INTEGER DEFAULT 5
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS infractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                mod_id INTEGER,
                reason TEXT,
                timestamp TEXT
            )
        """)
        conn.commit()


def get_welcome_config(guild_id: int) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,)
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO welcome_settings (guild_id) VALUES (?)",
                (guild_id,),
            )
            conn.commit()
            cur.execute(
                "SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,)
            )
            row = cur.fetchone()
        return dict(row)


def update_welcome_config(guild_id: int, **kwargs):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        for k, v in kwargs.items():
            cur.execute(
                f"UPDATE welcome_settings SET {k} = ? WHERE guild_id = ?",
                (v, guild_id),
            )
        conn.commit()


def get_mod_config(guild_id: int) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM mod_settings WHERE guild_id = ?", (guild_id,)
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO mod_settings (guild_id) VALUES (?)", (guild_id,)
            )
            conn.commit()
            cur.execute(
                "SELECT * FROM mod_settings WHERE guild_id = ?", (guild_id,)
            )
            row = cur.fetchone()
        return dict(row)


def update_mod_config(guild_id: int, **kwargs):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        for k, v in kwargs.items():
            cur.execute(
                f"UPDATE mod_settings SET {k} = ? WHERE guild_id = ?",
                (v, guild_id),
            )
        conn.commit()


def add_infraction(
    guild_id: int, user_id: int, mod_id: int, reason: str
) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO infractions (guild_id, user_id, mod_id, reason,"
            " timestamp) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, reason, now),
        )
        conn.commit()
        return cur.lastrowid


def get_infractions(guild_id: int, user_id: int) -> list[dict]:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM infractions WHERE guild_id = ? AND user_id = ? ORDER"
            " BY id DESC",
            (guild_id, user_id),
        )
        return [dict(r) for r in cur.fetchall()]


def remove_infraction(guild_id: int, infraction_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM infractions WHERE guild_id = ? AND id = ?",
            (guild_id, infraction_id),
        )
        conn.commit()
        return cur.rowcount > 0


def clear_infractions(guild_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM infractions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        conn.commit()
        return cur.rowcount


# ==============================================================================
# AUDIT & DISPATCH UTILITIES
# ==============================================================================
async def send_punishment_dm(
    member: discord.Member,
    action: str,
    reason: str,
    duration: Optional[str] = None,
) -> bool:
    """Dispatches a formatted direct message notice to the disciplined user."""
    try:
        embed = discord.Embed(
            title=f"Moderation Notice: {action}",
            description=(
                f"You have received a moderation action in"
                f" **{member.guild.name}**."
            ),
            color=discord.Color.red(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        if member.guild.icon:
            embed.set_thumbnail(url=member.guild.icon.url)
        embed.add_field(name="Server", value=member.guild.name, inline=True)
        embed.add_field(name="Action Taken", value=action, inline=True)
        if duration:
            embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(
            name="Reason", value=reason or "No reason provided", inline=False
        )
        embed.set_footer(
            text="If you believe this was an error, contact server staff."
        )

        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def send_log_embed(guild: discord.Guild, embed: discord.Embed):
    cfg = get_mod_config(guild.id)
    channel = None
    if cfg["log_channel_id"]:
        channel = guild.get_channel(cfg["log_channel_id"])
    if not channel:
        channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)

    if channel and channel.permissions_for(guild.me).send_messages:
        await channel.send(embed=embed)


def is_staff(member: discord.Member) -> bool:
    if (
        member.id == AUTHORIZED_OWNER_ID
        or member.guild_permissions.administrator
    ):
        return True
    return any(r.name == STAFF_ROLE_NAME for r in member.roles)


def format_text(template: str, member: discord.Member) -> str:
    if not template:
        return ""
    return (
        template.replace("{user}", member.name)
        .replace("{mention}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
        .replace("{created_at}", member.created_at.strftime("%b %d, %Y"))
    )


def parse_ticket_topic(topic: Optional[str]) -> dict:
    data = {"author_id": None, "claimer_id": None}
    if not topic:
        return data
    for part in topic.split("|"):
        part = part.strip()
        if part.startswith("Ticket Author:"):
            try:
                data["author_id"] = int(
                    part.replace("Ticket Author:", "").strip()
                )
            except ValueError:
                pass
        elif part.startswith("Claimed by:"):
            val = part.replace("Claimed by:", "").strip()
            if val != "None":
                try:
                    data["claimer_id"] = int(val)
                except ValueError:
                    pass
    return data


async def fetch_image_bytes(
    image_source: Optional[str] = None,
    attachment: Optional[discord.Attachment] = None,
) -> Optional[bytes]:
    """Retrieves binary image data from an attachment, an os file path, or an HTTP URL."""
    if attachment:
        return await attachment.read()

    if image_source:
        # Check local path using os
        if os.path.exists(image_source) and os.path.isfile(image_source):
            with open(image_source, "rb") as f:
                return f.read()

        # Fetch from remote URL
        if image_source.startswith("http://") or image_source.startswith(
            "https://"
        ):
            async with aiohttp.ClientSession() as session:
                async with session.get(image_source) as resp:
                    if resp.status == 200:
                        return await resp.read()
    return None


# ==============================================================================
# TICKET VIEWS & INTERFACE
# ==============================================================================
class TicketCloseConfirmView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="Confirm Close", style=discord.ButtonStyle.danger, emoji="🔒"
    )
    async def confirm_close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Closing and deleting ticket channel in 5 seconds..."
        )
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Closed by {interaction.user}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Ticket closure cancelled.", ephemeral=True
        )
        self.stop()


class TicketActionView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        custom_id="ticket_action_claim",
        emoji="🙋‍♂️",
    )
    async def claim_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await handle_claim(interaction)

    @discord.ui.button(
        label="Unclaim",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_action_unclaim",
        emoji="🔄",
    )
    async def unclaim_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await handle_unclaim(interaction)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_action_close",
        emoji="🔒",
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            view=TicketCloseConfirmView(),
            ephemeral=True,
        )


class TicketLauncherView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_launcher_create",
        emoji="📩",
    )
    async def create_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        guild = interaction.guild
        user = interaction.user

        for ch in guild.text_channels:
            if ch.topic and f"Ticket Author: {user.id}" in ch.topic:
                await interaction.response.send_message(
                    f"You already have an open ticket: {ch.mention}",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)

        category = discord.utils.get(
            guild.categories, name=TICKET_CATEGORY_NAME
        )
        if not category:
            category = await guild.create_category(name=TICKET_CATEGORY_NAME)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

        clean_name = "".join(
            c for c in user.name if c.isalnum() or c in ("-", "_")
        ).lower()
        channel_name = f"ticket-{clean_name[:15] or user.id}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket Author: {user.id} | Claimed by: None",
        )

        embed = discord.Embed(
            title=f"Support Ticket - {user.display_name}",
            description=(
                f"Welcome {user.mention}!\n\n"
                "Please describe your issue in detail. Support staff will"
                " assist shortly.\n"
                "• Click **Claim** to assign this ticket (Staff only).\n"
                "• Click **Unclaim** to return it to the queue.\n"
                "• Click **Close** when resolved."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text="Support System")
        await ticket_channel.send(
            content=f"{user.mention}", embed=embed, view=TicketActionView()
        )
        await interaction.followup.send(
            f"Ticket opened: {ticket_channel.mention}", ephemeral=True
        )


async def handle_claim(interaction: discord.Interaction):
    channel = interaction.channel
    topic_data = parse_ticket_topic(channel.topic)
    if not topic_data["author_id"]:
        await interaction.response.send_message(
            "This command is only valid inside a ticket channel.",
            ephemeral=True,
        )
        return
    if not is_staff(interaction.user):
        await interaction.response.send_message(
            "Only staff members can claim tickets.", ephemeral=True
        )
        return
    if topic_data["claimer_id"]:
        await interaction.response.send_message(
            f"This ticket is already claimed by <@{topic_data['claimer_id']}>.",
            ephemeral=True,
        )
        return

    new_topic = f"Ticket Author: {topic_data['author_id']} | Claimed by: {interaction.user.id}"
    try:
        await channel.edit(
            name=f"claimed-{interaction.user.name.lower()[:15]}",
            topic=new_topic,
        )
    except discord.HTTPException:
        pass

    embed = discord.Embed(
        title="Ticket Claimed",
        description=(
            f"This ticket has been claimed by {interaction.user.mention}."
        ),
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    await interaction.response.send_message(embed=embed)


async def handle_unclaim(interaction: discord.Interaction):
    channel = interaction.channel
    topic_data = parse_ticket_topic(channel.topic)
    if not topic_data["author_id"]:
        await interaction.response.send_message(
            "This command is only valid inside a ticket channel.",
            ephemeral=True,
        )
        return
    if not is_staff(interaction.user):
        await interaction.response.send_message(
            "Only staff members can unclaim tickets.", ephemeral=True
        )
        return
    if not topic_data["claimer_id"]:
        await interaction.response.send_message(
            "This ticket is not currently claimed.", ephemeral=True
        )
        return
    if (
        topic_data["claimer_id"] != interaction.user.id
        and not interaction.user.guild_permissions.administrator
        and interaction.user.id != AUTHORIZED_OWNER_ID
    ):
        await interaction.response.send_message(
            f"Only <@{topic_data['claimer_id']}> or an Administrator can"
            
