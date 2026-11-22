import os
import json
import re
import asyncio
import datetime
from typing import List, Dict, Any

import aiohttp
from aiohttp import web
import asyncpg
from loguru import logger
import discord
from discord.ext import commands
from discord import app_commands, Interaction, Permissions, Member, Guild, User

# -------------------------------------------------
# Configuration & Environment
# -------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "8080"))

if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN is not set in environment")
    raise SystemExit(1)
if not GROQ_API_KEY:
    logger.error("GROQ_API_KEY is not set in environment")
    raise SystemExit(1)
if not DATABASE_URL:
    logger.error("DATABASE_URL is not set in environment")
    raise SystemExit(1)

# -------------------------------------------------
# Logging
# -------------------------------------------------
logger.add("bot.log", rotation="10 MB", level="INFO", backtrace=True, diagnose=True)

# -------------------------------------------------
# Database helpers
# -------------------------------------------------
db_pool: asyncpg.pool.Pool = None

CREATE_USER_CONTEXT_TABLE = """
CREATE TABLE IF NOT EXISTS user_context (
    user_id BIGINT PRIMARY KEY,
    context JSONB NOT NULL
);
"""

CREATE_MODERATION_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS moderation_logs (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_USER_CONTEXT_TABLE)
        await conn.execute(CREATE_MODERATION_LOGS_TABLE)
    logger.info("Database tables ensured")

async def get_user_context(user_id: int) -> List[Dict[str, str]]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT context FROM user_context WHERE user_id = $1", user_id
        )
        if row:
            return row["context"]
    return []

async def set_user_context(user_id: int, context: List[Dict[str, str]]):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_context (user_id, context)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET context = $2
            """,
            user_id,
            json.dumps(context),
        )

async def clear_user_context(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_context WHERE user_id = $1", user_id
        )

async def add_moderation_log(user_id: int, action: str, reason: str | None):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO moderation_logs (user_id, action, reason)
            VALUES ($1, $2, $3)
            """,
            user_id,
            action,
            reason,
        )
    logger.info(f"Moderation log – {action} for {user_id} (reason: {reason})")

# -------------------------------------------------
# Bot setup
# -------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------------------------------
# Spam / Flood detection (in‑memory)
# -------------------------------------------------
user_message_history: Dict[int, List[datetime.datetime]] = {}
user_repeat_tracker: Dict[int, Dict[str, Any]] = {}
SPAM_MESSAGE_LIMIT = 3          # same message > 3 times
FLOOD_COUNT = 5                # >5 msgs in 3 sec
FLOOD_INTERVAL = datetime.timedelta(seconds=3)

BLACKLISTED_DOMAINS = {
    "badexample.com",
    "malicious.ru",
    "phishing.net",
}
URL_REGEX = re.compile(r"https?://[^\s]+")

def is_moderator(member: Member) -> bool:
    return member.guild_permissions.manage_messages or member.guild_permissions.administrator

# -------------------------------------------------
# Helper – Groq API
# -------------------------------------------------
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

async def query_groq(messages: List[Dict[str, str]]) -> str:
    payload = {
        "model": "llama2-70b",
        "messages": messages,
        "temperature": 0.7,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(GROQ_ENDPOINT, headers=HEADERS, json=payload, timeout=30) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Groq API error {resp.status}: {text}")
                raise RuntimeError("AI service unavailable")
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

# -------------------------------------------------
# Events
# -------------------------------------------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("Bot is ready")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # ---------- Spam (repeated content) ----------
    repeat_info = user_repeat_tracker.get(message.author.id, {"content": None, "count": 0})
    if message.content == repeat_info["content"]:
        repeat_info["count"] += 1
    else:
        repeat_info = {"content": message.content, "count": 1}
    user_repeat_tracker[message.author.id] = repeat_info

    if repeat_info["count"] > SPAM_MESSAGE_LIMIT:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, повторювані повідомлення вважаються спамом.",
                delete_after=5,
            )
            logger.info(f"Deleted spam message from {message.author.id}")
        except discord.Forbidden:
            logger.warning("Missing permissions to delete spam")
        return

    # ---------- Flood ----------
    now = datetime.datetime.utcnow()
    timestamps = user_message_history.setdefault(message.author.id, [])
    timestamps.append(now)
    # keep only recent
    timestamps = [t for t in timestamps if now - t <= FLOOD_INTERVAL]
    user_message_history[message.author.id] = timestamps
    if len(timestamps) > FLOOD_COUNT:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, ви надсилаєте повідомлення надто швидко (флуд).",
                delete_after=5,
            )
            logger.info(f"Deleted flood message from {message.author.id}")
        except discord.Forbidden:
            logger.warning("Missing permissions to delete flood")
        return

    # ---------- Dangerous URLs ----------
    urls = URL_REGEX.findall(message.content)
    for url in urls:
        domain = re.sub(r"https?://", "", url).split("/")[0].lower()
        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention}, посилання заборонені.",
                    delete_after=5,
                )
                logger.info(f"Deleted blacklisted URL from {message.author.id}")
            except discord.Forbidden:
                logger.warning("Missing permissions to delete blacklisted URL")
            return

    await bot.process_commands(message)

@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception(f"Error in event {event}")

# -------------------------------------------------
# Slash Commands
# -------------------------------------------------
@bot.tree.command(name="ping", description="Показує затримку бота.")
async def ping(interaction: Interaction):
    latency = bot.latency * 1000
    await interaction.response.send_message(f"Pong! `{latency:.0f} ms`")

@bot.tree.command(name="info", description="Інформація про бота.")
async def info(interaction: Interaction):
    embed = discord.Embed(title="🤖 Bot Info", color=0x00FF00)
    embed.add_field(name="Версія", value="1.0.0", inline=False)
    embed.add_field(name="Автор", value="Grox Team", inline=False)
    embed.add_field(name="Технології", value="Python 3.11, discord.py, Groq AI, PostgreSQL", inline=False)
    embed.set_footer(text=f"Запитано користувачем {interaction.user}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ai", description="Спілкування зі ШІ.")
@app_commands.describe(text="Текст, який буде переданий ШІ")
async def ai(interaction: Interaction, *, text: str):
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id
    # fetch existing context
    context = await get_user_context(user_id)
    # keep only last 10 exchanges
    if len(context) > 18:
        context = context[-18:]

    # add user message
    context.append({"role": "user", "content": text})

    try:
        reply = await query_groq(context)
    except Exception as e:
        logger.exception("Groq request failed")
        await interaction.followup.send("❌ Помилка сервісу ШІ. Спробуйте пізніше.")
        return

    # add assistant reply to context and store
    context.append({"role": "assistant", "content": reply})
    await set_user_context(user_id, context)

    await interaction.followup.send(reply)

@bot.tree.command(name="clear", description="Очистити контекст розмови.")
async def clear(interaction: Interaction):
    await clear_user_context(interaction.user.id)
    await interaction.response.send_message("✅ Ваш контекст розмови очищено.", ephemeral=True)

@bot.tree.command(name="server", description="Інформація про сервер.")
async def server(interaction: Interaction):
    guild: Guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Ця команда працює лише на сервері.", ephemeral=True)
        return
    embed = discord.Embed(title=f"📊 Інформація про сервер {guild.name}", color=0x3498DB)
    embed.add_field(name="ID", value=str(guild.id), inline=False)
    embed.add_field(name="Власник", value=str(guild.owner), inline=False)
    embed.add_field(name="Кількість учасників", value=str(guild.member_count), inline=False)
    embed.add_field(name="Створено", value=guild.created_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
    embed.add_field(name="Ролі", value=", ".join([r.name for r in guild.roles[:10]]) + ("…" if len(guild.roles) > 10 else ""), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="user", description="Інформація про користувача.")
@app_commands.describe(member="Користувач (за замовчуванням – ви)")
async def user(interaction: Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"👤 Користувач {member}", color=0xE67E22)
    embed.add_field(name="ID", value=str(member.id), inline=False)
    embed.add_field(name="Створено аккаунт", value=member.created_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
    embed.add_field(name="Приєднано до серверу", value=member.joined_at.strftime("%Y-%m-%d %H:%M UTC") if member.joined_at else "N/A", inline=False)
    embed.add_field(name="Ролі", value=", ".join([r.name for r in member.roles if r.name != "@everyone"]), inline=False)
    await interaction.response.send_message(embed=embed)

#