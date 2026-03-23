EDITED_MESSAGE_FORMAT = (
    '<b>👤 ✏️ [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ] '
    '<code>{user_id}</code></b>\n'
    'Message from {timestamp}\n\n'
    '<b>Changed from:</b>\n'
    '<blockquote><code>{old_text}</code></blockquote>\n'
    '<b>To:</b>\n'
    '<blockquote><code>{new_text}</code></blockquote>'
)

DELETED_MESSAGE_FORMAT = (
    '<b>👤 🗑 [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ] '
    '<code>{user_id}</code></b>\n'
    'Message from {timestamp}\n\n'
    '<b>Deleted:</b>\n'
    '<blockquote><code>{old_text}</code></blockquote>'
)

NEW_USER_MESSAGE_FORMAT = (
    '<b>👤 📡 [ <a href="tg://user?id={user_id}">{user_fullname_escaped}</a> ]</b>\n\n'
    '<b>🆔 ID: </b><code>{user_id}</code>\n'
    '<b>📦 Source: </b><a href="https://github.com/evolv-X/all-seeing-Telegram-Userbot">GitHub</a>'
)

SELF_DESTRUCT_HEADER = (
    '🔥 <b>Self-destructing message</b> [{timer_label}]\n'
    '👤 <a href="tg://user?id={user_id}">{user_fullname_escaped}</a>\n'
)

TIMER_IMMEDIATELY = '⚡ immediately'
TIMER_SECONDS = '⏱ {seconds}s'
TIMER_FIRE = '🔥'
MEDIA_UNAVAILABLE = '\n<i>[media unavailable]</i>'
MEDIA_UNAVAILABLE_GONE = '\n<i>[media unavailable — too slow or already gone]</i>'
