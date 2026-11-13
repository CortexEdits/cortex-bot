import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto, InputMediaVideo

# Токен берется из переменных окружения (для Bothost), если нет — подставляется ваш
TOKEN = os.getenv("BOT_TOKEN", "9287829274:AAFaGM4WwHNRlQm5Ukg36AojXtkgwEzMSzk")
WORK_GROUP_ID = -1004300868991  # ID закрытой группы с темами
DEV_IDS = [7492826311, 5469728559, 7274526703]  # Твой ID, ID друга и ID третьего разработчика

user_to_thread = {}
thread_to_user = {}
assigned_dev = {}

# Словарь для хранения ID текущего интерактивного сообщения у клиента: {user_id: message_id}
user_confirm_msg_id = {}

# Буферы для сбора медиафайлов от пользователя
order_buffers = {}
buffer_tasks = {}

class OrderState(StatesGroup):
    waiting_for_tz = State()  # Шаг: ожидание материалов и ТЗ

router = Router()

# Клавиатура главного меню (с возвращенной кнопкой отзывов)
def get_main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Заказать эдит", callback_data="menu_order")],
        [InlineKeyboardButton(text="🛠 Поддержка", callback_data="menu_profile")],
        [InlineKeyboardButton(text="⭐ Отзывы и работы", url="https://t.me/CortexEdits")]
    ])

# Клавиатура выбора тарифов
def get_prices_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Эдит за 50 ₽", callback_data="price_50")],
        [InlineKeyboardButton(text="🔥 Эдит за 100 ₽", callback_data="price_100")],
        [InlineKeyboardButton(text="💎 Эдит за 150 ₽", callback_data="price_150")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")]
    ])

# Клавиатура возврата в меню
def get_back_to_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")]
    ])

# Клавиатура для клиента
def get_client_order_kb(thread_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть и отменить заказ", callback_data=f"ask_close_{thread_id}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")]
    ])

# Клавиатура подтверждения закрытия заказа (Да / Отмена)
def get_confirm_close_kb(thread_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, закрыть", callback_data=f"confirm_close_{thread_id}"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data=f"cancel_close_{thread_id}")
        ]
    ])

# Функция отправки главного меню (с баннером)
async def show_main_menu(bot: Bot, chat_id: int, state: FSMContext):
    await state.clear()
    caption_text = (
        "🖤 **CortexEdits** — делаем стильные и красивые эдиты для твоей техники.\n\n"
        "Подчеркнем каждую детальку твоего железа:\n"
        "🚲 **Велосипеды, BMX и МТБ**\n"
        "🛴 **Самокаты**\n"
        "🏍 **Мотоциклы и питбайки**\n\n"
        "Плавные переходы, классная музыка, темная эстетика и сочный вайб без лишнего пафоса. Выбирай раздел ниже 👇"
    )

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile("menu.jpg"),
            caption=caption_text,
            reply_markup=get_main_menu_kb(),
            parse_mode="Markdown"
        )
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=caption_text,
            reply_markup=get_main_menu_kb(),
            parse_mode="Markdown"
        )

# 1. Команда /start
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await show_main_menu(bot, message.chat.id, state)

# Возврат в главное меню
@router.callback_query(F.data == "menu_home")
async def cb_menu_home(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await show_main_menu(bot, callback.message.chat.id, state)
    try:
        await callback.message.delete()
    except Exception:
        pass

# 2. Меню выбора тарифов
@router.callback_query(F.data == "menu_order")
async def cb_menu_order(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    
    caption_text = "💰 **Выберите ценовую категорию эдита:**\n\nНажмите на нужный вариант ниже 👇"
    banner_path = "prices_banner.jpg"

    try:
        await callback.message.answer_photo(
            photo=FSInputFile(banner_path),
            caption=caption_text,
            reply_markup=get_prices_kb(),
            parse_mode="Markdown"
        )
    except Exception:
        await callback.message.answer(
            f"{caption_text}\n\n⚠️ *(Баннер временно недоступен)*",
            reply_markup=get_prices_kb(),
            parse_mode="Markdown"
        )
    
    try:
        await callback.message.delete()
    except Exception:
        pass

# 3. Обработка конкретного тарифа (50, 100, 150 руб) с прикреплением видео-примера
@router.callback_query(F.data.startswith("price_"))
async def cb_select_price(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    
    price = callback.data.split("_")[1]
    await state.update_data(selected_price=price)

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

    order_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Оформить заказ с этим прайсом", callback_data=f"confirm_price_{price}")],
        [InlineKeyboardButton(text="⬅️ Назад к тарифам", callback_data="menu_order")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")]
    ])

    video_files = {
        "50": "videos/example_50.mp4",
        "100": "videos/example_100.mp4",
        "150": "videos/example_150.mp4"
    }

    video_path = video_files.get(price)

    try:
        await callback.message.answer_video(
            video=FSInputFile(video_path),
            caption=common_desc,
            reply_markup=order_kb,
            parse_mode="Markdown"
        )
    except Exception:
        await callback.message.answer(
            f"{common_desc}\n\n⚠️ *(Видео-пример временно недоступен)*",
            reply_markup=order_kb,
            parse_mode="Markdown"
        )

    try:
        await callback.message.delete()
    except Exception:
        pass

# 4. Нажатие «Оформить заказ» -> Запрос материалов пачкой
@router.callback_query(F.data.startswith("confirm_price_"))
async def cb_confirm_price(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    price = callback.data.split("_")[2]
    await state.set_state(OrderState.waiting_for_tz)
    await state.update_data(selected_price=price)

    tz_text = (
        f"🖤 **Оформление заказа ({price} ₽):**\n\n"
        "📌 **Требования к материалам:**\n"
        "• Ракурсы как на видео-примере\n"
        "• 15 кадров по 4 секунды\n\n"
        "💬 **Инструкция:**\n"
        "**Отправьте все видео и фото пачкой прямо в этот чат**.\n"
        "⏳ Бот подождет **1 минуту** после отправки и сразу передаст заказ команде!"
    )

    await callback.message.answer(
        text=tz_text,
        reply_markup=get_back_to_menu_kb(),
        parse_mode="Markdown"
    )

    try:
        await callback.message.delete()
    except Exception:
        pass

# Функция финализации заказа и отправки собранных файлов альбомом внутрь топика
async def finalize_order(user_id: int, bot: Bot, state: FSMContext):
    messages = order_buffers.pop(user_id, [])
    buffer_tasks.pop(user_id, None)

    if not messages:
        return

    data = await state.get_data()
    price = data.get("selected_price", "100")
    await state.clear()

    user = messages[0].from_user
    topic_name = f"[{price}₽] Заказ | {user.full_name}"

    try:
        forum_topic = await bot.create_forum_topic(chat_id=WORK_GROUP_ID, name=topic_name[:120])
        thread_id = forum_topic.message_thread_id
    except Exception as e:
        await bot.send_message(user_id, "⚠️ Ошибка при создании заказа. Попробуйте позже.")
        print(f"Ошибка создания топика: {e}")
        return

    user_to_thread[user.id] = thread_id
    thread_to_user[thread_id] = user.id

    pool_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋‍♂️ Взять заказ в работу", callback_data=f"take_{thread_id}")],
        [InlineKeyboardButton(text="❌ Закрыть и отменить заказ", callback_data=f"ask_close_{thread_id}")]
    ])

    username_str = f"@{user.username}" if user.username else "без юзернейма"
    order_msg_text = (
        f"🔥 **Новый заказ в общей очереди!**\n\n"
        f"💰 **Тариф:** {price} ₽\n"
        f"👤 **Клиент:** {username_str} (ID: `{user.id}`)\n"
        f"📌 **Статус:** 🟢 Свободен, ждет исполнителя."
    )

    await bot.send_message(
        chat_id=WORK_GROUP_ID,
        message_thread_id=thread_id,
        text=order_msg_text,
        reply_markup=pool_kb,
        parse_mode="Markdown"
    )

    media_group = []
    text_messages = []

    for msg in messages:
        if msg.photo:
            media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id))
        elif msg.video:
            media_group.append(InputMediaVideo(media=msg.video.file_id))
        else:
            text_messages.append(msg)

    if media_group:
        for i in range(0, len(media_group), 10):
            chunk = media_group[i:i+10]
            try:
                await bot.send_media_group(chat_id=WORK_GROUP_ID, message_thread_id=thread_id, media=chunk)
                await asyncio.sleep(0.6)
            except Exception as e:
                print(f"Ошибка отправки медиагруппы: {e}")

    for msg in text_messages:
        try:
            await msg.copy_to(chat_id=WORK_GROUP_ID, message_thread_id=thread_id)
        except Exception as e:
            print(f"Ошибка копирования текста/файла: {e}")

    sent_msg = await bot.send_message(
        user_id,
        "✅ **Ваше ТЗ и материалы успешно ушли команде разработчиков!**\nОжидайте, скоро кто-то возьмет заказ в работу. Можете писать сюда любые вопросы.",
        reply_markup=get_client_order_kb(thread_id),
        parse_mode="Markdown"
    )
    user_confirm_msg_id[user_id] = sent_msg.message_id

# 5. Сбор ТЗ пачкой с ожиданием 1 минуты (60 секунд)
@router.message(OrderState.waiting_for_tz, F.chat.type == "private")
async def process_tz(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id

    if user_id in user_to_thread:
        thread_id = user_to_thread[user_id]
        await message.copy_to(chat_id=WORK_GROUP_ID, message_thread_id=thread_id)
        return

    if user_id not in order_buffers:
        order_buffers[user_id] = []
    order_buffers[user_id].append(message)

    if user_id in buffer_tasks:
        buffer_tasks[user_id].cancel()

    async def delayed_finalize():
        await asyncio.sleep(60.0)
        await finalize_order(user_id, bot, state)

    buffer_tasks[user_id] = asyncio.create_task(delayed_finalize())

# 6. Кнопка «Взять заказ» разработчиком
@router.callback_query(F.data.startswith("take_"))
async def take_order_from_pool(callback: CallbackQuery, bot: Bot):
    dev_id = callback.from_user.id
    if dev_id not in DEV_IDS:
        await callback.answer(f"⛔ Ошибка: вашего ID ({dev_id}) нет в списке разработчиков!", show_alert=True)
        return

    thread_id = int(callback.data.split("_")[1])
    if thread_id in assigned_dev:
        await callback.answer("⚠️ Этот заказ уже занят другим разработчиком!", show_alert=True)
        return

    assigned_dev[thread_id] = dev_id

    close_only_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть и отменить заказ", callback_data=f"ask_close_{thread_id}")]
    ])
    
    try:
        updated_text = callback.message.text.replace(
            "📌 **Статус:** 🟢 Свободен, ждет исполнителя.", 
            "📌 **Статус:** 🔴 **ЗАНЯТ** (В работе)"
        )
        await callback.message.edit_text(
            updated_text,
            reply_markup=close_only_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка обновления статуса заказа: {e}")

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
            "📌 **Ваш заказ взят в работу!** Ожидайте результат. Можете писать сюда любые вопросы и уточнения.", 
            reply_markup=get_client_order_kb(thread_id),
            parse_mode="Markdown"
        )
        user_confirm_msg_id[user_id] = new_msg.message_id

    await callback.answer("Вы успешно взяли заказ!")

# === ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ ЗАКАЗА ===

@router.callback_query(F.data.startswith("ask_close_"))
async def ask_close_order(callback: CallbackQuery, bot: Bot):
    thread_id = int(callback.data.split("_")[2])
    confirm_text = "⚠️ **Вы уверены, что хотите закрыть и отменить этот заказ?**"
    
    try:
        await callback.message.edit_reply_markup(reply_markup=get_confirm_close_kb(thread_id))
    except Exception:
        await callback.message.answer(confirm_text, reply_markup=get_confirm_close_kb(thread_id), parse_mode="Markdown")
        
    await callback.answer()

@router.callback_query(F.data.startswith("cancel_close_"))
async def cancel_close_order(callback: CallbackQuery, bot: Bot):
    thread_id = int(callback.data.split("_")[2])
    
    if callback.message.chat.id == WORK_GROUP_ID:
        if thread_id in assigned_dev:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Закрыть и отменить заказ", callback_data=f"ask_close_{thread_id}")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🙋‍♂️ Взять заказ в работу", callback_data=f"take_{thread_id}")],
                [InlineKeyboardButton(text="❌ Закрыть и отменить заказ", callback_data=f"ask_close_{thread_id}")]
            ])
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
    
    user_id = thread_to_user.get(thread_id)
    if not user_id:
        for u_id, t_id in user_to_thread.items():
            if t_id == thread_id:
                user_id = u_id
                break

    if user_id:
        user_to_thread.pop(user_id, None)
    thread_to_user.pop(thread_id, None)
    assigned_dev.pop(thread_id, None)

    closing_text = "📌 **Ваш заказ был закрыт.** Спасибо, что обратились к нам!"

    if user_id:
        old_msg_id = user_confirm_msg_id.pop(user_id, None)
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
            except Exception:
                pass

        try:
            await bot.send_message(
                user_id, 
                closing_text, 
                reply_markup=get_back_to_menu_kb(),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    try:
        await callback.message.answer(
            "📌 **Заказ успешно закрыт.** Спасибо за сотрудничество!",
            reply_markup=get_back_to_menu_kb(),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await bot.delete_forum_topic(chat_id=WORK_GROUP_ID, message_thread_id=thread_id)
    except Exception:
        pass
        
    await callback.answer("Заказ закрыт и топик удален!")

# ===============================================

# 7. Пересылка сообщений от клиента в топик
@router.message(F.chat.type == "private")
async def client_to_dev_chat(message: Message, bot: Bot):
    user_id = message.from_user.id
    thread_id = user_to_thread.get(user_id)
    if not thread_id:
        return
    await message.copy_to(chat_id=WORK_GROUP_ID, message_thread_id=thread_id)

# 8. Пересылка от разработчика клиенту
@router.message(F.chat.id == WORK_GROUP_ID, F.message_thread_id)
async def dev_to_client_chat(message: Message, bot: Bot):
    if message.from_user.is_bot:
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

    await message.copy_to(user_id)

# 10. Поддержка
@router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    text = (
        "🛠 **Поддержка**\n\n"
        "Обращайтесь по идеям, вопросам и сообщайте нам об ошибках: @sliy100"
    )
    
    await callback.message.answer(text, reply_markup=get_back_to_menu_kb(), parse_mode="Markdown")
    try:
        await callback.message.delete()
    except Exception:
        pass

async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())