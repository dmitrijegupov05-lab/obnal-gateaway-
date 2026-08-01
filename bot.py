import os
import time
import json
import requests
import telebot
from supabase import create_client, Client
from datetime import datetime

# === КОНФИГУРАЦИЯ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", 0))

if not all([TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_KEY, OWNER_ID]):
    raise Exception("❌ Не все секреты заданы в GitHub Secrets")

# === ИНИЦИАЛИЗАЦИЯ ===
bot = telebot.TeleBot(TELEGRAM_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_client(tg_id):
    res = supabase.table("clients").select("*").eq("tg_id", tg_id).execute()
    return res.data[0] if res.data else None

def create_client(tg_id, username):
    data = {"tg_id": tg_id, "username": username}
    res = supabase.table("clients").insert(data).execute()
    return res.data[0] if res.data else None

def generate_deposit(amount, country):
    payload = {
        "from": "USDT",
        "to": "XMR",
        "amount": float(amount),
        "address": "TN886Bm2JUx88P3BEayjLRu2BsrpGfRk3S",  # ← замени на свой XMR-адрес
        "refundAddress": "TN886Bm2JUx88P3BEayjLRu2BsrpGfRk3S"  # ← замени на свой USDT-адрес
    }
    try:
        r = requests.post("https://api.simpleswap.io/v1/create_order", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def check_order_status(order_id):
    try:
        r = requests.get(f"https://api.simpleswap.io/v1/get_order/{order_id}", timeout=30)
        return r.json()
    except:
        return {"status": "error"}

# === КОМАНДЫ БОТА ===
@bot.message_handler(commands=['start'])
def cmd_start(message):
    tg_id = message.from_user.id
    username = message.from_user.username or "unknown"
    client = get_client(tg_id)
    if not client:
        create_client(tg_id, username)
    bot.reply_to(
        message,
        "⚡ Обнальный шлюз v8.0\n\n"
        "Доступные команды:\n"
        "/new <сумма> <страна> — создать заявку (пример: /new 500 Россия)\n"
        "/confirm — подтвердить оплату\n"
        "/status — статус последней заявки\n"
        "/support — связаться с оператором"
    )

@bot.message_handler(commands=['new'])
def cmd_new(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "⚠️ Формат: /new 500 Россия")
        return
    
    try:
        amount = float(args[1])
        country = " ".join(args[2:])
        tg_id = message.from_user.id
        client = get_client(tg_id)
        
        if not client:
            client = create_client(tg_id, message.from_user.username or "unknown")
        
        # Генерация депозита через SimpleSwap
        swap = generate_deposit(amount, country)
        if "error" in swap:
            bot.reply_to(message, f"❌ Ошибка API: {swap['error']}")
            return
        
        deposit_addr = swap.get("payinAddress")
        mixer_id = swap.get("id")
        
        if not deposit_addr:
            bot.reply_to(message, "❌ Не удалось получить адрес для депозита")
            return
        
        # Сохранение заявки в Supabase
        order_data = {
            "client_id": client["id"],
            "amount_usdt": amount,
            "country": country,
            "deposit_address": deposit_addr,
            "mixer_order_id": mixer_id,
            "status": "waiting_payment"
        }
        res = supabase.table("orders").insert(order_data).execute()
        
        if res.data:
            bot.reply_to(
                message,
                f"✅ Заявка создана!\n\n"
                f"💵 Сумма: {amount} USDT\n"
                f"🌍 Страна: {country}\n"
                f"📥 Адрес для депозита:\n`{deposit_addr}`\n\n"
                f"После отправки средств нажмите /confirm"
            )
        else:
            bot.reply_to(message, "❌ Ошибка сохранения заявки")
            
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")

@bot.message_handler(commands=['confirm'])
def cmd_confirm(message):
    tg_id = message.from_user.id
    client = get_client(tg_id)
    
    if not client:
        bot.reply_to(message, "⚠️ Сначала создайте заявку через /new")
        return
    
    # Поиск активной заявки
    res = supabase.table("orders") \
        .select("*") \
        .eq("client_id", client["id"]) \
        .eq("status", "waiting_payment") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()
    
    if not res.data:
        bot.reply_to(message, "⚠️ Нет активных заявок на подтверждение")
        return
    
    order = res.data[0]
    
    # Проверка статуса в SimpleSwap
    status_data = check_order_status(order["mixer_order_id"])
    status = status_data.get("status", "unknown")
    
    if status == "finished":
        # Обновление статуса в БД
        supabase.table("orders") \
            .update({"status": "ready"}) \
            .eq("id", order["id"]) \
            .execute()
        
        bot.reply_to(message, "✅ Платёж подтверждён! Ожидайте вывода в течение 12 часов.")
        
        # Уведомление оператору
        bot.send_message(
            OWNER_ID,
            f"🔔 Новая заявка #{order['id']}\n"
            f"💰 Сумма: {order['amount_usdt']} USDT\n"
            f"🌍 Страна: {order['country']}\n"
            f"👤 Клиент: @{message.from_user.username or 'unknown'}"
        )
    elif status in ["waiting", "confirming", "exchanging"]:
        bot.reply_to(message, f"⏳ Транзакция в обработке. Статус: {status}\nПопробуйте через 5-10 минут.")
    else:
        bot.reply_to(message, f"⚠️ Неизвестный статус: {status}\nСвяжитесь с оператором /support")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    tg_id = message.from_user.id
    client = get_client(tg_id)
    
    if not client:
        bot.reply_to(message, "⚠️ У вас нет заявок")
        return
    
    res = supabase.table("orders") \
        .select("*") \
        .eq("client_id", client["id"]) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()
    
    if not res.data:
        bot.reply_to(message, "⚠️ У вас нет заявок")
        return
    
    order = res.data[0]
    status_map = {
        "waiting_payment": "⏳ Ожидание оплаты",
        "ready": "✅ Готов к выводу",
        "assigned": "🔄 Назначен дроп",
        "completed": "✅ Завершено",
        "failed": "❌ Ошибка"
    }
    
    bot.reply_to(
        message,
        f"📋 Последняя заявка:\n"
        f"ID: {order['id']}\n"
        f"Сумма: {order['amount_usdt']} USDT\n"
        f"Страна: {order['country']}\n"
        f"Статус: {status_map.get(order['status'], order['status'])}\n"
        f"Дата: {order['created_at']}"
    )

@bot.message_handler(commands=['support'])
def cmd_support(message):
    bot.reply_to(message, "👤 Свяжитесь с оператором: @YOUR_TELEGRAM_USERNAME")

# === КОМАНДЫ ДЛЯ ОПЕРАТОРА ===
@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Доступ запрещён")
        return
    
    bot.reply_to(
        message,
        "🛠 Панель оператора:\n"
        "/orders — список готовых заявок\n"
        "/assign <order_id> <drop_id> — назначить дропа\n"
        "/complete <order_id> — закрыть заявку"
    )

@bot.message_handler(commands=['orders'])
def cmd_orders(message):
    if message.from_user.id != OWNER_ID:
        return
    
    res = supabase.table("orders") \
        .select("*, clients(username)") \
        .eq("status", "ready") \
        .execute()
    
    if not res.data:
        bot.reply_to(message, "📭 Нет готовых заявок")
        return
    
    text = "📋 Заявки, готовые к выводу:\n\n"
    for o in res.data:
        text += f"#{o['id']} | {o['amount_usdt']} USDT | {o['country']} | @{o['clients']['username']}\n"
    
    bot.reply_to(message, text)

@bot.message_handler(commands=['assign'])
def cmd_assign(message):
    if message.from_user.id != OWNER_ID:
        return
    
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "⚠️ Формат: /assign <order_id> <drop_id>")
        return
    
    order_id = int(args[1])
    drop_id = int(args[2])
    
    # Проверка существования дропа
    drop_res = supabase.table("drops").select("*").eq("id", drop_id).execute()
    if not drop_res.data:
        bot.reply_to(message, "❌ Дроп не найден")
        return
    
    # Назначение дропа
    supabase.table("orders") \
        .update({"drop_id": drop_id, "status": "assigned"}) \
        .eq("id", order_id) \
        .execute()
    
    bot.reply_to(message, f"✅ Дроп #{drop_id} назначен на заявку #{order_id}")

@bot.message_handler(commands=['complete'])
def cmd_complete(message):
    if message.from_user.id != OWNER_ID:
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ Формат: /complete <order_id>")
        return
    
    order_id = int(args[1])
    supabase.table("orders") \
        .update({"status": "completed"}) \
        .eq("id", order_id) \
        .execute()
    
    bot.reply_to(message, f"✅ Заявка #{order_id} завершена")

# === ЗАПУСК БОТА ===
if __name__ == "__main__":
    print("🚀 Бот запускается...")
    print(f"✅ Токен: {TELEGRAM_TOKEN[:10]}...")
    print(f"✅ Supabase URL: {SUPABASE_URL}")
    print(f"✅ Owner ID: {OWNER_ID}")
    
    try:
        bot.remove_webhook()
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
