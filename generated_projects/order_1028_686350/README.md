# Discord AI Bot

A versatile Discord bot powered by Groq's Llama2‑70B model.  
It can answer questions, provide server/user information, and includes a full moderation suite.

---

## Table of Contents
1. [Features](#features)  
2. [Prerequisites](#prerequisites)  
3. [Installation](#installation)  
4. [Configuration](#configuration)  
5. [Running the Bot](#running-the-bot)  
6. [Bot Commands](#bot-commands)  
7. [Deployment on Render](#deployment-on-render)  
8. [Development & Testing](#development--testing)  
9. [License](#license)

---

## Features
- **AI Chat** – `/ai <text>` communicates with Groq’s Llama2‑70B in Ukrainian & English.  
- **Context Management** – per‑user conversation history stored in PostgreSQL.  
- **Info Commands** – `/ping`, `/info`, `/server`, `/user`.  
- **Moderation** – automatic spam/flood detection, dangerous‑link filtering, warning system, mute/ban utilities.  
- **Logging** – `loguru` writes INFO & ERROR logs to `bot.log`.  
- **Async Architecture** – fully asynchronous using `discord.py`, `aiohttp`, and `asyncpg`.

---

## Prerequisites
- Python **3.11** or newer  
- A Discord Bot Token (create a bot at the Discord Developer Portal)  
- Groq API key (sign up at https://groq.com)  
- PostgreSQL database (local or Render‑managed)  

---

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/discord-ai-bot.git
cd discord-ai-bot

# Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root (it is ignored by Git) with the following variables:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=postgresql://user:password@host:port/database
```

> **Security note:** Never commit the `.env` file or expose your secrets.

---

## Running the Bot

```bash
python -m bot.bot
```

The bot will connect to Discord, set up its slash commands, and start listening for events. Logs are written to `bot.log`.

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | Shows a list of all available commands. |
| `/ping` | Displays the bot’s latency. |
| `/info` | Shows bot version, author, and used technologies. |
| `/ai <text>` | Chat with the AI. Supports Ukrainian & English. |
| `/clear` | Clears the conversation context for the invoking user. |
| `/server` | Provides server statistics (member count, creation date, roles, …). |
| `/user` | Shows information about the invoking user (name, ID, roles, join date). |
| **Moderation (admin only)** |
| `/warn <user> <reason>` | Issues a warning and logs it in PostgreSQL. |
| `/mute <user> <duration>` | Temporarily prevents a user from sending messages. |
| `/ban <user> <reason>` | Bans a user from the server. |
| `/unban <user>` | Removes a ban. |

### Automatic Moderation
- **Spam detection:** >5 messages in 3 seconds → flood; same message >3 times → spam.  
- **Dangerous link filter:** Simple regex checks against a blacklist defined in `config.py`.  
- Actions (delete message, warn user, log entry) are performed automatically.

---

## Deployment on Render

1. **Create a new Web Service** (Python) in Render.  
2. Set the **Build Command** to `pip install -r requirements.txt`.  
3. Set the **Start Command** to `python -m bot.bot`.  
4. Add the environment variables (`DISCORD_TOKEN`, `GROQ_API_KEY`, `DATABASE_URL`).  
5. (Optional) Attach a Render‑managed PostgreSQL instance and set its URL as `DATABASE_URL`.  
6. Enable **Auto‑Deploy** from your GitHub repository.

Render will install dependencies, start the bot, and keep it alive as long as the process runs.

---

## Development & Testing

```bash
# Run unit tests (if you added a tests/ folder)
pytest
```

- **Code style:** The project follows PEP 8.  
- **Static analysis:** `ruff` or `flake8` can be integrated.  
- **Continuous integration:** Add a GitHub Actions workflow to run tests on each push.

---

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.