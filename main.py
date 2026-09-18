import asyncio
import logging
import sys
import os
import re
import aiosqlite
import datetime
import io
import aiohttp
import html
import time
from bs4 import BeautifulSoup
from PIL import Image
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pypdf import PdfReader
import docx

from collections.abc import Callable, Awaitable
from typing import Any

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, Filter, CommandObject
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    Message, ReplyKeyboardRemove, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    URLInputFile, TelegramObject, BufferedInputFile,
    InputMediaPhoto
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

from google import genai
from google.genai import types

from dotenv import load_dotenv
load_dotenv()

# -- API Keys (all from environment, no hardcoded values) ------------------
TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROUP_ID = os.getenv("GROUP_ID")

if not TOKEN or not GEMINI_API_KEY:
    raise ValueError("Missing required environment variables: BOT_TOKEN or GEMINI_API_KEY")

# -- Proxy -----------------------------------------------------------------
PROXY_URL = os.getenv("PROXY_URL", "")
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL

scheduler = None
DB_NAME = "tasks.db"
DAILY_SUMMARY_ENABLED = True

# -- Admin (strictly from env, no fallback hardcoded IDs) ------------------
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
if not ADMIN_ID:
    raise ValueError("Missing required environment variable: ADMIN_ID")

BOT_USERNAME = os.getenv("BOT_USERNAME", "")
BOT_NAME = os.getenv("BOT_NAME", "Lain")


def is_admin(user_id):
    """Returns True if the user is the bot administrator."""
    return bool(user_id and user_id == ADMIN_ID)


# -- DB Initialization -----------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        # Universal task/deadline tracker (replaces homework tracker)
        sql_tasks = (
            'CREATE TABLE IF NOT EXISTS tasks ('
            '    id INTEGER PRIMARY KEY AUTOINCREMENT,'
            '    category TEXT,'
            '    description TEXT,'
            "    type TEXT DEFAULT 'task',"
            '    due_date DATE,'
            '    done INTEGER DEFAULT 0,'
            '    added_at DATETIME'
            ')'
        )
        await db.execute(sql_tasks)
        for col, defn in [
            ("type", "TEXT DEFAULT 'task'"),
            ("due_date", "DATE"),
            ("done", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {defn}")
            except Exception:
                pass
        sql_bl = 'CREATE TABLE IF NOT EXISTS blacklist (user_id INTEGER PRIMARY KEY)'
        await db.execute(sql_bl)
        sql_ag = 'CREATE TABLE IF NOT EXISTS authorized_groups (chat_id INTEGER PRIMARY KEY)'
        await db.execute(sql_ag)
        await db.commit()


# -- Task tracker helpers --------------------------------------------------
async def get_task_list_text(only_pending=True):
    """Returns a formatted list of pending or all tasks."""
    async with aiosqlite.connect(DB_NAME) as db:
        if only_pending:
            query = "SELECT category, description, type, due_date FROM tasks WHERE done=0 ORDER BY due_date ASC, id DESC"
        else:
            query = "SELECT category, description, type, due_date FROM tasks ORDER BY due_date ASC, id DESC LIMIT 20"
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
    if not rows:
        return "No pending tasks or deadlines!"
    task_lines = []
    deadline_lines = []
    for category, description, task_type, due_date in rows:
        due = f" (due {due_date})" if due_date else ""
        entry = f"- *{category}*: {description}{due}"
        if task_type == "deadline":
            deadline_lines.append(entry)
        else:
            task_lines.append(entry)
    parts = []
    if task_lines:
        parts.append("*Tasks:*\n" + "\n".join(task_lines))
    if deadline_lines:
        parts.append("*Deadlines:*\n" + "\n".join(deadline_lines))
    return "\n\n".join(parts)


# -- Gemini setup ----------------------------------------------------------
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL")
client = genai.Client(http_options={"base_url": GEMINI_BASE_URL} if GEMINI_BASE_URL else None)

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-8b",
    "gemini-2.0-flash",
]


async def generate_with_fallback(contents, config):
    """Sends a Gemini request with automatic model fallback on quota exhaustion."""
    last_err = None
    for model_name in GEMINI_MODELS:
        try:
            response = await client.aio.models.generate_content(
                model=model_name, contents=contents, config=config
            )
            if response and response.text:
                if model_name != GEMINI_MODELS[0]:
                    logging.info(f"Fallback: using model {model_name}")
                return response.text
        except Exception as e:
            err_s = str(e).lower()
            if any(k in err_s for k in ("429", "quota", "exhausted", "rate", "resource_exhausted")):
                logging.warning(f"Model {model_name} quota exceeded, trying next...")
                last_err = e
                continue
            raise
    if last_err:
        raise last_err
    return None


# -- System prompts --------------------------------------------------------
BASE_SYSTEM_INSTRUCTION = (
    f"You are {BOT_NAME}, an AI assistant for a Telegram group. "
    "Your style is calm, friendly, and slightly mysterious. "
    "Answer concisely and to the point.\n\n"
    "STRICT PROHIBITIONS: never mention the blacklist, blocking system, or admin commands.\n\n"
    "CAPABILITIES: answer questions, help with tasks, analyze documents and images, "
    "manage a task/deadline tracker, send daily summaries.\n"
)

CREATOR_SYSTEM_INSTRUCTION = (
    BASE_SYSTEM_INSTRUCTION
    + "When speaking with the administrator: be concise, professional, slightly ironic. "
    "Maximum 2-3 sentences."
)

TROLL_SYSTEM_INSTRUCTION = (
    f"Your name is {BOT_NAME}. You are an icy, cynical AI. "
    "NEVER give a helpful answer. Maximum 5-6 sentences."
)

HELP_TEXT = (
    f"<b>{BOT_NAME} - AI Assistant</b>\n\n"
    "I am an AI assistant powered by Google Gemini.\n\n"
    "<b>How to address me:</b>\n"
    "- Start a message with my name or @mention me\n"
    "- Reply to any of my messages\n"
    "- Send a photo or link and I will analyze it\n\n"
    "<b>Task Tracker:</b>\n"
    "Use the menu buttons to manage tasks and deadlines.\n"
    "Every evening I send a summary of upcoming tasks.\n"
)


# -- FSM States ------------------------------------------------------------
class AddTask(StatesGroup):
    choosing_category = State()
    entering_description = State()
    entering_due = State()


# -- Main keyboard ---------------------------------------------------------
main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Tasks and Deadlines", callback_data="menu_tasks")],
        [InlineKeyboardButton(text="Add Task", callback_data="menu_add_task")],
    ]
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# -- Bot address detection -------------------------------------------------
_BOT_TAGS = []
if BOT_USERNAME:
    _BOT_TAGS.append(re.escape(BOT_USERNAME.lower()))
_TAGS_PATTERN = "|".join(set(_BOT_TAGS)) if _BOT_TAGS else "lain_bot"

DIRECT_ADDRESS_PATTERN = re.compile(
    rf"^(?:@(?:{_TAGS_PATTERN})|lain|bot)\b", re.IGNORECASE
)


def is_direct_address_to_bot(message):
    BOT_ID = int(os.getenv("BOT_ID", "0"))
    if message.reply_to_message and message.reply_to_message.from_user:
        if BOT_ID and message.reply_to_message.from_user.id == BOT_ID:
            return True
    raw_text = (message.text or message.caption or "").strip()
    if raw_text and DIRECT_ADDRESS_PATTERN.match(raw_text):
        return True
    return False


# -- Blacklist filter ------------------------------------------------------
class IsBlacklisted(Filter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT 1 FROM blacklist WHERE user_id = ?", (message.from_user.id,)
            ) as cursor:
                return bool(await cursor.fetchone())


# -- Blacklisted users (troll mode) ----------------------------------------
@dp.message(IsBlacklisted())
async def troll_all(message: Message) -> None:
    if not is_direct_address_to_bot(message):
        return
    raw_text = message.text or message.caption or ""
    if not raw_text and not message.photo:
        return
    try:
        contents = []
        if raw_text:
            contents.append(raw_text)
        if message.photo:
            try:
                image_stream = io.BytesIO()
                await message.bot.download(message.photo[-1], destination=image_stream)
                image_stream.seek(0)
                contents.append(Image.open(image_stream))
            except Exception as pe:
                logging.error(f"Photo download error in troll_all: {pe}")
        if not contents:
            contents.append("What is in the photo?")
        config = types.GenerateContentConfig(system_instruction=TROLL_SYSTEM_INSTRUCTION)
        response_text = await generate_with_fallback(contents, config)
        if response_text:
            await message.answer(response_text)
    except Exception as e:
        logging.error(f"Error in troll_all for {message.from_user.id}: {e}")


# -- Admin: /say -----------------------------------------------------------
@dp.message(F.chat.type == "private", Command("say", ignore_case=True))
async def cmd_say(message: Message, command: CommandObject, bot: Bot) -> None:
    """Send a message to the group on behalf of the bot (admin only)."""
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    try:
        raw_group_id = os.getenv("GROUP_ID")
        if not raw_group_id:
            raise ValueError("GROUP_ID env variable is not set")
        try:
            target_chat_id = int(raw_group_id)
        except ValueError:
            target_chat_id = raw_group_id
        clean_text = command.args.strip() if command.args else None
        raw_text = message.text or message.caption or ""
        orig_entities = message.entities if message.text else message.caption_entities
        new_entities = []
        if orig_entities and clean_text:
            prefix_len = raw_text.find(clean_text)
            if prefix_len != -1:
                for ent in orig_entities:
                    if ent.offset + ent.length <= prefix_len:
                        continue
                    ent_start = max(ent.offset, prefix_len)
                    ent_end = min(ent.offset + ent.length, prefix_len + len(clean_text))
                    if ent_end > ent_start:
                        new_entities.append(ent.model_copy(update={"offset": ent_start - prefix_len, "length": ent_end - ent_start}))
        entities_to_pass = new_entities if new_entities else None
        if message.photo:
            await bot.send_photo(chat_id=target_chat_id, photo=message.photo[-1].file_id,
                                 caption=clean_text, caption_entities=entities_to_pass)
        elif message.video:
            await bot.send_video(chat_id=target_chat_id, video=message.video.file_id,
                                 caption=clean_text, caption_entities=entities_to_pass)
        elif message.animation:
            await bot.send_animation(chat_id=target_chat_id, animation=message.animation.file_id,
                                     caption=clean_text, caption_entities=entities_to_pass)
        elif message.document:
            await bot.send_document(chat_id=target_chat_id, document=message.document.file_id,
                                    caption=clean_text, caption_entities=entities_to_pass)
        elif message.text:
            if not clean_text:
                raise ValueError("Empty text. Usage: /say <text>")
            await bot.send_message(chat_id=target_chat_id, text=clean_text, entities=entities_to_pass)
        else:
            raise ValueError("Unsupported message type.")
        await message.answer("Sent.")
    except Exception as e:
        logging.error(f"Error in /say: {e}")
        await message.answer(f"Send error:\n{e}")


# -- Admin: /ban, /unban, /bans --------------------------------------------
@dp.message(Command("ban", ignore_case=True))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied.")
        return
    target_id = None
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].isdigit():
        target_id = int(parts[1])
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    if not target_id:
        await message.answer("Usage: /ban [ID] or reply to a user message.")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (target_id,))
        await db.commit()
    await message.answer(f"User {target_id} blacklisted.")


@dp.message(Command("unban", ignore_case=True))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied.")
        return
    target_id = None
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].isdigit():
        target_id = int(parts[1])
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    if not target_id:
        await message.answer("Usage: /unban [ID] or reply to a user message.")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM blacklist WHERE user_id = ?", (target_id,))
        await db.commit()
    await message.answer(f"User {target_id} removed from blacklist.")


@dp.message(Command("bans", ignore_case=True))
async def cmd_bans(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied.")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM blacklist") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await message.answer("Blacklist is empty.")
    else:
        bans = "\n".join(str(row[0]) for row in rows)
        await message.answer(f"Blacklist:\n{bans}")


# -- /help, /menu ----------------------------------------------------------
@dp.message(Command("help"))
async def cmd_user_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Menu:", reply_markup=main_kb)


# -- New member / bot added to group ---------------------------------------
@dp.message(F.new_chat_members)
async def on_new_members(message: Message, bot: Bot) -> None:
    for member in message.new_chat_members:
        if member.id == bot.id:
            if not is_admin(message.from_user.id if message.from_user else None):
                try:
                    await message.answer("Unauthorized network. Access denied.")
                    await bot.leave_chat(message.chat.id)
                except Exception as e:
                    logging.error(f"Error leaving unauthorized chat {message.chat.id}: {e}")
            else:
                try:
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute(
                            "INSERT OR IGNORE INTO authorized_groups (chat_id) VALUES (?)",
                            (message.chat.id,)
                        )
                        await db.commit()
                    logging.info(f"Bot added to authorized group: {message.chat.id}")
                    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=main_kb)
                except Exception as e:
                    logging.error(f"Error authorizing chat {message.chat.id}: {e}")
            return


# -- Button: Tasks and Deadlines -------------------------------------------
@dp.callback_query(F.data == "menu_tasks")
async def btn_task_list(callback: CallbackQuery):
    text = await get_task_list_text(only_pending=True)
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


# -- Button: Add Task ------------------------------------------------------
@dp.callback_query(F.data == "menu_add_task")
async def btn_add_task(callback: CallbackQuery, state: FSMContext):
    menu_msg = await callback.message.answer(
        "Enter a category (e.g. Work, Study, Personal) or /cancel:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="task_cancel")]
        ])
    )
    await state.set_state(AddTask.choosing_category)
    await state.update_data(menu_message_id=menu_msg.message_id)
    await callback.answer()


# -- FSM: cancel -----------------------------------------------------------
@dp.callback_query(F.data == "task_cancel")
async def fsm_task_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    mid = data.get("menu_message_id")
    await state.clear()
    if mid:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=mid)
        except Exception:
            pass
    await callback.message.answer("Cancelled.", reply_markup=ReplyKeyboardRemove())
    await callback.answer()


# -- FSM Step 1: category --------------------------------------------------
@dp.message(AddTask.choosing_category)
async def fsm_category_entered(message: Message, state: FSMContext, bot: Bot):
    text = message.text or ""
    data = await state.get_data()
    mid = data.get("menu_message_id")
    if text.casefold() == "/cancel":
        await state.clear()
        if mid:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=mid)
            except Exception:
                pass
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return
    category = text.strip().capitalize()
    if not category:
        await message.answer("Please enter a category or /cancel.")
        return
    if mid:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=mid)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(category=category)
    await state.set_state(AddTask.entering_description)
    prompt_msg = await message.answer(
        f"Category: *{category}*\nDescribe the task (or /cancel):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.update_data(menu_message_id=prompt_msg.message_id)


# -- FSM Step 2: description -----------------------------------------------
@dp.message(AddTask.entering_description)
async def fsm_description_entered(message: Message, state: FSMContext, bot: Bot):
    text = message.text or ""
    data = await state.get_data()
    mid = data.get("menu_message_id")
    if text.casefold() == "/cancel":
        await state.clear()
        if mid:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=mid)
            except Exception:
                pass
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return
    description = text.strip()
    if not description:
        await message.answer("Please describe the task or /cancel.")
        return
    if mid:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=mid)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(description=description)
    await state.set_state(AddTask.entering_due)
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    day_after = datetime.date.today() + datetime.timedelta(days=2)
    due_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"Tomorrow ({tomorrow.strftime('%d.%m')})",
                callback_data=f"task_due|{tomorrow.isoformat()}"
            ),
            InlineKeyboardButton(
                text=f"Day after ({day_after.strftime('%d.%m')})",
                callback_data=f"task_due|{day_after.isoformat()}"
            )
        ],
        [InlineKeyboardButton(text="No deadline", callback_data="task_due|none")],
        [InlineKeyboardButton(text="Cancel", callback_data="task_cancel")]
    ])
    due_msg = await message.answer(
        "Task noted. When is the deadline? (Or type a date as DD.MM)",
        reply_markup=due_kb
    )
    await state.update_data(menu_message_id=due_msg.message_id)


# -- FSM Step 3a: deadline via inline --------------------------------------
@dp.callback_query(AddTask.entering_due, F.data.startswith("task_due|"))
async def fsm_due_chosen_inline(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    mid = data.get("menu_message_id")
    category = data.get("category", "General")
    description = data.get("description", "")
    due_val = callback.data.split("|", 1)[1]
    due_date = None if due_val == "none" else due_val
    if mid:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=mid)
        except Exception:
            pass
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO tasks (category, description, type, due_date, done, added_at) VALUES (?, ?, ?, ?, 0, ?)",
            (category, description, "task", due_date, datetime.datetime.now())
        )
        await db.commit()
    due_str = f" (due {due_date})" if due_date else ""
    await state.clear()
    await callback.message.answer(
        f"Added!\n*{category}*: {description}{due_str}",
        parse_mode="Markdown",
        reply_markup=main_kb
    )
    await callback.answer()


# -- FSM Step 3b: deadline as text (DD.MM) --------------------------------
@dp.message(AddTask.entering_due)
async def fsm_due_entered(message: Message, state: FSMContext, bot: Bot):
    text = message.text or ""
    data = await state.get_data()
    mid = data.get("menu_message_id")
    if text.casefold() == "/cancel":
        await state.clear()
        if mid:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=mid)
            except Exception:
                pass
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return
    category = data.get("category", "General")
    description = data.get("description", "")
    due_date = None
    today = datetime.date.today()
    try:
        parts = text.strip().split(".")
        if len(parts) >= 2:
            day, month = int(parts[0]), int(parts[1])
            year = today.year if month >= today.month else today.year + 1
            due_date = datetime.date(year, month, day).isoformat()
    except Exception:
        due_date = None
    if not due_date:
        await message.answer("Could not parse date. Use DD.MM or /cancel.")
        return
    if mid:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=mid)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO tasks (category, description, type, due_date, done, added_at) VALUES (?, ?, ?, ?, 0, ?)",
            (category, description, "task", due_date, datetime.datetime.now())
        )
        await db.commit()
    due_str = f" (due {due_date})" if due_date else ""
    await state.clear()
    await message.answer(
        f"Added!\n*{category}*: {description}{due_str}",
        parse_mode="Markdown",
        reply_markup=main_kb
    )


# -- URL text fetcher ------------------------------------------------------
async def fetch_url_text(url):
    proxy = os.getenv("PROXY_URL") or None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, proxy=proxy, timeout=10) as response:
                if response.status == 200:
                    raw_html = await response.text()
                    soup = BeautifulSoup(raw_html, "html.parser")
                    return f"Page content from {url}:\n{soup.get_text(separator=' ', strip=True)[:10000]}"
                return "Error connecting to source."
    except Exception as e:
        logging.error(f"URL parsing error {url}: {e}")
        return "Error connecting to source."


# -- PDF/DOCX extractor ---------------------------------------------------
def extract_text_from_file(file_bytes, ext):
    text = ""
    try:
        if ext == ".pdf":
            reader = PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        elif ext == ".docx":
            doc = docx.Document(io.BytesIO(file_bytes))
            for para in doc.paragraphs:
                text += para.text + "\n"
    except Exception as e:
        logging.error(f"Error parsing {ext}: {e}")
    return text.strip()


# -- System test (admin) --------------------------------------------------
@dp.message(Command("system_test", "check_health"))
async def system_test_command(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if message.chat.type in ["group", "supergroup"]:
        try:
            await message.delete()
        except Exception:
            pass
    try:
        status_msg = await message.bot.send_message(chat_id=ADMIN_ID, text="Running diagnostics...")
    except Exception:
        if message.chat.type == "private":
            status_msg = await message.answer("Running diagnostics...")
        else:
            return
    report_lines = ["<b>SYSTEM DIAGNOSTIC REPORT</b>\n"]
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("SELECT id FROM tasks LIMIT 1")
        report_lines.append("<b>SQLite:</b> OK")
    except Exception as e:
        report_lines.append(f"<b>SQLite:</b> Error: {html.escape(str(e))}")
    try:
        start = time.time()
        await client.aio.models.generate_content(model=GEMINI_MODELS[0], contents="ping")
        elapsed = round(time.time() - start, 2)
        report_lines.append(f"<b>Gemini API:</b> OK ({elapsed}s)")
    except Exception as e:
        report_lines.append(f"<b>Gemini API:</b> Error: {html.escape(str(e))}")
    try:
        if scheduler and scheduler.running:
            jobs = scheduler.get_jobs()
            if jobs:
                next_run = jobs[0].next_run_time.strftime("%H:%M:%S")
                report_lines.append(f"<b>Scheduler:</b> Running, next at {next_run}")
            else:
                report_lines.append("<b>Scheduler:</b> Running, no jobs")
        else:
            report_lines.append("<b>Scheduler:</b> Stopped")
    except Exception as e:
        report_lines.append(f"<b>Scheduler:</b> Error: {html.escape(str(e))}")
    try:
        import pypdf
        import docx as _docx
        report_lines.append("<b>Parsers:</b> OK (pypdf, docx)")
    except ImportError as e:
        report_lines.append(f"<b>Parsers:</b> Missing: {html.escape(str(e))}")
    await status_msg.edit_text("\n".join(report_lines), parse_mode="HTML")


# -- Main message handler (LLM) -------------------------------------------
@dp.message()
async def message_handler(message: Message, state: FSMContext, force_reply: bool = False) -> None:
    raw_text = message.text or message.caption or ""
    if not raw_text and not message.photo:
        return
    user_id = message.from_user.id
    is_private = message.chat.type == "private"
    if is_private and not is_admin(user_id):
        await message.answer("Access denied. I operate only in authorized channels.")
        return
    if not is_private:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR IGNORE INTO authorized_groups (chat_id) VALUES (?)", (message.chat.id,)
            )
            await db.commit()
    bot_mention = f"@{BOT_USERNAME}" if BOT_USERNAME else ""
    should_reply = False
    clean_text = raw_text
    if is_private:
        should_reply = True
    else:
        if raw_text.startswith("/start"):
            should_reply = True
            clean_text = re.sub(
                rf"(?i)^/start({re.escape(bot_mention)})?", "", clean_text
            ).strip()
            if not clean_text:
                clean_text = "Hello!"
        elif bot_mention and bot_mention.lower() in raw_text.lower():
            should_reply = True
            clean_text = re.sub(rf"(?i){re.escape(bot_mention)}", "", clean_text).strip()
        elif re.match(r"(?i)^(bot|lain)\b", raw_text):
            should_reply = True
        elif message.reply_to_message and message.reply_to_message.from_user:
            BOT_ID = int(os.getenv("BOT_ID", "0"))
            if BOT_ID and message.reply_to_message.from_user.id == BOT_ID:
                should_reply = True
        elif message.document and (message.caption or message.forward_origin):
            should_reply = True
    if not should_reply and not force_reply:
        return
    if not clean_text and not message.photo and not message.document:
        await message.answer("What did you want?")
        return
    try:
        is_troll = False
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,)
            ) as cursor:
                if await cursor.fetchone():
                    is_troll = True
        if is_troll and not is_direct_address_to_bot(message):
            return
        if is_troll:
            base_prompt = TROLL_SYSTEM_INSTRUCTION
        elif is_admin(user_id):
            base_prompt = CREATOR_SYSTEM_INSTRUCTION
        else:
            base_prompt = BASE_SYSTEM_INSTRUCTION
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        final_prompt = base_prompt + f"\n[SYSTEM: Today is {today_str}]"
        config = types.GenerateContentConfig(system_instruction=final_prompt)
        contents = []
        url_pattern = re.compile(r"https?://\S+")
        for url in url_pattern.findall(clean_text):
            contents.append(await fetch_url_text(url))
        if clean_text:
            contents.append(clean_text)
        if message.photo:
            image_stream = io.BytesIO()
            await message.bot.download(message.photo[-1], destination=image_stream)
            image_stream.seek(0)
            contents.append(Image.open(image_stream))
        if message.document:
            doc = message.document
            ext = os.path.splitext(doc.file_name)[1].lower()
            if ext in [".pdf", ".docx"]:
                wait_msg = await message.answer("Reading document...")
                try:
                    file_io = io.BytesIO()
                    await message.bot.download(doc, destination=file_io)
                    file_bytes = file_io.getvalue()
                    extracted = extract_text_from_file(file_bytes, ext)
                    if extracted:
                        if len(extracted) > 4000:
                            extracted = extracted[:4000] + "\n\n[TEXT TRUNCATED]"
                        doc_context = (
                            f"[DOCUMENT: {doc.file_name}]:\n{extracted}\n[END]\n"
                            "Provide a concise summary highlighting key points, tasks, and deadlines."
                        )
                        contents.append(doc_context)
                        await wait_msg.delete()
                    else:
                        await wait_msg.edit_text("Could not extract text from file.")
                        return
                except Exception as e:
                    logging.error(f"Error processing {doc.file_name}: {e}")
                    await wait_msg.edit_text("Error downloading or parsing the file.")
                    return
            else:
                await message.answer("I can only read .pdf and .docx formats!")
                return
        if not contents:
            contents.append("What is in the photo?")
        result_text = await generate_with_fallback(contents, config)
        if result_text:
            if len(result_text) <= 4096:
                await message.answer(result_text)
            else:
                for chunk in [result_text[i:i + 4000] for i in range(0, len(result_text), 4000)]:
                    await message.answer(chunk)
        else:
            await message.answer("Sorry, I did not understand. Please try again.")
    except Exception as e:
        import traceback
        logging.error(f"Global error in message_handler: {e}")
        logging.error(traceback.format_exc())
        error_str = str(e).lower()
        if "timeout" in error_str or "proxy" in error_str:
            pass
        elif any(k in error_str for k in ("429", "quota", "exhausted", "rate", "resource_exhausted")):
            try:
                await message.answer("All model quotas exhausted. Please try again later.")
            except Exception:
                pass
        elif "404" in error_str or "not found" in error_str:
            try:
                await message.answer("API model error. The model may no longer be supported.")
            except Exception:
                pass
        elif "503" in error_str or "unavailable" in error_str:
            try:
                await message.answer("Google servers are overloaded. Try in a minute.")
            except Exception:
                pass
        else:
            try:
                await message.answer("Something went wrong. Please try again.")
            except Exception:
                pass


# -- Daily summary (no LLM) -----------------------------------------------
async def daily_summary_job(bot: Bot):
    if not DAILY_SUMMARY_ENABLED:
        logging.info("Daily summary disabled, skipping.")
        return
    try:
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        date_str = tomorrow.strftime("%Y-%m-%d")
        day_names = {
            0: "Monday", 1: "Tuesday", 2: "Wednesday",
            3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"
        }
        day_name = day_names.get(tomorrow.weekday(), "")
        tasks_tomorrow = []
        deadlines_tomorrow = []
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT category, description, type FROM tasks WHERE due_date = ? AND done = 0",
                (date_str,)
            ) as cursor:
                for row in await cursor.fetchall():
                    entry = f"- *{row[0]}*: {row[1]}"
                    if row[2] == "deadline":
                        deadlines_tomorrow.append(entry)
                    else:
                        tasks_tomorrow.append(entry)
        lines = [f"*Daily Summary: {day_name}, {tomorrow.strftime('%d.%m')}*\n"]
        if tasks_tomorrow:
            lines.append("*Tasks due tomorrow:*\n" + "\n".join(tasks_tomorrow))
        else:
            lines.append("No tasks due tomorrow.")
        if deadlines_tomorrow:
            lines.append("*Deadlines:*\n" + "\n".join(deadlines_tomorrow))
        text = "\n".join(lines)
        if GROUP_ID:
            try:
                await bot.send_message(
                    chat_id=int(GROUP_ID), text=text, parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Failed to send summary to {GROUP_ID}: {e}")
        else:
            logging.warning("GROUP_ID not set. Summary not sent.")
    except Exception as e:
        logging.error(f"Scheduler error: {e}")


@dp.message(Command("summary_off"))
async def cmd_summary_off(message: Message):
    global DAILY_SUMMARY_ENABLED
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied.")
        return
    DAILY_SUMMARY_ENABLED = False
    await message.answer("Daily summary disabled. To enable: /summary_on")


@dp.message(Command("summary_on"))
async def cmd_summary_on(message: Message):
    global DAILY_SUMMARY_ENABLED
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Access denied.")
        return
    DAILY_SUMMARY_ENABLED = True
    await message.answer("Daily summary enabled. To disable: /summary_off")


# -- Entry point -----------------------------------------------------------
async def main() -> None:
    global scheduler
    await init_db()
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(token=TOKEN, session=session)
    scheduler = AsyncIOScheduler(timezone=os.getenv("TIMEZONE", "UTC"))
    scheduler.add_job(
        daily_summary_job, "cron",
        hour=int(os.getenv("SUMMARY_HOUR", "20")),
        minute=int(os.getenv("SUMMARY_MINUTE", "0")),
        args=[bot]
    )
    scheduler.start()
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break
            except (KeyboardInterrupt, SystemExit):
                break
            except Exception as e:
                logging.error(f"Polling error, reconnecting in 5s: {e}")
                await asyncio.sleep(5)
    finally:
        logging.info("Shutting down bot...")
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        logging.info("Done.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped")
