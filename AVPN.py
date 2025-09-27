import telebot
from telebot import types
import requests
import json
import time
import threading
import schedule
from datetime import datetime, timedelta
import secrets
import logging
import random
import string
import sqlite3
from telebot import apihelper

# --- КОНФИГУРАЦИЯ БОТА И СЕРВЕРА ---
TELEGRAM_BOT_TOKEN = "7256242565:AAFZ4emLbt8qZKv_opA1iRS9Y2pAEeJJ0tA"
MARZBAN_URL = "http://144.31.26.98:7575"
MARZBAN_ADMIN_USERNAME = "admin"
MARZBAN_ADMIN_PASSWORD = "sS6cE4xO5wdK"

# --- КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ ---
DATABASE_NAME = "anolyte_vpn_bot.db"

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ЦЕНЫ В STARS (XTR) ---
STARS_PRICES = {
    7: 17,    # 30 RUB -> 17 Stars
    14: 31,  # 55 RUB -> 31 Stars
    30: 57  # 100 RUB -> 57 Stars
}
PLANS_INFO = {
    7: "7 дней (Пробный)",
    14: "14 дней (Оптимальный)",
    30: "30 дней (Выгодный)"
}

# --- ПРОМОКОДЫ (Теперь без 'used_by', которое хранится в БД) ---
# Формат: {код: {'type': 'days'/'hours', 'value': int, 'max_uses': int}}
PROMO_CODES = {
    "ANOLYTE7": {'type': 'days', 'value': 7, 'max_uses': 50},
    "TRIAL48H": {'type': 'hours', 'value': 48, 'max_uses': 100},
}

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ===============================================
# === ЛОГИКА ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ (SQLite) ===
# ===============================================

def init_db():
    """Инициализирует базу данных и создает необходимые таблицы."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    # Таблица для отслеживания использованных промокодов (гарантирует однократное использование)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            promo_code TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            -- Ограничение UNIQUE обеспечивает, что один chat_id может использовать один код только 1 раз
            UNIQUE(chat_id, promo_code)
        )
    """)

    # Таблица для отслеживания общего количества активаций промокода
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promo_counts (
            promo_code TEXT PRIMARY KEY,
            uses INTEGER DEFAULT 0
        )
    """)
    
    conn.commit()
    conn.close()
    logging.info("База данных SQLite инициализирована.")

def is_promo_used_by_user(chat_id, code):
    """Проверяет, использовал ли пользователь данный промокод."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM promo_uses WHERE chat_id = ? AND promo_code = ?", 
        (chat_id, code.upper())
    )
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_promo_total_uses(code):
    """Получает текущее общее количество использований промокода."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT uses FROM promo_counts WHERE promo_code = ?", 
        (code.upper(),)
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def record_promo_use(chat_id, code):
    """Записывает использование промокода пользователем и обновляет счетчик."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    code_upper = code.upper()

    try:
        # 1. Запись использования пользователем (таблица promo_uses)
        cursor.execute(
            "INSERT INTO promo_uses (chat_id, promo_code) VALUES (?, ?)", 
            (chat_id, code_upper)
        )
        
        # 2. Обновление общего счетчика (таблица promo_counts)
        cursor.execute(
            """
            INSERT INTO promo_counts (promo_code, uses) 
            VALUES (?, 1) 
            ON CONFLICT(promo_code) DO UPDATE SET uses = uses + 1
            """, 
            (code_upper,)
        )
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Ошибка записи использования промокода в БД: {e}")
        raise e
    finally:
        conn.close()


# ===============================================
# === ЛОГИКА ДЛЯ РАБОТЫ С MARZBAN API (Исправлен PUT/POST) ===
# ===============================================

def get_marzban_token():
    """Получает токен для доступа к Marzban API."""
    url = f"{MARZBAN_URL}/api/admin/token"
    data = {
        "username": MARZBAN_ADMIN_USERNAME,
        "password": MARZBAN_ADMIN_PASSWORD
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    
    try:
        response = requests.post(url, data=data, headers=headers)
        response.raise_for_status()
        return response.json()["access_token"]
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка получения токена Marzban: {e}")
        return None

def create_marzban_user(token, chat_id, days=0, hours=0):
    """Создает нового пользователя в Marzban (используется POST)."""
    if not token:
        raise Exception("Отсутствует токен Marzban.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    url = f"{MARZBAN_URL}/api/user"

    username = f"user_{chat_id}"
    
    delta = timedelta(days=days, hours=hours)
    if delta.total_seconds() == 0:
        delta = timedelta(hours=1)
        
    expire_date = int((datetime.now() + delta).timestamp())
    
    user_data = {
        "username": username,
        "proxies": {
            "vless": {
                "flow": "xtls-rprx-vision",
                "id": secrets.token_hex(16)
            }
        },
        "inbounds": {
            "vless": ["VLESS TCP REALITY"]
        },
        "expire": expire_date,
        "data_limit": 0,  # Безлимитный трафик
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": f"chat_id:{chat_id},plan:{days}d/{hours}h"
    }
    
    response = requests.post(url, json=user_data, headers=headers)
    response.raise_for_status()
    return response.json()

def update_marzban_user_expiry(token, username, days=0, hours=0):
    """Обновляет срок действия существующего пользователя в Marzban (используется PUT)."""
    if not token:
        raise Exception("Отсутствует токен Marzban.")
        
    user_info = get_marzban_user_info(token, username)
    if not user_info:
        raise Exception("Пользователь не найден.")
        
    current_expire = user_info.get('expire', 0)
    
    now = datetime.now()
    delta = timedelta(days=days, hours=hours)
    
    if current_expire <= now.timestamp():
        new_expire_dt = now + delta
    else:
        current_expire_dt = datetime.fromtimestamp(current_expire)
        new_expire_dt = current_expire_dt + delta

    new_expire_timestamp = int(new_expire_dt.timestamp())

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    url = f"{MARZBAN_URL}/api/user/{username}"

    update_data = {
        "expire": new_expire_timestamp,
        "status": "active"
    }
    
    # ИСПРАВЛЕНО: Используем PUT для обновления существующего пользователя в Marzban
    response = requests.put(url, json=update_data, headers=headers)
    response.raise_for_status()
    return response.json()

def get_marzban_user_info(token, username):
    """Получает данные пользователя."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    url = f"{MARZBAN_URL}/api/user/{username}"
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        if response.status_code == 404:
            return None
        raise e

def get_user_direct_configs(token, username):
    """Получает прямые конфигурации пользователя."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    
    url = f"{MARZBAN_URL}/api/user/{username}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    user_data = response.json()
    
    configs = {}
    server_host = "144.31.26.98" # Ваш реальный IP/домен

    if 'vless' in user_data.get('proxies', {}):
        try:
            vless_id = user_data['proxies']['vless']['id']
            # Внимание: pbk и sid должны быть актуальными значениями с вашего сервера!
            vless_config = f"vless://{vless_id}@{server_host}:443?type=tcp&security=reality&sni=yandex.ru&fp=chrome&pbk=dj139oX4r0gST5x-39bu8w9wf4hU0nrbPROOxnIeAFc&sid=c6c32c77180cae07&flow=xtls-rprx-vision#VPN-{username}"
            configs['vless'] = vless_config
        except Exception as e:
            logging.error(f"Ошибка генерации VLESS: {e}")
    
    return configs

# ===============================================
# === ЛОГИКА ДЛЯ ПРОМОКОДОВ (Использует БД) ===
# ===============================================

def is_promo_valid(code, chat_id):
    """Проверяет валидность промокода, используя БД для проверки использования."""
    code = code.upper()
    if code not in PROMO_CODES:
        return False, "Промокод не найден."
        
    promo_data = PROMO_CODES[code]
    
    # 1. Проверка на однократное использование (через БД)
    if is_promo_used_by_user(chat_id, code):
        return False, "Вы уже использовали этот промокод."
        
    # 2. Проверка на лимит активаций (через БД)
    current_uses = get_promo_total_uses(code)
    if current_uses >= promo_data['max_uses']:
        return False, "Срок действия промокода истек (закончились активации)."
        
    return True, promo_data

def activate_promo(code, chat_id, promo_data):
    """Активирует промокод, добавляя время к подписке и записывая использование в БД."""
    
    token = get_marzban_token()
    if not token:
        raise Exception("Не удалось получить токен Marzban.")
        
    username = f"user_{chat_id}"
    user_exists = get_marzban_user_info(token, username) is not None
    
    promo_type = promo_data['type']
    promo_value = promo_data['value']
    
    if promo_type == 'days':
        days = promo_value
        hours = 0
        time_str = f"{days} дней"
    elif promo_type == 'hours':
        days = 0
        hours = promo_value
        time_str = f"{hours} часов"
    else:
        raise ValueError("Неизвестный тип промокода.")

    if user_exists:
        # Пользователь уже есть, продлеваем (используется PUT в update_marzban_user_expiry)
        update_marzban_user_expiry(token, username, days, hours)
    else:
        # Пользователя нет, создаем (используется POST)
        create_marzban_user(token, chat_id, days, hours)
        
    # Отмечаем как использованный в БД
    record_promo_use(chat_id, code)
    
    return time_str

# ===============================================
# === ФОНОВАЯ ПРОВЕРКА ПОДПИСОК ===
# ===============================================

def check_subscriptions():
    """Проверяет статус подписок пользователей и отправляет уведомления."""
    try:
        token = get_marzban_token()
        if not token:
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        url = f"{MARZBAN_URL}/api/users"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        users = response.json().get("users", [])
        
        current_time = int(datetime.now().timestamp())
        
        for user in users:
            status = user.get("status")
            expire = user.get("expire", 0)
            note = user.get("note", "")
            
            chat_id = None
            if note and "chat_id:" in note:
                try:
                    chat_id = int(note.split("chat_id:")[1].split(",")[0])
                except (IndexError, ValueError):
                    continue
            
            if not chat_id:
                continue
            
            # Проверяем, истекла ли подписка
            if expire > 0 and expire <= current_time:
                bot.send_message(
                    chat_id=chat_id,
                    text="Ваша подписка истекла. Используйте /choose_plan для создания новой."
                )
            elif status in ["disabled", "expired"]:
                bot.send_message(
                    chat_id=chat_id,
                    text="Ваша подписка была отменена или истекла. Используйте /choose_plan для создания новой."
                )
    
    except Exception as e:
        logging.error(f"Ошибка в check_subscriptions: {e}")

def run_scheduler():
    """Запускает планировщик в отдельном потоке."""
    schedule.every(24).hours.do(check_subscriptions) 
    while True:
        schedule.run_pending()
        time.sleep(1)

# ===============================================
# === ОБРАБОТЧИКИ TELEBOT (Исправлено форматирование ошибок) ===
# ===============================================

def get_main_menu_keyboard():
    """Возвращает инлайн-клавиатуру главного меню."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("💳 Выбрать подписку (Stars)", callback_data="show_plans"),
        types.InlineKeyboardButton("🔑 Активировать промокод", callback_data="activate_promo"),
        types.InlineKeyboardButton("❓ Помощь / Поддержка", url="https://t.me/PathOfAnolyte")
    )
    return keyboard

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Отправляет приветственное сообщение с инлайн-меню."""
    welcome_text = (
        "👋 **Anolyte VPN - Ваш надежный VPN-помощник**\n\n"
        "Я помогу вам создать быстрое и безлимитное VPN подключение.\n\n"
        "Выберите действие в меню ниже:"
    )
    
    bot.send_message(
        message.chat.id, 
        welcome_text, 
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == 'show_plans')
def show_plans_callback(call):
    """Открывает меню выбора плана по инлайн-кнопке."""
    bot.answer_callback_query(call.id)
    choose_plan(call.message)

@bot.message_handler(commands=['choose_plan'])
def choose_plan(message):
    """Предлагает пользователю выбрать срок подписки с оплатой Stars (XTR)."""
    
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    
    for days, price in STARS_PRICES.items():
        label = PLANS_INFO[days]
        callback_data = f"pay_{days}"
        keyboard.add(types.InlineKeyboardButton(
            text=f"{label} - {price} ⭐ (XTR)", 
            callback_data=callback_data
        ))

    keyboard.add(types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="go_to_main_menu"))
    
    # Определяем, какой метод использовать: edit_message_text или send_message
    if hasattr(message, 'text') and message.text in ['/choose_plan', '/start', '/help']:
         # Если команда, отправляем новое сообщение
        bot.send_message(
            message.chat.id,
            "Выберите срок безлимитной подписки:\n\n"
            "Оплата производится через внутреннюю валюту Telegram - **Stars (XTR)**.\n\n"
            "Нажмите на выбранный план для выставления счета:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        # Если callback, редактируем существующее
        bot.edit_message_text(
            "Выберите срок безлимитной подписки:\n\n"
            "Оплата производится через внутреннюю валюту Telegram - **Stars (XTR)**.\n\n"
            "Нажмите на выбранный план для выставления счета:",
            message.chat.id,
            message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data == 'go_to_main_menu')
def go_to_main_menu_callback(call):
    """Возвращает пользователя в главное меню."""
    bot.answer_callback_query(call.id)
    send_welcome(call.message)

@bot.callback_query_handler(func=lambda call: call.data == 'activate_promo')
def request_promo_code(call):
    """Просит пользователя ввести промокод."""
    bot.answer_callback_query(call.id)
    # Редактируем сообщение, чтобы избежать "Message is not modified"
    try:
        msg = bot.edit_message_text(
            "Введите ваш **промокод**:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )
    except apihelper.ApiTelegramException:
        # Если не удалось отредактировать (напр., слишком старое сообщение), отправляем новое
        msg = bot.send_message(
            call.message.chat.id, 
            "Введите ваш **промокод**:", 
            parse_mode="Markdown"
        )
    # Регистрируем следующий шаг для обработки введенного промокода
    bot.register_next_step_handler(msg, process_promo_code_input)

def process_promo_code_input(message):
    """Обрабатывает введенный пользователем промокод."""
    try:
        code = message.text.strip().upper()
        chat_id = message.chat.id
        
        is_valid, promo_data = is_promo_valid(code, chat_id)
        
        if not is_valid:
            bot.send_message(chat_id, f"❌ **Ошибка:** {promo_data}", parse_mode="Markdown")
            return
            
        bot.send_message(chat_id, f"✅ Промокод **{code}** найден! Активирую подписку...", parse_mode="Markdown")
        
        # Активация
        time_added = activate_promo(code, chat_id, promo_data)
        
        # Получение конфигураций
        username = f"user_{chat_id}"
        token = get_marzban_token()
        configs = get_user_direct_configs(token, username)
        
        # Формируем сообщение
        config_message = (
            f"🎉 Промокод **{code}** успешно активирован!\n"
            f"Ваша подписка продлена/создана на **{time_added}**.\n\n"
        )
        
        if 'vless' in configs and configs['vless']:
            config_message += "**VLESS + Reality (основной ключ):**\n"
            config_message += f"```\n{configs['vless']}\n```\n\n"
        
        config_message += (
            "**Как использовать:**\n"
            "1. Скопируйте конфигурацию выше.\n"
            "2. Импортируйте ключ в VPN-клиент (например, V2RayTun).\n"
            "3. Подключитесь!\n\n"
            f"**Ваш текущий логин:** `{username}`"
        )

        # Клавиатура для возврата
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="go_to_main_menu"))
        
        bot.send_message(chat_id, config_message, parse_mode="Markdown", reply_markup=keyboard)
        
    except Exception as e:
        logging.error(f"Ошибка при активации промокода: {e}")
        # ИСПРАВЛЕНО: Заключаем детали ошибки в блок кода для предотвращения ошибки парсинга Markdown
        bot.send_message(
            message.chat.id, 
            f"❌ **Критическая ошибка** при активации. Свяжитесь с поддержкой. \n\nДетали ошибки:\n```\n{e}\n```", 
            parse_mode="Markdown"
        )
        
@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def process_payment_selection(call):
    """Обрабатывает выбор плана и отправляет инвойс для оплаты Stars (XTR)."""
    try:
        days = int(call.data.split('_')[1])
        price = STARS_PRICES[days]
        
        title = f"VPN-подписка на {days} дней"
        description = f"Безлимитный трафик на {days} дней (VLESS/Reality)"
        payload = f"vpn_plan_{days}_{call.message.chat.id}"
        
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title=title,
            description=description,
            invoice_payload=payload,
            provider_token="",  # Пустой для Stars
            currency="XTR",  # Валюта Stars
            prices=[types.LabeledPrice(label=f"VPN-подписка {days} дней", amount=price)],
            start_parameter=f"vpn_{days}"
        )
        
        bot.answer_callback_query(call.id, f"Отправлен счёт на {price} ⭐")
        
    except Exception as e:
        logging.error(f"Ошибка при создании счёта: {e}")
        bot.send_message(call.message.chat.id, f"Ошибка при создании счёта: {e}")
        bot.answer_callback_query(call.id)

@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout_query(pre_checkout_query):
    """Подтверждает платёж (Stars) перед списанием."""
    try:
        # Всегда подтверждаем для Stars
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        logging.error(f"Pre-checkout error: {e}")
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Ошибка обработки платежа")

@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    """Обрабатывает успешную оплату и создаёт/продлевает VPN-пользователя."""
    try:
        # Извлекаем данные из payload
        payload = message.successful_payment.invoice_payload
        parts = payload.split("_")
        days = int(parts[2])
        chat_id = int(parts[3])
        username = f"user_{chat_id}"
        
        bot.reply_to(message, "Оплата прошла успешно! Обрабатываю подписку...")
        
        # Получаем токен Marzban
        marzban_token = get_marzban_token()
        if not marzban_token:
            raise Exception("Не удалось получить токен Marzban.")

        # Проверяем, существует ли пользователь
        user_exists = get_marzban_user_info(marzban_token, username) is not None
        
        if user_exists:
            update_marzban_user_expiry(marzban_token, username, days=days)
            action_text = "Продление подписки выполнено успешно!"
        else:
            create_marzban_user(marzban_token, chat_id, days=days)
            action_text = "Пользователь создан успешно!"
            
        bot.send_message(chat_id, action_text)
        
        # Получаем конфигурации
        bot.send_message(chat_id, "Получаю конфигурации...")
        
        configs = get_user_direct_configs(marzban_token, username)
        
        # Формируем сообщение с конфигурациями
        config_message = "Ваши VPN конфигурации готовы!\n\n"
        
        if 'vless' in configs and configs['vless']:
            config_message += "**VLESS + Reality (основной ключ):**\n"
            config_message += f"```\n{configs['vless']}\n```\n\n"
        
        # Инструкция по использованию
        config_message += (
            "**Как использовать:**\n"
            "1. Скопируйте конфигурацию выше.\n"
            "2. Откройте VPN-клиент (например, V2RayTun).\n"
            "3. Импортируйте ключ в клиент.\n"
            "4. Подключитесь и наслаждайтесь безлимитным интернетом!\n\n"
            f"• Срок: {days} дней добавлено (продлено)\n"
            f"• Имя пользователя: `{username}`\n\n"
            "Если возникнут вопросы, обращайтесь: [Поддержка](https://t.me/PathOfAnolyte)"
        )
        
        # Клавиатура для возврата
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="go_to_main_menu"))
        
        bot.send_message(chat_id, config_message, parse_mode="Markdown", reply_markup=keyboard)
        
    except Exception as e:
        logging.error(f"Ошибка после оплаты: {e}")
        # ИСПРАВЛЕНО: Заключаем детали ошибки в блок кода
        bot.send_message(
            message.chat.id, 
            f"❌ **Ошибка после оплаты:** Произошла ошибка при создании/продлении ключа. Свяжитесь с поддержкой.\n\nДетали ошибки:\n```\n{e}\n```", 
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['promo_activate'])
def command_promo_activate(message):
    """Запускает процесс активации промокода."""
    msg = bot.send_message(
        message.chat.id, 
        "Введите ваш **промокод**:", 
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_promo_code_input)

@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    """Обрабатывает все остальные текстовые сообщения."""
    welcome_text = (
        "Не понимаю ваше сообщение. Выберите действие в **Главном меню**."
    )
    bot.reply_to(message, welcome_text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

# --- ЗАПУСК БОТА И ПЛАНИРОВЩИКА ---

if __name__ == '__main__':
    # 1. Инициализация базы данных
    init_db()
    
    # 2. Запуск планировщика в отдельном потоке
    checker_thread = threading.Thread(target=run_scheduler)
    checker_thread.daemon = True
    checker_thread.start()
    
    logging.info("Бот запускается...")
    # 3. Запуск Long Polling
    bot.remove_webhook() 
    bot.infinity_polling(timeout=10, long_polling_timeout=10)
