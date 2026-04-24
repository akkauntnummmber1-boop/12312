import random
import sqlite3
import time
import uuid
import html
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, ContextTypes, filters

BOT_TOKEN = "8442673427:AAEj15lEhVaxBFHUBw_EUYdJEV_-99_e6p4"
ADMIN_IDS = {5037478748, 6991875}
DB_PATH = Path("kto_ya.sqlite3")
TRIGGERS = {"кто я", "кто", "я"}
COOLDOWN = 600
BONUS = 100          # 0.1 USDT в тысячных
MIN_WD = 100000      # 100 USDT в тысячных
ADD_PHRASE, WD_WALLET, WD_AMOUNT, GIVE_USER, GIVE_AMOUNT, UID_USER, UID_VALUE = range(1, 8)


def ts():
    return int(time.time())


def money(v):
    whole, frac = divmod(int(v), 1000)
    if frac == 0:
        return f"{whole} USDT"
    return f"{whole}.{str(frac).zfill(3).rstrip('0')} USDT"


def mention(user):
    name = html.escape(user.full_name or user.username or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def admin(uid):
    return uid in ADMIN_IDS


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS phrases(id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT UNIQUE NOT NULL,created_at INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,uid TEXT UNIQUE,balance INTEGER DEFAULT 0,opens INTEGER DEFAULT 0,last_role INTEGER DEFAULT 0,created_at INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS bonuses(id TEXT PRIMARY KEY,user_id INTEGER,amount INTEGER,claimed INTEGER DEFAULT 0,created_at INTEGER,claimed_at INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS groups(chat_id INTEGER PRIMARY KEY,title TEXT,username TEXT,type TEXT,added_at INTEGER,last_seen INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,wallet TEXT,amount INTEGER,status TEXT DEFAULT 'pending',created_at INTEGER,reviewed_by INTEGER,reviewed_at INTEGER)")
        c.commit()


def make_uid(user_id):
    return str(10000000 + user_id % 90000000)


def save_user(user):
    if not user:
        return
    with db() as c:
        exists = c.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,)).fetchone()
        if exists:
            c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (user.username, user.first_name, user.id))
        else:
            uid = make_uid(user.id)
            try:
                c.execute("INSERT INTO users(user_id,username,first_name,uid,created_at) VALUES(?,?,?,?,?)", (user.id, user.username, user.first_name, uid, ts()))
            except sqlite3.IntegrityError:
                c.execute("INSERT INTO users(user_id,username,first_name,uid,created_at) VALUES(?,?,?,?,?)", (user.id, user.username, user.first_name, uid + str(random.randint(10,99)), ts()))
        c.commit()


def save_group(chat):
    if not chat or chat.type not in ("group", "supergroup"):
        return
    with db() as c:
        if c.execute("SELECT 1 FROM groups WHERE chat_id=?", (chat.id,)).fetchone():
            c.execute("UPDATE groups SET title=?,username=?,type=?,last_seen=? WHERE chat_id=?", (chat.title, chat.username, chat.type, ts(), chat.id))
        else:
            c.execute("INSERT INTO groups(chat_id,title,username,type,added_at,last_seen) VALUES(?,?,?,?,?,?)", (chat.id, chat.title, chat.username, chat.type, ts(), ts()))
        c.commit()


def get_user(user_id):
    with db() as c:
        return c.execute("SELECT user_id,username,first_name,uid,balance,opens,last_role FROM users WHERE user_id=?", (user_id,)).fetchone()


def add_balance(user_id, amount):
    with db() as c:
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        c.commit()


def take_balance(user_id, amount):
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or row[0] < amount:
            return False
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
        c.commit()
        return True


def add_phrase_db(text):
    text = text.strip()
    if not text:
        return False
    with db() as c:
        try:
            c.execute("INSERT INTO phrases(text,created_at) VALUES(?,?)", (text, ts()))
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def random_phrase():
    with db() as c:
        rows = c.execute("SELECT text FROM phrases").fetchall()
    return random.choice(rows)[0] if rows else None


def last_phrases(n=10):
    with db() as c:
        return c.execute("SELECT id,text FROM phrases ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def del_phrase(pid):
    with db() as c:
        cur = c.execute("DELETE FROM phrases WHERE id=?", (pid,))
        c.commit()
        return cur.rowcount > 0


def create_bonus(user_id):
    bid = uuid.uuid4().hex[:16]
    with db() as c:
        c.execute("INSERT INTO bonuses(id,user_id,amount,created_at) VALUES(?,?,?,?)", (bid, user_id, BONUS, ts()))
        c.commit()
    return bid


def claim_bonus(bid, user_id):
    with db() as c:
        row = c.execute("SELECT user_id,amount,claimed FROM bonuses WHERE id=?", (bid,)).fetchone()
        if not row:
            return "Бонус не найден."
        owner, amount, claimed = row
        if owner != user_id:
            return "Этот бонус не для вас."
        if claimed:
            return "Вы уже получили этот бонус."
        c.execute("UPDATE bonuses SET claimed=1,claimed_at=? WHERE id=?", (ts(), bid))
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        c.commit()
    return f"Вы получили {money(amount)}"


def set_uid(user_id, new_uid):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            return False, "Пользователь не найден. Он должен сначала написать /start."
        try:
            c.execute("UPDATE users SET uid=? WHERE user_id=?", (new_uid.strip(), user_id))
            c.commit()
            return True, "UID изменен."
        except sqlite3.IntegrityError:
            return False, "Такой UID уже занят."


def create_wd(user_id, wallet, amount):
    with db() as c:
        cur = c.execute("INSERT INTO withdrawals(user_id,wallet,amount,created_at) VALUES(?,?,?,?)", (user_id, wallet, amount, ts()))
        c.commit()
        return cur.lastrowid


def get_wd(wid):
    with db() as c:
        return c.execute("SELECT id,user_id,wallet,amount,status FROM withdrawals WHERE id=?", (wid,)).fetchone()


def set_wd(wid, status, admin_id):
    with db() as c:
        row = c.execute("SELECT status FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row or row[0] != "pending":
            return False
        c.execute("UPDATE withdrawals SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", (status, admin_id, ts(), wid))
        c.commit()
        return True


def main_kb(is_admin=False):
    rows = [
        [InlineKeyboardButton("🎭 Кто я?", callback_data="whoami")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile"), InlineKeyboardButton("💸 Вывод USDT", callback_data="withdraw")],
        [InlineKeyboardButton("🏆 Топ 3", callback_data="top3")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def role_kb(bid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Бонус", callback_data=f"bonus:{bid}")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile"), InlineKeyboardButton("💸 Вывод USDT", callback_data="withdraw")],
    ])


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить фразу", callback_data="add_phrase")],
        [InlineKeyboardButton("📋 Последние фразы", callback_data="last_phrases")],
        [InlineKeyboardButton("🔢 Количество фраз", callback_data="count_phrases")],
        [InlineKeyboardButton("💰 Выдать USDT", callback_data="give_usdt")],
        [InlineKeyboardButton("🆔 Выдать кастом UID", callback_data="custom_uid")],
        [InlineKeyboardButton("👥 Группы с ботом", callback_data="groups")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ])


def profile_text(user_id):
    u = get_user(user_id)
    if not u:
        return "Профиль не найден. Напиши /start."
    uid, username, first_name, custom_uid, balance, opens, last_role = u
    username = f"@{username}" if username else "нет"
    return (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID пользователя: <code>{uid}</code>\n"
        f"🔖 UID: <code>{html.escape(str(custom_uid))}</code>\n"
        f"👁 Открытия: <b>{opens}</b>\n"
        f"💰 Баланс USDT: <b>{money(balance)}</b>\n"
        f"📛 Username: {html.escape(username)}"
    )


def top_text():
    with db() as c:
        rows = c.execute("SELECT user_id,username,first_name,uid,balance FROM users ORDER BY balance DESC LIMIT 3").fetchall()
    if not rows:
        return "Топ пока пуст."
    medals = ["🥇", "🥈", "🥉"]
    out = ["🏆 <b>Топ 3 по USDT</b>\n"]
    for i, (user_id, username, first_name, uid, balance) in enumerate(rows):
        name = f"@{username}" if username else (first_name or str(user_id))
        out.append(f"{medals[i]} {html.escape(name)} | UID: <code>{html.escape(str(uid))}</code> | <b>{money(balance)}</b>")
    return "\n".join(out)


def groups_text():
    with db() as c:
        rows = c.execute("SELECT chat_id,title,username,type,last_seen FROM groups ORDER BY last_seen DESC").fetchall()
    if not rows:
        return "Бот пока не найден ни в одной группе."
    out = ["👥 <b>Группы с ботом</b>\n"]
    for chat_id, title, username, typ, last_seen in rows[:50]:
        out.append(f"• <b>{html.escape(title or 'Без названия')}</b>\n  ID: <code>{chat_id}</code>\n  Username: {html.escape('@'+username if username else 'нет')}\n")
    return "\n".join(out)


async def send_role(message, user, chat):
    save_user(user)
    save_group(chat)
    row = get_user(user.id)
    if not row:
        await message.reply_text("Ошибка профиля. Напиши /start.")
        return
    if not admin(user.id):
        left = COOLDOWN - (ts() - row[6])
        if left > 0:
            await message.reply_text(f"⏳ {mention(user)}, подожди еще {left//60} мин. {left%60} сек.", parse_mode="HTML")
            return
    phrase = random_phrase()
    if not phrase:
        await message.reply_text("В базе пока нет фраз.")
        return
    with db() as c:
        c.execute("UPDATE users SET opens=opens+1,last_role=? WHERE user_id=?", (ts(), user.id))
        c.commit()
    bid = create_bonus(user.id)
    await message.reply_text(f"🎭 {mention(user)}, ты: <b>{html.escape(phrase)}</b>", parse_mode="HTML", reply_markup=role_kb(bid))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    save_group(update.effective_chat)
    await update.message.reply_text(
        "🎭 Бот для игры «Кто я?»\n\nВ группе напиши: <b>кто я</b>, <b>кто</b> или <b>я</b>.",
        parse_mode="HTML",
        reply_markup=main_kb(admin(update.effective_user.id)),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_role(update.message, update.effective_user, update.effective_chat)


async def text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    save_group(update.effective_chat)
    if update.message and update.message.text and update.message.text.strip().lower() in TRIGGERS:
        await send_role(update.message, update.effective_user, update.effective_chat)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    save_group(update.effective_chat)
    if not admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return
    await update.message.reply_text("⚙️ Админ-меню:", reply_markup=admin_kb())


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return await update.message.reply_text("⛔ У тебя нет доступа.")
    txt = " ".join(context.args).strip()
    if not txt:
        return await update.message.reply_text("Напиши так:\n/add Гарри Поттер")
    await update.message.reply_text("✅ Фраза добавлена." if add_phrase_db(txt) else "⚠️ Такая фраза уже есть или текст пустой.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return await update.message.reply_text("⛔ У тебя нет доступа.")
    rows = last_phrases(20)
    if not rows:
        return await update.message.reply_text("Фраз пока нет.")
    await update.message.reply_text("📋 Последние фразы:\n\n" + "\n".join(f"{i}. {html.escape(t)}" for i, t in rows), parse_mode="HTML")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return await update.message.reply_text("⛔ У тебя нет доступа.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Напиши так:\n/delete 12")
    await update.message.reply_text("🗑 Удалено." if del_phrase(int(context.args[0])) else "⚠️ Фраза не найдена.")


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    await update.message.reply_text(profile_text(update.effective_user.id), parse_mode="HTML")


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(top_text(), parse_mode="HTML")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    save_user(user)
    if q.message:
        save_group(q.message.chat)
    data = q.data

    if data.startswith("bonus:"):
        msg = claim_bonus(data.split(":", 1)[1], user.id)
        return await q.answer(msg, show_alert=True)

    if data.startswith("wd_ok:") or data.startswith("wd_no:"):
        if not admin(user.id):
            return await q.answer("Нет доступа.", show_alert=True)
        wid = int(data.split(":", 1)[1])
        row = get_wd(wid)
        if not row:
            return await q.edit_message_text("Заявка не найдена.")
        _, target, wallet, amount, status = row
        if status != "pending":
            return await q.edit_message_text("Эта заявка уже обработана.")
        if data.startswith("wd_ok:"):
            set_wd(wid, "approved", user.id)
            await q.edit_message_text(f"✅ Заявка #{wid} одобрена.\nСумма: {money(amount)}")
            try:
                await context.bot.send_message(target, f"✅ Ваша заявка на вывод {money(amount)} одобрена.")
            except Exception:
                pass
        else:
            set_wd(wid, "declined", user.id)
            add_balance(target, amount)
            await q.edit_message_text(f"❌ Заявка #{wid} отклонена.\nСумма возвращена: {money(amount)}")
            try:
                await context.bot.send_message(target, f"❌ Ваша заявка на вывод {money(amount)} отклонена. Средства возвращены на баланс.")
            except Exception:
                pass
        return

    if data == "whoami":
        return await send_role(q.message, user, q.message.chat)
    if data == "profile":
        return await q.message.reply_text(profile_text(user.id), parse_mode="HTML")
    if data == "top3":
        return await q.message.reply_text(top_text(), parse_mode="HTML")
    if data == "back":
        return await q.edit_message_text("Главное меню:", reply_markup=main_kb(admin(user.id)))
    if data == "admin":
        if not admin(user.id):
            return await q.edit_message_text("⛔ У тебя нет доступа.")
        return await q.edit_message_text("⚙️ Админ-меню:", reply_markup=admin_kb())
    if data == "last_phrases":
        rows = last_phrases(10)
        txt = "Фраз пока нет." if not rows else "📋 Последние фразы:\n\n" + "\n".join(f"{i}. {html.escape(t)}" for i, t in rows)
        return await q.edit_message_text(txt, parse_mode="HTML", reply_markup=admin_kb())
    if data == "count_phrases":
        with db() as c:
            cnt = c.execute("SELECT COUNT(*) FROM phrases").fetchone()[0]
        return await q.edit_message_text(f"🔢 В базе фраз: {cnt}", reply_markup=admin_kb())
    if data == "groups":
        return await q.message.reply_text(groups_text(), parse_mode="HTML")


async def add_phrase_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("➕ Отправь новую фразу одним сообщением. Для отмены /cancel")
    return ADD_PHRASE


async def add_phrase_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        await update.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    ok = add_phrase_db(update.message.text)
    await update.message.reply_text("✅ Фраза добавлена." if ok else "⚠️ Такая фраза уже есть или текст пустой.", reply_markup=admin_kb())
    return ConversationHandler.END


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    save_user(q.from_user)
    row = get_user(q.from_user.id)
    bal = row[4]
    if bal < MIN_WD:
        await q.message.reply_text(f"❌ Недостаточно средств для вывода.\nМинимальная сумма вывода: <b>{money(MIN_WD)}</b>\nВаш баланс: <b>{money(bal)}</b>", parse_mode="HTML")
        return ConversationHandler.END
    await q.message.reply_text("💸 Введите адрес кошелька USDT в сети TON:")
    return WD_WALLET


async def withdraw_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = update.message.text.strip()
    if len(wallet) < 10:
        await update.message.reply_text("Адрес слишком короткий. Отправь корректный адрес.")
        return WD_WALLET
    context.user_data["wallet"] = wallet
    await update.message.reply_text("Введите сумму вывода в USDT. Например: 100")
    return WD_AMOUNT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.replace(",", ".").strip()
    try:
        amount = int(round(float(raw) * 1000))
    except ValueError:
        await update.message.reply_text("Введите сумму числом.")
        return WD_AMOUNT
    if amount < MIN_WD:
        await update.message.reply_text(f"Минимальная сумма вывода: {money(MIN_WD)}")
        return WD_AMOUNT
    user = update.effective_user
    save_user(user)
    bal = get_user(user.id)[4]
    if amount > bal:
        await update.message.reply_text(f"Недостаточно средств. Ваш баланс: {money(bal)}")
        return ConversationHandler.END
    if not take_balance(user.id, amount):
        await update.message.reply_text("Недостаточно средств.")
        return ConversationHandler.END
    wallet = context.user_data["wallet"]
    wid = create_wd(user.id, wallet, amount)
    await update.message.reply_text("✅ Заявка на вывод создана и отправлена админам на проверку.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Одобрить", callback_data=f"wd_ok:{wid}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_no:{wid}")]])
    txt = f"💸 <b>Новая заявка на вывод</b>\n\nID заявки: <code>{wid}</code>\nПользователь: {mention(user)}\nTelegram ID: <code>{user.id}</code>\nСумма: <b>{money(amount)}</b>\nКошелек TON USDT:\n<code>{html.escape(wallet)}</code>"
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, txt, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    return ConversationHandler.END


async def give_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("💰 Введите Telegram ID пользователя:")
    return GIVE_USER


async def give_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return ConversationHandler.END
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("Введите ID числом.")
        return GIVE_USER
    uid = int(update.message.text.strip())
    if not get_user(uid):
        await update.message.reply_text("Пользователь не найден. Он должен сначала написать /start.")
        return ConversationHandler.END
    context.user_data["give_uid"] = uid
    await update.message.reply_text("Введите сумму USDT для выдачи:")
    return GIVE_AMOUNT


async def give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        amount = int(round(float(update.message.text.replace(",", ".")) * 1000))
    except ValueError:
        await update.message.reply_text("Введите сумму числом.")
        return GIVE_AMOUNT
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше 0.")
        return GIVE_AMOUNT
    uid = context.user_data["give_uid"]
    add_balance(uid, amount)
    await update.message.reply_text(f"✅ Пользователю <code>{uid}</code> выдано <b>{money(amount)}</b>.", parse_mode="HTML")
    try:
        await context.bot.send_message(uid, f"💰 Вам начислено <b>{money(amount)}</b>.", parse_mode="HTML")
    except Exception:
        pass
    return ConversationHandler.END


async def uid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(q.from_user.id):
        await q.message.reply_text("⛔ У тебя нет доступа.")
        return ConversationHandler.END
    await q.message.reply_text("🆔 Введите Telegram ID пользователя:")
    return UID_USER


async def uid_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return ConversationHandler.END
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("Введите ID числом.")
        return UID_USER
    uid = int(update.message.text.strip())
    if not get_user(uid):
        await update.message.reply_text("Пользователь не найден. Он должен сначала написать /start.")
        return ConversationHandler.END
    context.user_data["uid_user"] = uid
    await update.message.reply_text("Введите новый кастом UID:")
    return UID_VALUE


async def uid_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return ConversationHandler.END
    ok, msg = set_uid(context.user_data["uid_user"], update.message.text)
    await update.message.reply_text(("✅ " if ok else "⚠️ ") + msg)
    return ConversationHandler.END


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

    app.add_handler(ConversationHandler([CallbackQueryHandler(add_phrase_start, pattern="^add_phrase$")], {ADD_PHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phrase_recv)]}, [CommandHandler("cancel", cancel)]))
    app.add_handler(ConversationHandler([CallbackQueryHandler(withdraw_start, pattern="^withdraw$")], {WD_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet)], WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)]}, [CommandHandler("cancel", cancel)]))
    app.add_handler(ConversationHandler([CallbackQueryHandler(give_start, pattern="^give_usdt$")], {GIVE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, give_user)], GIVE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, give_amount)]}, [CommandHandler("cancel", cancel)]))
    app.add_handler(ConversationHandler([CallbackQueryHandler(uid_start, pattern="^custom_uid$")], {UID_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, uid_user)], UID_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, uid_value)]}, [CommandHandler("cancel", cancel)]))

    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_trigger))
    app.run_polling()


if __name__ == "__main__":
    main()
