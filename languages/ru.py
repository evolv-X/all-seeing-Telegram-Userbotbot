EDITED_MESSAGE_FORMAT = (
    '<b>👤 ✏️ [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ] '
    '<code>{user_id}</code></b>\n'
    'Сообщение от {timestamp}\n\n'
    '<b>Изменено с:</b>\n'
    '<blockquote><code>{old_text}</code></blockquote>\n'
    '<b>На:</b>\n'
    '<blockquote><code>{new_text}</code></blockquote>'
)

DELETED_MESSAGE_FORMAT = (
    '<b>👤 🗑 [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ] '
    '<code>{user_id}</code></b>\n'
    'Сообщение от {timestamp}\n\n'
    '<b>Удалено:</b>\n'
    '<blockquote><code>{old_text}</code></blockquote>'
)

NEW_USER_MESSAGE_FORMAT = (
    '<b>👤 📡 [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ]</b>\n\n'
    '<b>🆔 ID: </b><code>{user_id}</code>\n'
)

START_MESSAGE_FORMAT = (
    '<b>👤 📡 [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ]</b>\n\n'
    '<b>🆔 ID: </b><code>{user_id}</code>\n'
    '<b>📦 Репозиторий: </b><a href="https://github.com/evolv-X/all-seeing-Telegram-Userbot">GitHub</a>'
)

SELF_DESTRUCT_HEADER = (
    '🔥 <b>Исчезающее сообщение</b> [{timer_label}]\n'
    '👤 <a href="tg://user?id={user_id}">{user_fullname_escaped}</a>\n'
)

TIMER_IMMEDIATELY = '⚡ немедленно'
TIMER_SECONDS = '⏱ {seconds}с'
TIMER_FIRE = '🔥'
MEDIA_UNAVAILABLE = '\n<i>[медиа недоступно]</i>'
MEDIA_UNAVAILABLE_GONE = '\n<i>[медиа недоступно — слишком медленно или уже удалено]</i>'
