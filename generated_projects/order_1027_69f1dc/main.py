import os
import asyncio
import re
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import discord
from discord import app_commands, Interaction, Embed, Permissions, Member, Guild, TextChannel
from discord.ext import commands

import asyncpg
from loguru import logger
from aiohttp import web
from dotenv import load_dotenv

# -----------------------------------------------------------
# Load environment variables
# -----------------------------------------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN is not set in the environment.")
    raise SystemExit(1)
if not GROQ_API_KEY:
    logger.error("GROQ_API_KEY is not set in the environment.")
    raise SystemExit(1)
if not DATABASE_URL:
    logger.error("DATABASE_URL is not set in the environment.")
    raise SystemExit(1)

# -----------------------------------------------------------
# Logger configuration
# -----------------------------------------------------------
logger.remove()
logger.add(
    "bot.log",
    level=LOG_LEVEL,
    rotation="10 MB",
    retention="7 days",
    compression="zip",
)
logger.add(lambda msg: print(msg, end=""), level=LOG_LEVEL)

# -----------------------------------------------------------
# Global state
# -----------------------------------------------------------
discord_ready = False  # updated after the bot connects
message_counters: Dict[int, List[datetime]] = {}  # user_id -> timestamps list
spam_threshold = 5  # messages
spam_interval = timedelta(seconds=10)

dangerous_domains = {
    "bit.ly",
    "tinyurl.com",
    "malware.com",
    "phishing.com",
    "example-bad.com",
}

url_regex = re.compile(
    r"https?://(?:www\.)?([^/\s]+)", re.IGNORECASE
)

# -----------------------------------------------------------
# Database helpers
# -----------------------------------------------------------
class Database:
    """Simple wrapper around asyncpg for required tables."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_contexts (
                    user_id BIGINT PRIMARY KEY,
                    context JSONB,
                    updated_at TIMESTAMP
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    moderator_id BIGINT,
                    action TEXT,
                    reason TEXT,
                    timestamp TIMESTAMP,
                    expiry TIMESTAMP NULL
                );
                """
            )
        logger.info("Database tables ensured.")

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("Database pool closed.")

    # ---------- User context ----------
    async def get_context(self, user_id: int) -> List[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT context FROM user_contexts WHERE user_id = $1", user_id
            )
            if row:
                return row["context"]
            return []

    async def set_context(self, user_id: int, context: List[dict]):
        now = datetime.utcnow()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_contexts (user_id, context, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE
                SET context = $2, updated_at = $3;
                """,
                user_id,
                json.dumps(context),
                now,
            )

    async def clear_context(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_contexts WHERE user_id = $1", user_id
            )

    # ---------- Moderation logs ----------
    async def add_mod_log(
        self,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str,
        expiry: Optional[datetime] = None,
    ):
        ts = datetime.utcnow()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO moderation_logs
                (user_id, moderator_id, action, reason, timestamp, expiry)
                VALUES ($1,$2,$3,$4,$5,$6);
                """,
                user_id,
                moderator_id,
                action,
                reason,
                ts,
                expiry,
            )

    async def fetch_mod_logs(
        self, user_id: Optional[int] = None
    ) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            if user_id:
                rows = await conn.fetch(
                    """
                    SELECT * FROM moderation_logs
                    WHERE user_id = $1
                    ORDER BY timestamp DESC;
                    """,
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM moderation_logs
                    ORDER BY timestamp DESC LIMIT 100;
                    """
                )
            return rows


db = Database(DATABASE_URL)

# -----------------------------------------------------------
# Groq client wrapper
# -----------------------------------------------------------
class GroqClient:
    """Very thin wrapper over Groq API using aiohttp."""

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "mixtral-8x7b-32768"):
        self.api_key = api_key
        self.model = model

    async def chat(self, messages: List[dict]) -> str:
        import aiohttp

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.ENDPOINT, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.error(f"Groq API error {resp.status}: {txt}")
                    raise RuntimeError("AI service unavailable.")
                data = await resp.json()
                return data["choices"][0]["message"]["content"]


groq_client = GroqClient(GROQ_API_KEY)

# -----------------------------------------------------------
# Helper utilities
# -----------------------------------------------------------
def embed_message(title: str, description: str, color: int = 0x2F3136) -> Embed:
    return Embed(title=title, description=description, color=color, timestamp=datetime.utcnow())


def is_moderator(member: Member) -> bool:
    """Check if the member has Administrator or Manage Messages permission."""
    perms: Permissions = member.guild_permissions
    return perms.administrator or perms.manage_messages


def extract_domains(message_content: str) -> List[str]:
    return url_regex.findall(message_content)


def is_dangerous(domain: str) -> bool:
    return any(d in domain.lower() for d in dangerous_domains)


# -----------------------------------------------------------
# Bot definition
# -----------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class AIHelperBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            activity=discord.Game(name="AI Assistant"),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register cogs (implemented as methods below)
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def on_ready(self):
        global discord_ready
        discord_ready = True
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("Bot is ready.")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # ---- Automatic moderation ----
        await self.handle_spam(message)
        await self.handle_dangerous_links(message)

        # Process commands (if any)
        await self.process_commands(message)

    async def handle_spam(self, message: discord.Message):
        user_id = message.author.id
        now = datetime.utcnow()
        timestamps = message_counters.setdefault(user_id, [])
        timestamps.append(now)

        # Keep only recent timestamps
        timestamps = [t for t in timestamps if now - t <= spam_interval]
        message_counters[user_id] = timestamps

        if len(timestamps) > spam_threshold:
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention}, ви надсилаєте повідомлення занадто часто. Будь ласка, сповільніться.",
                    delete_after=10,
                )
                await db.add_mod_log(
                    user_id=user_id,
                    moderator_id=self.user.id,
                    action="spam_warn",
                    reason="Exceeded message rate limit",
                )
                logger.info(f"Spam warning issued to user {user_id}")
            except discord.HTTPException:
                pass

    async def handle_dangerous_links(self, message: discord.Message):
        domains = extract_domains(message.content)
        for domain in domains:
            if is_dangerous(domain):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention}, посилання на небезпечний ресурс заборонено.",
                        delete_after=10,
                    )
                    await db.add_mod_log(
                        user_id=message.author.id,
                        moderator_id=self.user.id,
                        action="link_block",
                        reason=f"Blocked domain: {domain}",
                    )
                    logger.info(f"Blocked dangerous link from {message.author.id}: {domain}")
                except discord.HTTPException:
                    pass
                break

bot = AIHelperBot()


# -----------------------------------------------------------
# Slash command implementations
# -----------------------------------------------------------

# ---- General Commands ----
@bot.tree.command(name="help", description="Показує список всіх доступних команд.")
async def help_cmd(interaction: Interaction):
    embed = embed_message(
        "Довідка по командах",
        """
**Загальні**
`/help` – Показати цю довідку  
`/ping` – Перевірити затримку бота  
`/info` – Інформація про бота  

**AI**
`/ai <prompt>` – Запит до AI (зберігає контекст)  
`/clear` – Очистити контекст  

**Сервер**
`/server` – Інформація про сервер  

**Користувач**
`/user` – Інформація про вас  

**Модерація** *(тільки для модераторів)*
`/warn @користувач <причина>` – Попередження  
`/mute @користувач <тривалість> <причина>` – Тимчасовий мут  
`/ban @користувач <причина>` – Бан  
`/unmute @користувач` – Скасувати мут  
`/unban @користувач` – Скасувати бан  
`/modlog [користувач]` – Журнал модерації
        """,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Повертає затримку (latency) бота.")
async def ping_cmd(interaction: Interaction):
    latency_ms = round(bot.latency * 1000, 2)
    await interaction.response.send_message(f"Pong! Latency: {latency_ms} ms", ephemeral=True)


@bot.tree.command(name="info", description="Інформація про бота.")
async def info_cmd(interaction: Interaction):
    embed = embed_message(
        "AI‑Assistant Bot",
        f"""
**Версія:** 1.0.0  
**Автор:** Grox Development Team  
**Технології:** Python 3.11, discord.py 2.x, asyncpg, Groq API
        """,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- AI Commands ----
@bot.tree.command(name="ai", description="Отримати відповідь від AI.")
@app_commands.describe(prompt="Текст запиту до AI (max 1500 символів)")
async def ai_cmd(interaction: Interaction, prompt: str):
    if len(prompt) > 1500:
        await interaction.response.send_message(
            "⚠️ Запит занадто довгий (max 1500 символів).", ephemeral=True
        )
        return

    await interaction.response.defer()
    user_id = interaction.user.id
    # Retrieve prior context
    prior = await db.get_context(user_id)
    messages = prior + [{"role": "user", "content": prompt}]
    try:
        answer = await groq_client.chat(messages)
        # Save updated context (keep last 10 exchanges)
        new_context = (messages + [{"role": "assistant", "content": answer}])[-20:]
        await db.set_context(user_id, new_context)
        await interaction.followup.send(embed=embed_message("AI Відповідь", answer))
        logger.info(f"AI request processed for user {user_id}")
    except Exception as e:
        logger.error(f"AI error for user {user_id}: {e}")
        await interaction.followup