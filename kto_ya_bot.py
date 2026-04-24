import random
import sqlite3
import logging
import time
import uuid
import html
import os
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

BOT_TOKEN = "8442673427:AAEj15lEhVaxBFHUBw_EUYdJEV_-99_e6p4"
ADMIN_IDS = {5037478748, 6991875}

# =========================
# База данных
# =========================
# Сделано как в рабочем примере:
# папка data создается рядом с запуском проекта, база лежит в data/bot.db

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "bot.db")

os.makedirs(DB_DIR, exist_ok=True)
TRIGGERS = {"кто я", "кто", "я"}

ROLE_COOLDOWN_SECONDS = 10 * 60
BONUS_AMOUNT_MILLI = 100
MIN_WITHDRAW_MILLI = 100_000

WAIT_PHRASE = 1
WAIT_WALLET = 2
WAIT_AMOUNT = 3
WAIT_GIVE_USER = 4
WAIT_GIVE_AMOUNT = 5
WAIT_TAKE_USER = 6
WAIT_TAKE_AMOUNT = 7
WAIT_UID_USER = 8
WAIT_UID_VALUE = 9
WAIT_HIDE_USER = 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def ts() -> int:
    return int(time.time())


def is_admin(user_id: int | None) -> bool:
    return user_id in ADMIN_IDS


def is_group(chat) -> bool:
    return chat and chat.type in ("group", "supergroup")


def money(milli: int) -> str:
    whole = milli // 1000
    frac = milli % 1000
    if frac == 0:
        return f"{whole} USDT"
    return f"{whole}.{frac:03d}".rstrip("0") + " USDT"


def parse_money(text: str) -> int | None:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return int(round(value * 1000))


def mention(user) -> str:
    name = user.full_name or user.username or str(user.id)
    return f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'


def db():
    os.makedirs(DB_DIR, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)

    conn = db()
    cur = conn.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS phrases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            uid TEXT UNIQUE,
            balance_milli INTEGER NOT NULL DEFAULT 0,
            openings INTEGER NOT NULL DEFAULT 0,
            last_role_at INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bonus_claims (
            bonus_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_milli INTEGER NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            claimed_at INTEGER
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT,
            type TEXT,
            added_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            wallet TEXT NOT NULL,
            amount_milli INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            reviewed_by INTEGER,
            reviewed_at INTEGER
        )
        """
    )

    user_cols = table_columns(conn, "users")
    if "hidden" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

    cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('next_uid', '1')")

    conn.commit()
    conn.close()

    logger.info("База данных создана/открыта: %s", os.path.abspath(DB_PATH))

def next_uid(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='next_uid'").fetchone()
    current = int(row[0]) if row else 1
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('next_uid', ?)", (str(current + 1),))
    return str(current)


def register_user(user):
    if not user:
        return
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (user.username, user.first_name, user.id),
            )
        else:
            conn.execute(
                """
                INSERT INTO users
                (user_id, username, first_name, uid, balance_milli, openings, last_role_at, hidden, created_at)
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?)
                """,
                (user.id, user.username, user.first_name, next_uid(conn), ts()),
            )
        conn.commit()


def remember_group(chat):
    if not is_group(chat):
        return
    with db() as conn:
        row = conn.execute("SELECT chat_id FROM groups WHERE chat_id=?", (chat.id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE groups SET title=?, username=?, type=?, last_seen_at=? WHERE chat_id=?",
                (chat.title, chat.username, chat.type, ts(), chat.id),
            )
        else:
            conn.execute(
                "INSERT INTO groups (chat_id, title, username, type, added_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat.id, chat.title, chat.username, chat.type, ts(), ts()),
            )
        conn.commit()


def get_user(user_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT user_id, username, first_name, uid, balance_milli, openings, last_role_at, hidden
            FROM users WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()


def add_phrase_db(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    with db() as conn:
        try:
            conn.execute("INSERT INTO phrases (text, created_at) VALUES (?, ?)", (text, ts()))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def random_phrase() -> str | None:
    with db() as conn:
        rows = conn.execute("SELECT text FROM phrases").fetchall()
    if not rows:
        return None
    return random.choice(rows)[0]


def last_phrases(limit=10):
    with db() as conn:
        return conn.execute("SELECT id, text FROM phrases ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def phrase_count() -> int:
    with db() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0])


def delete_phrase_db(pid: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM phrases WHERE id=?", (pid,))
        conn.commit()
        return cur.rowcount > 0


def add_balance(user_id: int, amount: int):
    with db() as conn:
        conn.execute("UPDATE users SET balance_milli=balance_milli+? WHERE user_id=?", (amount, user_id))
        conn.commit()


def take_balance(user_id: int, amount: int) -> tuple[bool, str]:
    with db() as conn:
        row = conn.execute("SELECT balance_milli FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return False, "Пользователь не найден."
        bal = int(row[0])
        if amount > bal:
            return False, f"У пользователя только {money(bal)}."
        conn.execute("UPDATE users SET balance_milli=balance_milli-? WHERE user_id=?", (amount, user_id))
        conn.commit()
    return True, "Готово."


def set_uid(user_id: int, uid: str) -> tuple[bool, str]:
    uid = uid.strip()
    if not uid:
        return False, "UID пустой."
    with db() as conn:
        if not conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone():
            return False, "Пользователь не найден. Он должен сначала вызвать бота или написать /start."
        try:
            conn.execute("UPDATE users SET uid=? WHERE user_id=?", (uid, user_id))
            conn.commit()
            return True, "UID изменен."
        except sqlite3.IntegrityError:
            return False, "Такой UID уже занят."


def hide_user(user_id: int) -> tuple[bool, str]:
    with db() as conn:
        if not conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone():
            return False, "Пользователь не найден."
        conn.execute("UPDATE users SET hidden=1 WHERE user_id=?", (user_id,))
        conn.commit()
    return True, "Пользователь скрыт."


def inc_opening(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET openings=openings+1, last_role_at=? WHERE user_id=?", (ts(), user_id))
        conn.commit()


def create_bonus(user_id: int) -> str:
    bonus_id = uuid.uuid4().hex[:16]
    with db() as conn:
        conn.execute(
            "INSERT INTO bonus_claims (bonus_id, user_id, amount_milli, claimed, created_at) VALUES (?, ?, ?, 0, ?)",
            (bonus_id, user_id, BONUS_AMOUNT_MILLI, ts()),
        )
        conn.commit()
    return bonus_id


def claim_bonus(bonus_id: str, user_id: int) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT user_id, amount_milli, claimed FROM bonus_claims WHERE bonus_id=?",
            (bonus_id,),
        ).fetchone()
        if not row:
            return "Бонус не найден."
        owner, amount, claimed = row
        if int(owner) != int(user_id):
            return "Этот бонус не для вас."
        if claimed:
            return "Вы уже получили этот бонус."
        conn.execute("UPDATE bonus_claims SET claimed=1, claimed_at=? WHERE bonus_id=?", (ts(), bonus_id))
        conn.execute("UPDATE users SET balance_milli=balance_milli+? WHERE user_id=?", (amount, user_id))
        conn.commit()
    return f"Вы получили {money(amount)}"


def top_text() -> str:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, first_name, uid, balance_milli
            FROM users
            WHERE hidden=0
            ORDER BY balance_milli DESC
            LIMIT 3
            """
        ).fetchall()
    if not rows:
        return "Топ пока пуст."
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ 3 по USDT</b>\n"]
    for i, (user_id, username, first_name, uid, balance) in enumerate(rows):
        name = f"@{username}" if username else (first_name or f"ID {user_id}")
        lines.append(f"{medals[i]} {html.escape(name)} | UID: <code>{html.escape(str(uid))}</code> | <b>{money(balance)}</b>")
    return "\n".join(lines)


def profile_text(user_id: int) -> str:
    row = get_user(user_id)
    if not row:
        return "Профиль не найден. Напиши /start."
    user_id, username, first_name, uid, balance, openings, last_role, hidden = row
    uname = f"@{username}" if username else "нет"
    hidden_line = "\n🙈 Статус: <b>скрыт</b>" if hidden else ""
    return (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n"
        f"🔖 UID: <code>{html.escape(str(uid))}</code>\n"
        f"👁 Открытия: <b>{openings}</b>\n"
        f"💰 Баланс: <b>{money(balance)}</b>\n"
        f"📛 Username: {html.escape(uname)}"
        f"{hidden_line}"
    )


def groups_text() -> str:
    with db() as conn:
        rows = conn.execute("SELECT chat_id, title, username, type FROM groups ORDER BY last_seen_at DESC").fetchall()
    if not rows:
        return "Бот пока не найден ни в одной группе."
    lines = ["👥 <b>Группы с ботом</b>\n"]
    for chat_id, title, username, typ in rows[:50]:
        title = title or "Без названия"
        uname = f"@{username}" if username else "нет username"
        lines.append(f"• <b>{html.escape(title)}</b>\n  ID: <code>{chat_id}</code>\n  Username: {html.escape(uname)}")
    return "\n\n".join(lines)


def create_withdrawal(user_id: int, wallet: str, amount: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO withdrawals (user_id, wallet, amount_milli, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (user_id, wallet, amount, ts()),
        )
        conn.commit()
        return cur.lastrowid


def get_withdrawal(wid: int):
    with db() as conn:
        return conn.execute("SELECT id, user_id, wallet, amount_milli, status FROM withdrawals WHERE id=?", (wid,)).fetchone()


def set_withdrawal(wid: int, status: str, admin_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row or row[0] != "pending":
            return False
        conn.execute("UPDATE withdrawals SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?", (status, admin_id, ts(), wid))
        conn.commit()
    return True


def main_menu(admin=False, group=False):
    buttons = [[InlineKeyboardButton("🎭 Кто я?", callback_data="whoami")]]
    if not group:
        buttons.append([
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            InlineKeyboardButton("💸 Вывод USDT", callback_data="withdraw"),
        ])
    buttons.append([InlineKeyboardButton("🏆 Топ 3", callback_data="top3")])
    if admin:
        buttons.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin_menu")])
    return InlineKeyboardMarkup(buttons)


def role_menu(bonus_id: str, group=False):
    buttons = [[InlineKeyboardButton("🎁 Бонус", callback_data=f"bonus:{bonus_id}")]]
    if not group:
        buttons.append([
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            InlineKeyboardButton("💸 Вывод USDT", callback_data="withdraw"),
        ])
    return InlineKeyboardMarkup(buttons)


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить фразу", callback_data="add_phrase")],
        [InlineKeyboardButton("📋 Последние фразы", callback_data="last_phrases")],
        [InlineKeyboardButton("🔢 Количество фраз", callback_data="phrase_count")],
        [InlineKeyboardButton("💰 Выдать USDT", callback_data="give_usdt")],
        [InlineKeyboardButton("➖ Забрать USDT", callback_data="take_usdt")],
        [InlineKeyboardButton("🆔 Выдать кастом UID", callback_data="custom_uid")],
        [InlineKeyboardButton("🙈 Скрыть пользователя", callback_data="hide_user")],
        [InlineKeyboardButton("👥 Группы с ботом", callback_data="groups")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ])


def withdraw_admin_menu(wid: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"wd_ok:{wid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_no:{wid}"),
    ]])


async def delete_last_private(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    mid = context.user_data.get("last_private_result")
    if not mid:
        return
    try:
        await context.bot.delete_message(chat_id, mid)
    except BadRequest:
        pass
    except Exception:
        pass
    context.user_data["last_private_result"] = None


async def send_result(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    chat = update.effective_chat
    if chat.type == "private":
        await delete_last_private(context, chat.id)
    msg = await context.bot.send_message(chat.id, text, parse_mode="HTML", reply_markup=reply_markup)
    if chat.type == "private":
        context.user_data["last_private_result"] = msg.message_id
    return msg


async def send_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    register_user(user)
    remember_group(chat)

    row = get_user(user.id)
    if not row:
        await send_result(update, context, "Ошибка профиля. Напиши /start.")
        return

    last_role = row[6]
    left = ROLE_COOLDOWN_SECONDS - (ts() - last_role)

    if left > 0:
        await send_result(
            update,
            context,
            f"⏳ {mention(user)}, подожди еще {left // 60} мин. {left % 60} сек."
        )
        return

    phrase = random_phrase()
    if not phrase:
        await send_result(update, context, "В базе пока нет фраз.")
        return

    inc_opening(user.id)
    bonus_id = create_bonus(user.id)
    await send_result(
        update,
        context,
        f"🎭 {mention(user)}, ты: <b>{html.escape(phrase)}</b>",
        reply_markup=role_menu(bonus_id, group=is_group(chat)),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    remember_group(update.effective_chat)
    await update.message.reply_text(
        "🎭 Бот для игры «Кто я?»\n\n"
        "В группе напиши: <b>кто я</b>, <b>кто</b> или <b>я</b>.",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin(update.effective_user.id), is_group(update.effective_chat)),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_role(update, context)


async def trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    register_user(update.effective_user)
    remember_group(update.effective_chat)
    if update.message.text.strip().lower() in TRIGGERS:
        await send_role(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    remember_group(update.effective_chat)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return
    await update.message.reply_text("⚙️ Админ-меню:", reply_markup=admin_menu())


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Напиши так:\n/add Шрек")
        return
    if add_phrase_db(text):
        await update.message.reply_text(f"✅ Фраза добавлена: <b>{html.escape(text)}</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Такая фраза уже есть или текст пустой.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return
    rows = last_phrases(20)
    if not rows:
        await update.message.reply_text("Фраз пока нет.")
        return
    text = "📋 Последние фразы:\n\n" + "\n".join(f"{pid}. {html.escape(txt)}" for pid, txt in rows)
    await update.message.reply_text(text, parse_mode="HTML")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Напиши так:\n/delete 12")
        return
    ok = delete_phrase_db(int(context.args[0]))
    await update.message.reply_text("🗑 Удалено." if ok else "⚠️ ID не найден.")


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await send_result(update, context, profile_text(update.effective_user.id))


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await send_result(update, context, top_text())


async def dbpath_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return

    os.makedirs(DB_DIR, exist_ok=True)

    # Проверочная запись, чтобы сразу понять, сохраняет ли база.
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS db_check (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL)"
    )
    cur.execute("INSERT INTO db_check (created_at) VALUES (?)", (ts(),))
    cur.execute("SELECT COUNT(*) FROM db_check")
    count = cur.fetchone()[0]
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🗄 База данных:\n"
        f"<code>{html.escape(os.path.abspath(DB_PATH))}</code>\n\n"
        f"Файл существует: <b>{os.path.exists(DB_PATH)}</b>\n"
        f"Проверочных записей: <b>{count}</b>",
        parse_mode="HTML",
    )



async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def add_phrase_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("➕ Отправь новую фразу одним сообщением.\n\nДля отмены напиши /cancel")
    return WAIT_PHRASE


async def receive_phrase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    text = update.message.text.strip()
    if add_phrase_db(text):
        await update.message.reply_text(f"✅ Фраза добавлена: <b>{html.escape(text)}</b>", parse_mode="HTML", reply_markup=admin_menu())
    else:
        await update.message.reply_text("⚠️ Такая фраза уже есть или текст пустой.", reply_markup=admin_menu())
    return ConversationHandler.END


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.type != "private":
        await q.answer("Вывод доступен только в личке с ботом.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    register_user(q.from_user)
    row = get_user(q.from_user.id)
    bal = row[4]
    if bal < MIN_WITHDRAW_MILLI:
        await send_result(
            update,
            context,
            "❌ Недостаточно средств для вывода.\n"
            f"Минимальная сумма вывода: <b>{money(MIN_WITHDRAW_MILLI)}</b>\n"
            f"Ваш баланс: <b>{money(bal)}</b>",
        )
        return ConversationHandler.END
    await send_result(update, context, "💸 Введите адрес кошелька USDT в сети TON:")
    return WAIT_WALLET


async def withdraw_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = update.message.text.strip()
    if len(wallet) < 10:
        await update.message.reply_text("Адрес слишком короткий. Отправь корректный адрес.")
        return WAIT_WALLET
    context.user_data["wallet"] = wallet
    await update.message.reply_text("Теперь введи сумму вывода в USDT.\nНапример: 100 или 150.5")
    return WAIT_AMOUNT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    amount = parse_money(update.message.text)
    if amount is None:
        await update.message.reply_text("Введите сумму числом.")
        return WAIT_AMOUNT
    if amount < MIN_WITHDRAW_MILLI:
        await update.message.reply_text(f"Минимальная сумма вывода: {money(MIN_WITHDRAW_MILLI)}")
        return WAIT_AMOUNT
    row = get_user(update.effective_user.id)
    if amount > row[4]:
        await update.message.reply_text(f"Недостаточно средств.\nВаш баланс: {money(row[4])}")
        return ConversationHandler.END
    ok, msg = take_balance(update.effective_user.id, amount)
    if not ok:
        await update.message.reply_text(msg)
        return ConversationHandler.END

    wallet = context.user_data["wallet"]
    wid = create_withdrawal(update.effective_user.id, wallet, amount)
    await update.message.reply_text("✅ Заявка на вывод создана и отправлена админам на проверку.")

    text = (
        "💸 <b>Новая заявка на вывод</b>\n\n"
        f"ID заявки: <code>{wid}</code>\n"
        f"Пользователь: {mention(update.effective_user)}\n"
        f"Telegram ID: <code>{update.effective_user.id}</code>\n"
        f"Сумма: <b>{money(amount)}</b>\n"
        f"Кошелек TON USDT:\n<code>{html.escape(wallet)}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=withdraw_admin_menu(wid))
        except Exception as e:
            logger.warning("Не удалось отправить заявку админу %s: %s", admin_id, e)
    return ConversationHandler.END


async def give_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("💰 Введите Telegram ID пользователя, которому нужно выдать USDT:")
    return WAIT_GIVE_USER


async def give_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("Введите Telegram ID числом.")
        return WAIT_GIVE_USER
    uid = int(update.message.text.strip())
    if not get_user(uid):
        await update.message.reply_text("Пользователь не найден. Он должен сначала вызвать бота или написать /start.")
        return ConversationHandler.END
    context.user_data["give_user"] = uid
    await update.message.reply_text("Введите сумму USDT для выдачи:")
    return WAIT_GIVE_AMOUNT


async def give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_money(update.message.text)
    if amount is None or amount <= 0:
        await update.message.reply_text("Введите сумму больше 0.")
        return WAIT_GIVE_AMOUNT
    user_id = context.user_data["give_user"]
    add_balance(user_id, amount)
    await update.message.reply_text(f"✅ Пользователю <code>{user_id}</code> выдано <b>{money(amount)}</b>.", parse_mode="HTML")
    try:
        await context.bot.send_message(user_id, f"💰 Вам начислено <b>{money(amount)}</b>.", parse_mode="HTML")
    except Exception:
        pass
    return ConversationHandler.END


async def take_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("➖ Введите Telegram ID пользователя, у которого нужно забрать USDT:")
    return WAIT_TAKE_USER


async def take_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("Введите Telegram ID числом.")
        return WAIT_TAKE_USER
    uid = int(update.message.text.strip())
    if not get_user(uid):
        await update.message.reply_text("Пользователь не найден.")
        return ConversationHandler.END
    context.user_data["take_user"] = uid
    await update.message.reply_text("Введите сумму USDT, которую нужно забрать:")
    return WAIT_TAKE_AMOUNT


async def take_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_money(update.message.text)
    if amount is None or amount <= 0:
        await update.message.reply_text("Введите сумму больше 0.")
        return WAIT_TAKE_AMOUNT
    user_id = context.user_data["take_user"]
    ok, msg = take_balance(user_id, amount)
    if ok:
        await update.message.reply_text(f"✅ У пользователя <code>{user_id}</code> забрано <b>{money(amount)}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"⚠️ {msg}")
    return ConversationHandler.END


async def uid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("🆔 Введите Telegram ID пользователя:")
    return WAIT_UID_USER


async def uid_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("Введите Telegram ID числом.")
        return WAIT_UID_USER
    target = int(update.message.text.strip())
    if not get_user(target):
        await update.message.reply_text("Пользователь не найден.")
        return ConversationHandler.END
    context.user_data["uid_user"] = target
    await update.message.reply_text("Введите новый кастом UID:")
    return WAIT_UID_VALUE


async def uid_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, msg = set_uid(context.user_data["uid_user"], update.message.text)
    await update.message.reply_text(("✅ " if ok else "⚠️ ") + msg)
    return ConversationHandler.END


async def hide_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("🙈 Введите Telegram ID пользователя, которого нужно скрыть:")
    return WAIT_HIDE_USER


async def hide_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("Введите Telegram ID числом.")
        return WAIT_HIDE_USER
    target = int(update.message.text.strip())
    ok, msg = hide_user(target)
    await update.message.reply_text(f"{'✅' if ok else '⚠️'} {msg}")
    return ConversationHandler.END


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    register_user(q.from_user)
    if q.message:
        remember_group(q.message.chat)

    if data.startswith("bonus:"):
        msg = claim_bonus(data.split(":", 1)[1], q.from_user.id)
        await q.answer(msg, show_alert=True)
        return

    if data.startswith("wd_ok:") or data.startswith("wd_no:"):
        await q.answer()
        if not is_admin(q.from_user.id):
            await q.message.reply_text("⛔ У тебя нет доступа.")
            return
        wid = int(data.split(":", 1)[1])
        row = get_withdrawal(wid)
        if not row:
            await q.edit_message_text("Заявка не найдена.")
            return
        _, target, wallet, amount, status = row
        if status != "pending":
            await q.edit_message_text("Эта заявка уже обработана.")
            return
        if data.startswith("wd_ok:"):
            if set_withdrawal(wid, "approved", q.from_user.id):
                await q.edit_message_text(f"✅ Заявка #{wid} одобрена.\nСумма: {money(amount)}")
                try:
                    await context.bot.send_message(target, f"✅ Ваша заявка на вывод {money(amount)} одобрена.")
                except Exception:
                    pass
        else:
            if set_withdrawal(wid, "declined", q.from_user.id):
                add_balance(target, amount)
                await q.edit_message_text(f"❌ Заявка #{wid} отклонена.\nСумма возвращена пользователю: {money(amount)}")
                try:
                    await context.bot.send_message(target, f"❌ Ваша заявка на вывод {money(amount)} отклонена. Средства возвращены на баланс.")
                except Exception:
                    pass
        return

    await q.answer()

    if data == "whoami":
        await send_role(update, context)
    elif data == "profile":
        if q.message.chat.type != "private":
            await q.answer("Профиль доступен только в личке.", show_alert=True)
        else:
            await send_result(update, context, profile_text(q.from_user.id))
    elif data == "top3":
        await send_result(update, context, top_text())
    elif data == "admin_menu":
        if is_admin(q.from_user.id):
            await q.edit_message_text("⚙️ Админ-меню:", reply_markup=admin_menu())
        else:
            await q.message.reply_text("⛔ У тебя нет доступа.")
    elif data == "back":
        await q.edit_message_text("Главное меню:", reply_markup=main_menu(is_admin(q.from_user.id), is_group(q.message.chat)))
    elif data == "last_phrases":
        rows = last_phrases(10)
        text = "Фраз пока нет." if not rows else "📋 Последние фразы:\n\n" + "\n".join(f"{pid}. {html.escape(txt)}" for pid, txt in rows)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=admin_menu())
    elif data == "phrase_count":
        await q.edit_message_text(f"🔢 В базе фраз: {phrase_count()}", reply_markup=admin_menu())
    elif data == "groups":
        await q.message.reply_text(groups_text(), parse_mode="HTML")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("dbpath", dbpath_cmd))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_phrase_start, pattern="^add_phrase$")],
        states={WAIT_PHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phrase)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^withdraw$")],
        states={
            WAIT_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet)],
            WAIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(give_start, pattern="^give_usdt$")],
        states={
            WAIT_GIVE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, give_user)],
            WAIT_GIVE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, give_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(take_start, pattern="^take_usdt$")],
        states={
            WAIT_TAKE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_user)],
            WAIT_TAKE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(uid_start, pattern="^custom_uid$")],
        states={
            WAIT_UID_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, uid_user)],
            WAIT_UID_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, uid_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(hide_start, pattern="^hide_user$")],
        states={WAIT_HIDE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, hide_finish)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trigger))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
