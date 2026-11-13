import asyncio
import html
import json
import logging
import os
import time

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
    Message,
)

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
]  # Твой ID, ID друга и ID третьего разработчика

# Сколько секунд тишины ждать, прежде чем отправить ПЕРВУЮ пачку материалов
FIRST_BATCH_DELAY = 60.0
# Сколько секунд ждать, собирая дополнительные файлы уже после создания заказа
FOLLOWUP_DELAY = 5.0
# Лимит Telegram на размер одного альбома
ALBUM_SIZE = 10

# Файл, где хранятся открытые заказы (чтобы они не терялись при перезапуске бота)
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

STATUS_FREE = "🟢 Свободен, ждет исполнителя."

user_to_thread = {}  # {user_id: thread_id}
thread_to_user = {}  # {thread_id: user_id}
assigned_dev = {}  # {thread_id: dev_id}

# Словарь для хранения ID текущего интерактивного сообщения у клиента: {user_id: message_id}
user_confirm_msg_id = {}

# Клиенты, которые нажали «Оформить заказ» и сейчас присылают материалы: {user_id: тариф}
pending_price = {}

# Когда клиенту в последний раз показывали подсказку (чтобы не спамить)
last_hint = {}

router = Router()


# ===================== СОХРАНЕНИЕ СОСТОЯНИЯ =====================


def save_state():
  data = {
      "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
      "assigned_dev": {str(k): v for k, v in assigned_dev.items()},
  }
  tmp_path = STATE_FILE + ".tmp"
  try:
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(data, f)
    os.replace(tmp_path, STATE_FILE)
  except OSError:
    logging.exception("Не удалось сохранить состояние в %s", STATE_FILE)


def load_state():
  try:
    with open(STATE_FILE, encoding="utf-8") as f:
      data = json.load(f)
  except FileNotFoundError:
    return
  except Exception:
    logging.exception("Не удалось прочитать %s", STATE_FILE)
    return

  for user_id, thread_id in data.get("user_to_thread", {}).items():
    user_to_thread[int(user_id)] = int(thread_id)
    thread_to_user[int(thread_id)] = int(user_id)
  for thread_id, dev_id in data.get("assigned_dev", {}).items():
    assigned_dev[int(thread_id)] = int(dev_id)
  logging.info("Загружено открытых заказов: %d", len(user_to_thread))


# ===================== ВСПОМОГАТЕЛЬНОЕ =====================


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
  """Копит сообщения пользователя и отдаёт их одной пачкой после паузы.

  Каждое новое сообщение сбрасывает таймер. Пачки одного пользователя
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

  def has_buffer(self, key) -> bool:
    return key in self.buffers

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


async def send_album(
    bot: Bot, chat_id: int, thread_id, items: list
):
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
  """Отправляет собранные сообщения: фото/видео — альбомами по 10, документы — отдельными
  альбомами, всё остальное (текст, голосовые, кружки и т.д.) — копированием.
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
        logging.exception("Не удалось отправить альбом из %d файлов", len(chunk))
      await asyncio.sleep(0.5)

  if captions:
    text = "\n\n".join(captions)
    if captions_title:
      text = f"{captions_title}\n{text}"
    try:
      await tg_call(
          bot.send_message, chat_id=chat_id, text=text[:4096], **extra
      )
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
          text=f"⚠️ Не удалось доставить файлов: {failed}. Попросите клиента отправить их ещё раз.",
          **extra,
      )
    except Exception:
      logging.exception("Не удалось отправить предупреждение об ошибке")


# ===================== КЛАВИАТУРЫ =====================


# Клавиатура главного меню
def get_main_menu_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🎬 Заказать эдит", callback_data="menu_order"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🛠 Поддержка", callback_data="menu_profile"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⭐ Отзывы и работы", url="https://t.me/CortexEdits"
              )
          ],
      ]
  )


# Клавиатура выбора тарифов
def get_prices_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⚡ Эдит за 50 ₽", callback_data="price_50"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔥 Эдит за 100 ₽", callback_data="price_100"
              )
          ],
          [
              InlineKeyboardButton(
                  text="💎 Эдит за 150 ₽", callback_data="price_150"
              )
          ],
          [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")],
      ]
  )


# Клавиатура возврата в меню
def get_back_to_menu_kb():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")]
      ]
  )


# Клавиатура для клиента
def get_client_order_kb(thread_id: int):
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="❌ Закрыть и отменить заказ",
                  callback_data=f"ask_close_{thread_id}",
              )
          ],
          [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")],
      ]
  )


# Клавиатура подтверждения закрытия заказа (Да / Отмена)
def get_confirm_close_kb(thread_id: int):
  return InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="✅ Да, закрыть", callback_data=f"confirm_close_{thread_id}"
          ),
          InlineKeyboardButton(
              text="↩️ Отмена", callback_data=f"cancel_close_{thread_id}"
          ),
      ]]
  )


# Функция отправки главного меню (с баннером)
async def show_main_menu(bot: Bot, chat_id: int):
  # В личке chat_id совпадает с id пользователя.
  # Если клиент ещё ничего не загрузил — отменяем ожидание материалов.
  # Если файлы уже загружены, заказ всё равно будет создан.
  if not batcher.busy(chat_id):
    pending_price.pop(chat_id, None)

  caption_text = (
      "🖤 **CortexEdits** — делаем стильные и красивые эдиты для твоей"
      " техники.\n\nПодчеркнем каждую детальку твоего железа:\n🚲"
      " **Велосипеды, BMX и МТБ**\n🛴 **Самокаты**\n🏍 **Мотоциклы и"
      " питбайки**\n\nПлавные переходы, классная музыка, темная эстетика и"
      " сочный вайб без лишнего пафоса. Выбирай раздел ниже 👇"
  )

  try:
    await bot.send_photo(
        chat_id=chat_id,
        photo=FSInputFile("menu.jpg"),
        caption=caption_text,
        reply_markup=get_main_menu_kb(),
        parse_mode="Markdown",
    )
  except Exception:
    await bot.send_message(
        chat_id=chat_id,
        text=caption_text,
        reply_markup=get_main_menu_kb(),
        parse_mode="Markdown",
    )


# 1. Команда /start
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot):
  await show_main_menu(bot, message.chat.id)


def is_order_message(message) -> bool:
  """Сообщение о заказе у клиента («ТЗ отправлено», «заказ взят в работу») —
  у него есть кнопка закрытия заказа."""
  markup = getattr(message, "reply_markup", None)
  if not markup:
    return False
  return any(
      (button.callback_data or "").startswith("ask_close_")
      for row in markup.inline_keyboard
      for button in row
  )


# Возврат в главное меню
@router.callback_query(F.data == "menu_home")
async def cb_menu_home(callback: CallbackQuery, bot: Bot):
  await callback.answer()
  await show_main_menu(bot, callback.message.chat.id)

  if is_order_message(callback.message):
    # Сообщение о заказе НЕ удаляем. Убираем из него только кнопку
    # «Главное меню», чтобы повторными нажатиями не плодить новые меню.
    # Кнопка «Закрыть и отменить заказ» остаётся.
    rows = [
        [b for b in row if b.callback_data != "menu_home"]
        for row in callback.message.reply_markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    try:
      await callback.message.edit_reply_markup(
          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
      )
    except Exception:
      pass
    return

  try:
    await callback.message.delete()
  except Exception:
    pass


# 2. Меню выбора тарифов
@router.callback_query(F.data == "menu_order")
async def cb_menu_order(callback: CallbackQuery, bot: Bot):
  await callback.answer()

  caption_text = (
      "💰 **Выберите ценовую категорию эдита:**\n\nНажмите на нужный вариант"
      " ниже 👇"
  )
  banner_path = "prices_banner.jpg"

  try:
    await callback.message.answer_photo(
        photo=FSInputFile(banner_path),
        caption=caption_text,
        reply_markup=get_prices_kb(),
        parse_mode="Markdown",
    )
  except Exception:
    await callback.message.answer(
        f"{caption_text}\n\n⚠️ *(Баннер временно недоступен)*",
        reply_markup=get_prices_kb(),
        parse_mode="Markdown",
    )

  try:
    await callback.message.delete()
  except Exception:
    pass


# 3. Обработка конкретного тарифа (50, 100, 150 руб) с прикреплением видео-примера
@router.callback_query(F.data.startswith("price_"))
async def cb_select_price(callback: CallbackQuery, bot: Bot):
  await callback.answer()

  price = callback.data.split("_")[1]

  common_desc = (
      f"🖤 **Тариф: Эдит за {price} ₽**\n\n"
      "⬛ ⬆️ **Пример выше!**\n\n"
      "📌 **Требования к материалам:**\n"
      "• Ракурсы как на видео\n"
      "• 15 кадров по 4 секунды\n\n"
      "💳 **Порядок оформления:**\n"
      "1. Загрузите ваши видео/фото.\n"
      "2. Мы отправляем запись экрана что видео готово\n"
      "3. Вы оплачиваете заказ\n"
      "4. Мы отправляем вам ваше видео!!!"
  )

  order_kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="📝 Оформить заказ с этим прайсом",
                  callback_data=f"confirm_price_{price}",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к тарифам", callback_data="menu_order"
              )
          ],
          [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")],
      ]
  )

  video_files = {
      "50": "videos/example_50.mp4",
      "100": "videos/example_100.mp4",
      "150": "videos/example_150.mp4",
  }

  video_path = video_files.get(price)

  try:
    await callback.message.answer_video(
        video=FSInputFile(video_path),
        caption=common_desc,
        reply_markup=order_kb,
        parse_mode="Markdown",
    )
  except Exception:
    await callback.message.answer(
        f"{common_desc}\n\n⚠️ *(Видео-пример временно недоступен)*",
        reply_markup=order_kb,
        parse_mode="Markdown",
    )

  try:
    await callback.message.delete()
  except Exception:
    pass


# 4. Нажатие «Оформить заказ» -> Запрос материалов пачкой
@router.callback_query(F.data.startswith("confirm_price_"))
async def cb_confirm_price(callback: CallbackQuery, bot: Bot):
  await callback.answer()
  price = callback.data.split("_")[2]
  pending_price[callback.from_user.id] = price

  tz_text = (
      f"🖤 **Оформление заказа ({price} ₽):**\n\n"
      "📌 **Требования к материалам:**\n"
      "• Ракурсы как на видео-примере\n"
      "• 15 кадров по 4 секунды\n\n"
      "💬 **Инструкция:**\n"
      "**Отправьте все видео и фото пачкой прямо в этот чат**.\n"
      "⏳ Бот подождет **1 минуту** после отправки и сразу передаст заказ"
      " команде!"
  )

  await callback.message.answer(
      text=tz_text, reply_markup=get_back_to_menu_kb(), parse_mode="Markdown"
  )

  try:
    await callback.message.delete()
  except Exception:
    pass


# ===================== ЗАКАЗЫ =====================


def build_order_text(user, price) -> str:
  # HTML + html.escape: юзернеймы с «_» и другие символы больше не ломают сообщение
  name = html.escape(user.full_name or "Без имени")
  username = f"@{html.escape(user.username)}" if user.username else "без юзернейма"
  return (
      "🔥 <b>Новый заказ в общей очереди!</b>\n\n"
      f"💰 <b>Тариф:</b> {html.escape(str(price))} ₽\n"
      f'👤 <b>Клиент:</b> <a href="tg://user?id={user.id}">{name}</a>'
      f" ({username})\n"
      f"🆔 <code>{user.id}</code>\n"
      f"📌 <b>Статус:</b> {STATUS_FREE}"
  )


async def create_order(bot: Bot, user, price) -> int:
  """Создаёт тему и сообщение с кнопкой «Взять заказ». Возвращает thread_id."""
  topic_name = f"[{price}₽] Заказ | {user.full_name}"
  forum_topic = await tg_call(
      bot.create_forum_topic, chat_id=WORK_GROUP_ID, name=topic_name[:120]
  )
  thread_id = forum_topic.message_thread_id

  pool_kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🙋‍♂️ Взять заказ в работу",
                  callback_data=f"take_{thread_id}",
              )
          ],
          [
              InlineKeyboardButton(
                  text="❌ Закрыть и отменить заказ",
                  callback_data=f"ask_close_{thread_id}",
              )
          ],
      ]
  )

  try:
    await tg_call(
        bot.send_message,
        chat_id=WORK_GROUP_ID,
        message_thread_id=thread_id,
        text=build_order_text(user, price),
        reply_markup=pool_kb,
        parse_mode="HTML",
    )
  except Exception:
    # Не оставляем пустую тему без кнопки — удаляем и сообщаем об ошибке выше
    try:
      await bot.delete_forum_topic(
          chat_id=WORK_GROUP_ID, message_thread_id=thread_id
      )
    except Exception:
      pass
    raise

  user_to_thread[user.id] = thread_id
  thread_to_user[thread_id] = user.id
  save_state()
  return thread_id


async def flush_client_batch(bot: Bot, user_id: int, messages: list):
  """Отправляет собранную пачку от клиента в тему заказа (создавая её при необходимости)."""
  thread_id = user_to_thread.get(user_id)
  is_new = thread_id is None

  if is_new:
    price = pending_price.get(user_id, "100")
    try:
      thread_id = await create_order(bot, messages[0].from_user, price)
    except Exception:
      logging.exception("Ошибка создания заказа для %s", user_id)
      await bot.send_message(
          user_id,
          "⚠️ Ошибка при создании заказа. Отправьте материалы ещё раз чуть"
          " позже.",
      )
      return
    pending_price.pop(user_id, None)

  await send_batch(
      bot,
      WORK_GROUP_ID,
      messages,
      thread_id=thread_id,
      captions_title="💬 Комментарий клиента:",
  )

  if is_new:
    sent_msg = await bot.send_message(
        user_id,
        "✅ **Ваше ТЗ и материалы успешно ушли команде"
        " разработчиков!**\nОжидайте, скоро кто-то возьмет заказ в работу."
        " Можете писать сюда любые вопросы.",
        reply_markup=get_client_order_kb(thread_id),
        parse_mode="Markdown",
    )
    user_confirm_msg_id[user_id] = sent_msg.message_id
  else:
    await bot.send_message(user_id, "✅ Файлы переданы команде.")


batcher = Batcher(flush_client_batch)


# 5. Все сообщения клиента в личке: сбор материалов и переписка по заказу
@router.message(F.chat.type == "private")
async def client_message(message: Message, bot: Bot):
  user_id = message.from_user.id
  thread_id = user_to_thread.get(user_id)

  # Нет ни активного заказа, ни ожидания материалов (например, после перезапуска бота)
  if thread_id is None and user_id not in pending_price:
    now = time.monotonic()
    if now - last_hint.get(user_id, 0) > 30:
      last_hint[user_id] = now
      await message.answer(
          "Не вижу активного заказа 🤔\nНажмите «Главное меню» → «Заказать"
          " эдит», чтобы оформить заказ.",
          reply_markup=get_back_to_menu_kb(),
      )
    return

  # Первая пачка: ждём, пока клиент закончит загрузку
  if thread_id is None:
    batcher.add(user_id, message, FIRST_BATCH_DELAY, bot)
    return

  # Заказ уже создан. Обычный текст пересылаем сразу, файлы собираем в альбомы.
  is_file = bool(message.photo or message.video or message.document)
  if not is_file and not batcher.busy(user_id):
    try:
      await tg_call(
          message.copy_to, chat_id=WORK_GROUP_ID, message_thread_id=thread_id
      )
    except Exception:
      logging.exception("Не удалось переслать сообщение клиента %s", user_id)
    return

  batcher.add(user_id, message, FOLLOWUP_DELAY, bot)


# 6. Кнопка «Взять заказ» разработчиком
@router.callback_query(F.data.startswith("take_"))
async def take_order_from_pool(callback: CallbackQuery, bot: Bot):
  dev_id = callback.from_user.id
  if dev_id not in DEV_IDS:
    await callback.answer(
        f"⛔ Ошибка: вашего ID ({dev_id}) нет в списке разработчиков!",
        show_alert=True,
    )
    return

  thread_id = int(callback.data.split("_")[1])
  if thread_id in assigned_dev:
    await callback.answer(
        "⚠️ Этот заказ уже занят другим разработчиком!", show_alert=True
    )
    return

  assigned_dev[thread_id] = dev_id
  save_state()

  close_only_kb = InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="❌ Закрыть и отменить заказ",
              callback_data=f"ask_close_{thread_id}",
          )
      ]]
  )

  taker = html.escape(callback.from_user.full_name or str(dev_id))
  try:
    updated_text = callback.message.html_text.replace(
        STATUS_FREE, f"🔴 <b>ЗАНЯТ</b> (в работе: {taker})"
    )
    await callback.message.edit_text(
        updated_text, reply_markup=close_only_kb, parse_mode="HTML"
    )
  except Exception:
    logging.exception("Ошибка обновления статуса заказа")
    # Хотя бы уберём кнопку «Взять заказ»
    try:
      await callback.message.edit_reply_markup(reply_markup=close_only_kb)
    except Exception:
      pass

  user_id = thread_to_user.get(thread_id)
  if user_id:
    old_msg_id = user_confirm_msg_id.pop(user_id, None)
    if old_msg_id:
      try:
        await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
      except Exception:
        pass

    new_msg = await bot.send_message(
        user_id,
        "📌 **Ваш заказ взят в работу!** Ожидайте результат. Можете писать"
        " сюда любые вопросы и уточнения.",
        reply_markup=get_client_order_kb(thread_id),
        parse_mode="Markdown",
    )
    user_confirm_msg_id[user_id] = new_msg.message_id

  await callback.answer("Вы успешно взяли заказ!")


# === ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ ЗАКАЗА ===


@router.callback_query(F.data.startswith("ask_close_"))
async def ask_close_order(callback: CallbackQuery, bot: Bot):
  thread_id = int(callback.data.split("_")[2])
  confirm_text = "⚠️ **Вы уверены, что хотите закрыть и отменить этот заказ?**"

  try:
    await callback.message.edit_reply_markup(
        reply_markup=get_confirm_close_kb(thread_id)
    )
  except Exception:
    await callback.message.answer(
        confirm_text,
        reply_markup=get_confirm_close_kb(thread_id),
        parse_mode="Markdown",
    )

  await callback.answer()


@router.callback_query(F.data.startswith("cancel_close_"))
async def cancel_close_order(callback: CallbackQuery, bot: Bot):
  thread_id = int(callback.data.split("_")[2])

  if callback.message.chat.id == WORK_GROUP_ID:
    if thread_id in assigned_dev:
      kb = InlineKeyboardMarkup(
          inline_keyboard=[[
              InlineKeyboardButton(
                  text="❌ Закрыть и отменить заказ",
                  callback_data=f"ask_close_{thread_id}",
              )
          ]]
      )
    else:
      kb = InlineKeyboardMarkup(
          inline_keyboard=[
              [
                  InlineKeyboardButton(
                      text="🙋‍♂️ Взять заказ в работу",
                      callback_data=f"take_{thread_id}",
                  )
              ],
              [
                  InlineKeyboardButton(
                      text="❌ Закрыть и отменить заказ",
                      callback_data=f"ask_close_{thread_id}",
                  )
              ],
          ]
      )
  else:
    kb = get_client_order_kb(thread_id)

  try:
    await callback.message.edit_reply_markup(reply_markup=kb)
  except Exception:
    pass

  await callback.answer("Действие отменено.")


@router.callback_query(F.data.startswith("confirm_close_"))
async def confirm_close_order(callback: CallbackQuery, bot: Bot):
  thread_id = int(callback.data.split("_")[2])
  is_closed_by_admin = callback.message.chat.id == WORK_GROUP_ID

  user_id = thread_to_user.get(thread_id)
  if not user_id:
    for u_id, t_id in user_to_thread.items():
      if t_id == thread_id:
        user_id = u_id
        break

  if user_id:
    user_to_thread.pop(user_id, None)
    user_confirm_msg_id.pop(user_id, None)
    pending_price.pop(user_id, None)
    batcher.drop(user_id)
  thread_to_user.pop(thread_id, None)
  assigned_dev.pop(thread_id, None)
  save_state()

  if is_closed_by_admin:
    # Закрыли админы в рабочей группе -> отправляем сообщение клиенту
    if user_id:
      try:
        await bot.send_message(
            user_id,
            "Ваш заказ был закрыт. Спасибо что обратились к нам",
            reply_markup=get_back_to_menu_kb(),
        )
      except Exception:
        pass
  else:
    # Закрыл сам клиент в личке с ботом -> отправляем только 1 сообщение
    try:
      await callback.message.answer(
          "Заказ успешно закрыт. Спасибо за сотрудничество",
          reply_markup=get_back_to_menu_kb(),
      )
    except Exception:
      pass

  try:
    await callback.message.delete()
  except Exception:
    pass

  try:
    await bot.delete_forum_topic(
        chat_id=WORK_GROUP_ID, message_thread_id=thread_id
    )
  except Exception:
    pass

  await callback.answer("Заказ закрыт!")


# ===============================================


# 8. Пересылка от разработчика клиенту
@router.message(F.chat.id == WORK_GROUP_ID, F.message_thread_id)
async def dev_to_client_chat(message: Message, bot: Bot):
  if message.from_user is None or message.from_user.is_bot:
    return

  thread_id = message.message_thread_id
  if not thread_id:
    return

  user_id = thread_to_user.get(thread_id)
  if not user_id:
    return

  assigned = assigned_dev.get(thread_id)
  if assigned and assigned != message.from_user.id:
    await message.reply("⚠️ Заказ закреплен за другим разработчиком.")
    return

  if not assigned:
    await message.reply("⚠️ Сначала нажмите «Взять заказ в работу».")
    return

  try:
    await tg_call(message.copy_to, user_id)
  except Exception:
    logging.exception("Не удалось отправить сообщение клиенту %s", user_id)
    await message.reply("⚠️ Не удалось доставить сообщение клиенту.")


# 10. Поддержка
@router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery, bot: Bot):
  await callback.answer()
  text = (
      "🛠 **Поддержка**\n\nОбращайтесь по идеям, вопросам и сообщайте нам об"
      " ошибках: @sliy100"
  )

  await callback.message.answer(
      text, reply_markup=get_back_to_menu_kb(), parse_mode="Markdown"
  )
  try:
    await callback.message.delete()
  except Exception:
    pass


async def main():
  load_state()
  bot = Bot(token=TOKEN)
  dp = Dispatcher()
  dp.include_router(router)
  await bot.delete_webhook(drop_pending_updates=True)
  print("Бот успешно запущен!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO)
  asyncio.run(main())
