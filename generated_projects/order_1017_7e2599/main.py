import os
import logging
import asyncio
import random
import json
from datetime import datetime, timedelta

import discord
from discord import app_commands, Interaction, PermissionOverwrite, Permissions, Embed
from discord.ext import commands, tasks
import asyncpg
from dotenv import load_dotenv
from aiohttp import web

# -------------------------------------------------
# Load environment variables
# -------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID")
SUPPORT_ROLE_ID = os.getenv("SUPPORT_ROLE_ID")
ARCHIVE_CATEGORY_ID = os.getenv("ARCHIVE_CATEGORY_ID")
PORT = int(os.getenv("PORT", 8080))

# -------------------------------------------------
# Basic validation of critical env vars
# -------------------------------------------------
missing = []
for var_name, var_value in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("DATABASE_URL", DATABASE_URL),
    ("WELCOME_CHANNEL_ID", WELCOME_CHANNEL_ID),
    ("SUPPORT_ROLE_ID", SUPPORT_ROLE_ID),
    ("ARCHIVE_CATEGORY_ID", ARCHIVE_CATEGORY_ID),
]:
    if not var_value:
        missing.append(var_name)

if missing:
    raise RuntimeError(f"Відсутні необхідні змінні середовища: {', '.join(missing)}")

WELCOME_CHANNEL_ID = int(WELCOME_CHANNEL_ID)
SUPPORT_ROLE_ID = int(SUPPORT_ROLE_ID)
ARCHIVE_CATEGORY_ID = int(ARCHIVE_CATEGORY_ID)

# -------------------------------------------------
# Logging configuration
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("grox-bot")

# -------------------------------------------------
# Database utilities
# -------------------------------------------------
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    member_id BIGINT PRIMARY KEY,
                    xp BIGINT NOT NULL DEFAULT 0,
                    level INT NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id SERIAL PRIMARY KEY,
                    member_id BIGINT NOT NULL,
                    moderator_id BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    channel_id BIGINT NOT NULL,
                    creator_id BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    closed_at TIMESTAMP
                );
                """
            )
        log.info("База даних підключена і готова.")

    async def close(self):
        if self.pool:
            await self.pool.close()
            log.info("База даних закрита.")

    # ------------------- members -------------------
    async def get_member(self, member_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT xp, level FROM members WHERE member_id=$1", member_id
            )
            if row:
                return {"xp": row["xp"], "level": row["level"]}
            else:
                await conn.execute(
                    "INSERT INTO members (member_id, xp, level) VALUES ($1, 0, 0)",
                    member_id,
                )
                return {"xp": 0, "level": 0}

    async def update_member(self, member_id: int, xp: int, level: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO members (member_id, xp, level) VALUES ($1, $2, $3)
                ON CONFLICT (member_id) DO UPDATE SET xp = $2, level = $3
                """,
                member_id,
                xp,
                level,
            )

    # ------------------- warnings -------------------
    async def add_warning(self, member_id: int, moderator_id: int, reason: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO warnings (member_id, moderator_id, reason)
                VALUES ($1, $2, $3)
                """,
                member_id,
                moderator_id,
                reason,
            )

    async def get_warnings(self, member_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, moderator_id, reason, timestamp
                FROM warnings
                WHERE member_id=$1
                ORDER BY timestamp DESC
                """,
                member_id,
            )
            return rows

    # ------------------- tickets -------------------
    async def create_ticket(self, channel_id: int, creator_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tickets (channel_id, creator_id, status)
                VALUES ($1, $2, 'open')
                """,
                channel_id,
                creator_id,
            )

    async def close_ticket(self, channel_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tickets
                SET status='closed', closed_at=now()
                WHERE channel_id=$1 AND status='open'
                """,
                channel_id,
            )

    async def get_user_tickets(self, creator_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, channel_id, status, created_at, closed_at
                FROM tickets
                WHERE creator_id=$1
                ORDER BY created_at DESC
                """,
                creator_id,
            )
            return rows

# -------------------------------------------------
# Bot definition
# -------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
db = Database(DATABASE_URL)

# Global flag for health endpoint
discord_ready = False

# Simple in‑memory anti‑spam tracker
spam_tracker: dict[int, list[datetime]] = {}

# -------------------------------------------------
# Helper functions
# -------------------------------------------------
def xp_needed_for_next_level(level: int) -> int:
    """Формула: XP_needed = 5 * (level ** 2) + 50 * level + 100"""
    return 5 * (level ** 2) + 50 * level + 100


async def add_xp(member: discord.Member, amount: int):
    data = await db.get_member(member.id)
    new_xp = data["xp"] + amount
    level = data["level"]
    needed = xp_needed_for_next_level(level)

    while new_xp >= needed:
        new_xp -= needed
        level += 1
        needed = xp_needed_for_next_level(level)

    await db.update_member(member.id, new_xp, level)
    return level, new_xp, needed


def is_spamming(user_id: int) -> bool:
    now = datetime.utcnow()
    timestamps = spam_tracker.get(user_id, [])
    # Keep only timestamps within last 5 seconds
    timestamps = [t for t in timestamps if now - t < timedelta(seconds=5)]
    timestamps.append(now)
    spam_tracker[user_id] = timestamps
    return len(timestamps) > 5


# -------------------------------------------------
# Events
# -------------------------------------------------
@bot.event
async def on_ready():
    global discord_ready
    discord_ready = True
    log.info(f"Підключено як {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        log.info("Слеш‑команди синхронізовано.")
    except Exception as e:
        log.exception("Не вдалося синхронізувати команди:", exc_info=e)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Anti‑spam check
    if is_spamming(message.author.id):
        try:
            await message.author.timeout(
                timedelta(minutes=10), reason="Спам повідомлень"
            )
            await message.channel.send(
                f"{message.author.mention}, ви отримали тайм‑аут на 10 хвилин за спам."
            )
        except discord.Forbidden:
            log.warning("Недостатньо прав для тайм‑ауту користувача %s", message.author)
        return

    # Mass mention protection
    if "@everyone" in message.content or "@here" in message.content:
        await message.delete()
        await message.channel.send(
            f"{message.author.mention}, масові згадки заборонені.", delete_after=5
        )
        return

    # XP award (only if not spam)
    xp_gain = random.randint(5, 15)
    level, total_xp, next_needed = await add_xp(message.author, xp_gain)

    # Optionally send level‑up message
    if total_xp == 0:  # means we just leveled up
        await message.channel.send(
            f"🎉 {message.author.mention} піднявся до **{level}** рівня! Вітаємо!"
        )

    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        await channel.send(
            f"👋 Вітаємо {member.mention} у сервері **{member.guild.name}**! Сподіваємось, вам тут сподобається."
        )


@bot.event
async def on_member_remove(member: discord.Member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        await channel.send(f"😢 {member.mention} покинув сервер.")


# -------------------------------------------------
# Error handling
# -------------------------------------------------
@bot.tree.error
async def on_app_command_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ У вас немає необхідних прав для виконання цієї команди.", ephemeral=True
        )
    elif isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "❌ Ви не маєте доступу до цієї команди.", ephemeral=True
        )
    else:
        log.exception("Помилка під час виконання команди:", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "⚠️ Сталася помилка під час обробки вашої команди.", ephemeral=True
            )


# -------------------------------------------------
# Moderation commands
# -------------------------------------------------
@bot.tree.command(name="ban", description="Забанити користувача.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(user="Користувач, якого треба банити", reason="Причина бану")
async def ban(interaction: Interaction, user: discord.Member, reason: str = "Без причини"):
    try:
        await user.ban(reason=reason)
        await interaction.response.send_message(
            f"✅ {user.mention} був забанений. Причина: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Недостатньо прав для бану.", ephemeral=True
        )


@bot.tree.command(name="unban", description="Розбанити користувача.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(user_id="ID користувача, якого треба розбанити")
async def unban(interaction: Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ {user.mention} розбанений.")
    except discord.NotFound:
        await interaction.response.send_message("❌ Користувач не знайдений.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Недостатньо прав для розбану.", ephemeral=True
        )


@bot.tree.command(name="kick", description="Вигнати користувача.")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(user="Користувач, якого треба вигнати", reason="Причина")
async def kick(interaction: Interaction, user: discord.Member, reason: str = "Без причини"):
    try:
        await user.kick(reason=reason)
        await interaction.response.send_message(
            f"✅ {user.mention} був вигнаний. Причина: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Недостатньо прав для вигнання.", ephemeral=True
        )


@bot.tree.command(name="mute", description="Тайм‑аут користувача.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(user="Користувач", minutes="Тривалість у хвилинах", reason="Причина")
async def mute(
    interaction: Interaction,
    user: discord.Member,
    minutes: int = 10,
    reason: str = "Без причини",
):
    try:
        await user.timeout(timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(
            f"✅ {user.mention} отримав тайм‑аут на {minutes} хв. Причина: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Недостатньо прав для тайм‑ауту.", ephemeral=True
        )


@bot.tree.command(name="warn", description="Видати попередження.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(user="Користувач", reason="Причина")
async def warn(interaction: Interaction, user: discord.Member, reason: str):
    await db.add_warning(user.id, interaction.user.id, reason)
    await interaction.response.send_message(
        f"⚠️ {user.mention} отримав попередження. Причина: {reason}"
    )


@bot.tree.command(name="warnings", description="Переглянути історію попереджень.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(user="Користувач")
async def warnings(interaction: Interaction, user: discord.Member):
    rows = await db.get_warnings(user.id)
    if not rows:
        await interaction.response.send_message("⚪ У користувача немає попереджень.")
        return

    embed = Embed(title=f"Попередження для {user}", colour=discord.Color.orange())
    for row in rows[:10]:
        embed.add_field(
            name=f"ID {row['id']} — {row['timestamp'].strftime('%Y-%m-%d %H:%M')}",
            value=f"Модератор: <@{row['moderator_id']}>\nПричина: {row['reason']}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


# -------------------------------------------------
# XP commands
# -------------------------------------------------
@bot.tree.command(name="rank", description="Переглянути ваш рівень та XP.")
async def rank(interaction: Interaction, member: discord.Member = None):
    member = member or interaction.user
    data = await db.get_member(member.id)
    needed = xp_needed_for_next_level(data["level"])
    embed = Embed(title=f"Ранг {member}", colour=discord.Color.green())
    embed.add_field(name="Рівень", value=str(data["level"]), inline=True)
    embed.add_field(name="XP", value=f"{data['xp']} / {needed}", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Топ‑10 користувачів за XP.")
async def leaderboard(interaction: Interaction):
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT member_id, xp, level FROM members
            ORDER BY xp DESC, level DESC
            LIMIT 10
            """
        )
    embed = Embed(title="Топ‑10 користувачів", colour=discord.Color.gold())
    for i, row in enumerate(rows, start=1):
        user = bot.get_user(row["member_id"])
        name = user.name if user else f"ID {row['member_id']}"
        embed.add_field(
            name=f"{i}. {name}",
            value=f"Рівень: {row['level']}, XP: {row['xp']}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


# -------------------------------------------------
# Ticket system
# -------------------------------------------------
@bot.tree.command(name="ticket", description="Управління тікетами.")
@app_commands.describe(action="Дія (open/close/history)", reason="Причина (для open)")
async def ticket(interaction: Interaction, action: str, reason: str = "Не вказано"):
    if action.lower() == "open":
        await open_ticket(interaction, reason)
    elif action.lower() == "close":
        await close_ticket(interaction)
    elif action.lower() == "history":
        await ticket_history(interaction)
    else:
        await interaction.response.send_message(
            "❌ Невірна дія. Доступні: open, close, history.", ephemeral=True
        )


async def open_ticket(interaction: Interaction, reason: str):
    guild = interaction.guild
    overwrites = {
        guild.default_role: PermissionOverwrite(read_messages=False),
        interaction.user: PermissionOverwrite(read_messages=True, send_messages=True),
        guild.get_role(SUPPORT_ROLE_ID): PermissionOverwrite(read_messages=True, send_messages=True),
    }
    channel = await guild.create_text_channel(
        name=f"ticket-{interaction.user.name}",
        overwrites=overwrites,
        reason=reason,
    )
    await db.create_ticket(channel.id, interaction.user.id)
    await channel.send(
        f"🎫 Тікет створено {interaction.user.mention}. Підтримка скоро відповість."
    )
    await interaction.response.send_message(
        f"✅ Тікет створено: {channel.mention}", ephemeral=True
    )


async def close_ticket(interaction: Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Ця команда працює лише у текстових каналах.", ephemeral=True
        )
        return
    archive_category = interaction.guild.get_channel(ARCHIVE_CATEGORY_ID)
    if not isinstance(archive_category, discord.CategoryChannel):
        await interaction.response.send_message(
            "❌ Категорія архіву не знайдена.", ephemeral=True
        )
        return
    await db.close_ticket(channel.id)
    await channel.edit(category=archive_category)
    await interaction.response.send_message("✅ Тікет закрито та переміщено в архів.")


async def ticket_history(interaction: Interaction):
    rows = await db.get_user_tickets(interaction.user.id)
    if not rows:
        await interaction.response.send_message("⚪ У вас ще немає тікетів.", ephemeral=True)
        return
    embed = Embed(title="Історія тікетів", colour=discord.Color.blurple())
    for row in rows[:10]:
        status = row["status"]
        created = row["created_at"].strftime("%Y-%m-%d")
        closed = row["closed_at"].strftime("%Y-%m-%d") if row["closed_at"] else "—"
        embed.add_field(
            name=f"Тікет #{row['id']} ({status})",
            value=f"Створено: {created}\nЗакрито: {closed}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


# -------------------------------------------------
# Info commands
# -------------------------------------------------
@bot.tree.command(name="help", description="Список всіх команд.")
async def help_cmd(interaction: Interaction):
    embed = Embed(title="🛠️ Команди бота", colour=discord.Color.dark_green())
    embed.add_field(
        name="/help",
        value="Показати це повідомлення.",
        inline=False,
    )
    embed.add_field(
        name="/ping",
        value="Перевірити затримку.",
        inline=False,
    )
    embed.add_field(
        name="/serverinfo",
        value="Інформація про сервер.",
        inline=False,
    )
    embed.add_field(
        name="/userinfo [користувач]",
        value="Інформація про користувача.",
        inline=False,
    )
    embed.add_field(
        name="/stats",
        value="Статистика бота.",
        inline=False,
    )
    embed.add_field(
        name="/rank [користувач]",
        value="Ваш XP та рівень.",
        inline=False,
    )
    embed.add_field(
        name="/leaderboard",
        value="Топ‑10 за XP.",
        inline=False,
    )
    embed.add_field(
        name="/ticket open/close/history",
        value="Керування тікетами.",
        inline=False,
    )
    embed.add_field(
        name="/ban, /unban, /kick, /mute, /warn, /warnings",
        value="Модераційні інструменти.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Перевірка затримки.")
async def ping(interaction: Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


@bot.tree.command(name="serverinfo", description="Інформація про сервер.")
async def serverinfo(interaction: Interaction):
    guild = interaction.guild
    embed = Embed(title=f"Інфо про сервер: {guild.name}", colour=discord.Color.blue())
    embed.add_field(name="ID", value=str(guild.id), inline=True)
    embed.add_field(name="Власник", value=str(guild.owner), inline=True)
    embed.add_field(name="Учасників", value=str(guild.member_count), inline=True)
    embed.add_field(
        name="Створено", value=guild.created_at.strftime("%Y-%m-%d"), inline=True
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="Інформація про користувача.")
@app_commands.describe(user="Користувач (необов'язково)")
async def userinfo(interaction: Interaction, user: discord.Member = None):
    user = user or interaction.user
    embed = Embed(title=f"Користувач: {user}", colour=discord.Color.purple())
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Створено акаунт", value=user.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Приєднався", value=user.joined_at.strftime("%Y-%m-%d"), inline=True)
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Статистика бота.")
async def stats(interaction: Interaction):
    guilds = len(bot.guilds)
    users = sum(guild.member_count for guild in bot.guilds)
    async with db.pool.acquire() as conn:
        ticket_count = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status='open'")
    embed = Embed(title="Статистика бота", colour=discord.Color.dark_gray())
    embed.add_field(name="Сервери", value=str(guilds), inline=True)
    embed.add_field(name="Користувачі", value=str(users), inline=True)
    embed.add_field(name="Відкриті тікети", value=str(ticket_count), inline=True)
    await interaction.response.send_message(embed=embed)


# -------------------------------------------------
# Health HTTP endpoint
# -------------------------------------------------
async def health_handler(request):
    data = {"status": "ok", "discord_ready": discord_ready}
    return web.json_response(data)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health endpoint запущено на порту {PORT}.")


# -------------------------------------------------
# Bot startup & shutdown
# -------------------------------------------------
@bot.event
async def setup_hook():
    # Запуск health‑серверу у фоні
    bot.loop.create_task(start_health_server())
    await db.connect()


@bot.event
async def close():
    await db.close()


def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN не вказаний у змінних середовища.")
        return
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        log.exception("Помилка під час запуску бота:", exc_info=e)


if __name__ == "__main__":
    main()