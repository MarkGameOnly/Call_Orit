# === Импорты ===
import os
import logging
import asyncio
import sqlite3
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, UploadFile
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup, BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from openai import AsyncOpenAI

from io import BytesIO
import base64

# === Настройка логирования ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === Настройка окружения ===
# Загружаем переменные окружения из файла .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1082828397")) # ID администратора
CHANNEL_ID = os.getenv("CHANNEL_ID") # ID канала для уведомлений (например, -1001234567890)

# Указываем относительный путь к директории с изображениями
# Убедитесь, что папка 'img' находится в том же каталоге, что и main.py
IMG_DIR = "img" 

# Проверяем, существуют ли необходимые переменные окружения
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не установлен в переменных окружения.")
    exit("BOT_TOKEN не установлен. Пожалуйста, создайте файл .env и добавьте BOT_TOKEN.")
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY не установлен в переменных окружения.")
    exit("OPENAI_API_KEY не установлен. Пожалуйста, создайте файл .env и добавьте OPENAI_API_KEY.")
if not CHANNEL_ID:
    logger.warning("CHANNEL_ID не установлен в переменных окружения. Уведомления в канал будут отключены.")
    # Устанавливаем CHANNEL_ID в None, если он не указан, чтобы избежать ошибок при отправке сообщений
    CHANNEL_ID = None 

# === Инициализация бота ===
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# === База данных ===
# Подключаемся к базе данных SQLite. check_same_thread=False нужен для работы с FastAPI.
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()
FREE_USES_LIMIT = 10 # Лимит бесплатных использований для новых пользователей

def init_db():
    """Инициализирует базу данных, создавая таблицу users, если она не существует."""
    try:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                uses_left INTEGER DEFAULT {FREE_USES_LIMIT},
                last_active TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        logger.info("База данных успешно инициализирована.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка инициализации базы данных: {e}")

async def setup_bot_commands():
    """Устанавливает стандартные команды бота, которые будут отображаться в меню Telegram."""
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="menu", description="Открыть меню"),
            BotCommand(command="profile", description="Показать профиль"),
            BotCommand(command="buy", description="Оплатить подписку"),
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="broadcast", description="Рассылка от администратора"),
            BotCommand(command="iqtest", description="Пройти IQ Тест") # Добавлена новая команда
        ])
        logger.info("Команды бота успешно установлены.")
    except Exception as e:
        logger.error(f"Ошибка установки команд бота: {e}")

# === Состояния FSM (Finite State Machine) ===
# Используются для управления потоком диалога с пользователем
class GenStates(StatesGroup):
    """Определяет состояния для FSM."""
    await_photo = State() # Состояние ожидания фотографии от пользователя
    await_broadcast = State() # Состояние ожидания текста для рассылки от администратора

# === Главное меню ===
def main_menu():
    """Возвращает объект ReplyKeyboardMarkup для главного меню бота."""
    kb = [
        [KeyboardButton(text="🍱 Узнать калории по фото")],
        [KeyboardButton(text="📸 Сделать фото"), KeyboardButton(text="🏋️ Программы тренировок")],
        [KeyboardButton(text="📚 Как пользоваться?"), KeyboardButton(text="💳 Оплата подписки")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🧠 IQ Тест")] # Добавлена новая кнопка
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# === Хелперы (вспомогательные функции) ===
def ensure_user(user_id: int):
    """
    Проверяет существование пользователя в базе данных.
    Если пользователь не найден, добавляет его с начальным лимитом использований.
    Обновляет время последней активности пользователя.
    """
    now = datetime.utcnow().isoformat()
    try:
        cursor.execute("""
            INSERT INTO users (user_id, last_active)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_active=excluded.last_active
        """, (user_id, now))
        conn.commit()
        logger.info(f"Пользователь {user_id} проверен/добавлен в БД.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при проверке/добавлении пользователя {user_id}: {e}")

def get_user_stats():
    """Возвращает общую статистику по пользователям."""
    try:
        cursor.execute("SELECT COUNT(*), SUM(uses_left) FROM users")
        total_users, total_uses = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM users WHERE uses_left < ?", (FREE_USES_LIMIT,))
        paid_users = cursor.fetchone()[0]
        return total_users, total_uses, paid_users
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении статистики пользователей: {e}")
        return 0, 0, 0

def get_generation_count():
    """Возвращает количество пользователей, которые использовали бота (т.е. их uses_left уменьшился)."""
    try:
        cursor.execute("SELECT COUNT(*) FROM users WHERE uses_left < ?", (FREE_USES_LIMIT,))
        return cursor.fetchone()[0]
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении количества генераций: {e}")
        return 0

async def send_photo_or_text(chat_id: int, img_path: str, caption: str, parse_mode: str = None):
    """
    Отправляет фото с подписью. Если фото не найдено, отправляет только текст.
    """
    full_img_path = os.path.join(IMG_DIR, img_path)
    if os.path.exists(full_img_path):
        try:
            await bot.send_photo(chat_id=chat_id, photo=types.FSInputFile(full_img_path), caption=caption, parse_mode=parse_mode)
            logger.info(f"Фото {img_path} успешно отправлено пользователю {chat_id}.")
        except Exception as e:
            logger.error(f"Ошибка при отправке фото {img_path} пользователю {chat_id}: {e}")
            await bot.send_message(chat_id=chat_id, text=f"🖼️ Не удалось загрузить изображение. {caption}", parse_mode=parse_mode)
    else:
        logger.warning(f"Файл изображения не найден: {full_img_path}. Отправляю только текст.")
        await bot.send_message(chat_id=chat_id, text=f"🖼️ Изображение не найдено. {caption}", parse_mode=parse_mode)

# === Обработчики команд ===

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Обработчик команды /stats для администратора."""
    if message.from_user.id != ADMIN_ID:
        return await message.answer("🚫 Нет доступа.")

    total, _, paid = get_user_stats()
    active = get_generation_count()

    await message.answer(
        f"📊 Статистика:\n"
        f"👥 Всего пользователей: {total}\n"
        f"💸 С подпиской: {paid}\n"
        f"📈 Активных генераций: {active}"
    )
    logger.info(f"Администратор {message.from_user.id} запросил статистику.")

@dp.message(Command("users"))
async def cmd_list_users(message: Message):
    """Обработчик команды /users для администратора (показывает последних активных пользователей)."""
    if message.from_user.id != ADMIN_ID:
        return await message.answer("🚫 Нет доступа.")
    
    try:
        cursor.execute("SELECT user_id, uses_left, last_active FROM users ORDER BY last_active DESC LIMIT 20")
        rows = cursor.fetchall()
        if not rows:
            return await message.answer("❌ Пользователей нет.")
        
        text = "🧾 Последние пользователи:\n\n"
        for uid, uses, last in rows:
            text += f"🆔 {uid} — Осталось: {uses} — Активен: {last}\n"
        
        await message.answer(text)
        logger.info(f"Администратор {message.from_user.id} запросил список пользователей.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка пользователей для администратора: {e}")
        await message.answer("❌ Произошла ошибка при получении списка пользователей.")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    """Обработчик команды /broadcast для администратора (инициирует рассылку)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет доступа к этой команде.")
        return
    await message.answer("✏️ Введите текст рассылки:")
    await state.set_state(GenStates.await_broadcast)
    logger.info(f"Администратор {message.from_user.id} инициировал рассылку.")

@dp.message(GenStates.await_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    """Обработчик состояния ожидания текста для рассылки."""
    text = message.text
    if not text:
        await message.answer("⚠️ Текст рассылки не может быть пустым. Попробуйте снова.")
        await state.clear()
        return

    try:
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()
        success, failed = 0, 0
        for user in users:
            try:
                await bot.send_message(chat_id=user[0], text=text)
                success += 1
                await asyncio.sleep(0.05) # Небольшая задержка для избежания флуда
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
                failed += 1
        
        if CHANNEL_ID:
            try:
                await bot.send_message(chat_id=CHANNEL_ID, text=f"📢 Новая рассылка: {text}")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление о рассылке в канал {CHANNEL_ID}: {e}")

        await message.answer(f"✅ Рассылка завершена\nОтправлено: {success}\nОшибок: {failed}")
        logger.info(f"Рассылка завершена. Отправлено: {success}, Ошибок: {failed}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка пользователей для рассылки: {e}")
        await message.answer("❌ Произошла ошибка при выполнении рассылки.")
    finally:
        await state.clear()

@dp.message(Command("find"))
async def cmd_find_user(message: Message):
    """Обработчик команды /find для администратора (поиск пользователя по ID)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет доступа к этой команде.")
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("⚠️ Используйте формат: /find ID")
        return
    try:
        target_id = int(parts[1])
        cursor.execute("SELECT uses_left FROM users WHERE user_id = ?", (target_id,))
        row = cursor.fetchone()
        if row:
            await message.answer(f"👤 Пользователь {target_id} найден. Осталось использований: {row[0]}")
            logger.info(f"Администратор {message.from_user.id} нашел пользователя {target_id}.")
        else:
            await message.answer("❌ Пользователь не найден.")
            logger.info(f"Администратор {message.from_user.id} не нашел пользователя {target_id}.")
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом.")
        logger.warning(f"Администратор {message.from_user.id} ввел некорректный ID: {parts[1]}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при поиске пользователя {target_id}: {e}")
        await message.answer("❌ Ошибка при поиске пользователя в базе данных.")

@dp.message(F.text.in_(["/start", "/menu"]))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команд /start и /menu."""
    ensure_user(message.from_user.id)
    await message.answer("👋 Добро пожаловать! Выберите действие:", reply_markup=main_menu())
    await state.clear() # Очищаем состояние, если пользователь вернулся в меню
    logger.info(f"Пользователь {message.from_user.id} запустил бота или открыл меню.")

@dp.message(F.text == "/profile")
@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: Message):
    """Обработчик команды /profile и кнопки 'Профиль'."""
    user_id = message.from_user.id
    ensure_user(user_id)
    cursor.execute("SELECT uses_left FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    caption = f"👤 Профиль:\nID: {user_id}\nОсталось использований: {row[0] if row else 0}"
    await send_photo_or_text(message.chat.id, "profile.png", caption)
    logger.info(f"Пользователь {user_id} запросил профиль.")

@dp.message(F.text == "/admin")
async def cmd_admin(message: Message):
    """Обработчик команды /admin для администратора."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 У вас нет доступа к этой команде.")
        return

    total_users, total_uses, paid_users = get_user_stats()

    await message.answer(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"🔄 Общих использований осталось: {total_uses or 0}\n"
        f"💸 Купили подписку: {paid_users}\n\n"
        f"✏️ Чтобы активировать подписку пользователю, напиши: /activate ID", parse_mode="HTML")
    logger.info(f"Администратор {message.from_user.id} открыл админ-панель.")

@dp.message(Command("activate"))
async def cmd_activate_user(message: Message):
    """Обработчик команды /activate для администратора (активация подписки)."""
    if message.from_user.id != ADMIN_ID:
        return await message.answer("🚫 Нет доступа.")
    
    parts = message.text.strip().split()
    if len(parts) != 2:
        return await message.answer("⚠️ Используйте формат: /activate user_id")

    user_id_str = parts[1]
    try:
        user_id = int(user_id_str)
        cursor.execute("UPDATE users SET uses_left = 999 WHERE user_id = ?", (user_id,))
        conn.commit()
        if cursor.rowcount > 0:
            await message.answer(f"✅ Подписка для пользователя {user_id} активирована.")
            logger.info(f"Администратор {message.from_user.id} активировал подписку для {user_id}.")
            try:
                await bot.send_message(chat_id=user_id, text="🎉 Ваша подписка активирована! Теперь у вас неограниченный доступ.")
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {user_id} об активации подписки: {e}")
        else:
            await message.answer(f"❌ Пользователь {user_id} не найден в базе данных.")
            logger.warning(f"Администратор {message.from_user.id} попытался активировать подписку для несуществующего пользователя {user_id}.")
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом.")
        logger.warning(f"Администратор {message.from_user.id} ввел некорректный ID для активации: {user_id_str}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при активации подписки для пользователя {user_id_str}: {e}")
        await message.answer("❌ Ошибка при активации подписки.")

@dp.message(F.text == "💳 Оплата подписки")
@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    """Обработчик команды /buy и кнопки 'Оплата подписки'."""
    caption = (
        "💳 Подписка стоит $2.5 через CryptoBot:\n\n"
        "<a href='https://t.me/send?start=IVncR7b5DNSe'>🔗 Оплатить</a>\n\n"
        "После оплаты администратор активирует подписку вручную.\n"
        "Если вы оплатили, сообщите в <a href='https://t.me/calloritpay'>канал поддержки</a>."
    )
    await send_photo_or_text(message.chat.id, "pay.png", caption, parse_mode="HTML")
    logger.info(f"Пользователь {message.from_user.id} запросил информацию об оплате.")

@dp.message(F.text.in_(["🍱 Узнать калории по фото", "📸 Сделать фото"]))
async def prompt_photo(message: Message, state: FSMContext):
    """Обработчик кнопок 'Узнать калории по фото' и 'Сделать фото'."""
    image_name = "photocall.png" if "Узнать" in message.text else "sendphoto.png"
    caption = "📸 Пожалуйста, отправьте фото блюда."
    await send_photo_or_text(message.chat.id, image_name, caption)
    await state.set_state(GenStates.await_photo)
    logger.info(f"Пользователь {message.from_user.id} запросил отправку фото.")

@dp.message(F.photo, GenStates.await_photo)
async def handle_photo(message: Message, state: FSMContext):
    """Обработчик полученной фотографии для определения калорийности и БЖУ."""
    user_id = message.from_user.id
    ensure_user(user_id)

    cursor.execute("SELECT uses_left FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    uses_left = row[0] if row else 0

    if uses_left <= 0:
        await message.answer("🔐 Лимит использований исчерпан. Оформите подписку для продолжения.")
        logger.info(f"Пользователь {user_id} исчерпал лимит использований.")
        await state.clear()
        return

    # Отправляем сообщение о начале обработки
    processing_message = await message.answer("⏳ Обрабатываю ваше фото, пожалуйста, подождите...")

    try:
        photo = message.photo[-1] # Берем фото наилучшего качества
        file = await bot.get_file(photo.file_id)
        file_path = file.file_path
        file_data = await bot.download_file(file_path)
        image_bytes = file_data.read()

        # Вызываем OpenAI API для анализа изображения с запросом на БЖУ
        response = await openai_client.chat.completions.create(
            model="gpt-4o", # Используем модель gpt-4o для анализа изображений
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Что изображено на фото? Укажи примерную калорийность блюда, а также содержание белков, жиров и углеводов (БЖУ) в граммах. Отвечай только на русском языке."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"}}
                    ]
                }
            ],
            max_tokens=500 # Максимальное количество токенов в ответе
        )
        answer = response.choices[0].message.content

        # Отправка результата пользователю
        await message.answer(f"📊 Ответ:\n{answer}")
        logger.info(f"Пользователь {user_id} получил ответ от OpenAI с калориями и БЖУ.")

        # Отправка уведомления в канал (если CHANNEL_ID установлен)
        if CHANNEL_ID:
            username_or_id = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
            try:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"📷 Пользователь {username_or_id} загрузил фото еды. Калории и БЖУ вычислены."
                )
                logger.info(f"Уведомление о фото от {user_id} отправлено в канал.")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление в канал {CHANNEL_ID}: {e}")

        # Обновляем счётчик использований
        cursor.execute("UPDATE users SET uses_left = uses_left - 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        logger.info(f"Счетчик использований для пользователя {user_id} уменьшен. Осталось: {uses_left - 1}.")

    except Exception as e:
        logger.exception(f"Ошибка при обработке изображения для пользователя {user_id}: {e}")
        await message.answer("❌ Ошибка при обработке изображения. Попробуйте ещё раз позже.")
    finally:
        # Удаляем сообщение "Обрабатываю..."
        try:
            await bot.delete_message(chat_id=processing_message.chat.id, message_id=processing_message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение об обработке: {e}")
        await state.clear() # Очищаем состояние после обработки

@dp.message(F.text == "📚 Как пользоваться?")
async def cmd_help(message: Message):
    """Обработчик кнопки 'Как пользоваться?'."""
    caption = (
        "ℹ️ <b>Как пользоваться ботом</b>\n\n"
        "1. Нажмите \"Узнать калории по фото\"\n"
        "2. Отправьте фото еды\n"
        "3. Получите приблизительную калорийность блюда и БЖУ.\n\n"
        "🔁 Бесплатно доступно 10 использований. Подписка откроет неограниченный доступ."
    )
    await send_photo_or_text(message.chat.id, "help.png", caption, parse_mode="HTML")
    logger.info(f"Пользователь {message.from_user.id} запросил помощь.")

@dp.message(F.text == "🏋️ Программы тренировок")
async def send_training_programs(message: Message):
    """Обработчик кнопки 'Программы тренировок'."""
    caption = (
        "🏋️‍♀️ <b>Полезные программы:</b>\n\n"
        "- <a href='https://t.me/Itmarket1_bot?start=good_82171'>Авторские программы питания</a>\n"
        "- <a href='https://t.me/Itmarket1_bot?start=good_82170'>Спорт без инвентаря (курс)</a>"
    )
    await send_photo_or_text(message.chat.id, "programm.png", caption, parse_mode="HTML")
    logger.info(f"Пользователь {message.from_user.id} запросил программы тренировок.")

# === НОВЫЙ ФУНКЦИОНАЛ: IQ ТЕСТ ===
@dp.message(F.text == "🧠 IQ Тест")
@dp.message(Command("iqtest"))
async def cmd_iq_test(message: Message):
    """
    Обработчик для кнопки 'IQ Тест' и команды /iqtest.
    Отправляет пользователю картинку и ссылку на IQ-бота.
    """
    caption = (
        "🧠 Пройди наш IQ-тест и узнай свой результат!\n\n"
        "<a href='https://t.me/iqmanager1_bot'>🔗 Перейти к IQ-тесту</a>"
    )
    await send_photo_or_text(message.chat.id, "iq.png", caption, parse_mode="HTML")
    logger.info(f"Пользователь {message.from_user.id} запросил IQ-тест.")

# === Запуск через webhook (FastAPI) ===
@app.post("/")
async def telegram_webhook(req: Request):
    """
    Основная точка входа для вебхуков Telegram.
    Принимает входящие обновления от Telegram и передает их диспетчеру aiogram.
    """
    try:
        body = await req.body()
        update = types.Update.model_validate_json(body)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}")
        return {"ok": False, "error": str(e)}, 500

# === Альтернативный запуск через polling для локального теста ===
# Этот блок кода будет выполняться только при прямом запуске файла (например, python main.py)
# и не будет активен при развертывании через FastAPI/webhook.
if __name__ == "__main__":
    init_db() # Инициализируем базу данных при запуске
    
    async def main():
        """Основная функция для запуска бота в режиме polling."""
        logger.info("Запуск бота в режиме polling...")
        await setup_bot_commands() # Устанавливаем команды бота
        await dp.start_polling(bot) # Запускаем polling
        logger.info("Бот остановлен.")

    # Запускаем асинхронную функцию main
    asyncio.run(main())
