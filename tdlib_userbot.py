"""
tdlib_userbot.py

TDLib userbot singleton. Handles:
  - login / auth
  - gift enumeration and upgrade
  - message tracking: new / edited / deleted (replaces Business Bot API)
  - self-destructing message forwarding

Uses raw TDLib API via aiotdlib for maximum speed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Set

import aiotdlib.utils as td_utils
import aiotdlib.client as td_client_mod
from aiotdlib import Client, ClientSettings
from aiotdlib.api import API, BaseObject
from aiotdlib.client import Client as _TDClient

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Monkeypatches
# ─────────────────────────────────────────────

_original_parse_td = td_utils.parse_tdlib_object


class UnknownUpdate:
    def __init__(self, data: dict):
        self.data = data
        self.ID = data.get("@type", "unknownUpdate")
        self.EXTRA = data.get("@extra") or {}


def _safe_parse_tdlib_object(data):
    if isinstance(data, dict):
        for key in ("message", "last_message"):
            node = data.get(key)
            if isinstance(node, dict) and node.get("@type") == "message":
                node.setdefault("message_thread_id", 0)
    try:
        obj = _original_parse_td(data)
    except Exception as e:
        if isinstance(data, dict):
            logger.debug("safe_parse: failed for type=%s: %s", data.get("@type"), e)
            return UnknownUpdate(data)
        raise
    if isinstance(obj, dict):
        return UnknownUpdate(obj)
    return obj


_orig_handle_pending = _TDClient._handle_pending_request


async def _safe_handle_pending_request(self: _TDClient, update):
    if not hasattr(update, "EXTRA"):
        logger.debug("Skip pending (no EXTRA): %s", getattr(update, "ID", type(update)))
        return
    try:
        await _orig_handle_pending(self, update)
    except Exception as e:
        logger.debug(
            "Error in _handle_pending_request for %s: %s",
            getattr(update, "ID", type(update)), e,
        )


td_utils.parse_tdlib_object = _safe_parse_tdlib_object
td_client_mod.parse_tdlib_object = _safe_parse_tdlib_object
_TDClient._handle_pending_request = _safe_handle_pending_request


# ─────────────────────────────────────────────
# Fix: td_set_log_message_callback access violation
# ─────────────────────────────────────────────

from aiotdlib.tdjson import CoreTDJson as _CoreTDJson

_orig_core_init = _CoreTDJson.__init__


def _patched_core_init(self, library_path):
    import ctypes
    import pathlib

    self.logger = logging.getLogger("aiotdlib.tdjson")
    library_path = pathlib.Path(library_path).resolve()
    if not library_path.exists():
        raise FileNotFoundError(f"Library path {library_path} does not exist")
    if not library_path.is_file():
        raise IsADirectoryError(f"Library path {library_path} must point to a binary file")

    self.logger.info('Using "%s" TDLib binary', library_path)
    self.library_path = library_path
    self._tdjson = ctypes.CDLL(str(self.library_path))

    self._td_create_client_id = self._tdjson.td_create_client_id
    self._td_create_client_id.restype = ctypes.c_int
    self._td_create_client_id.argtypes = []

    self._td_receive = self._tdjson.td_receive
    self._td_receive.restype = ctypes.c_char_p
    self._td_receive.argtypes = [ctypes.c_double]

    self._td_send = self._tdjson.td_send
    self._td_send.restype = None
    self._td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]

    self._td_execute = self._tdjson.td_execute
    self._td_execute.restype = ctypes.c_char_p
    self._td_execute.argtypes = [ctypes.c_char_p]

    try:
        from aiotdlib.tdjson import LogMessageCallback
        self._td_set_log_message_callback = self._tdjson.td_set_log_message_callback
        self._td_set_log_message_callback.restype = None
        self._td_set_log_message_callback.argtypes = [LogMessageCallback]
        self._td_set_log_message_callback(LogMessageCallback(self.__log_message_callback))
        self.logger.debug("td_set_log_message_callback registered successfully")
    except (OSError, AttributeError) as e:
        self.logger.warning("td_set_log_message_callback not available in this DLL build: %s", e)


_CoreTDJson.__init__ = _patched_core_init
logger.debug("CoreTDJson.__init__ patched for DLL compatibility")


# ─────────────────────────────────────────────
# Content helpers
# ─────────────────────────────────────────────

def _as_dict(obj: Any) -> dict:
    """Normalize any TDLib object to a plain dict best-effort."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "data") and isinstance(obj.data, dict):
        return obj.data
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True)
    return {}


def _deep_get(obj: Any, *attrs):
    """Walk a chain of attrs/keys, returning None if any step fails."""
    for a in attrs:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(a)
        else:
            obj = getattr(obj, a, None)
    return obj


def _extract_text_from_content(content: Any) -> str:
    """Return plain text from any TDLib message content."""
    d = _as_dict(content)
    # messageText → text.text
    t = d.get("text")
    if isinstance(t, dict):
        return t.get("text", "") or ""
    # media with caption
    c = d.get("caption")
    if isinstance(c, dict):
        return c.get("text", "") or ""
    return ""


# Maps TDLib content @type → (media_type_label, path_to_File_object_as_list_of_keys)
# Each entry says: to get the File object, traverse content[key0][key1]...
# For photo we traverse: content → photo → sizes[-1] → photo (which IS the File)
_CONTENT_MEDIA_MAP = {
    # type              label         traverse keys to reach File dict
    "messagePhoto":     ("photo",     None),       # special case: sizes array
    "messageVideo":     ("video",     ["video", "video"]),
    "messageVoiceNote": ("voice",     ["voice_note", "voice"]),
    "messageVideoNote": ("video_note",["video_note", "video"]),
    "messageAnimation": ("animation", ["animation", "animation"]),
    "messageSticker":   ("sticker",   ["sticker", "sticker"]),
    "messageDocument":  ("document",  ["document", "document"]),
    "messageAudio":     ("audio",     ["audio", "audio"]),
}


def _extract_tdlib_file(content: Any) -> tuple[int | None, str]:
    """
    Returns (tdlib_int_file_id, media_type).
    tdlib_int_file_id is the integer 'id' used in downloadFile requests.
    Returns (None, 'text') if no media found.
    """
    d = _as_dict(content)
    ctype = d.get("@type", "")

    entry = _CONTENT_MEDIA_MAP.get(ctype)
    if entry is None:
        return None, "text"

    media_label, keys = entry

    if ctype == "messagePhoto":
        # photo → sizes → last → photo (File)
        photo = d.get("photo", {})
        if isinstance(photo, dict):
            sizes = photo.get("sizes", [])
        else:
            sizes = getattr(photo, "sizes", []) or []
        if not sizes:
            return None, media_label
        last_size = sizes[-1]
        file_obj = _as_dict(last_size).get("photo") or {}
    else:
        # Walk the key path
        node = d
        for k in keys:
            node = (node.get(k) if isinstance(node, dict) else getattr(node, k, None)) or {}
            if not node:
                return None, media_label
        file_obj = node

    file_obj_d = _as_dict(file_obj) if not isinstance(file_obj, dict) else file_obj
    file_id = file_obj_d.get("id")
    if file_id is not None:
        return int(file_id), media_label
    return None, media_label


def _is_self_destruct(msg_dict: dict) -> bool:
    """Return True if message has a self-destruct timer or immediate type."""
    sdt = msg_dict.get("self_destruct_type")
    if not sdt:
        return False
    t = sdt.get("@type", "") if isinstance(sdt, dict) else getattr(sdt, "ID", "") or ""
    return t in ("messageSelfDestructTypeTimer", "messageSelfDestructTypeImmediately")


# ─────────────────────────────────────────────
# TdlibUserbot Class
# ─────────────────────────────────────────────

class TdlibUserbot:
    """
    Thin wrapper around aiotdlib.Client.
    Call start() once at bot startup. All methods are async-safe.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone_number: str,
        password: str,
        tdjson_path: str,
    ):
        self._settings = ClientSettings(
            api_id=api_id,
            api_hash=api_hash,
            phone_number=phone_number,
            library_path=tdjson_path,
            password=password,
        )
        self.client: Client | None = None

    async def start(self) -> None:
        """Start TDLib client (triggers auth on first run)."""
        self.client = Client(settings=self._settings)
        self._ctx = self.client.__aenter__()
        await self._ctx
        me = await self.client.api.get_me()
        logger.info("TDLib userbot logged in as %s (id=%d)", me.first_name, me.id)

    async def stop(self) -> None:
        """Gracefully close TDLib client."""
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
                logger.info("TDLib userbot stopped.")
            except Exception as e:
                logger.warning("Error stopping TDLib userbot: %s", e)

    # ─────────────────────────────────────────
    # Raw request helper
    # ─────────────────────────────────────────

    async def _raw_request(self, query: dict, timeout: float = 15.0) -> dict:
        """Send a raw TDLib JSON request and await the matching response."""
        import uuid
        import ujson

        request_id = uuid.uuid4().hex
        query["@extra"] = {"request_id": request_id}

        pending = asyncio.get_event_loop().create_future()

        class _FakePR:
            update = None
            def set_update(self, upd):
                self.update = upd
                if not pending.done():
                    pending.set_result(upd)

        self.client._pending_requests[request_id] = _FakePR()  # type: ignore
        await self.client.tdjson_client.send(ujson.dumps(query, ensure_ascii=False))

        try:
            result = await asyncio.wait_for(pending, timeout=timeout)
        except asyncio.TimeoutError:
            self.client._pending_requests.pop(request_id, None)
            raise TimeoutError(f"TDLib request timed out: {query.get('@type')}")

        if isinstance(result, dict):
            return result
        if hasattr(result, "data"):
            return result.data
        if hasattr(result, "model_dump"):
            return result.model_dump(by_alias=True)
        return {}

    # ─────────────────────────────────────────
    # File download helper
    # ─────────────────────────────────────────

    async def _download_file(self, tdlib_file_id: int, timeout: float = 30.0) -> str | None:
        """
        Download a file by its TDLib integer file ID.
        Returns the local filesystem path when complete, or None on failure.
        """
        result = await self._raw_request(
            {
                "@type": "downloadFile",
                "file_id": tdlib_file_id,
                "priority": 32,        # highest priority
                "synchronous": True,   # wait until fully downloaded
            },
            timeout=timeout,
        )

        if result.get("@type") == "error":
            logger.warning("downloadFile(%d) error: %s", tdlib_file_id, result.get("message"))
            return None

        local = result.get("local", {})
        if isinstance(local, dict):
            if local.get("is_downloading_completed"):
                path = local.get("path", "")
                return path if path else None
            else:
                logger.warning("downloadFile(%d): not fully downloaded yet", tdlib_file_id)
                return None

        # aiotdlib Pydantic model
        local_obj = result.get("local") if isinstance(result, dict) else getattr(result, "local", None)
        if local_obj:
            completed = getattr(local_obj, "is_downloading_completed", False)
            path = getattr(local_obj, "path", "")
            if completed and path:
                return path
        return None

    # ─────────────────────────────────────────
    # User info helper
    # ─────────────────────────────────────────

    async def _get_user_fullname(self, user_id: int) -> str:
        """
        Resolve a Telegram user_id to a human-readable name via TDLib getUser.
        Returns "FirstName LastName (@username)" where available.
        Falls back to str(user_id) if the request fails.
        """
        try:
            result = await self._raw_request(
                {"@type": "getUser", "user_id": user_id},
                timeout=10.0,
            )
            if result.get("@type") == "error":
                return str(user_id)
            first = result.get("first_name") or ""
            last  = result.get("last_name")  or ""
            uname = result.get("username")   or ""
            # Try usernames list as well (TDLib 1.8+)
            if not uname:
                usernames = result.get("usernames") or {}
                if isinstance(usernames, dict):
                    active = usernames.get("active_usernames") or []
                    if active:
                        uname = active[0]
            fullname = " ".join(filter(None, [first, last]))
            if uname:
                fullname = f"{fullname} (@{uname})" if fullname else f"@{uname}"
            return fullname or str(user_id)
        except Exception as e:
            logger.debug("_get_user_fullname(%d) failed: %s", user_id, e)
            return str(user_id)

    async def _is_bot_user(self, user_id: int) -> bool:
        """
        Return True if user_id belongs to a bot (userTypeBot).
        Returns False on any error (fail-open: don't skip if uncertain).
        """
        try:
            result = await self._raw_request(
                {"@type": "getUser", "user_id": user_id},
                timeout=10.0,
            )
            if result.get("@type") == "error":
                return False
            user_type = result.get("type") or {}
            utype = (
                user_type.get("@type") if isinstance(user_type, dict)
                else getattr(user_type, "@type", None) or getattr(user_type, "ID", None)
            )
            return utype == "userTypeBot"
        except Exception as e:
            logger.debug("_is_bot_user(%d) failed: %s", user_id, e)
            return False

    # ─────────────────────────────────────────
    # Message tracking handlers
    # ─────────────────────────────────────────

    def register_message_tracking_handlers(
        self,
        aiogram_bot,
        notify_user_id: int,
        timezone_local,
        edited_fmt: str,
        deleted_fmt: str,
        new_user_fmt: str,
        self_destruct_fmt: str,
        timer_immediately: str,
        timer_seconds: str,
        timer_fire: str,
        media_unavailable: str,
        media_unavailable_gone: str,
        messagesx_cls,
        usersdb_cls,
        watched_chat_ids: Set[int] | None = None,
    ) -> None:
        """
        Wire up updateNewMessage / updateMessageEdited / updateDeleteMessages handlers.
        Media is downloaded via TDLib's downloadFile and sent as local files.
        Self-destructing messages are forwarded immediately upon receipt.
        """
        from datetime import datetime, timezone
        from html import escape

        client = self.client
        if client is None:
            raise RuntimeError("TDLib userbot not started — call start() first")

        # ── Notification sender ───────────────
        async def _send_notification(
            message_old: str,
            message_new: str | None,
            user_fullname: str,
            user_id: int,
            timestamp: str,
            tdlib_file_id: int | None = None,
            media_type: str = "text",
        ) -> None:
            """
            Send edit/delete notification via aiogram bot.
            If tdlib_file_id is set, file is downloaded via TDLib and sent as FSInputFile.
            """
            from aiogram.types import FSInputFile

            user_fullname_escaped = escape(user_fullname)
            if message_new is None:
                caption = deleted_fmt.format(
                    user_fullname_escaped=user_fullname_escaped,
                    user_id=user_id,
                    timestamp=timestamp,
                    old_text=message_old,
                )
            else:
                caption = edited_fmt.format(
                    user_fullname_escaped=user_fullname_escaped,
                    user_id=user_id,
                    timestamp=timestamp,
                    old_text=message_old,
                    new_text=message_new,
                )

            try:
                if tdlib_file_id and media_type != "text":
                    local_path = await self._download_file(tdlib_file_id)
                    if local_path and os.path.exists(local_path):
                        input_file = FSInputFile(local_path)
                        methods = {
                            "photo":      aiogram_bot.send_photo,
                            "video":      aiogram_bot.send_video,
                            "voice":      aiogram_bot.send_voice,
                            "video_note": aiogram_bot.send_video_note,
                            "animation":  aiogram_bot.send_animation,
                            "sticker":    aiogram_bot.send_sticker,
                            "document":   aiogram_bot.send_document,
                            "audio":      aiogram_bot.send_audio,
                        }
                        method = methods.get(media_type)
                        if method:
                            if media_type in ("video_note", "sticker"):
                                await aiogram_bot.send_message(notify_user_id, caption, parse_mode="html")
                                await method(notify_user_id, input_file)
                            else:
                                await method(notify_user_id, input_file, caption=caption, parse_mode="html")
                            return
                    # Fall through to text if download failed
                    caption += media_unavailable

                await aiogram_bot.send_message(notify_user_id, caption, parse_mode="html")

            except Exception as e:
                logger.error("Failed to send notification to user %d: %s", notify_user_id, e)

        # ── Self-destruct forwarder ───────────
        async def _forward_self_destruct(
            msg_dict: dict,
            user_fullname: str,
            user_id: int,
        ) -> None:
            """Immediately download and forward a self-destructing message."""
            from aiogram.types import FSInputFile

            content = msg_dict.get("content", {})
            tdlib_fid, media_type = _extract_tdlib_file(content)
            text = _extract_text_from_content(content)

            sdt = msg_dict.get("self_destruct_type", {})
            sdt_type = sdt.get("@type", "") if isinstance(sdt, dict) else ""
            timer_s = sdt.get("self_destruct_time", 0) if isinstance(sdt, dict) else 0

            if sdt_type == "messageSelfDestructTypeImmediately":
                timer_label = timer_immediately
            elif timer_s:
                timer_label = timer_seconds.format(seconds=timer_s)
            else:
                timer_label = timer_fire

            header = self_destruct_fmt.format(
                timer_label=timer_label,
                user_id=user_id,
                user_fullname_escaped=escape(user_fullname),
            )
            if text:
                header += f"\n{escape(text)}"

            try:
                if tdlib_fid and media_type != "text":
                    local_path = await self._download_file(tdlib_fid, timeout=60.0)
                    if local_path and os.path.exists(local_path):
                        input_file = FSInputFile(local_path)
                        methods = {
                            "photo":      aiogram_bot.send_photo,
                            "video":      aiogram_bot.send_video,
                            "voice":      aiogram_bot.send_voice,
                            "video_note": aiogram_bot.send_video_note,
                            "animation":  aiogram_bot.send_animation,
                            "sticker":    aiogram_bot.send_sticker,
                            "document":   aiogram_bot.send_document,
                            "audio":      aiogram_bot.send_audio,
                        }
                        method = methods.get(media_type)
                        if method:
                            if media_type in ("video_note", "sticker"):
                                await aiogram_bot.send_message(notify_user_id, header, parse_mode="html")
                                await method(notify_user_id, input_file)
                            else:
                                await method(notify_user_id, input_file, caption=header, parse_mode="html")
                            return
                    header += media_unavailable_gone

                await aiogram_bot.send_message(notify_user_id, header, parse_mode="html")

            except Exception as e:
                logger.error("Failed to forward self-destruct from %d: %s", user_id, e)

        # ── updateNewMessage ──────────────────
        @client.on_event(API.Types.UPDATE_NEW_MESSAGE)
        async def _on_new_message(c: Client, update):
            try:
                msg = getattr(update, "message", None)
                if msg is None and hasattr(update, "data"):
                    msg = update.data.get("message")
                if not msg:
                    return

                msg_d = _as_dict(msg)
                chat_id = msg_d.get("chat_id")
                message_id = msg_d.get("id")
                date = msg_d.get("date")

                if not chat_id or not message_id:
                    return
                if chat_id <= 0:
                    return  # only private chats
                if watched_chat_ids and chat_id not in watched_chat_ids:
                    return

                content = msg_d.get("content", {})
                text = _extract_text_from_content(content)
                tdlib_fid, media_type = _extract_tdlib_file(content)

                msg_datetime_utc = (
                    datetime.fromtimestamp(date, tz=timezone.utc) if date
                    else datetime.now(timezone.utc)
                )
                timestamp_iso = msg_datetime_utc.isoformat()

                # Resolve author: for private chats chat_id == the other user
                sender = msg_d.get("sender_id", {})
                author_id = (
                    sender.get("user_id") if isinstance(sender, dict) else
                    getattr(sender, "user_id", None)
                ) or chat_id

                # Skip messages from bots
                if await self._is_bot_user(author_id):
                    logger.debug("Skipping message from bot user_id=%d", author_id)
                    return

                # Register new user if needed
                user_in_db = usersdb_cls.get(user_id=author_id)
                if user_in_db is None:
                    resolved_name = await self._get_user_fullname(author_id)
                    usersdb_cls.add(user_id=author_id, user_fullname=resolved_name)
                    logger.info("New user tracked via TDLib: user_id=%d name=%s", author_id, resolved_name)
                    try:
                        msg_text = new_user_fmt.format(
                            user_fullname_escaped=escape(resolved_name),
                            user_id=author_id,
                        )
                        await aiogram_bot.send_message(notify_user_id, msg_text, parse_mode="html")
                    except Exception as e:
                        logger.warning("Failed to send new-user notification: %s", e)

                # Save to DB (store tdlib_fid as string in file_id column)
                messagesx_cls.add(
                    user_id=author_id,
                    message_id=message_id,
                    message_text=text,
                    timestamp=timestamp_iso,
                    file_id=str(tdlib_fid) if tdlib_fid else None,
                    media_type=media_type,
                )
                logger.debug("Saved message chat_id=%d msg_id=%d media=%s", chat_id, message_id, media_type)

                # Self-destruct: forward immediately before it disappears
                if _is_self_destruct(msg_d):
                    logger.info("🔥 Self-destructing message from chat_id=%d — forwarding now", chat_id)
                    user_in_db2 = usersdb_cls.get(user_id=author_id)
                    if isinstance(user_in_db2, dict):
                        fullname = user_in_db2.get("user_fullname") or await self._get_user_fullname(author_id)
                    elif user_in_db2:
                        fullname = getattr(user_in_db2, "user_fullname", None) or await self._get_user_fullname(author_id)
                    else:
                        fullname = await self._get_user_fullname(author_id)
                    await _forward_self_destruct(msg_d, fullname, author_id)

            except Exception:
                logger.exception("Error in _on_new_message")

        # ── updateMessageEdited ───────────────
        @client.on_event(API.Types.UPDATE_MESSAGE_EDITED)
        async def _on_message_edited(c: Client, update):
            """
            updateMessageEdited has chat_id + message_id but NO new content.
            We call getMessage to retrieve the updated message.
            """
            try:
                data = update.data if hasattr(update, "data") else {}
                chat_id = getattr(update, "chat_id", None) or data.get("chat_id")
                message_id = getattr(update, "message_id", None) or data.get("message_id")

                if not chat_id or not message_id or chat_id <= 0:
                    return
                if watched_chat_ids and chat_id not in watched_chat_ids:
                    return

                user_msg = messagesx_cls.get(user_id=chat_id, message_id=message_id)
                if user_msg is None:
                    logger.debug("updateMessageEdited: no record for chat_id=%d msg_id=%d", chat_id, message_id)
                    return

                # Fetch updated content
                raw_msg = await self._raw_request({
                    "@type": "getMessage",
                    "chat_id": chat_id,
                    "message_id": message_id,
                })
                if raw_msg.get("@type") == "error":
                    logger.warning("getMessage failed: %s", raw_msg.get("message"))
                    return

                new_content = raw_msg.get("content", {})
                new_text = _extract_text_from_content(new_content)
                new_tdlib_fid, new_media_type = _extract_tdlib_file(new_content)

                user_in_db = usersdb_cls.get(user_id=chat_id)
                if isinstance(user_in_db, dict):
                    user_fullname = user_in_db.get("user_fullname") or await self._get_user_fullname(chat_id)
                elif user_in_db:
                    user_fullname = getattr(user_in_db, "user_fullname", None) or await self._get_user_fullname(chat_id)
                else:
                    user_fullname = await self._get_user_fullname(chat_id)

                message_timestamp = (
                    __import__("datetime").datetime
                    .fromisoformat(user_msg.timestamp)
                    .astimezone(timezone_local)
                )
                timestamp_formatted = message_timestamp.strftime("%d/%m/%y %H:%M")

                # Use new file if available, otherwise fall back to stored one
                tdlib_fid = new_tdlib_fid or (int(user_msg.file_id) if user_msg.file_id else None)
                media_type = new_media_type if new_tdlib_fid else user_msg.media_type

                await _send_notification(
                    message_old=user_msg.message_text,
                    message_new=new_text,
                    user_fullname=user_fullname,
                    user_id=chat_id,
                    timestamp=timestamp_formatted,
                    tdlib_file_id=tdlib_fid,
                    media_type=media_type,
                )

                messagesx_cls.update(
                    user_id=chat_id,
                    message_id=message_id,
                    message_text=new_text,
                    file_id=str(new_tdlib_fid) if new_tdlib_fid else user_msg.file_id,
                    media_type=media_type,
                )
                logger.debug("Edit recorded chat_id=%d msg_id=%d", chat_id, message_id)

            except Exception:
                logger.exception("Error in _on_message_edited")

        # ── updateDeleteMessages ──────────────
        @client.on_event(API.Types.UPDATE_DELETE_MESSAGES)
        async def _on_messages_deleted(c: Client, update):
            """
            Only process is_permanent=True (skip cache-purge noise).
            """
            try:
                data = update.data if hasattr(update, "data") else {}
                chat_id = getattr(update, "chat_id", None) or data.get("chat_id")
                message_ids = getattr(update, "message_ids", None) or data.get("message_ids", [])
                is_permanent = getattr(update, "is_permanent", None)
                if is_permanent is None:
                    is_permanent = data.get("is_permanent", True)

                if not chat_id or chat_id <= 0:
                    return
                if watched_chat_ids and chat_id not in watched_chat_ids:
                    return
                if not is_permanent:
                    return

                user_in_db = usersdb_cls.get(user_id=chat_id)
                if isinstance(user_in_db, dict):
                    user_fullname = user_in_db.get("user_fullname") or await self._get_user_fullname(chat_id)
                elif user_in_db:
                    user_fullname = getattr(user_in_db, "user_fullname", None) or await self._get_user_fullname(chat_id)
                else:
                    user_fullname = await self._get_user_fullname(chat_id)

                for msg_id in message_ids:
                    user_msg = messagesx_cls.get(user_id=chat_id, message_id=msg_id)
                    if user_msg is None:
                        continue

                    message_timestamp = (
                        __import__("datetime").datetime
                        .fromisoformat(user_msg.timestamp)
                        .astimezone(timezone_local)
                    )
                    timestamp_formatted = message_timestamp.strftime("%d/%m/%y %H:%M")
                    tdlib_fid = int(user_msg.file_id) if user_msg.file_id else None

                    await _send_notification(
                        message_old=user_msg.message_text,
                        message_new=None,
                        user_fullname=user_fullname,
                        user_id=chat_id,
                        timestamp=timestamp_formatted,
                        tdlib_file_id=tdlib_fid,
                        media_type=user_msg.media_type,
                    )
                    messagesx_cls.delete(user_id=chat_id, message_id=msg_id)
                    logger.debug("Deletion recorded chat_id=%d msg_id=%d", chat_id, msg_id)

            except Exception:
                logger.exception("Error in _on_messages_deleted")

        logger.info("Message tracking handlers registered on TDLib client")


# ─────────────────────────────────────────────
# Global Singleton
# ─────────────────────────────────────────────

_userbot: TdlibUserbot | None = None


def init_userbot(
    api_id: int,
    api_hash: str,
    phone_number: str,
    password: str,
    tdjson_path: str,
) -> TdlibUserbot:
    global _userbot
    _userbot = TdlibUserbot(api_id, api_hash, phone_number, password, tdjson_path)
    return _userbot


def get_userbot() -> TdlibUserbot:
    if _userbot is None:
        raise RuntimeError("TDLib userbot not initialized. Call init_userbot() first.")
    return _userbot
