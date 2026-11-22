# AI‑Assistant Discord Bot

Універсальний Discord‑бот, що забезпечує інтерактивне спілкування українською та англійською за допомогою **Groq API**, автоматичну модерацію та багато корисних слеш‑команд. Проект готовий до розгортання на Render (або будь‑якому іншому хостингу) з PostgreSQL.

## 📋 Зміст

- [Функціональність](#функціональність)
- [Технічні вимоги](#технічні-вимоги)
- [Структура проєкту](#структура-проєкту)
- [Встановлення та запуск локально](#встановлення-та-запуск-локально)
- [Налаштування змінних середовища](#налаштування-змінних-середовища)
- [Docker](#docker)
- [Розгортання на Render](#розгортання-на-render)
- [Тестування](#тестування)
- [Ліцензія](#ліцензія)

## Функціональність

| Категорія | Команда | Опис |
|---|---|---|
| **Загальні** | `/help` | Показує список всіх доступних команд. |
|  | `/ping` | Повертає затримку (latency) бота. |
|  | `/info` | Інформація про бота (версія, автор, технології). |
| **AI** | `/ai <prompt>` | Надсилає prompt до Groq API, повертає відповідь, зберігаючи контекст для користувача. |
|  | `/clear` | Очищує збережений контекст розмови. |
| **Сервер** | `/server` | Інформація про сервер (кількість учасників, ролі, дата створення, ID). |
| **Користувач** | `/user` | Інформація про користувача (ім’я, ID, ролі, дата приєднання). |
| **Модерація** (тільки для ролей *Moderator* / *Admin*) | `/warn @user <reason>` | Надсилає попередження та зберігає його в БД. |
|  | `/mute @user <duration> <reason>` | Тимчасово блокує користувачу можливість писати. |
|  | `/ban @user <reason>` | Банить користувача. |
|  | `/unmute @user` | Скасовує мут. |
|  | `/unban @user` | Скасовує бан. |
|  | `/modlog [user]` | Журнал попереджень/мутів/банів (за користувачем або загальний). |
| **Автоматична модерація** | — | Спам/флуд, небезпечні посилання, масові емодзі, капслок – все обробляється без слеш‑команд. |

## Технічні вимоги

- **Python** ≥ 3.11
- **Бібліотеки** (вказані в `requirements.txt`):
  - `discord.py` – взаємодія з Discord API
  - `aiohttp` – асинхронні HTTP‑запити
  - `asyncpg` – драйвер PostgreSQL
  - `python-dotenv` – читання `.env`
  - `loguru` – логування
  - `groq` – офіційний SDK для Groq API
  - `pytest-asyncio` – асинхронне тестування
- **База даних** – PostgreSQL
- **Секрети** – передаються лише через змінні середовища (`DISCORD_TOKEN`, `GROQ_API_KEY`, `DATABASE_URL`, `LOG_LEVEL`).

## Структура проєкту

```
/bot
│   main.py                # точка входу, ініціалізація клієнта
│   config.py              # завантаження змінних середовища
│   logger.py              # налаштування loguru
│
├── cogs/
│   ├── ai.py              # /ai, /clear, зберігання контексту
│   ├── admin.py           # /help, /ping, /info, /server, /user
│   ├── moderation.py      # авт. модерація, /warn, /mute, /ban, …
│
├── db/
│   ├── models.py          # SQL‑запити asyncpg
│   └── migrations/        # SQL‑файли ініціалізації таблиць
│
├── utils/
│   ├── groq_client.py     # обгортка над Groq API
│   └── helpers.py         # embed, permission checks, etc.
│
requirements.txt
Dockerfile
.env.example
README.md
```

## Встановлення та запуск локально

1. **Клонувати репозиторій**

   ```bash
   git clone https://github.com/yourusername/discord-ai-bot.git
   cd discord-ai-bot
   ```

2. **Створити та активувати віртуальне середовище**

   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

3. **Встановити залежності**

   ```bash
   pip install -r requirements.txt
   ```

4. **Налаштувати `.env`**

   Скопіюйте шаблон та заповніть свої значення:

   ```bash
   cp .env.example .env
   ```

   ```dotenv
   DISCORD_TOKEN=your_discord_bot_token
   GROQ_API_KEY=your_groq_api_key
   DATABASE_URL=postgresql://user:password@host:port/dbname
   LOG_LEVEL=INFO
   ```

5. **Ініціалізувати базу даних**

   ```bash
   python -m bot.db.migrations   # у цьому файлі виконується CREATE TABLE, якщо треба
   ```

6. **Запустити бота**

   ```bash
   python -m bot.main
   ```

Бот під’єднається до Discord і буде готовий обробляти команди.

## Налаштування змінних середовища

| Змінна | Опис | Приклад |
|---|---|---|
| `DISCORD_TOKEN` | Токен вашого Discord‑бота | `MTAx...` |
| `GROQ_API_KEY` | API‑ключ Groq | `gsk_...` |
| `DATABASE_URL` | URL підключення до PostgreSQL | `postgresql://user:pass@localhost:5432/botdb` |
| `LOG_LEVEL` | Рівень логування (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |

## Docker

Dockerfile вже включений у репозиторій.

```bash
# Побудова образу
docker build -t discord-ai-bot .

# Запуск контейнера
docker run -d \
  --name discord-ai \
  --restart unless-stopped \
  --env-file .env \
  discord-ai-bot
```

Контейнер автоматично підключиться до PostgreSQL, зазначеної в `DATABASE_URL`.

## Розгортання на Render

1. **Створити новий Web Service** у Render з типом *Python*.
2. **Підключити репозиторій GitHub** (приватний або публічний).
3. **Команда запуску:** `python -m bot.main`
4. **Додати змінні середовища** у розділі *Environment* (`DISCORD_TOKEN`, `GROQ_API_KEY`, `DATABASE_URL`, `LOG_LEVEL`).
5. **Підключити PostgreSQL‑instance** (Render надає безкоштовний план) та вказати його URL у `DATABASE_URL`.
6. **Налаштувати автодеплой** – кожен push у гілку `main` автоматично перезапустить сервіс.

## Тестування

Тести розташовані у каталозі `tests/`. Для їх запуску:

```bash
pytest -v
```

Для асинхронних тестів використовується `pytest-asyncio`.

## Ліцензія

Проєкт розповсюджується під ліцензією **MIT** – вільно використовуйте, модифікуйте та поширюйте.

---

> **Примітка:** Жодних секретних даних (токенів, ключів, паролів) не зберігається в репозиторії. Переконайтеся, що `.env` додається до `.gitignore`.