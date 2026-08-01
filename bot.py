import os
import time
import telebot
import requests
from supabase import create_client
from datetime import datetime

# === КОНФИГ ===
TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = int(os.getenv("OWNER_ID"))

bot = telebot.TeleBot(TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_or_create_client(tg_id, username):
    client = supabase.table("clients").select("*").eq("tg_id", tg_id).execute()
    if not client.data:
        supabase.table("clients").insert({"tg_id": tg_id, "username": username}).execute()
        return supabase.table("clients").select("*").eq("tg_id", tg_id).execute().data[0]
    return client.data[0]

def generate_deposit(amount_usdt):
    payload = {
        "from": "USDT",
        "to": "XMR",
        "amount": amount_usdt,
        "address": "твой_кошелек_XMR_из_TrustWallet",
        "refundAddress": "твой_кошелек_USDT_на_случай_возврата"
    }
    r = requests.post("https://api.simpleswap.io/v1/create_order", json=payload)
    return r.json()

# === ОБРАБОТЧИКИ КОМАНД ===
@bot.message_handler(commands=['start'])
def start(message):
    get_or_create_client(message.from_user.id, message.from_user.username)
    bot.reply_to(message,
        "🟢 Обнальный шлюз v8.0\n\n"
        "Доступные команды:\n"
        "/new <сумма> <страна> — создать заявку\n"
        "/confirm — подтвердить оплату\n"
        "/status — проверить статус заявки\n"
        "/support — связаться с оператором"
    )

@bot.message_handler(commands=['new'])
def new_order(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "⚠️ Формат: /new 1000 USDT Россия")
        return

    try:
        amount = float(args[1])
        country = " ".join(args[2:])

        # Генерация депозита
        swap = generate_deposit(amount)
        deposit_addr = swap['payinAddress']
        mixer_id = swap['id']

        # Сохраняем заявку
        client = get_or_create_client(message.from_user.id, message.from_user.username)
        supabase.table("orders").insert({
            "client_id": client['id'],
            "amount_usdt": amount,
            "country": country,
            "deposit_address": deposit_addr,
            "mixer_order_id": mixer_id,
            "status": "waiting_payment"
        }).execute()

        bot.reply_to(message,
            f"✅ Заявка создана!\n\n"
            f"Сумма: {amount} USDT\n"
            f"Страна: {country}\n"
            f"Адрес для депозита:\n`{deposit_addr}`\n\n"
            f"После отправки нажмите /confirm"
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")

@bot.message_handler(commands=['confirm'])
def confirm_payment(message):
    client = get_or_create_client(message.from_user.id, message.from_user.username)
    order = supabase.table("orders").select("*").eq("client_id", client['id']).eq("status", "waiting_payment").execute()

    if not order.data:
        bot.reply_to(message, "⚠️ Нет активных заявок на подтверждение.")
        return

    order = order.data[0]
    mixer_id = order['mixer_order_id']

    # Проверка статуса в SimpleSwap
    check = requests.get(f"https://api.simpleswap.io/v1/get_order/{mixer_id}").json()

    if check['status'] == 'finished':
        supabase.table("orders").update({"status": "ready"}).eq("id", order['id']).execute()
        bot.reply_to(message, "✅ Средства получены! Ожидайте вывода в течение 12 часов.")
        bot.send_message(OWNER_ID, f"🔔 Заявка #{order['id']} готова к выводу. Сумма: {order['amount_usdt']} USDT, страна: {order['country']}")
    else:
        bot.reply_to(message, f"⏳ Транзакция ещё не завершена. Текущий статус: {check['status']}\nПопробуйте через 5 минут.")

@bot.message_handler(commands=['status'])
def order_status(message):
    client = get_or_create_client(message.from_user.id, message.from_user.username)
    orders = supabase.table("orders").select("*").eq("client_id", client['id']).order("created_at", desc=True).limit(1).execute()

    if not orders.data:
        bot.reply_to(message, "У вас нет активных заявок.")
        return

    order = orders.data[0]
    status_map = {
        "waiting_payment": "⏳ Ожидание оплаты",
        "ready": "✅ Средства получены, ожидание вывода",
        "assigned": "🔄 Назначен дроп",
        "completed": "✅ Завершено"
    }
    bot.reply_to(message,
        f"📋 Ваша последняя заявка:\n"
        f"ID: {order['id']}\n"
        f"Сумма: {order['amount_usdt']} USDT\n"
        f"Страна: {order['country']}\n"
        f"Статус: {status_map.get(order['status'], order['status'])}"
    )

@bot.message_handler(commands=['support'])
def support(message):
    bot.reply_to(message, f"👤 Свяжитесь с оператором: @ваш_логин_в_телеграм")

# === КОМАНДЫ ДЛЯ ОПЕРАТОРА ===
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != OWNER_ID:
        return
    bot.reply_to(message,
        "🛠 Панель оператора:\n"
        "/orders — список всех заявок\n"
        "/assign <id_заявки> <id_дропа> — назначить дропа\n"
        "/complete <id_заявки> — закрыть заявку\n"
        "/broadcast <текст> — рассылка клиентам"
    )

@bot.message_handler(commands=['orders'])
def list_orders(message):
    if message.from_user.id != OWNER_ID:
        return
    orders = supabase.table("orders").select("*, clients(username)").eq("status", "ready").execute()
    if not orders.data:
        bot.reply_to(message, "Нет готовых заявок.")
        return
    text = "📋 Готовые к выводу:\n"
    for o in orders.data:
        text += f"#{o['id']} | {o['amount_usdt']} USDT | {o['country']} | клиент: @{o['clients']['username']}\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if message.from_user.id != OWNER_ID:
        return
    text = message.text.replace("/broadcast ", "")
    clients = supabase.table("clients").select("tg_id").execute()
    for c in clients.data:
        try:
            bot.send_message(c['tg_id'], f"📢 Анонс: {text}")
            time.sleep(0.1)
        except:
            pass
    bot.reply_to(message, f"✅ Рассылка выполнена для {len(clients.data)} клиентов.")

# === ЗАПУСК ===
if __name__ == "__main__":
    bot.infinity_polling()
