import configparser
import importlib
import os
import sqlite3
from pydantic import BaseModel
import asyncio
from typing import Union
import logging
from aiogram import Router, Bot, Dispatcher, F, types
from aiogram.filters import Command
from html import escape
from datetime import datetime, timezone, timedelta
import pytz


config = configparser.ConfigParser()
config.read("config.ini")

TOKEN = config["telegram"]["token"].strip('"')
USER_ID = int(config["telegram"]["user_id"].strip('"'))
TIMEZONE_NAME = config["timezone"]["name"].strip('"')
timezone_local = pytz.timezone(TIMEZONE_NAME)
LANGUAGE = config["settings"]["language"].strip('"')

try:
    language_module = importlib.import_module(f"languages.{LANGUAGE}")
except ImportError:
    raise ImportError(f"Language module for '{LANGUAGE}' not found.")

router = Router(name=__name__)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EDITED_MESSAGE_FORMAT = language_module.EDITED_MESSAGE_FORMAT
DELETED_MESSAGE_FORMAT = language_module.DELETED_MESSAGE_FORMAT
NEW_USER_MESSAGE_FORMAT = language_module.NEW_USER_MESSAGE_FORMAT
START_MESSAGE_FORMAT = language_module.START_MESSAGE_FORMAT
SELF_DESTRUCT_HEADER = language_module.SELF_DESTRUCT_HEADER
TIMER_IMMEDIATELY = language_module.TIMER_IMMEDIATELY
TIMER_SECONDS = language_module.TIMER_SECONDS
TIMER_FIRE = language_module.TIMER_FIRE
MEDIA_UNAVAILABLE = language_module.MEDIA_UNAVAILABLE
MEDIA_UNAVAILABLE_GONE = language_module.MEDIA_UNAVAILABLE_GONE


def dict_factory(cursor, row) -> dict:
    save_dict = {}
    for idx, col in enumerate(cursor.description):
        save_dict[col[0]] = row[idx]
    return save_dict


def update_format(sql, parameters: dict) -> tuple[str, list]:
    values = ", ".join([f"{item} = ?" for item in parameters])
    sql += f" {values}"
    return sql, list(parameters.values())


class MessageRecord(BaseModel):
    user_id: int
    message_id: int
    message_text: str
    timestamp: str
    file_id: Union[str, None] = None
    media_type: Union[str, None] = "text"


class Messagesx:
    storage_name = "messages"
    PATH_DATABASE = "messages.db"

    @staticmethod
    def create_db():
        with sqlite3.connect(Messagesx.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS messages
                              (id INTEGER PRIMARY KEY,
                               user_id INTEGER,
                               message_id INTEGER,
                               message_text TEXT,
                               timestamp TEXT,
                               file_id TEXT,
                               media_type TEXT)''')
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")
                cursor.execute("ALTER TABLE messages ADD COLUMN media_type TEXT")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def add(user_id: int, message_id: int, message_text: str, timestamp: str, file_id: str = None, media_type: str = "text"):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            con.execute(
                f"INSERT INTO {Messagesx.storage_name} (user_id, message_id, message_text, timestamp, file_id, media_type) VALUES (?, ?, ?, ?, ?, ?)",
                [user_id, message_id, message_text, timestamp, file_id, media_type],
            )

    @staticmethod
    def get(user_id: int, message_id: int) -> Union[MessageRecord, None]:
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"SELECT * FROM {Messagesx.storage_name} WHERE user_id = ? AND message_id = ?"
            response = con.execute(sql, [user_id, message_id]).fetchone()
            if response is not None:
                response = MessageRecord(**response)
            return response

    @staticmethod
    def update(user_id: int, message_id: int, **kwargs):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"UPDATE {Messagesx.storage_name} SET"
            sql, parameters = update_format(sql, kwargs)
            parameters.extend([user_id, message_id])
            con.execute(sql + " WHERE user_id = ? AND message_id = ?", parameters)

    @staticmethod
    def delete(user_id: int, message_id: int):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"DELETE FROM {Messagesx.storage_name} WHERE user_id = ? AND message_id = ?"
            con.execute(sql, [user_id, message_id])

    @staticmethod
    def delete_old_messages(cutoff_timestamp: str):
        with sqlite3.connect(Messagesx.PATH_DATABASE) as con:
            sql = f"DELETE FROM {Messagesx.storage_name} WHERE timestamp < ?"
            con.execute(sql, [cutoff_timestamp])


class UsersDB:
    storage_name = "users"
    PATH_DATABASE = "users.db"

    @staticmethod
    def create_db():
        with sqlite3.connect(UsersDB.PATH_DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS users
                              (user_id INTEGER PRIMARY KEY, user_fullname TEXT)''')

    @staticmethod
    def add(user_id: int, user_fullname: str):
        with sqlite3.connect(UsersDB.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            con.execute(
                f"INSERT INTO {UsersDB.storage_name} (user_id, user_fullname) VALUES (?, ?)",
                [user_id, user_fullname],
            )

    @staticmethod
    def get(user_id: int) -> Union[dict, None]:
        with sqlite3.connect(UsersDB.PATH_DATABASE) as con:
            con.row_factory = dict_factory
            sql = f"SELECT * FROM {UsersDB.storage_name} WHERE user_id = ?"
            response = con.execute(sql, [user_id]).fetchone()
            return response


Messagesx.create_db()
UsersDB.create_db()


async def cleanup_old_messages():
    while True:
        now_local = datetime.now(timezone_local)
        next_run = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        sleep_seconds = (next_run - now_local).total_seconds()
        await asyncio.sleep(sleep_seconds)
        cutoff_datetime = datetime.now(timezone.utc) - timedelta(days=30)
        cutoff_timestamp_iso = cutoff_datetime.isoformat()
        Messagesx.delete_old_messages(cutoff_timestamp_iso)


@router.message(Command(commands=["start"]))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    user_fullname_escaped = escape(message.from_user.full_name)
    msg = START_MESSAGE_FORMAT.format(user_fullname_escaped=user_fullname_escaped, user_id=user_id)
    await message.answer(msg, parse_mode='html')


async def main() -> None:
    from tdlib_userbot import init_userbot

    # ── TDLib userbot credentials from config ──
    tdlib_cfg = config["tdlib"]
    api_id      = int(tdlib_cfg["api_id"].strip('"'))
    api_hash    = tdlib_cfg["api_hash"].strip('"')
    phone       = tdlib_cfg["phone"].strip('"')
    password    = tdlib_cfg.get("password", "").strip('"')
    tdjson_path = tdlib_cfg["tdjson_path"].strip('"')

    # ── Init and start userbot ──
    userbot = init_userbot(
        api_id=api_id,
        api_hash=api_hash,
        phone_number=phone,
        password=password,
        tdjson_path=tdjson_path,
    )
    await userbot.start()

    # ── Register message-tracking handlers ──
    userbot.register_message_tracking_handlers(
        aiogram_bot=Bot(token=TOKEN),
        notify_user_id=USER_ID,
        timezone_local=timezone_local,
        edited_fmt=EDITED_MESSAGE_FORMAT,
        deleted_fmt=DELETED_MESSAGE_FORMAT,
        new_user_fmt=NEW_USER_MESSAGE_FORMAT,
        self_destruct_fmt=SELF_DESTRUCT_HEADER,
        timer_immediately=TIMER_IMMEDIATELY,
        timer_seconds=TIMER_SECONDS,
        timer_fire=TIMER_FIRE,
        media_unavailable=MEDIA_UNAVAILABLE,
        media_unavailable_gone=MEDIA_UNAVAILABLE_GONE,
        messagesx_cls=Messagesx,
        usersdb_cls=UsersDB,
    )

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)

    # ── Run aiogram polling + TDLib idle concurrently ──
    cleanup_task = asyncio.create_task(cleanup_old_messages())
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            userbot.client.idle(),
        )
    finally:
        cleanup_task.cancel()
        await userbot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user")