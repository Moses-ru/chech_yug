from flask import Flask, request, jsonify, send_from_directory
import yaml
from sheets import SheetsDB
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import threading
import time
import os

# Загружаем конфигурацию
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# Инициализируем бота и базу данных
bot = Bot(token=config['telegram_token'])
sheets = SheetsDB()

app = Flask(__name__, static_folder='static')

# Храним активные сессии пользователей
active_sessions = {}

# Создаём асинхронный цикл для уведомлений
loop = None

# ==================== МАРШРУТЫ API ====================

@app.route('/')
def serve_index():
    """Сервируем главную страницу"""
    return send_from_directory('static', 'index.html')

@app.route('/api/login', methods=['POST'])
def login():
    """Проверка логина/пароля из таблицы 'Аккаунты'"""
    data = request.json
    login = data.get('login', '').strip()
    password = data.get('password', '').strip()
    
    if not login or not password:
        return jsonify({'success': False, 'error': 'Логин и пароль обязательны'}), 400
    
    # Проверяем в таблице
    account = sheets.check_account(login, password)
    
    if account:
        # Регистрируем в таблице 'Сотрудники' если ещё не зарегистрирован
        tg_id = data.get('tg_id', f'web_{int(time.time())}')
        sheets.register_user(tg_id, account['name'], account['role'], account.get('location', 'Сургут'))
        
        return jsonify({
            'success': True,
            'user': {
                'tg_id': tg_id,
                'name': account['name'],
                'role': account['role'],
                'location': account.get('location', 'Сургут')
            }
        })
    else:
        return jsonify({'success': False, 'error': 'Неверный логин или пароль'}), 401

@app.route('/api/check_telegram', methods=['POST'])
def check_telegram():
    """Проверка пользователя из параметров URL"""
    data = request.json
    tg_id = data.get('tg_id')
    
    if not tg_id:
        return jsonify({'success': False, 'error': 'tg_id обязателен'}), 400
    
    # Ищем пользователя в таблице 'Сотрудники'
    user = sheets.get_user_by_tg_id(tg_id)
    
    if user:
        return jsonify({
            'success': True,
            'user': {
                'tg_id': tg_id,
                'name': user['name'],
                'role': user['role'],
                'location': user.get('location', 'Сургут')
            }
        })
    else:
        return jsonify({'success': False, 'error': 'Пользователь не найден. Сначала войдите через /start в боте'}), 404

@app.route('/api/employees', methods=['GET'])
def get_employees():
    """Получение списка всех сотрудников"""
    all_users = sheets.get_all_users()
    
    # Фильтруем только активных сотрудников
    employees = [
        {
            'id': u.get('tg_id', ''),
            'name': u.get('name', ''),
            'role': u.get('role', ''),
            'role_name': get_role_name(u.get('role', ''))
        }
        for u in all_users
        if u.get('status') == 'active'
    ]
    
    return jsonify({'success': True, 'employees': employees})

@app.route('/api/tasks', methods=['POST'])
def create_task():
    """Создание задачи и отправка уведомления в Telegram через бота"""
    data = request.json
    
    required_fields = ['sender_tg_id', 'recipient_ids', 'title', 'zone', 'priority']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'success': False, 'error': f'Поле {field} обязательно'}), 400
    
    # Создаём задачу для каждого получателя
    task_ids = []
    for recipient_id in data['recipient_ids']:
        task_id = sheets.create_task(
            sender_tg_id=data['sender_tg_id'],
            recipient_tg_id=recipient_id,
            title=data['title'],
            description=data.get('description', data['title']),
            deadline=data.get('deadline', '18:00'),
            priority=data['priority'],
            zone=data['zone']
        )
        task_ids.append(task_id)
        
        # Отправляем уведомление получателю через бота (асинхронно)
        if loop:
            asyncio.run_coroutine_threadsafe(
                send_task_notification(
                    bot,
                    data['sender_tg_id'],
                    recipient_id,
                    data['title'],
                    data['zone'],
                    data.get('deadline', '18:00'),
                    data['priority']
                ),
                loop
            )
    
    return jsonify({'success': True, 'task_ids': task_ids})

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_role_name(role):
    """Преобразование роли в читаемое название"""
    roles = {
        'bartender': 'Бармен',
        'waiter': 'Официант',
        'cook': 'Повар',
        'bar_manager': 'Бар-менеджер',
        'floor_manager': 'Менеджер зала',
        'head_chef': 'Шеф-повар',
        'restaurant_manager': 'Управляющий'
    }
    return roles.get(role, role)

async def send_task_notification(bot, sender_tg_id, recipient_tg_id, task_title, zone, deadline, priority):
    """Отправка уведомления о новой задаче в Telegram (только для реальных пользователей)"""
    sender = sheets.get_user_by_tg_id(sender_tg_id)
    recipient = sheets.get_user_by_tg_id(recipient_tg_id)
    if not recipient:
        return False

    # Проверяем, что это реальный Telegram ID (числовой), а не веб-сессия
    try:
        chat_id = int(recipient_tg_id)
    except (ValueError, TypeError):
        print(f"⚠️ Пропуск уведомления для {recipient_tg_id}: не является валидным Telegram ID")
        return False  # Не отправляем уведомление веб-пользователям

    priority_emojis = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
    priority_names = {'high': 'Высокий', 'medium': 'Средний', 'low': 'Низкий'}

    message = (
        f"🔔 <b>Новая задача</b> от {sender['name']} ({get_role_name(sender['role'])})\n\n"
        f"📌 <b>{task_title}</b>\n"
        f"📍 Зона: {zone}\n"
        f"⏰ Срок: {deadline}\n"
        f"📊 Приоритет: {priority_emojis.get(priority, '⚪')} {priority_names.get(priority, '')}\n\n"
        f"✅ Выполните задачу и отправьте фото отчёта"
    )

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Отправить отчёт", callback_data=f"report_task_{sender_tg_id}")]
            ])
        )
        return True
    except Exception as e:
        print(f"Ошибка отправки уведомления: {e}")
        return False

# ==================== ЗАПУСК СЕРВЕРА ====================

def run_flask():
    """Запуск Flask сервера"""
    print("=" * 60)
    print("🚀 ЗАПУСК СИСТЕМЫ УПРАВЛЕНИЯ РЕСТОРАНОМ")
    print("=" * 60)
    print("🌐 Flask сервер: http://localhost:5000")
    print("📱 Для доступа с телефона:")
    print("   1. Убедитесь, что телефон в той же сети Wi-Fi")
    print("   2. Узнайте локальный IP компьютера (см. ниже)")
    print("   3. Откройте в браузере: http://ВАШ_IP:5000")
    print()
    print("💡 Как узнать локальный IP:")
    print("   Windows: ipconfig → найти 'IPv4 Address'")
    print("   Mac: ifconfig | grep 'inet ' | grep -v 127.0.0.1")
    print("=" * 60)
    
    # Показываем локальный IP
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()
    
    print(f"✅ Ваш локальный IP: http://{ip}:5000")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

def run_bot():
    """Запуск Telegram бота"""
    from bot_flask import run_bot_loop
    asyncio.run(run_bot_loop(bot, sheets))

def run_event_loop():
    """Запуск асинхронного цикла для уведомлений"""
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_forever()

if __name__ == '__main__':
    # Запускаем асинхронный цикл для уведомлений
    event_loop_thread = threading.Thread(target=run_event_loop, daemon=True)
    event_loop_thread.start()
    
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем Flask в основном потоке
    run_flask()
