import hashlib
import html
import json
import os
import re
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
PRICES = json.loads((BASE_DIR / "prices.json").read_text(encoding="utf-8"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

app = Flask(__name__)


def normalize(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def format_price(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def escape(value: str) -> str:
    return html.escape(str(value or ""))


def webhook_path() -> str:
    digest = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    return digest[:24]


def telegram(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data


def send_message(chat_id: int, text: str) -> None:
    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def search_product(query: str):
    q = normalize(query)
    if not q:
        return None, []

    # Exact / contained match first.
    matches = []
    for name, item in PRICES.items():
        n = normalize(name)
        if n == q or q in n or n in q:
            matches.append((len(n), name, item))

    if matches:
        matches.sort(key=lambda x: (0 if normalize(x[1]) == q else 1, x[0]))
        _, name, item = matches[0]
        return (name, item), []

    # Fuzzy word scoring.
    q_words = q.split()
    scored = []
    for name, item in PRICES.items():
        words = normalize(name).split()
        score = 0
        for qw in q_words:
            for w in words:
                if qw == w:
                    score += 4
                elif qw in w or w in qw:
                    score += 2
        if score > 0:
            scored.append((score, len(words), name, item))

    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return None, [(name, item) for _, _, name, item in scored[:5]]


def handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()

    if not chat_id or not text:
        return

    if text.startswith("/start"):
        send_message(
            chat_id,
            "👨‍🍳 <b>ChefCalc UZ</b>\n\n"
            "Напиши название товара — я покажу последнюю закупочную цену "
            "из твоего отчёта.\n\n"
            "Например: <code>мука</code>, <code>авокадо</code>, "
            "<code>сливки 35%</code>.\n\n"
            "📦 В базе: 134 товара",
        )
        return

    if text.startswith("/help"):
        send_message(
            chat_id,
            "🔎 <b>Как пользоваться</b>\n\n"
            "Просто напиши название товара.\n"
            "Я найду товар в базе и покажу цену, единицу и дату последней закупки.",
        )
        return

    query = re.sub(r"^/price\s*", "", text, flags=re.I).strip()
    result, suggestions = search_product(query)

    if result:
        name, item = result
        send_message(
            chat_id,
            f"📦 <b>{escape(name)}</b>\n\n"
            f"💰 <b>{format_price(item['price'])} сум</b> / {escape(item['unit'])}\n"
            f"📅 Последняя закупка: {escape(item['date'])}\n"
            f"🧾 Цена с НДС\n\n"
            f"Источник: твой отчёт о закупках",
        )
        return

    if suggestions:
        lines = [
            f"{i}. {escape(name)} — <b>{format_price(item['price'])} сум/{escape(item['unit'])}</b>"
            for i, (name, item) in enumerate(suggestions, 1)
        ]
        send_message(
            chat_id,
            "🔎 Точного совпадения не нашёл.\n\n"
            "Возможно, ты имел в виду:\n" + "\n".join(lines),
        )
        return

    send_message(
        chat_id,
        "❌ Не нашёл товар в твоём отчёте.\n\n"
        "Попробуй написать название короче, например: <code>мука</code> или <code>помидор</code>.",
    )


@app.get("/")
def home():
    return jsonify({"ok": True, "service": "ChefCalc UZ"})


@app.get("/health")
def health():
    return "OK", 200


@app.post(f"/webhook/{webhook_path()}")
def webhook():
    update = request.get_json(silent=True) or {}
    try:
        message = update.get("message")
        if message:
            # Respond asynchronously so Telegram receives HTTP 200 immediately.
            threading.Thread(target=handle_message, args=(message,), daemon=True).start()
    except Exception as exc:
        app.logger.exception("Webhook handling failed: %s", exc)
    return "OK", 200


def set_webhook():
    if not PUBLIC_URL:
        app.logger.warning("RENDER_EXTERNAL_URL is not set; webhook not configured.")
        return

    url = f"{PUBLIC_URL}/webhook/{webhook_path()}"
    data = telegram(
        "setWebhook",
        {
            "url": url,
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        },
    )
    app.logger.info("Webhook set: %s", data)


def startup():
    # Give the web server a moment to bind, then configure Telegram.
    def worker():
        time.sleep(2)
        try:
            set_webhook()
        except Exception:
            app.logger.exception("Failed to set Telegram webhook")

    threading.Thread(target=worker, daemon=True).start()


startup()
