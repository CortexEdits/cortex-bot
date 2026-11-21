import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ===================== НАСТРОЙКИ =====================

# ВАЖНО: токен только из переменных окружения (в коде его хранить нельзя).
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "Не задан BOT_TOKEN. Добавьте токен бота в переменные окружения хостинга."
    )

WORK_GROUP_ID = -1004300868991  # ID закрытой группы с темами
DEV_IDS = [
    7492826311,
    5469728559,
    7274526703,
    6055045976,
]  # Первый ID — владелец (может передавать/возвращать любые заказы)
OWNER_ID = DEV_IDS[0]

REVIEWS_URL = "https://t.me/CortexEdits"
SUPPORT_USERNAME = "@sliy100"

# Тексты, которые удобно править
MSG_NOT_TAKEN = (
    "🤨 Эй, торопыга! Сначала нажми «Взять заказ в работу», а потом пиши"
    " клиенту."
)
MSG_OTHER_DEV = "⚠️ Заказ закреплен за другим разработчиком."
PAYMENT_TEXT = "Реквизиты для оплаты исполнитель пришлёт прямо в этом чате."
PRIVATE_CHANNEL_TEXT = "🔒 Приватный канал — в скором времени будет!"

# Требовать хотя бы 1 видео/фото в заявке (False — можно отправить только текст)
REQUIRE_MEDIA = True

# Кнопки, которые видит клиент под строкой ввода при загрузке исходников
BTN_DONE = "✅ Готово — отправить заказ"
BTN_CANCEL = "✖ Отменить"

# Сколько секунд ждать, собирая дополнительные файлы клиента уже после создания заказа
FOLLOWUP_DELAY = 5.0
# Через сколько секунд после последнего файла обновлять счётчик в карточке загрузки
SESSION_COUNT_DELAY = 2.0
# Лимит Telegram на размер одного альбома
ALBUM_SIZE = 10
# Сколько прошлых заказов показывать в «Мои заказы»
MAX_HISTORY = 5

# Файл, где хранятся заказы (чтобы они не терялись при перезапуске бота)
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

TZ = timezone(timedelta(hours=3))  # время в «Моих заказах» — по Москве

TOTAL_STEPS = 5
STEP_NAMES = {
    1: "Заявка отправлена",
    2: "Заказ взят в работу",
    3: "Превью отправлено, ждём оплату",
    4: "Оплата получена",
    5: "Заказ выдан",
}
LINE = "▬▬▬▬▬▬▬▬▬▬▬▬"

VIDEO_FILES = {
    "50": "videos/example_50.mp4",
    "100": "videos/example_100.mp4",
    "150": "videos/example_150.mp4",
}

# ===================== ДАННЫЕ =====================

orders = {}  # {order_no: dict} — все заказы (активные и прошлые)
user_active = {}  # {user_id: order_no} — активный заказ клиента (один на клиента)
thread_order = {}  # {thread_id: order_no} — активные заказы по темам
counters = {"order": 0}

drafts = {}  # {user_id: {"price", "messages", "summary_id"}} — заявка собирается
dev_sessions = {}  # {thread_id: {"kind", "dev_id", "order_no", "messages", "prompt_id"}}
dev_names = {}  # {dev_id: имя} — кэш для кнопки «Передать заказ»
last_hint = {}  # когда клиенту в последний раз показывали подсказку
last_paid_ping = {}  # {order_no: monotonic} — защита от спама «Я оплатил»

router = Router()
_create_lock = None


def get_create_lock():
    global _create_lock
    if _create_lock is None:
        _create_lock = asyncio.Lock()
    return _create_lock


# ===================== СОХРАНЕНИЕ СОСТОЯНИЯ =====================


def now_ts() -> float:
    return time.time()


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")
    except Exception:
        return "—"


def rebuild_indexes():
    user_active.clear()
    thread_order.clear()
    for no, order in orders.items():
        if order["status"] == "active":
            user_active[order["user_id"]] = no
            thread_order[order["thread_id"]] = no


def save_state():
    data = {
        "counter": counters["order"],
        "orders": {str(no): order for no, order in orders.items()},
    }
    tmp_path = STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, STATE_FILE)
    except OSError:
        logging.exception("Не удалось сохранить состояние в %s", STATE_FILE)


def load_state():
    """Загружает заказы. Возвращает номера заказов, перенесённых из старого формата."""
    migrated = []
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return migrated
    except Exception:
        logging.exception("Не удалось прочитать %s", STATE_FILE)
        return migrated

    counters["order"] = int(data.get("counter", 0))
    for no, order in data.get("orders", {}).items():
        orders[int(no)] = order
        counters["order"] = max(counters["order"], int(no))

    # Перенос заказов из старой версии бота (user_to_thread / assigned_dev)
    if "orders" not in data and "user_to_thread" in data:
        assigned = data.get("assigned_dev", {})
        for user_id, thread_id in data["user_to_thread"].items():
            counters["order"] += 1
            no = counters["order"]
            dev_id = assigned.get(str(thread_id))
            orders[no] = {
                "no": no,
                "user_id": int(user_id),
                "client_name": "Клиент",
                "client_username": None,
                "thread_id": int(thread_id),
                "price": "—",
                "stage": 2 if dev_id else 1,
                "dev_id": int(dev_id) if dev_id else None,
                "dev_name": None,
                "status": "active",
                "created": now_ts(),
                "updated": now_ts(),
                "topic_msg_id": None,
                "status_msg_id": None,
            }
            migrated.append(no)

    rebuild_indexes()
    logging.info("Загружено заказов: %d", len(orders))
    return migrated


# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

esc = html.escape


def mention(user_id, name) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(str(name))}</a>'


def progress_bar(stage: int) -> str:
    return "▰" * stage + "▱" * (TOTAL_STEPS - stage)


def btn(text, cb=None, url=None, style=None):
    """Inline-кнопка. style: primary (синяя) / success (зелёная) / danger (красная)."""
    kwargs = {"text": text}
    if cb:
        kwargs["callback_data"] = cb
    if url:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


def kbtn(text, style=None):
    """Кнопка обычной клавиатуры (под строкой ввода)."""
    kwargs = {"text": text}
    if style:
        kwargs["style"] = style
    try:
        return KeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return KeyboardButton(**kwargs)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def tg_call(method, *args, **kwargs):
    """Вызов Telegram API с повтором при flood control и сетевых сбоях."""
    for attempt in range(1, 6):
        try:
            return await method(*args, **kwargs)
        except TelegramRetryAfter as e:
            logging.warning("Flood control, жду %s сек.", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 5:
                raise
            await asyncio.sleep(2 * attempt)
    return await method(*args, **kwargs)


class Batcher:
    """Копит сообщения и отдаёт их одной пачкой после паузы.

    Каждое новое сообщение сбрасывает таймер. Пачки одного ключа
    обрабатываются строго по очереди (через Lock), поэтому ничего не
    перемешивается и не теряется.
    """

    def __init__(self, flush_cb):
        self.flush_cb = flush_cb
        self.buffers = {}
        self.timers = {}
        self.locks = {}

    def busy(self, key) -> bool:
        lock = self.locks.get(key)
        return key in self.buffers or bool(lock and lock.locked())

    def add(self, key, message, delay: float, bot):
        self.buffers.setdefault(key, []).append(message)
        old = self.timers.pop(key, None)
        if old:
            old.cancel()
        self.timers[key] = asyncio.create_task(self._run(key, delay, bot))

    def drop(self, key):
        self.buffers.pop(key, None)
        task = self.timers.pop(key, None)
        if task:
            task.cancel()

    async def _run(self, key, delay: float, bot):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # С этого момента таймер уже не отменяется новыми сообщениями —
        # они попадут в новый буфер и уйдут следующей пачкой.
        if self.timers.get(key) is asyncio.current_task():
            self.timers.pop(key, None)

        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            messages = self.buffers.pop(key, [])
            if not messages:
                return
            try:
                await self.flush_cb(bot, key, messages)
            except Exception:
                logging.exception("Ошибка при отправке пачки (key=%s)", key)


async def send_album(bot: Bot, chat_id: int, thread_id, items: list):
    """Отправляет альбом. Telegram не принимает альбом из 1 файла — шлём обычным сообщением."""
    extra = {"message_thread_id": thread_id} if thread_id else {}

    if len(items) == 1:
        item = items[0]
        if isinstance(item, InputMediaPhoto):
            await tg_call(bot.send_photo, chat_id=chat_id, photo=item.media, **extra)
        elif isinstance(item, InputMediaVideo):
            await tg_call(bot.send_video, chat_id=chat_id, video=item.media, **extra)
        else:
            await tg_call(
                bot.send_document, chat_id=chat_id, document=item.media, **extra
            )
        return

    await tg_call(bot.send_media_group, chat_id=chat_id, media=items, **extra)


async def send_batch(
    bot: Bot,
    chat_id: int,
    messages: list,
    thread_id=None,
    captions_title: str = "",
):
    """Отправляет собранные сообщения: фото/видео — альбомами по 10, документы —
    отдельными альбомами, всё остальное (текст, голосовые, кружки) — копированием.
    """
    visual = []  # фото и видео (можно в одном альбоме)
    docs = []  # файлы-документы (только в своём альбоме)
    others = []  # текст, голосовые, стикеры и т.д.
    captions = []  # подписи к медиа (в альбоме они бы потерялись)

    for m in messages:
        if m.photo:
            visual.append(InputMediaPhoto(media=m.photo[-1].file_id))
        elif m.video:
            visual.append(InputMediaVideo(media=m.video.file_id))
        elif m.document and not m.animation:
            docs.append(InputMediaDocument(media=m.document.file_id))
        else:
            others.append(m)
            continue
        if m.caption:
            captions.append(m.caption)

    extra = {"message_thread_id": thread_id} if thread_id else {}
    failed = 0

    for group in (visual, docs):
        for i in range(0, len(group), ALBUM_SIZE):
            chunk = group[i : i + ALBUM_SIZE]
            try:
                await send_album(bot, chat_id, thread_id, chunk)
            except Exception:
                failed += len(chunk)
                logging.exception(
                    "Не удалось отправить альбом из %d файлов", len(chunk)
                )
            await asyncio.sleep(0.5)

    if captions:
        text = "\n\n".join(captions)
        if captions_title:
            text = f"{captions_title}\n{text}"
        try:
            await tg_call(bot.send_message, chat_id=chat_id, text=text[:4096], **extra)
        except Exception:
            logging.exception("Не удалось отправить подписи к файлам")

    for m in others:
        try:
            await tg_call(m.copy_to, chat_id=chat_id, **extra)
        except Exception:
            failed += 1
            logging.exception("Не удалось скопировать сообщение %s", m.message_id)

    if failed:
        try:
            await tg_call(
                bot.send_message,
                chat_id=chat_id,
                text=f"⚠️ Не удалось доставить файлов: {failed}.",
                **extra,
            )
        except Exception:
            logging.exception("Не удалось отправить предупреждение об ошибке")


def is_file_message(m) -> bool:
    return bool(m.photo or m.video or m.document)


def is_uploadable(m) -> bool:
    """Что считается файлом при загрузке превью/готового заказа разработчиком."""
    return bool(m.photo or m.video or m.document or m.animation)


async def get_dev_name(bot: Bot, dev_id: int) -> str:
    if dev_id in dev_names:
        return dev_names[dev_id]
    try:
        member = await bot.get_chat_member(WORK_GROUP_ID, dev_id)
        name = member.user.full_name
    except Exception:
        name = f"ID {dev_id}"
    dev_names[dev_id] = name
    return name


# ===================== ТЕКСТЫ И КЛАВИАТУРЫ: КЛИЕНТ =====================


def main_menu_text(user_id: int) -> str:
    text = (
        "🖤 <b>CORTEX EDITS</b>\n"
        f"{LINE}\n\n"
        "Делаем стильные эдиты для твоей техники:\n"
        "🚲 Велосипеды, BMX и МТБ\n"
        "🛴 Самокаты\n"
        "🏍 Мотоциклы и питбайки\n\n"
        "⬛ Плавные переходы, классная музыка, тёмная эстетика и сочный вайб"
        " без лишнего пафоса.\n\n"
        "Выбирай раздел ниже 👇"
    )
    order_no = user_active.get(user_id)
    if order_no:
        order = orders[order_no]
        text += (
            f"\n\n{LINE}\n"
            f"📦 <b>Активный заказ №{order['no']}</b>\n"
            f"{progress_bar(order['stage'])} шаг {order['stage']} из"
            f" {TOTAL_STEPS} · {STEP_NAMES[order['stage']]}"
        )
    return text


def main_menu_kb(user_id: int):
    orders_label = "📦 Мои заказы"
    if user_id in user_active:
        orders_label += " • 1"
    return kb([
        [btn("🎬 Заказать эдит", "menu_order", style="primary")],
        [
            btn(orders_label, "menu_orders"),
            btn("🛠 Поддержка", "menu_profile"),
        ],
        [btn("⭐ Отзывы и работы", url=REVIEWS_URL)],
        [btn("🔒 Приватный канал", "menu_private")],
    ])


def prices_kb():
    return kb([
        [btn("⚡ Эдит за 50 ₽", "price_50")],
        [btn("🔥 Эдит за 100 ₽", "price_100")],
        [btn("💎 Эдит за 150 ₽", "price_150")],
        [btn("🏠 Главное меню", "menu_home")],
    ])


def back_to_menu_kb():
    return kb([[btn("🏠 Главное меню", "menu_home")]])


def upload_reply_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[kbtn(BTN_DONE, "success")], [kbtn(BTN_CANCEL, "danger")]],
        resize_keyboard=True,
        input_field_placeholder="Кидай видео, фото и пожелания…",
    )


def build_client_text(order) -> str:
    stage = order["stage"]
    descriptions = {
        1: (
            "Команда уже видит твою заявку 📨\nСкоро кто-то возьмёт её в"
            " работу. Можешь писать сюда уточнения — всё долетит до"
            " исполнителя."
        ),
        2: (
            "Исполнитель уже занимается твоим эдитом 🎬\nПиши сюда любые"
            " вопросы и уточнения."
        ),
        3: (
            "Превью готово 👆\nПосмотри видео. Если всё нравится — оплати"
            " заказ, и после оплаты придёт готовый файл в полном"
            f" качестве.\n\n💳 {esc(PAYMENT_TEXT)}"
        ),
        4: (
            "Оплата получена ✅\nГотовим файл в полном качестве — он придёт"
            " сюда."
        ),
        5: (
            "Заказ выдан 🎉 Готовый эдит выше 👆\nСпасибо, что выбрал"
            " CortexEdits 🖤"
        ),
    }
    return (
        f"{progress_bar(stage)}  <b>Шаг {stage} из {TOTAL_STEPS}</b> ·"
        f" {STEP_NAMES[stage]}\n"
        f"{LINE}\n\n"
        f"<b>Заказ №{order['no']}</b> · {esc(str(order['price']))} ₽\n\n"
        f"{descriptions[stage]}"
    )


def client_status_kb(order):
    no, stage = order["no"], order["stage"]
    rows = []
    if order["status"] == "active" and stage == 3:
        rows.append([btn("💳 Я оплатил(а)", f"c_paid_{no}", style="success")])
    if order["status"] == "active" and stage <= 3:
        rows.append([btn("❌ Отменить заказ", f"c_cancel_{no}", style="danger")])
    if stage == 5:
        rows.append([btn("⭐ Отзывы и работы", url=REVIEWS_URL)])
        rows.append([btn("🎬 Заказать ещё", "menu_order", style="primary")])
    rows.append([
        btn("📦 Мои заказы", "menu_orders"),
        btn("🏠 Главное меню", "menu_home"),
    ])
    return kb(rows)


def orders_screen(user_id: int):
    lines = [f"📦 <b>Мои заказы</b>\n{LINE}\n"]
    active_order = orders.get(user_active.get(user_id))
    rows = []

    if active_order:
        o = active_order
        lines.append(
            f"🟢 <b>Активный заказ №{o['no']}</b> · {esc(str(o['price']))} ₽\n"
            f"{progress_bar(o['stage'])} шаг {o['stage']} из {TOTAL_STEPS} ·"
            f" {STEP_NAMES[o['stage']]}\n"
            f"🕒 Создан: {fmt_ts(o['created'])}"
        )
        if o["stage"] <= 3:
            rows.append([btn("❌ Отменить заказ", f"c_cancel_{o['no']}", style="danger")])
    else:
        lines.append("Активных заказов нет.")
        rows.append([btn("🎬 Заказать эдит", "menu_order", style="primary")])

    history = sorted(
        (
            o
            for o in orders.values()
            if o["user_id"] == user_id and o["status"] != "active"
        ),
        key=lambda o: o["created"],
        reverse=True,
    )[:MAX_HISTORY]

    lines.append(f"\n🗂 <b>История</b>")
    if history:
        for o in history:
            mark = "✅ выдан" if o["status"] == "done" else "✖ отменён"
            lines.append(
                f"▪️ №{o['no']} · {esc(str(o['price']))} ₽ · {mark} ·"
                f" {fmt_ts(o['updated'])}"
            )
    else:
        lines.append("Пока пусто — самое время сделать первый эдит 🖤")

    rows.append([btn("🏠 Главное меню", "menu_home")])
    return "\n".join(lines), kb(rows)


def is_status_message(user_id: int, message_id: int) -> bool:
    """Это сообщение — статус заказа (шаги 1–5)? Такие сообщения не удаляем."""
    return any(
        o["user_id"] == user_id and o.get("status_msg_id") == message_id
        for o in orders.values()
    )


async def leave_screen(callback: CallbackQuery):
    """Закрывает экран, с которого ушёл клиент. Сообщение со статусом заказа
    не удаляется — из него только убирается нажатая кнопка (чтобы не плодить
    дубли экранов)."""
    msg = callback.message
    if is_status_message(callback.from_user.id, msg.message_id):
        markup = getattr(msg, "reply_markup", None)
        if markup:
            rows = [
                [b for b in row if b.callback_data != callback.data]
                for row in markup.inline_keyboard
            ]
            rows = [row for row in rows if row]
            try:
                await msg.edit_reply_markup(reply_markup=kb(rows))
            except Exception:
                pass
        return
    try:
        await msg.delete()
    except Exception:
        pass


async def remove_reply_kb(bot: Bot, chat_id: int):
    """Убирает клавиатуру «Готово / Отменить» из-под строки ввода."""
    try:
        m = await bot.send_message(chat_id, "🏠", reply_markup=ReplyKeyboardRemove())
        await bot.delete_message(chat_id, m.message_id)
    except Exception:
        pass


async def show_main_menu(bot: Bot, chat_id: int):
    # В личке chat_id совпадает с id пользователя
    text = main_menu_text(chat_id)
    markup = main_menu_kb(chat_id)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile("menu.jpg"),
            caption=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup, parse_mode="HTML"
        )


# ===================== КЛИЕНТ: МЕНЮ И ОФОРМЛЕНИЕ =====================


@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot):
    if drafts.pop(message.chat.id, None) is not None:
        await remove_reply_kb(bot, message.chat.id)
    await show_main_menu(bot, message.chat.id)


@router.callback_query(F.data == "menu_home")
async def cb_menu_home(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await show_main_menu(bot, callback.message.chat.id)
    await leave_screen(callback)


@router.callback_query(F.data == "menu_private")
async def cb_menu_private(callback: CallbackQuery):
    await callback.answer(PRIVATE_CHANNEL_TEXT, show_alert=True)


@router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    text = (
        f"🛠 <b>Поддержка</b>\n{LINE}\n\n"
        "Обращайтесь по идеям, вопросам и сообщайте нам об ошибках:"
        f" {esc(SUPPORT_USERNAME)}"
    )
    await bot.send_message(
        callback.from_user.id, text, reply_markup=back_to_menu_kb(), parse_mode="HTML"
    )
    await leave_screen(callback)


@router.callback_query(F.data == "menu_orders")
async def cb_menu_orders(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    text, markup = orders_screen(callback.from_user.id)
    await bot.send_message(
        callback.from_user.id, text, reply_markup=markup, parse_mode="HTML"
    )
    await leave_screen(callback)


async def show_has_active_order(bot: Bot, user_id: int):
    order = orders[user_active[user_id]]
    await bot.send_message(
        user_id,
        f"📦 <b>У тебя уже есть активный заказ №{order['no']}</b>\nНовый можно"
        " оформить после того, как он будет выдан или отменён.",
        reply_markup=kb([
            [btn("📦 Мои заказы", "menu_orders", style="primary")],
            [btn("🏠 Главное меню", "menu_home")],
        ]),
        parse_mode="HTML",
    )


# Выбор тарифа
@router.callback_query(F.data == "menu_order")
async def cb_menu_order(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id

    if user_id in user_active:
        await show_has_active_order(bot, user_id)
        await leave_screen(callback)
        return

    caption = (
        f"💰 <b>Выбери ценовую категорию эдита</b>\n{LINE}\n\nНажми на нужный"
        " вариант ниже 👇"
    )
    try:
        await bot.send_photo(
            chat_id=user_id,
            photo=FSInputFile("prices_banner.jpg"),
            caption=caption,
            reply_markup=prices_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await bot.send_message(
            chat_id=user_id,
            text=caption,
            reply_markup=prices_kb(),
            parse_mode="HTML",
        )
    await leave_screen(callback)


def tariff_kb(price: str):
    return kb([
        [btn("📥 Скинуть исходники", f"src_{price}", style="success")],
        [btn("🎥 Как правильно снимать", f"how_{price}", style="primary")],
        [btn("⬅️ Назад", "menu_order")],
    ])


# Экран тарифа
@router.callback_query(F.data.startswith("price_"))
async def cb_select_price(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    price = callback.data.split("_")[1]
    text = (
        f"🎬 <b>Эдит за {esc(price)} ₽</b>\n{LINE}\n\n"
        "<blockquote>1. Скидываешь исходники\n"
        "2. Мы делаем эдит и показываем превью\n"
        "3. Оплачиваешь — и получаешь готовый файл</blockquote>\n\n"
        "Не знаешь, как лучше снять? Глянь примеры 👇"
    )
    await bot.send_message(
        callback.from_user.id, text, reply_markup=tariff_kb(price), parse_mode="HTML"
    )
    await leave_screen(callback)


# Как правильно снимать — с видео-примером
@router.callback_query(F.data.startswith("how_"))
async def cb_how_to_shoot(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    price = callback.data.split("_")[1]
    caption = (
        f"🎥 <b>Как правильно снимать · {esc(price)} ₽</b>\n{LINE}\n\n"
        "⬛ Пример выше 👆\n\n"
        "<b>Требования к материалам:</b>\n"
        "▪️ Ракурсы как на примере\n"
        "▪️ 15 кадров по 4 секунды\n"
        "▪️ Видео лучше слать файлом — так не потеряется качество"
    )
    markup = kb([
        [btn("📥 Скинуть исходники", f"src_{price}", style="success")],
        [btn("⬅️ Назад", f"price_{price}")],
    ])
    video_path = VIDEO_FILES.get(price)
    try:
        await bot.send_video(
            chat_id=callback.from_user.id,
            video=FSInputFile(video_path),
            caption=caption,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        await bot.send_message(
            callback.from_user.id,
            f"{caption}\n\n⚠️ <i>(Видео-пример временно недоступен)</i>",
            reply_markup=markup,
            parse_mode="HTML",
        )
    await leave_screen(callback)


# Начало загрузки исходников
@router.callback_query(F.data.startswith("src_"))
async def cb_start_upload(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    price = callback.data.split("_")[1]

    if user_id in user_active:
        await show_has_active_order(bot, user_id)
        await leave_screen(callback)
        return

    draft = drafts.get(user_id)
    if draft:
        draft["price"] = price  # уже загруженное не теряем
    else:
        drafts[user_id] = {"price": price, "messages": [], "summary_id": None}

    text = (
        f"📥 <b>Скидывай исходники</b>\n{LINE}\n\n"
        "Присылай видео, фото и музыку. Пожелания к монтажу напиши"
        " сообщением вместе с исходниками.\n"
        "Когда всё отправишь — нажми «✅ Готово», проверь сводку и подтверди"
        " отправку.\n\n"
        "💡 Видео лучше отправлять файлом, чтобы не потерялось качество."
    )
    await bot.send_message(
        user_id, text, reply_markup=upload_reply_kb(), parse_mode="HTML"
    )
    await leave_screen(callback)


def analyze_draft(messages):
    media = texts = 0
    wishes = []
    for m in messages:
        if (
            m.photo
            or m.video
            or m.document
            or m.audio
            or m.voice
            or m.video_note
            or m.animation
        ):
            media += 1
        if m.text:
            texts += 1
            wishes.append(m.text)
        elif m.caption:
            wishes.append(m.caption)
    return media, texts, wishes


def format_wishes(wishes) -> str:
    if not wishes:
        return " ▪️ Без особых пожеланий"
    out = []
    for w in wishes[:6]:
        w = " ".join(w.split())
        if len(w) > 200:
            w = w[:200] + "…"
        out.append(f" ▪️ {esc(w)}")
    if len(wishes) > 6:
        out.append(f" … и ещё {len(wishes) - 6}")
    return "\n".join(out)


# Кнопка «Готово» (из клавиатуры под строкой ввода)
@router.message(F.chat.type == "private", F.text == BTN_DONE)
async def draft_done(message: Message, bot: Bot):
    user_id = message.from_user.id
    draft = drafts.get(user_id)

    if not draft:
        await remove_reply_kb(bot, user_id)
        await message.answer(
            "Не вижу активной заявки 🤔 Нажми «Главное меню» → «Заказать эдит».",
            reply_markup=back_to_menu_kb(),
        )
        return

    messages = draft["messages"]
    if not messages:
        await message.answer(
            "🫥 Пока пусто — я ничего не получил.\nСкинь видео или фото, а"
            f" потом снова жми «{BTN_DONE}»."
        )
        return

    media_count, text_count, wishes = analyze_draft(messages)
    if REQUIRE_MEDIA and media_count == 0:
        await message.answer(
            "📝 Текст получил, но исходников пока нет.\nДобавь хотя бы одно"
            " видео или фото и жми «Готово» ещё раз."
        )
        return

    if draft.get("summary_id"):
        try:
            await bot.delete_message(user_id, draft["summary_id"])
        except Exception:
            pass

    text = (
        f"🔎 <b>Проверь заявку перед отправкой</b>\n{LINE}\n\n"
        f"🎞 Видео, фото и файлов: <b>{media_count}</b>\n"
        f"💬 Текстовых сообщений: <b>{text_count}</b>\n"
        f"💰 Тариф: <b>{esc(str(draft['price']))} ₽</b>\n\n"
        f"📝 <b>Твои пожелания:</b>\n{format_wishes(wishes)}\n\n"
        "Чего-то не хватает? Просто пришли ещё и нажми «Готово» снова — я"
        " обновлю сводку."
    )
    sent = await message.answer(
        text,
        reply_markup=kb([[btn("🚀 Отправить заказ", "draft_send", style="success")]]),
        parse_mode="HTML",
    )
    draft["summary_id"] = sent.message_id


# Кнопка «Отменить» (из клавиатуры под строкой ввода)
@router.message(F.chat.type == "private", F.text == BTN_CANCEL)
async def draft_cancel(message: Message, bot: Bot):
    user_id = message.from_user.id
    draft = drafts.pop(user_id, None)
    if draft and draft.get("summary_id"):
        try:
            await bot.delete_message(user_id, draft["summary_id"])
        except Exception:
            pass

    await remove_reply_kb(bot, user_id)
    await bot.send_message(
        user_id,
        f"✖ <b>Заявка отменена</b>\n{LINE}\n\nНичего не отправлено. Можно"
        " вернуться в меню или выбрать тариф заново.",
        reply_markup=kb([
            [btn("🎬 Выбрать тариф", "menu_order", style="primary")],
            [btn("🏠 Главное меню", "menu_home")],
        ]),
        parse_mode="HTML",
    )


# Пока идёт загрузка, всё, что шлёт клиент, копится в заявке
@router.message(
    F.chat.type == "private",
    lambda m: m.from_user is not None and m.from_user.id in drafts,
)
async def draft_collect(message: Message):
    drafts[message.from_user.id]["messages"].append(message)


# Подтверждение: создаём заказ и отправляем материалы
@router.callback_query(F.data == "draft_send")
async def cb_draft_send(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    draft = drafts.pop(user_id, None)
    if not draft:
        await callback.answer("Заявка уже отправлена или устарела", show_alert=True)
        return
    await callback.answer("Отправляю…")

    try:
        await callback.message.edit_text("⏳ Отправляю заказ команде…", reply_markup=None)
    except Exception:
        pass
    await remove_reply_kb(bot, user_id)

    try:
        order = await create_order(bot, callback.from_user, draft["price"])
    except Exception:
        logging.exception("Ошибка создания заказа для %s", user_id)
        drafts[user_id] = draft
        await bot.send_message(
            user_id,
            "⚠️ Не получилось отправить заказ. Ничего не потеряно — нажми"
            f" «{BTN_DONE}» ещё раз чуть позже.",
            reply_markup=upload_reply_kb(),
        )
        return

    await send_batch(
        bot,
        WORK_GROUP_ID,
        draft["messages"],
        thread_id=order["thread_id"],
        captions_title="💬 Комментарий клиента:",
    )
    await safe_push_client_status(bot, order)
    try:
        await callback.message.delete()
    except Exception:
        pass


# ===================== ЗАКАЗЫ: СОЗДАНИЕ И КАРТОЧКИ =====================


def build_topic_text(order) -> str:
    stage = order["stage"]
    uid = order["user_id"]
    username = (
        f"@{esc(order['client_username'])}"
        if order.get("client_username")
        else "без юзернейма"
    )
    lines = [
        f"🔥 <b>Заказ №{order['no']}</b> · 💰 {esc(str(order['price']))} ₽",
        f"👤 {mention(uid, order['client_name'])} ({username}) · <code>{uid}</code>",
        "",
        f"{progress_bar(stage)} <b>Шаг {stage} из {TOTAL_STEPS}</b> ·"
        f" {STEP_NAMES[stage]}",
    ]
    if order["status"] == "done":
        lines.append("✅ Заказ выдан — клиенту сообщения из этой темы больше не уходят.")
    elif order["status"] == "cancelled":
        lines.append("✖ Заказ отменён.")
    elif order["dev_id"]:
        name = order.get("dev_name") or f"ID {order['dev_id']}"
        lines.append(f"👨‍💻 Исполнитель: {mention(order['dev_id'], name)}")
    else:
        lines.append("🟢 Свободен — ждёт исполнителя.")
    return "\n".join(lines)


def topic_kb(order):
    no, stage = order["no"], order["stage"]
    if order["status"] != "active":
        return kb([[btn("🗑 Удалить тему", f"d_close_{no}", style="danger")]])

    rows = []
    if order["dev_id"] is None:
        rows.append([btn("🙋‍♂️ Взять заказ в работу", f"d_take_{no}", style="success")])
    else:
        if stage == 2:
            rows.append([btn("🎬 Отправить превью клиенту", f"d_preview_{no}", style="primary")])
        elif stage == 3:
            rows.append([btn("💰 Оплата получена", f"d_paid_{no}", style="success")])
            rows.append([btn("🎬 Отправить другое превью", f"d_preview_{no}")])
        elif stage == 4:
            rows.append([btn("📦 Отправить заказ заказчику", f"d_final_{no}", style="success")])
        rows.append([btn("🔁 Передать заказ", f"d_xfer_{no}")])
    rows.append([btn("❌ Закрыть заказ", f"d_close_{no}", style="danger")])
    return kb(rows)


async def refresh_topic_card(bot: Bot, order):
    """Обновляет карточку заказа в теме (или создаёт заново, если её нет)."""
    text = build_topic_text(order)
    markup = topic_kb(order)
    msg_id = order.get("topic_msg_id")
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=WORK_GROUP_ID,
                message_id=msg_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            logging.warning("Не удалось отредактировать карточку: %s", e)
    sent = await tg_call(
        bot.send_message,
        chat_id=WORK_GROUP_ID,
        message_thread_id=order["thread_id"],
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    order["topic_msg_id"] = sent.message_id
    save_state()


async def create_order(bot: Bot, user, price) -> dict:
    """Создаёт заказ: тема в группе + карточка с кнопкой «Взять заказ»."""
    async with get_create_lock():
        no = counters["order"] + 1
        topic_name = f"[{price}₽] Заказ №{no} | {user.full_name}"
        forum_topic = await tg_call(
            bot.create_forum_topic, chat_id=WORK_GROUP_ID, name=topic_name[:120]
        )
        thread_id = forum_topic.message_thread_id

        order = {
            "no": no,
            "user_id": user.id,
            "client_name": user.full_name or "Без имени",
            "client_username": user.username,
            "thread_id": thread_id,
            "price": price,
            "stage": 1,
            "dev_id": None,
            "dev_name": None,
            "status": "active",
            "created": now_ts(),
            "updated": now_ts(),
            "topic_msg_id": None,
            "status_msg_id": None,
        }
        try:
            await refresh_topic_card(bot, order)
        except Exception:
            # Не оставляем пустую тему без кнопки
            try:
                await bot.delete_forum_topic(
                    chat_id=WORK_GROUP_ID, message_thread_id=thread_id
                )
            except Exception:
                pass
            raise

        counters["order"] = no
        orders[no] = order
        user_active[user.id] = no
        thread_order[thread_id] = no
        save_state()
        return order


async def push_client_status(bot: Bot, order, media=None):
    """Показывает клиенту статус заказа: новое сообщение появляется, старое
    удаляется. media — список сообщений разработчика (превью / готовый заказ),
    они прикрепляются к статусу."""
    user_id = order["user_id"]
    old_id = order.get("status_msg_id")
    text = build_client_text(order)
    markup = client_status_kb(order)
    new_id = None

    if media:
        if len(media) == 1:
            # Один файл — статус идёт подписью прямо к нему
            res = await tg_call(
                media[0].copy_to,
                chat_id=user_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            new_id = res.message_id
        else:
            await send_batch(bot, user_id, media)

    if new_id is None:
        sent = await tg_call(
            bot.send_message,
            chat_id=user_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        new_id = sent.message_id

    order["status_msg_id"] = new_id
    save_state()

    if old_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_id)
        except Exception:
            pass


async def safe_push_client_status(bot: Bot, order, media=None) -> bool:
    try:
        await push_client_status(bot, order, media)
        return True
    except Exception:
        logging.exception("Не удалось обновить статус клиента (заказ №%s)", order["no"])
        try:
            await bot.send_message(
                chat_id=WORK_GROUP_ID,
                message_thread_id=order["thread_id"],
                text=(
                    "⚠️ Не получилось написать клиенту (возможно, он заблокировал"
                    " бота)."
                ),
            )
        except Exception:
            pass
        return False


def finish_order(order, status: str):
    """Закрывает заказ (done / cancelled): убирает его из активных."""
    order["status"] = status
    order["updated"] = now_ts()
    user_active.pop(order["user_id"], None)
    thread_order.pop(order["thread_id"], None)
    dev_sessions.pop(order["thread_id"], None)
    batcher.drop(order["user_id"])
    session_batcher.drop(order["thread_id"])
    save_state()


# ===================== КЛИЕНТ: СООБЩЕНИЯ ПО АКТИВНОМУ ЗАКАЗУ =====================


async def flush_client_followup(bot: Bot, user_id: int, messages: list):
    """Файлы, которые клиент досылает уже после создания заказа, уходят альбомами."""
    order = orders.get(user_active.get(user_id))
    if not order:
        return
    await send_batch(
        bot,
        WORK_GROUP_ID,
        messages,
        thread_id=order["thread_id"],
        captions_title="💬 Комментарий клиента:",
    )
    await bot.send_message(user_id, "✅ Файлы переданы команде.")


batcher = Batcher(flush_client_followup)


# Все остальные сообщения клиента в личке
@router.message(F.chat.type == "private")
async def client_message(message: Message, bot: Bot):
    user_id = message.from_user.id
    order = orders.get(user_active.get(user_id))

    if order is None:
        now = time.monotonic()
        if now - last_hint.get(user_id, 0) > 30:
            last_hint[user_id] = now
            await message.answer(
                "Не вижу активного заказа 🤔\nНажми «Главное меню» → «Заказать"
                " эдит», чтобы оформить заказ.",
                reply_markup=back_to_menu_kb(),
            )
        return

    # Обычный текст пересылаем сразу, файлы собираем в альбомы
    if not is_file_message(message) and not batcher.busy(user_id):
        try:
            await tg_call(
                message.copy_to,
                chat_id=WORK_GROUP_ID,
                message_thread_id=order["thread_id"],
            )
        except Exception:
            logging.exception("Не удалось переслать сообщение клиента %s", user_id)
        return

    batcher.add(user_id, message, FOLLOWUP_DELAY, bot)


# ===================== КЛИЕНТ: ОТМЕНА И ОПЛАТА =====================


def get_client_order(callback: CallbackQuery, no: int):
    order = orders.get(no)
    if (
        not order
        or order["user_id"] != callback.from_user.id
        or order["status"] != "active"
    ):
        return None
    return order


@router.callback_query(F.data.startswith("c_cancel_"))
async def c_cancel_ask(callback: CallbackQuery):
    order = get_client_order(callback, int(callback.data.split("_")[2]))
    if not order:
        await callback.answer("Этот заказ уже закрыт", show_alert=True)
        return
    if order["stage"] >= 4:
        await callback.answer(
            "После оплаты заказ отменить нельзя — напиши исполнителю в этом чате.",
            show_alert=True,
        )
        return
    try:
        await callback.message.edit_reply_markup(
            reply_markup=kb([[
                btn("✅ Да, отменить", f"c_cancelyes_{order['no']}", style="danger"),
                btn("↩️ Нет", f"c_cancelno_{order['no']}"),
            ]])
        )
    except Exception:
        pass
    await callback.answer("Точно отменить заказ?")


@router.callback_query(F.data.startswith("c_cancelno_"))
async def c_cancel_no(callback: CallbackQuery):
    order = get_client_order(callback, int(callback.data.split("_")[2]))
    if order:
        if is_status_message(callback.from_user.id, callback.message.message_id):
            markup = client_status_kb(order)
        else:
            _, markup = orders_screen(callback.from_user.id)
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            pass
    await callback.answer("Действие отменено.")


@router.callback_query(F.data.startswith("c_cancelyes_"))
async def c_cancel_yes(callback: CallbackQuery, bot: Bot):
    order = get_client_order(callback, int(callback.data.split("_")[2]))
    if not order:
        await callback.answer("Этот заказ уже закрыт", show_alert=True)
        return
    if order["stage"] >= 4:
        await callback.answer("После оплаты заказ отменить нельзя.", show_alert=True)
        return

    finish_order(order, "cancelled")
    await callback.answer("Заказ отменён")

    for msg_id in {order.get("status_msg_id"), callback.message.message_id}:
        if msg_id:
            try:
                await bot.delete_message(callback.from_user.id, msg_id)
            except Exception:
                pass
    await bot.send_message(
        callback.from_user.id,
        f"✖ <b>Заказ №{order['no']} отменён</b>\nСпасибо, что заглянул. Будем"
        " рады видеть тебя снова 🖤",
        reply_markup=kb([
            [btn("🎬 Заказать эдит", "menu_order", style="primary")],
            [btn("🏠 Главное меню", "menu_home")],
        ]),
        parse_mode="HTML",
    )
    try:
        await bot.delete_forum_topic(
            chat_id=WORK_GROUP_ID, message_thread_id=order["thread_id"]
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("c_paid_"))
async def c_paid(callback: CallbackQuery, bot: Bot):
    order = get_client_order(callback, int(callback.data.split("_")[2]))
    if not order or order["stage"] != 3:
        await callback.answer("Сейчас оплата не требуется", show_alert=True)
        return

    now = time.monotonic()
    if now - last_paid_ping.get(order["no"], 0) < 60:
        await callback.answer("Исполнитель уже получил сообщение — он проверит оплату.")
        return
    last_paid_ping[order["no"]] = now

    who = (
        mention(order["dev_id"], order.get("dev_name") or "исполнитель")
        if order["dev_id"]
        else "Команда"
    )
    try:
        await bot.send_message(
            chat_id=WORK_GROUP_ID,
            message_thread_id=order["thread_id"],
            text=(
                f"💳 Клиент пишет, что оплатил(а) заказ №{order['no']}."
                f" {who}, проверь поступление и нажми «💰 Оплата получена»."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Не удалось отправить уведомление об оплате")
    await callback.answer(
        "✅ Передал исполнителю — он проверит оплату и вернётся к тебе.",
        show_alert=True,
    )


# ===================== РАЗРАБОТЧИКИ: КНОПКИ ЗАКАЗА =====================


async def dev_guard(callback: CallbackQuery, no: int):
    """Проверяет, что кнопку нажал разработчик и заказ существует."""
    dev_id = callback.from_user.id
    if dev_id not in DEV_IDS:
        await callback.answer(
            f"⛔ Ошибка: вашего ID ({dev_id}) нет в списке разработчиков!",
            show_alert=True,
        )
        return None
    dev_names[dev_id] = callback.from_user.full_name
    order = orders.get(no)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return None
    return order


async def dev_guard_assignee(callback: CallbackQuery, no: int):
    """То же + нажавший должен быть исполнителем заказа."""
    order = await dev_guard(callback, no)
    if not order:
        return None
    if order["status"] != "active":
        await callback.answer("Заказ уже завершён", show_alert=True)
        return None
    if order["dev_id"] is None:
        await callback.answer("Сначала возьми заказ в работу", show_alert=True)
        return None
    if order["dev_id"] != callback.from_user.id:
        await callback.answer("⚠️ Заказ закреплён за другим разработчиком", show_alert=True)
        return None
    return order


@router.callback_query(F.data.startswith("d_take_"))
async def d_take(callback: CallbackQuery, bot: Bot):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    if order["status"] != "active":
        await callback.answer("Заказ уже завершён", show_alert=True)
        return
    if order["dev_id"] is not None:
        await callback.answer(
            "⚠️ Этот заказ уже занят другим разработчиком!", show_alert=True
        )
        return

    dev = callback.from_user
    order["dev_id"] = dev.id
    order["dev_name"] = dev.full_name
    advance = order["stage"] == 1
    if advance:
        order["stage"] = 2
    order["updated"] = now_ts()
    save_state()

    await callback.answer("Вы успешно взяли заказ!")
    await refresh_topic_card(bot, order)
    if advance:
        await safe_push_client_status(bot, order)


# --- загрузка превью / готового заказа ---


def session_text(kind: str, count: int) -> str:
    if kind == "preview":
        head = (
            "🎬 <b>Превью для клиента</b>\nПришли в эту тему видео (запись"
            " экрана или превью) — можно несколько файлов."
        )
    else:
        head = (
            "📦 <b>Выдача заказа</b>\nОплата подтверждена ✅ Пришли готовый"
            " файл (лучше документом, в полном качестве) — можно несколько"
            " файлов."
        )
    return (
        f"{head}\nКогда загрузишь — нажми «✅ Отправить клиенту».\n\n📎 Принято"
        f" файлов: <b>{count}</b>"
    )


def session_kb(no: int):
    return kb([[
        btn("✅ Отправить клиенту", f"d_send_{no}", style="success"),
        btn("✖ Отмена", f"d_sessno_{no}", style="danger"),
    ]])


async def start_dev_session(bot: Bot, order, kind: str, dev_id: int):
    thread_id = order["thread_id"]
    old = dev_sessions.pop(thread_id, None)
    if old and old.get("prompt_id"):
        try:
            await bot.delete_message(WORK_GROUP_ID, old["prompt_id"])
        except Exception:
            pass
    session_batcher.drop(thread_id)

    sent = await tg_call(
        bot.send_message,
        chat_id=WORK_GROUP_ID,
        message_thread_id=thread_id,
        text=session_text(kind, 0),
        reply_markup=session_kb(order["no"]),
        parse_mode="HTML",
    )
    dev_sessions[thread_id] = {
        "kind": kind,
        "dev_id": dev_id,
        "order_no": order["no"],
        "messages": [],
        "prompt_id": sent.message_id,
    }


async def flush_session_count(bot: Bot, thread_id: int, messages: list):
    """Обновляет счётчик принятых файлов в карточке загрузки."""
    session = dev_sessions.get(thread_id)
    if not session:
        return
    try:
        await bot.edit_message_text(
            chat_id=WORK_GROUP_ID,
            message_id=session["prompt_id"],
            text=session_text(session["kind"], len(session["messages"])),
            reply_markup=session_kb(session["order_no"]),
            parse_mode="HTML",
        )
    except Exception:
        pass


session_batcher = Batcher(flush_session_count)


@router.callback_query(F.data.startswith("d_preview_"))
async def d_preview(callback: CallbackQuery, bot: Bot):
    order = await dev_guard_assignee(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    if order["stage"] not in (2, 3):
        await callback.answer("Превью сейчас отправить нельзя", show_alert=True)
        return
    await callback.answer()
    await start_dev_session(bot, order, "preview", callback.from_user.id)


@router.callback_query(F.data.startswith("d_final_"))
async def d_final(callback: CallbackQuery, bot: Bot):
    order = await dev_guard_assignee(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    if order["stage"] != 4:
        await callback.answer(
            "Готовый заказ можно отправить только после оплаты", show_alert=True
        )
        return
    await callback.answer()
    await start_dev_session(bot, order, "final", callback.from_user.id)


@router.callback_query(F.data.startswith("d_sessno_"))
async def d_session_cancel(callback: CallbackQuery, bot: Bot):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    session = dev_sessions.get(order["thread_id"])
    if session and session["dev_id"] != callback.from_user.id:
        await callback.answer("Это не твоя загрузка", show_alert=True)
        return
    dev_sessions.pop(order["thread_id"], None)
    session_batcher.drop(order["thread_id"])
    await callback.answer("Отменено")
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("d_send_"))
async def d_send_to_client(callback: CallbackQuery, bot: Bot):
    order = await dev_guard_assignee(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    thread_id = order["thread_id"]
    session = dev_sessions.get(thread_id)
    if not session or session["dev_id"] != callback.from_user.id:
        await callback.answer("Загрузка не найдена — нажми кнопку заново", show_alert=True)
        return
    if not session["messages"]:
        await callback.answer("Сначала пришли файл в эту тему", show_alert=True)
        return

    dev_sessions.pop(thread_id, None)
    session_batcher.drop(thread_id)
    await callback.answer("Отправляю клиенту…")

    kind = session["kind"]
    prev_stage = order["stage"]
    order["stage"] = 3 if kind == "preview" else 5

    ok = await safe_push_client_status(bot, order, media=session["messages"])
    if not ok:
        order["stage"] = prev_stage
        dev_sessions[thread_id] = session
        return

    order["updated"] = now_ts()
    if kind == "final":
        finish_order(order, "done")
    save_state()

    done_text = (
        "✅ Превью отправлено клиенту. Ждём оплату."
        if kind == "preview"
        else "✅ Заказ отправлен клиенту. Заказ выполнен 🎉"
    )
    try:
        await callback.message.edit_text(done_text, reply_markup=None)
    except Exception:
        pass
    await refresh_topic_card(bot, order)


@router.callback_query(F.data.startswith("d_paid_"))
async def d_paid(callback: CallbackQuery, bot: Bot):
    order = await dev_guard_assignee(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    if order["stage"] != 3:
        await callback.answer("Сначала отправь клиенту превью", show_alert=True)
        return

    order["stage"] = 4
    order["updated"] = now_ts()
    save_state()
    await callback.answer("Оплата отмечена ✅")
    await refresh_topic_card(bot, order)
    await safe_push_client_status(bot, order)


# --- передача заказа другому разработчику ---


def can_transfer(order, dev_id: int) -> bool:
    return dev_id == order["dev_id"] or dev_id == OWNER_ID


@router.callback_query(F.data.startswith("d_xfer_"))
async def d_transfer_menu(callback: CallbackQuery, bot: Bot):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    if order["status"] != "active" or order["dev_id"] is None:
        await callback.answer("Передавать можно только взятый заказ", show_alert=True)
        return
    if not can_transfer(order, callback.from_user.id):
        await callback.answer("⚠️ Заказ закреплён за другим разработчиком", show_alert=True)
        return

    await callback.answer()
    rows = []
    for dev_id in DEV_IDS:
        if dev_id == order["dev_id"]:
            continue
        name = await get_dev_name(bot, dev_id)
        rows.append([btn(f"👨‍💻 {name}", f"d_xferto_{order['no']}_{dev_id}")])
    rows.append([btn("🔓 Вернуть в общую очередь", f"d_xferq_{order['no']}")])
    rows.append([btn("↩️ Назад", f"d_xferback_{order['no']}")])
    try:
        await callback.message.edit_reply_markup(reply_markup=kb(rows))
    except Exception:
        pass


@router.callback_query(F.data.startswith("d_xferback_"))
async def d_transfer_back(callback: CallbackQuery):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=topic_kb(order))
    except Exception:
        pass
    await callback.answer()


async def reset_dev_session(bot: Bot, order):
    """При передаче заказа незавершённая загрузка старого исполнителя сбрасывается."""
    session = dev_sessions.pop(order["thread_id"], None)
    session_batcher.drop(order["thread_id"])
    if session and session.get("prompt_id"):
        try:
            await bot.delete_message(WORK_GROUP_ID, session["prompt_id"])
        except Exception:
            pass


@router.callback_query(F.data.startswith("d_xferto_"))
async def d_transfer_to(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    order = await dev_guard(callback, int(parts[2]))
    if not order:
        return
    new_dev = int(parts[3])
    if order["status"] != "active" or order["dev_id"] is None:
        await callback.answer("Передавать можно только взятый заказ", show_alert=True)
        return
    if not can_transfer(order, callback.from_user.id):
        await callback.answer("⚠️ Заказ закреплён за другим разработчиком", show_alert=True)
        return
    if new_dev not in DEV_IDS or new_dev == order["dev_id"]:
        await callback.answer("Нельзя передать этому разработчику", show_alert=True)
        return

    old_dev = order["dev_id"]
    old_name = order.get("dev_name") or await get_dev_name(bot, old_dev)
    new_name = await get_dev_name(bot, new_dev)

    order["dev_id"] = new_dev
    order["dev_name"] = new_name
    order["updated"] = now_ts()
    save_state()

    await callback.answer("Заказ передан")
    await reset_dev_session(bot, order)
    await refresh_topic_card(bot, order)
    try:
        await bot.send_message(
            chat_id=WORK_GROUP_ID,
            message_thread_id=order["thread_id"],
            text=(
                f"🔁 Заказ №{order['no']} передан: {mention(old_dev, old_name)} →"
                f" {mention(new_dev, new_name)}"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Не удалось отправить уведомление о передаче")


@router.callback_query(F.data.startswith("d_xferq_"))
async def d_transfer_to_queue(callback: CallbackQuery, bot: Bot):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    if order["status"] != "active" or order["dev_id"] is None:
        await callback.answer("Заказ и так свободен", show_alert=True)
        return
    if not can_transfer(order, callback.from_user.id):
        await callback.answer("⚠️ Заказ закреплён за другим разработчиком", show_alert=True)
        return

    old_dev = order["dev_id"]
    old_name = order.get("dev_name") or await get_dev_name(bot, old_dev)
    order["dev_id"] = None
    order["dev_name"] = None
    regress = order["stage"] == 2  # клиент не должен думать, что над заказом кто-то работает
    if regress:
        order["stage"] = 1
    order["updated"] = now_ts()
    save_state()

    await callback.answer("Заказ возвращён в очередь")
    await reset_dev_session(bot, order)
    await refresh_topic_card(bot, order)
    try:
        await bot.send_message(
            chat_id=WORK_GROUP_ID,
            message_thread_id=order["thread_id"],
            text=(
                f"🔓 {mention(old_dev, old_name)} вернул заказ №{order['no']} в"
                " общую очередь — кто-нибудь, возьмите его."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Не удалось отправить уведомление о возврате")
    if regress:
        await safe_push_client_status(bot, order)


# --- закрытие заказа / удаление темы ---


@router.callback_query(F.data.startswith("d_close_"))
async def d_close_ask(callback: CallbackQuery):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    try:
        await callback.message.edit_reply_markup(
            reply_markup=kb([[
                btn("✅ Да, закрыть", f"d_closeyes_{order['no']}", style="danger"),
                btn("↩️ Отмена", f"d_closeno_{order['no']}"),
            ]])
        )
    except Exception:
        pass
    await callback.answer("Вы уверены?")


@router.callback_query(F.data.startswith("d_closeno_"))
async def d_close_no(callback: CallbackQuery):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=topic_kb(order))
    except Exception:
        pass
    await callback.answer("Действие отменено.")


@router.callback_query(F.data.startswith("d_closeyes_"))
async def d_close_yes(callback: CallbackQuery, bot: Bot):
    order = await dev_guard(callback, int(callback.data.split("_")[2]))
    if not order:
        return

    was_active = order["status"] == "active"
    if was_active:
        finish_order(order, "cancelled")
        status_id = order.get("status_msg_id")
        if status_id:
            try:
                await bot.delete_message(order["user_id"], status_id)
            except Exception:
                pass
        try:
            await bot.send_message(
                order["user_id"],
                "Ваш заказ был закрыт. Спасибо что обратились к нам",
                reply_markup=back_to_menu_kb(),
            )
        except Exception:
            pass

    await callback.answer("Заказ закрыт!")
    try:
        await bot.delete_forum_topic(
            chat_id=WORK_GROUP_ID, message_thread_id=order["thread_id"]
        )
    except Exception:
        logging.exception("Не удалось удалить тему заказа №%s", order["no"])


# ===================== РАЗРАБОТЧИКИ: СООБЩЕНИЯ В ТЕМЕ =====================


def find_order_by_thread(thread_id: int):
    no = thread_order.get(thread_id)
    if no:
        return orders[no]
    for order in orders.values():
        if order["thread_id"] == thread_id:
            return order
    return None


@router.message(F.chat.id == WORK_GROUP_ID, F.message_thread_id)
async def dev_to_client_chat(message: Message, bot: Bot):
    if message.from_user is None or message.from_user.is_bot:
        return

    thread_id = message.message_thread_id
    order = find_order_by_thread(thread_id)
    if not order:
        return

    if order["status"] != "active":
        await message.reply("⚠️ Заказ уже завершён — клиенту сообщения не уходят.")
        return

    dev_id = message.from_user.id

    # Идёт загрузка превью/готового заказа: файлы собираем, а не пересылаем
    session = dev_sessions.get(thread_id)
    if session and session["dev_id"] == dev_id and is_uploadable(message):
        session["messages"].append(message)
        session_batcher.add(thread_id, message, SESSION_COUNT_DELAY, bot)
        return

    assigned = order["dev_id"]
    if not assigned:
        await message.reply(MSG_NOT_TAKEN)
        return
    if assigned != dev_id:
        await message.reply(MSG_OTHER_DEV)
        return

    try:
        await tg_call(message.copy_to, order["user_id"])
    except Exception:
        logging.exception("Не удалось отправить сообщение клиенту %s", order["user_id"])
        await message.reply("⚠️ Не удалось доставить сообщение клиенту.")


# Кнопки из старых версий бота и любые устаревшие (должен быть последним!)
@router.callback_query()
async def cb_unknown(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Нажми /start", show_alert=True)


async def restore_migrated(bot: Bot, migrated: list):
    """Для заказов из старой версии бота создаём новые карточки с рабочими кнопками."""
    for no in migrated:
        order = orders[no]
        try:
            await refresh_topic_card(bot, order)
            await push_client_status(bot, order)
        except Exception:
            logging.exception("Не удалось обновить перенесённый заказ №%s", no)


async def main():
    migrated = load_state()
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    if migrated:
        await restore_migrated(bot, migrated)
    print("Бот успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
