"""Telegram-бот: меню выбора очереди, график на сегодня/завтра, уведомления."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import poe
from .poe import MAYBE, OFF, intervals
from .storage import Storage
from .texts import WELCOME, hhmm, render_day

log = logging.getLogger(__name__)

TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Kyiv")
CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "10"))
REMIND_BEFORE_MIN = int(os.environ.get("REMIND_BEFORE_MIN", "30"))
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or None

BTN_TODAY = "📅 Сьогодні"
BTN_TOMORROW = "📆 Завтра"
BTN_QUEUE = "⚙️ Моя черга"
BTN_SETTINGS = "🔔 Сповіщення"

MAIN_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_TODAY), KeyboardButton(BTN_TOMORROW)],
     [KeyboardButton(BTN_QUEUE), KeyboardButton(BTN_SETTINGS)]],
    resize_keyboard=True,
)


def now() -> datetime:
    return datetime.now(TZ)


def storage(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.application.bot_data["storage"]


# ---------- клавиатуры ----------

def queue_keyboard(current: str | None) -> InlineKeyboardMarkup:
    rows = []
    for q in range(1, 7):
        row = []
        for s in (1, 2):
            key = f"{q}.{s}"
            row.append(InlineKeyboardButton(("✅ " if key == current else "") + key,
                                            callback_data=f"q:{key}"))
        rows.append(row)
    return InlineKeyboardMarkup([rows[0] + rows[1], rows[2] + rows[3], rows[4] + rows[5]])


def settings_keyboard(notify: bool, remind: bool) -> InlineKeyboardMarkup:
    mark = lambda v: "✅" if v else "❌"  # noqa: E731
    remind_label = f"{mark(remind)} Нагадування за {REMIND_BEFORE_MIN} хв до відключення"
    rows = [[InlineKeyboardButton(f"{mark(notify)} Зміни графіка", callback_data="t:notify")]]
    if REMIND_BEFORE_MIN > 0:
        rows.append([InlineKeyboardButton(remind_label, callback_data="t:remind")])
    return InlineKeyboardMarkup(rows)


# ---------- команды и кнопки ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage(ctx).get_user(update.effective_chat.id)
    await update.message.reply_text(WELCOME, reply_markup=MAIN_MENU)
    await update.message.reply_text("Ваша черга:", reply_markup=queue_keyboard(user.queue))


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage(ctx).get_user(update.effective_chat.id)
    text = f"Зараз обрана черга <b>{user.queue}</b>." if user.queue else "Черга ще не обрана."
    await update.message.reply_text(text + "\nОберіть чергу:", parse_mode=ParseMode.HTML,
                                    reply_markup=queue_keyboard(user.queue))


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage(ctx).get_user(update.effective_chat.id)
    await update.message.reply_text("Що надсилати:", reply_markup=settings_keyboard(user.notify, user.remind))


async def show_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE, offset: int) -> None:
    st = storage(ctx)
    user = st.get_user(update.effective_chat.id)
    if not user.queue:
        await update.message.reply_text("Спочатку оберіть чергу:", reply_markup=queue_keyboard(None))
        return
    day = (now() + timedelta(days=offset)).date()
    await update.message.reply_text(render_day(st.get_schedule(day), user.queue, day),
                                    parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await show_day(update, ctx, 0)


async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await show_day(update, ctx, 1)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    st = storage(ctx)
    chat_id = q.message.chat.id
    kind, _, value = q.data.partition(":")
    if kind == "q" and value in poe.QUEUES:
        st.set_queue(chat_id, value)
        await q.answer(f"Черга {value} збережена")
        await q.edit_message_text(f"Ваша черга: <b>{value}</b>", parse_mode=ParseMode.HTML,
                                  reply_markup=queue_keyboard(value))
        day = now().date()
        await ctx.bot.send_message(chat_id, render_day(st.get_schedule(day), value, day),
                                   parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
    elif kind == "t" and value in ("notify", "remind"):
        st.toggle(chat_id, value)
        user = st.get_user(chat_id)
        await q.answer("Збережено")
        await q.edit_message_reply_markup(settings_keyboard(user.notify, user.remind))
    else:
        await q.answer()


# ---------- фоновые задачи ----------

async def safe_send(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    try:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Forbidden:
        log.info("Пользователь %s заблокировал бота — удаляю", chat_id)
        storage(ctx).delete_user(chat_id)


async def job_check(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Забирает график, сравнивает с сохранённым, рассылает изменения."""
    st = storage(ctx)
    today = now().date()
    try:
        fresh = await poe.fetch_schedules(today)
    except Exception as e:  # сеть, 5xx, гео-блок и т.п.
        log.warning("Не удалось получить график: %r", e)
        await report_failure(ctx, repr(e))
        return
    if not fresh:
        log.warning("Ответ сайта разобран, но графиков в нём не найдено")
        await report_failure(ctx, "в ответе сайта не найдено графиков (сменилась разметка?)")
        return
    if st.get_meta("failing"):
        st.set_meta("failing", "")
        if ADMIN_CHAT_ID:
            await safe_send(ctx, int(ADMIN_CHAT_ID), "✅ Сайт обленерго знову відповідає.")

    users = st.users_with_queue()
    for sched in fresh:
        if sched.day < today:
            continue
        old = st.get_schedule(sched.day)
        st.save_schedule(sched)
        for user in users:
            if not user.notify:
                continue
            if old is not None and old.fingerprint(user.queue) == sched.fingerprint(user.queue):
                continue
            title = "🆕 Опубліковано графік" if old is None else "⚠️ Графік змінився"
            await safe_send(ctx, user.chat_id, f"{title}\n\n" + render_day(sched, user.queue, sched.day))
    st.cleanup(today - timedelta(days=2))


async def report_failure(ctx: ContextTypes.DEFAULT_TYPE, reason: str) -> None:
    st = storage(ctx)
    if ADMIN_CHAT_ID and not st.get_meta("failing"):
        await safe_send(ctx, int(ADMIN_CHAT_ID), f"❗️ Не вдається отримати графік з poe.pl.ua: {reason}")
    st.set_meta("failing", "1")


async def job_remind(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в минуту: предупреждает о ближайшем отключении."""
    if REMIND_BEFORE_MIN <= 0:
        return
    st = storage(ctx)
    t = now()
    horizon = t + timedelta(minutes=REMIND_BEFORE_MIN)
    for user in st.users_with_queue():
        if not user.remind:
            continue
        for day in {t.date(), horizon.date()}:
            sched = st.get_schedule(day)
            slots = sched.slots(user.queue) if sched else None
            if not slots:
                continue
            midnight = datetime.combine(day, datetime.min.time(), TZ)
            # Отключение, тянущееся с прошлых суток, не считается новым.
            prev = st.get_schedule(day - timedelta(days=1))
            prev_slots = prev.slots(user.queue) if prev else None
            prev_state = prev_slots[-1] if prev_slots else None
            for iv in intervals(slots):
                start = midnight + timedelta(minutes=iv.start_min)
                starts_outage = iv.state in (OFF, MAYBE) and prev_state not in (OFF, MAYBE)
                prev_state = iv.state
                if not starts_outage or not (t < start <= horizon):
                    continue
                if not st.mark_reminded(user.chat_id, day, iv.start_min):
                    continue
                what = "відключення" if iv.state == OFF else "можливе відключення"
                mins = int((start - t).total_seconds() // 60) + 1
                await safe_send(
                    ctx, user.chat_id,
                    f"⏰ Через ~{mins} хв ({hhmm(iv.start_min)}–{hhmm(iv.end_min)}) — {what}, черга {user.queue}.",
                )


def build_app(token: str, db_path: str) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["storage"] = Storage(db_path)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(MessageHandler(filters.Text([BTN_TODAY]), cmd_today))
    app.add_handler(MessageHandler(filters.Text([BTN_TOMORROW]), cmd_tomorrow))
    app.add_handler(MessageHandler(filters.Text([BTN_QUEUE]), cmd_queue))
    app.add_handler(MessageHandler(filters.Text([BTN_SETTINGS]), cmd_settings))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_repeating(job_check, interval=CHECK_INTERVAL_MIN * 60, first=5)
    app.job_queue.run_repeating(job_remind, interval=60, first=30)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    app = build_app(token, os.environ.get("DB_PATH", "data/bot.sqlite3"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
