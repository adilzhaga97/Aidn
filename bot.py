import os
import re
import base64
import sqlite3
from datetime import datetime, date
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from openai import AsyncOpenAI

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "ВСТАВЬ_OPENAI_КЛЮЧ")

USER_PROFILE = {
    "age": 30,
    "current_weight": 84,
    "goal_weight": 75,
    "height": 175,
    "daily_target": 2500,
    "weekly_target": 17500,
}

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
DB_PATH = "nutritionist.db"

# Хранит состояние диалога после фото: { user_id: {image_b64, dish_name, step} }
photo_pending = {}

SYSTEM_PROMPT = f"""Ты — жёсткий личный нутролог. Подопечный — мужик {USER_PROFILE['age']} лет, {USER_PROFILE['current_weight']} кг, рост {USER_PROFILE['height']} см. Цель — {USER_PROFILE['goal_weight']} кг. Дневная цель: {USER_PROFILE['daily_target']} ккал.

СТИЛЬ:
- Уложился в норму — скупо похвали
- Перебрал до 200 ккал — "Чуть перебрал, бывает"
- Перебрал 200-500 — "Слабак, не мог остановиться?"
- Перебрал 500+ — "Позор. Иди отжимайся прямо сейчас"
- Очень сильно перебрал — "Ты что, готовишься к зимней спячке?"
- Отвечай коротко, с характером, без воды

ПОДСЧЁТ ЕДЫ:
Формат: "🍽 [Название] — ~X ккал | Б:Xг Ж:Xг У:Xг"
Всегда учитывай вес порции и способ приготовления при расчёте.
"""

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS food_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, description TEXT, calories INTEGER, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT, content TEXT, created_at TEXT)""")
    conn.commit()
    conn.close()

def save_message(role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO chat_history (role, content, created_at) VALUES (?, ?, ?)",
              (role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_history(limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM chat_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def get_today_calories():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT SUM(calories) FROM food_log WHERE date = ?", (date.today().isoformat(),))
    result = c.fetchone()[0] or 0
    conn.close()
    return result

def get_week_calories():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    week = date.today().strftime("%Y-%W")
    c.execute("SELECT SUM(calories) FROM food_log WHERE strftime('%Y-%W', date) = ?", (week,))
    result = c.fetchone()[0] or 0
    conn.close()
    return result

def save_food(description, calories):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO food_log (date, description, calories, created_at) VALUES (?, ?, ?, ?)",
              (date.today().isoformat(), description, calories, datetime.now().isoformat()))
    conn.commit()
    conn.close()

async def ask_gpt(user_message, image_base64=None):
    today_cal = get_today_calories()
    week_cal = get_week_calories()
    context = f"\n[Сегодня: {today_cal} ккал из {USER_PROFILE['daily_target']} | Неделя: {week_cal} из {USER_PROFILE['weekly_target']}]"

    if image_base64:
        content = [
            {"type": "text", "text": user_message},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]
    else:
        content = user_message

    messages = [{"role": "system", "content": SYSTEM_PROMPT + context}]
    messages.extend(get_history(20))
    messages.append({"role": "user", "content": content})

    response = await client.chat.completions.create(
        model="gpt-4o", messages=messages, max_tokens=400, temperature=0.9
    )
    return response.choices[0].message.content

async def transcribe_voice(file_path):
    with open(file_path, "rb") as f:
        transcript = await client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
        )
    return transcript.text

# ─── ОБРАБОТЧИК ФОТО ─────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    user_id = update.effective_user.id

    # Скачиваем фото
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_path = f"/tmp/{photo.file_id}.jpg"
    await file.download_to_drive(file_path)
    with open(file_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    # GPT определяет блюдо по фото
    dish_response = await ask_gpt(
        "Посмотри на фото и определи что это за блюдо. Назови только название блюда, одной строкой, без калорий.",
        image_b64
    )
    dish_name = dish_response.strip()

    # Сохраняем состояние — ждём вес порции
    photo_pending[user_id] = {
        "image_b64": image_b64,
        "dish_name": dish_name,
        "step": 1
    }

    await update.message.reply_text(
        f"Вижу: {dish_name}\n\nСколько грамм? (или примерный объём — тарелка, стакан и т.д.)"
    )

# ─── ОБРАБОТЧИК ТЕКСТА ───────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    await update.message.chat.send_action("typing")

    # Если ждём уточнений после фото
    if user_id in photo_pending:
        pending = photo_pending[user_id]

        if pending["step"] == 1:
            # Получили вес — спрашиваем способ приготовления
            pending["weight"] = text
            pending["step"] = 2
            await update.message.reply_text(
                f"Понял, {text}.\n\nКак готовили? (варёное, жареное, на пару, сырое, из магазина и т.д.)"
            )
            return

        elif pending["step"] == 2:
            # Получили способ приготовления — считаем калории
            pending["cooking"] = text
            pending["step"] = 3

            dish = pending["dish_name"]
            weight = pending["weight"]
            cooking = pending["cooking"]

            prompt = f"""Блюдо: {dish}
Вес порции: {weight}
Способ приготовления: {cooking}

Посчитай точные калории и БЖУ с учётом этих данных.
Формат: "🍽 {dish} ({weight}, {cooking}) — ~X ккал | Б:Xг Ж:Xг У:Xг"
Потом кратко прокомментируй в своём стиле."""

            reply = await ask_gpt(prompt, pending["image_b64"])

            # Сохраняем калории
            cal_match = re.search(r'~?(\d{2,4})\s*ккал', reply)
            if cal_match:
                calories = int(cal_match.group(1))
                save_food(f"{dish} ({weight}, {cooking})", calories)

            save_message("user", f"[фото: {dish}, {weight}, {cooking}]")
            save_message("assistant", reply)

            # Очищаем состояние
            del photo_pending[user_id]

            await update.message.reply_text(reply)
            return

    # Обычное сообщение
    save_message("user", text)
    reply = await ask_gpt(text)

    cal_match = re.search(r'~?(\d{2,4})\s*ккал', reply)
    if cal_match:
        save_food(text, int(cal_match.group(1)))

    save_message("assistant", reply)
    await update.message.reply_text(reply)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = f"/tmp/{voice.file_id}.ogg"
    await file.download_to_drive(file_path)
    text = await transcribe_voice(file_path)
    await update.message.reply_text(f"🎙 {text}")
    save_message("user", text)
    reply = await ask_gpt(text)
    save_message("assistant", reply)
    await update.message.reply_text(reply)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_cal = get_today_calories()
    remaining = USER_PROFILE['daily_target'] - today_cal
    await update.message.reply_text(
        f"Слушай меня внимательно.\n\n"
        f"Твои параметры:\n"
        f"⚖️ Вес: {USER_PROFILE['current_weight']} кг → цель {USER_PROFILE['goal_weight']} кг\n"
        f"🎯 Норма: {USER_PROFILE['daily_target']} ккал/день\n\n"
        f"Сегодня съел: {today_cal} ккал\n"
        f"Осталось: {remaining} ккал\n\n"
        f"Что умею:\n"
        f"📸 Фото еды → спрошу граммы и способ готовки → точные калории\n"
        f"🎙 Голосовые → распознаю\n"
        f"📊 /today — сводка дня\n"
        f"📈 /week — сводка недели\n"
        f"🎯 /goal 2000 — поменять цель\n\n"
        f"Начинай отчитываться."
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_cal = get_today_calories()
    target = USER_PROFILE['daily_target']
    remaining = target - today_cal
    over = today_cal - target

    if today_cal == 0:
        comment = "Ты вообще ел сегодня? Или просто не отчитываешься?"
    elif remaining > 0:
        comment = f"Норм. Ещё {remaining} ккал можешь позволить."
    elif over <= 200:
        comment = "Чуть перебрал. Бывает. Но это последний раз."
    elif over <= 500:
        comment = "Слабак. Не мог остановиться вовремя?"
    else:
        comment = f"Позор. +{over} ккал сверху. Иди отжимайся."

    status = f"✅ Остаток: {remaining} ккал" if remaining > 0 else f"❌ Перебор: +{over} ккал"
    await update.message.reply_text(
        f"📊 День:\n\n🍽 Съедено: {today_cal} ккал\n🎯 Цель: {target} ккал\n{status}\n\n{comment}"
    )

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_cal = get_week_calories()
    target = USER_PROFILE['weekly_target']
    remaining = target - week_cal
    day_num = date.today().weekday() + 1
    avg = round(week_cal / day_num)

    comment = "Идёшь в норме." if remaining > 0 else f"Вышел за неделю на {abs(remaining)} ккал. Позорище."
    status = f"Осталось: {remaining} ккал" if remaining > 0 else f"Перебор: +{abs(remaining)} ккал"

    await update.message.reply_text(
        f"📈 Неделя:\n\nСъедено: {week_cal} ккал\nЦель: {target} ккал\n{status}\nСредний день: {avg} ккал\n\n{comment}"
    )

async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_goal = int(context.args[0])
        USER_PROFILE['daily_target'] = new_goal
        USER_PROFILE['weekly_target'] = new_goal * 7
        await update.message.reply_text(f"Новая цель: {new_goal} ккал/день. Выполняй.")
    except:
        await update.message.reply_text("Напиши так: /goal 2000")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Нутролог запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
