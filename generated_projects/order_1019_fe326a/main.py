#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import asyncio
import logging
import random
import re
import json
from datetime import datetime, timedelta

from dotenv import load_dotenv
from aiohttp import web

import discord
from discord import Intents, Embed, Colour, PermissionOverwrite, app_commands
from discord.ext import commands

import asyncpg

# --------------------------------------------------------------
# Load environment
# --------------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID")
SUPPORT_ROLE_ID = os.getenv("SUPPORT_ROLE_ID")
PORT = int(os.getenv("PORT", "8080"))

missing = []
for name, val in [("DISCORD_TOKEN", DISCORD_TOKEN),
                  ("DATABASE_URL", DATABASE_URL),
                  ("WELCOME_CHANNEL_ID", WELCOME_CHANNEL_ID),
                  ("SUPPORT_ROLE_ID", SUPPORT_ROLE_ID)]:
    if not val:
        missing.append(name)

if missing:
    sys.stderr.write(f"❌ Missing environment variables: {', '.join(missing)}\n")
    sys.exit(1)

WELCOME_CHANNEL_ID = int(WELCOME_CHANNEL_ID)
SUPPORT_ROLE_ID = int(SUPPORT_ROLE_ID)

# --------------------------------------------------------------
# Logging
# --------------------------------------------------------------
if not os.path.isdir("logs"):
    os.makedirs("logs")
log_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
)
log_format = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
log_handler.setFormatter(log_format)

logger = logging.getLogger("grox_bot")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# --------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn, command_timeout=60)
        async with self.pool.acquire() as conn:
            # moderation logs
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_logs (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    moderator_id BIGINT,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                );
                """
            )
            # warnings (separate for easy lookup)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS warnings (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    moderator_id BIGINT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                );
                """
            )
            # xp system
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS xp (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    xp BIGINT NOT NULL DEFAULT 0,
                    level BIGINT NOT NULL DEFAULT 1,
                    PRIMARY KEY (guild_id, user_id)
                );
                """
            )
            # tickets
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    author_id BIGINT NOT NULL,
                    subject TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    closed_at TIMESTAMP
                );
                """
            )

    async def close(self):
        await self.pool.close()

db = Database(DATABASE_URL)

# --------------------------------------------------------------
# Helper utilities
# --------------------------------------------------------------
def is_url(text: str) -> bool:
    url_regex = re.compile(
        r"(https?://|www\.)[^\s]+", re.IGNORECASE
    )
    return bool(url_regex.search(text))

def count_urls(text: str) -> int:
    url_regex = re.compile(
        r"(https?://|www\.)[^\s]+", re.IGNORECASE
    )
    return len(url_regex.findall(text))

def get_xp_needed(level: int) -> int:
    # simple exponential curve
    return 100 * (level ** 2)

# --------------------------------------------------------------
# Bot definition
# --------------------------------------------------------------
intents = Intents.default()
intents.members = True
intents.message_content = True

class GroxBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!", intents=intents, help_command=None
        )
        self.db = db
        self.spam_tracker = {}  # {(guild_id, message_content): [timestamps]}
        self.ready_event = asyncio.Event()

    async def setup_hook(self):
        # Load cogs defined below (they are classes, not separate modules)
        await self.add_cog(Greetings(self))
        await self.add_cog(Moderation(self))
        await self.add_cog(AntiSpam(self))
        await self.add_cog(XPSystem(self))
        await self.add_cog(Tickets(self))
        await self.add_cog(Info(self))

        # Sync slash commands once ready
        self.tree.on_error = self.on_app_command_error

    async def on_ready(self):
        logger.info(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        self.ready_event.set()

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.exception("App command error", exc_info=error)
        await interaction.response.send_message(
            f"❌ Сталася помилка: {str(error)}", ephemeral=True
        )

bot = GroxBot()

# --------------------------------------------------------------
# COGS
# --------------------------------------------------------------

# 1. Greetings (welcome/leave)
class Greetings(commands.Cog):
    def __init__(self, bot: GroxBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if not channel:
            return
        embed = Embed(
            title="👋 Ласкаво просимо!",
            description=f"{member.mention}, радi вітати вас на сервері!",
            colour=Colour.green(),
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if not channel:
            return
        embed = Embed(
            title="👋 Прощання",
            description=f"{member.mention} покинув сервер.",
            colour=Colour.red(),
            timestamp=datetime.utcnow(),
        )
        await channel.send(embed=embed)

# 2. Moderation
class Moderation(commands.Cog):
    def __init__(self, bot: GroxBot):
        self.bot = bot

    async def log_action(self, guild_id, user_id, action, reason=None, moderator_id=None):
        async with self.bot.db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO moderation_logs (guild_id, user_id, action, reason, moderator_id)
                VALUES ($1,$2,$3,$4,$5)
                """,
                guild_id, user_id, action, reason, moderator_id,
            )
            if action == "warn":
                await conn.execute(
                    """
                    INSERT INTO warnings (guild_id, user_id, reason, moderator_id)
                    VALUES ($1,$2,$3,$4)
                    """,
                    guild_id, user_id, reason, moderator_id,
                )

    @app_commands.command(name="ban", description="Бан користувача")
    @app_commands.describe(user="Користувач", reason="Причина")
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = None):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ У вас немає прав для бану.", ephemeral=True)
            return
        await user.ban(reason=reason)
        await self.log_action(interaction.guild.id, user.id, "ban", reason, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} забанений.", ephemeral=False)

    @app_commands.command(name="unban", description="Розбанити користувача")
    @app_commands.describe(user_id="ID користувача")
    async def unban(self, interaction: discord.Interaction, user_id: int):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ У вас немає прав для розбану.", ephemeral=True)
            return
        user = await interaction.guild.fetch_ban(discord.Object(id=user_id))
        await interaction.guild.unban(user.user)
        await self.log_action(interaction.guild.id, user_id, "unban", moderator_id=interaction.user.id)
        await interaction.response.send_message(f"✅ Користувач з ID `{user_id}` розбанений.", ephemeral=False)

    @app_commands.command(name="kick", description="Кік користувача")
    @app_commands.describe(user="Користувач", reason="Причина")
    async def kick(self, interaction: discord.InterInteraction, user: discord.Member, reason: str = None):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ У вас немає прав для кік.", ephemeral=True)
            return
        await user.kick(reason=reason)
        await self.log_action(interaction.guild.id, user.id, "kick", reason, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} вигнано.", ephemeral=False)

    @app_commands.command(name="mute", description="Таймаут користувача")
    @app_commands.describe(user="Користувач", duration="Тривалість (сек)")
    async def mute(self, interaction: discord.Interaction, user: discord.Member, duration: int):
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ У вас немає прав для тайм-ауту.", ephemeral=True)
            return
        until = datetime.utcnow() + timedelta(seconds=duration)
        await user.timeout(until, reason=f"Мут від {interaction.user}")
        await self.log_action(interaction.guild.id, user.id, "mute", f"{duration} sec", interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} замучений на {duration} секунд.", ephemeral=False)

    @app_commands.command(name="warn", description="Видача попередження")
    @app_commands.describe(user="Користувач", reason="Причина")
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ У вас немає прав для попереджень.", ephemeral=True)
            return
        await self.log_action(interaction.guild.id, user.id, "warn", reason, interaction.user.id)
        await interaction.response.send_message(f"⚠️ {user.mention} отримав попередження: {reason}", ephemeral=False)

    @app_commands.command(name="warnings", description="Перегляд попереджень користувача")
    @app_commands.describe(user="Користувач")
    async def warnings(self, interaction: discord.Interaction, user: discord.Member):
        async with self.bot.db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT reason, created_at, moderator_id FROM warnings
                WHERE guild_id=$1 AND user_id=$2 ORDER BY created_at DESC
                """,
                interaction.guild.id, user.id,
            )
        if not rows:
            await interaction.response.send_message(f"ℹ️ У {user.mention} немає попереджень.", ephemeral=True)
            return
        embed = Embed(title=f"⚠️ Попередження {user}", colour=Colour.orange())
        for i, r in enumerate(rows[:10], start=1):
            mod = interaction.guild.get_member(r["moderator_id"])
            mod_name = mod.display_name if mod else "Unknown"
            embed.add_field(
                name=f"{i}. {r['created_at'].strftime('%Y-%m-%d %H:%M')}",
                value=f"**Модератор:** {mod_name}\n**Причина:** {r['reason']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# 3. Anti‑spam
class AntiSpam(commands.Cog):
    def __init__(self, bot: GroxBot):
        self.bot = bot
        self.message_cache = {}  # {(guild_id, content): [timestamps]}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # ---- Repeat detection ----
        key = (message.guild.id, message.content.lower())
        now = datetime.utcnow().timestamp()
        timestamps = self.message_cache.get(key, [])
        timestamps = [t for t in timestamps if now - t < 5]  # keep last 5 sec
        timestamps.append(now)
        self.message_cache[key] = timestamps

        if len(timestamps) > 3:
            await self._handle_spam(message, "повторюване повідомлення")
            return

        # ---- URL count detection ----
        if count_urls(message.content) > 5:
            await self._handle_spam(message, "забагато посилань")
            return

    async def _handle_spam(self, message: discord.Message, reason: str):
        await message.delete()
        # Simple escalation: first offense warning, second mute 10 sec
        user_warnings = getattr(message.author, "grox_warns", 0) + 1
        setattr(message.author, "grox_warns", user_warnings)

        if user_warnings == 1:
            await message.channel.send(
                f"{message.author.mention} ⚠️ Попередження: {reason}. Будь ласка, не спамте."
            )
        else:
            await message.author.timeout(datetime.utcnow() + timedelta(seconds=10), reason=reason)
            await message.channel.send(
                f"{message.author.mention} 🔇 Ви отримали тайм-аут на 10 секунд за спам."
            )
            setattr(message.author, "grox_warns", 0)  # reset

# 4. XP system
class XPSystem(commands.Cog):
    def __init__(self, bot: GroxBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        async with self.bot.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT xp, level FROM xp WHERE guild_id=$1 AND user_id=$2",
                message.guild.id, message.author.id,
            )
            if row:
                xp = row["xp"] + random.randint(1, 5)
                level = row["level"]
                await conn.execute(
                    "UPDATE xp SET xp=$1 WHERE guild_id=$2 AND user_id=$3",
                    xp, message.guild.id, message.author.id,
                )
            else:
                xp = random.randint(1, 5)
                level = 1
                await conn.execute(
                    "INSERT INTO xp (guild_id, user_id, xp, level) VALUES ($1,$2,$3,$4)",
                    message.guild.id, message.author.id, xp, level,
                )
            # Level up?
            needed = get_xp_needed(level)
            if xp >= needed:
                level += 1
                await conn.execute(
                    "UPDATE xp SET level=$1, xp=$2 WHERE guild_id=$3 AND user_id=$4",
                    level, xp - needed, message.guild.id, message.author.id,
                )
                await message.channel.send(
                    f"🎉 {message.author.mention} підвищив рівень до **{level}**!"
                )

    @app_commands.command(name="rank", description="Переглянути ваш рівень")
    async def rank(self, interaction: discord.Interaction):
        async with self.bot.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT xp, level FROM xp WHERE guild_id=$1 AND user_id=$2",
                interaction.guild.id, interaction.user.id,
            )
        if not row:
            await interaction.response.send_message("ℹ️ У вас ще немає XP.", ephemeral=True)
            return
        xp = row["xp"]
        level = row["level"]
        next_needed = get_xp_needed(level) - xp
        embed = Embed(
            title=f"📊 Ранг {interaction.user}",
            colour=Colour.blue(),
        )
        embed.add_field(name="Рівень", value=str(level))
        embed.add_field(name="XP", value=f"{xp} / {get_xp_needed(level)}")
        embed.set_thumbnail(url=interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="Топ користувачів за XP")
    async def leaderboard(self, interaction: discord.Interaction):
        async with self.bot.db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, xp, level FROM xp
                WHERE guild_id=$1 ORDER BY xp DESC LIMIT 10
                """,
                interaction.guild.id,
            )
        embed = Embed(title="🏆 Топ 10 користувачів", colour=Colour.gold())
        for i, r in enumerate(rows, start=1):
            member = interaction.guild.get_member(r["user_id"])
            name = member.display_name if member else f"ID {r['user_id']}"
            embed.add_field(
                name=f"{i}. {name}",
                value=f"Рівень: {r['level']} | XP: {r['xp']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=False)

# 5. Tickets
class Tickets(commands.Cog):
    def __init__(self, bot: GroxBot):
        self.bot = bot

    @app_commands.command(name="ticket", description="Система підтримки")
    @app_commands.describe(action="open/close", subject="Тема (тільки для open)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Відкрити", value="open"),
        app_commands.Choice(name="Закрити", value="close")
    ])
    async def ticket(self, interaction: discord.Interaction, action: str, subject: str = None):
        if action == "open":
            await self._open_ticket(interaction, subject)
        else:
            await self._close_ticket(interaction)

    async def _open_ticket(self, interaction: discord.Interaction, subject: str):
        guild = interaction.guild
        overwrites = {
            guild.default_role: PermissionOverwrite(read_messages=False),
            interaction.user: PermissionOverwrite(read_messages=True, send_messages=True),
            guild.get_role(SUPPORT_ROLE_ID): PermissionOverwrite(read_messages=True, send_messages=True)
        }
        channel_name = f"ticket-{interaction.user.name}"
        # Ensure unique name
        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            await interaction.response.send_message("⚠️ У вас уже є відкритий тикет.", ephemeral=True)
            return
        channel = await guild.create_text_channel(
            channel_name, overwrites=overwrites, reason="Ticket opened"
        )
        async with self.bot.db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tickets (guild_id, channel_id, author_id, subject)
                VALUES ($1,$2,$3,$4)
                """,
                guild.id, channel.id, interaction.user.id, subject or "Без теми",
            )
        await channel.send(
            f"Привіт {interaction.user.mention}! 🎫 Підтримка, будь ласка, відповідайте тут.\nТема: {subject or 'Без теми'}"
        )
        await interaction.response.send_message(f"✅ Тикет створено: {channel.mention}", ephemeral=True)

    async def _close_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        async with self.bot.db.pool.acquire() as conn:
            ticket = await conn.fetchrow(
                "SELECT * FROM tickets WHERE channel_id=$1 AND closed_at IS NULL",
                channel.id,
            )
            if not ticket:
                await interaction.response.send_message("❌ Це не тикет або вже закритий.", ephemeral=True)
                return
            await conn.execute(
                "UPDATE tickets SET closed_at=now() WHERE id=$1", ticket["id"]
            )
        await interaction.response.send_message("✅ Тикет закрито. Канал буде видалено через 5 секунд.", ephemeral=True)
        await asyncio.sleep(5)
        await channel.delete(reason="Ticket closed")

# 6. Info / basic commands
class Info(commands.Cog):
    def __init__(self, bot: GroxBot):
        self.bot = bot

    @app_commands.command(name="help", description="Список команд")
    async def help(self, interaction: discord.Interaction):
        embed = Embed(title="❓ Допомога", colour=Colour.blurple())
        embed.add_field(name="/help", value="Показати це повідомлення", inline=False)
        embed.add_field(name="/ping", value="Перевірити затримку бота", inline=False)
        embed.add_field(name="/serverinfo", value="Інформація про сервер", inline=False)
        embed.add_field(name="/userinfo", value="Дані про користувача", inline=False)
        embed.add_field(name="/stats", value="Статистика бота", inline=False)
        embed.add_field(name="/rank", value="Ваш рівень та XP", inline=False)
        embed.add_field(name="/leaderboard", value="Топ користувачів", inline=False)
        embed.add_field(name="/ticket open", value="Відкрити тикет підтримки", inline=False)
        embed.add_field(name="/ticket close", value="Закрити ваш тикет", inline=False)
        embed.add_field(name="/ban, /kick, /mute, /warn, /warnings", value="Модерація (слеш)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ping", description="Затримка бота")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong! 🏓 `{self.bot.latency*1000:.0f} ms`", ephemeral=True)

    @app_commands.command(name="serverinfo", description="Інфо про сервер")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        embed = Embed(title="📊 Інформація про сервер", colour=Colour.dark_green())
        embed.add_field(name="Назва", value=guild.name, inline=True)
        embed.add_field(name="ID", value=str(guild.id), inline=True)
        embed.add_field(name="Власник", value=guild.owner.mention if guild.owner else "Невідомо", inline=True)
        embed.add_field(name="Учасників", value=str(guild.member_count), inline=True)
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="userinfo", description="Інформація про користувача")
    @app_commands.describe(user="Користувач")
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member = None):
        user = user or interaction.user
        embed = Embed(title=f"👤 Інформація про {user}", colour=Colour.dark_blue())
        embed.add_field(name="ID", value=str(user.id), inline=True)
        embed.add_field(name="Створено акаунт", value=user.created_at.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="Приєднався", value=user.joined_at.strftime("%Y-%m-%d") if user.joined_at else "N/A", inline=True)
        embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="stats", description="Статистика бота")
    async def stats(self, interaction: discord.Interaction):
        uptime = datetime.utcnow() - self.bot.start_time
        embed = Embed(title="📈 Статистика бота", colour=Colour.dark_magenta())
        embed.add_field(name="Сервери", value=str(len(self.bot.guilds)), inline=True)
        total_members = sum(g.member_count for g in self.bot.guilds)
        embed.add_field(name="Користувачі", value=str(total_members), inline=True)
        embed.add_field(name="Uptime", value=str(uptime).split(".")[0], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        # store start time for uptime
        self.bot.start_time = datetime.utcnow()

# --------------------------------------------------------------
# Health HTTP endpoint
# --------------------------------------------------------------
async def health(request):
    ready = bot.is_ready()
    return web.json_response({"status": "ok", "discord_ready": ready})

async def start_http():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"💡 Health endpoint listening on port {PORT}")

# --------------------------------------------------------------
# Run everything
# --------------------------------------------------------------
async def main():
    await db.connect()
    await asyncio.gather(start_http(), bot.start(DISCORD_TOKEN))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")