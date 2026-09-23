import hashlib
import html
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent

# ============================================================
# ENVIRONMENT
# ============================================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

app = Flask(__name__)

# ============================================================
# DATABASES
# ============================================================
# Old/current ChefCalc database stays compatible:
# prices.json:
# {
#   "Мука Турон": {"price": 8600, "unit": "кг", "date": "22.08.2026"}
# }
OLD_PRICES_FILE = BASE_DIR / "prices.json"

# New restaurant market database:
# market_prices.json:
# [
#   {
#     "category": "Бакалея",
#     "product": "Mutabar мука 1 сорт, мешок 50 кг",
#     "unit": "мешок",
#     "price_type": "fixed",
#     "min_price": 335000,
#     "max_price": 335000,
#     "avg_price": 335000,
#     "min_order": "1 мешок",
#     "supplier": "SANPACK",
#     "date": "23.09.2026",
#     "source_url": "...",
#     "notes": ""
#   }
# ]
MARKET_PRICES_FILE = BASE_DIR / "market_prices.json"

PRICES = {}
MARKET_PRICES = []


def load_databases() -> None:
    """Load both the existing price database and the new market database."""
    global PRICES, MARKET_PRICES

    PRICES = {}
    MARKET_PRICES = []

    if OLD_PRICES_FILE.exists():
        try:
            PRICES = json.loads(OLD_PRICES_FILE.read_text(encoding="utf-8"))
            if not isinstance(PRICES, dict):
                PRICES = {}
        except Exception:
            app.logger.exception("Cannot read prices.json")

    if MARKET_PRICES_FILE.exists():
        try:
            raw = json.loads(MARKET_PRICES_FILE.read_text(encoding="utf-8"))
            MARKET_PRICES = raw if isinstance(raw, list) else []
        except Exception:
            app.logger.exception("Cannot read market_prices.json")

    app.logger.info(
        "Loaded databases: old=%s, market=%s",
        len(PRICES),
        len(MARKET_PRICES),
    )


load_databases()

# ============================================================
# HELPERS
# ============================================================
STOP_WORDS = {
    "цена", "цены", "стоимость", "стоит", "где", "сколько",
    "есть", "найди", "покажи", "дай", "для", "ресторан",
    "ресторана", "закупка", "закупочную", "закупочная",
}


def normalize(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9%]+", " ", value)
    words = value.split()

    # Простая нормализация русских окончаний без внешних библиотек.
    # Это позволяет "мука" / "муку", "помидор" / "помидоры" и т.п.
    normalized = []
    for word in words:
        if word in STOP_WORDS:
            continue

        replacements = {
            "муку": "мука",
            "муке": "мука",
            "мукой": "мука",
            "помидоры": "помидор",
            "помидора": "помидор",
            "помидору": "помидор",
            "огурцы": "огурец",
            "огурца": "огурец",
            "сливки": "сливка",
            "сливок": "сливка",
            "курицу": "курица",
            "куриную": "курица",
            "куриное": "курица",
            "говядину": "говядина",
            "баранину": "баранина",
            "картошку": "картофель",
            "картошка": "картофель",
        }
        word = replacements.get(word, word)
        normalized.append(word)

    return " ".join(normalized).strip()


def format_price(value: Any) -> str:
    try:
        return f"{round(float(value)):,}".replace(",", " ")
    except Exception:
        return str(value or "").strip()


def escape(value: Any) -> str:
    return html.escape(str(value or ""))


def get_old_price_records() -> list[dict]:
    records = []
    for name, item in PRICES.items():
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "product": name,
                "category": "Твоя база закупок",
                "unit": item.get("unit", ""),
                "price_type": "fixed",
                "min_price": item.get("price"),
                "max_price": item.get("price"),
                "avg_price": item.get("price"),
                "supplier": "Твой отчёт о закупках",
                "date": item.get("date", ""),
                "min_order": "",
                "source_url": "",
                "notes": "",
            }
        )
    return records


def all_records() -> list[dict]:
    return get_old_price_records() + MARKET_PRICES


def score_product(query: str, product: str) -> int:
    q = normalize(query)
    p = normalize(product)

    if not q or not p:
        return 0

    if q == p:
        return 1000

    if q in p:
        return 800 - max(0, len(p) - len(q))

    q_words = q.split()
    p_words = p.split()

    score = 0
    for qw in q_words:
        best = 0
        for pw in p_words:
            if qw == pw:
                best = max(best, 100)
            elif qw in pw or pw in qw:
                best = max(best, 70)
            elif len(qw) >= 4 and pw.startswith(qw[:4]):
                best = max(best, 50)
        score += best

    # Поощряем совпадение нескольких слов.
    if q_words and all(any(qw == pw or qw in pw or pw in qw for pw in p_words) for qw in q_words):
        score += 100

    return score


def extract_pack_kg(product: str, unit: str) -> float | None:
    """
    Пытаемся понять вес упаковки из названия:
    'мешок 50 кг', '25 кг', '2 кг', '500 гр', '450г'.
    """
    text = normalize(product + " " + unit).replace("гр", " г")
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*кг\b", text)
    if match:
        return float(match.group(1).replace(",", "."))

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*г\b", text)
    if match:
        return float(match.group(1).replace(",", ".")) / 1000.0

    return None


def sort_price(record: dict) -> float:
    try:
        return float(record.get("min_price") or 10**18)
    except Exception:
        return 10**18


def search_products(query: str, limit: int = 30) -> list[dict]:
    """
    Main search.
    Returns ALL meaningful variants, sorted by price where possible.
    """
    q = normalize(query)
    if not q:
        return []

    records = all_records()
    exact = []
    partial = []
    fuzzy = []

    for record in records:
        score = score_product(q, record.get("product", ""))
        if score >= 1000:
            exact.append((score, record))
        elif score >= 650:
            partial.append((score, record))
        elif score >= 100:
            fuzzy.append((score, record))

    if exact:
        found = [r for _, r in exact]
        found.sort(key=sort_price)
        return found[:limit]

    if partial:
        found = [r for _, r in partial]
        found.sort(key=sort_price)
        return found[:limit]

    fuzzy.sort(key=lambda x: (-x[0], sort_price(x[1])))
    return [r for _, r in fuzzy[:10]]


def render_record(index: int, record: dict) -> str:
    product = escape(record.get("product"))
    supplier = escape(record.get("supplier") or "Твой отчёт о закупках")
    unit = escape(record.get("unit") or "—")
    date = escape(record.get("date") or "—")
    min_order = record.get("min_order") or ""
    source_url = record.get("source_url") or ""
    notes = record.get("notes") or ""
    price_type = record.get("price_type")

    if price_type == "range":
        price_text = (
            f"{format_price(record.get('min_price'))} — "
            f"{format_price(record.get('max_price'))} сум"
        )
    elif price_type == "request":
        price_text = "Цена по запросу"
    else:
        price_text = f"{format_price(record.get('min_price'))} сум"

    lines = [
        f"<b>{index}. {product}</b>",
        f"💰 {price_text} / {unit}",
        f"🏢 Поставщик: {supplier}",
        f"📅 Дата: {date}",
    ]

    # Для мешков/упаковок дополнительно считаем цену за кг.
    pack_kg = extract_pack_kg(str(record.get("product") or ""), str(record.get("unit") or ""))
    if pack_kg and pack_kg > 0 and record.get("min_price") not in ("", None):
        try:
            price_kg = float(record["min_price"]) / pack_kg
            lines.append(f"⚖️ ≈ {format_price(price_kg)} сум/кг")
        except Exception:
            pass

    if min_order:
        lines.append(f"📦 Мин. партия: {escape(min_order)}")

    if notes:
        clean_notes = str(notes).replace("prices.json", "отчёт о закупках")
        lines.append(f"📝 {escape(clean_notes)}")

    if source_url.startswith("http"):
        lines.append(f'🔗 <a href="{escape(source_url)}">Прайс / сайт поставщика</a>')

    return "\n".join(lines)


def make_search_response(query: str, records: list[dict]) -> str:
    if not records:
        return (
            f"❌ Не нашёл товар: <b>{escape(query)}</b>\n\n"
            "Попробуй написать короче: <code>мука</code>, "
            "<code>курица</code>, <code>говядина</code>, "
            "<code>сыр</code>."
        )

    title = f"🔎 <b>Цены: {escape(query)}</b>"
    total = len(records)

    chunks = [title, f"Найдено вариантов: <b>{total}</b>\n"]

    for i, record in enumerate(records, 1):
        chunks.append(render_record(i, record))

    # Telegram ограничивает длину одного сообщения примерно 4096 символами.
    text = "\n\n".join(chunks)
    if len(text) <= 3900:
        return text

    # Не теряем начало и первые результаты.
    return text[:3850] + "\n\n…"


# ============================================================
# TELEGRAM
# ============================================================
def webhook_path() -> str:
    digest = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    return digest[:24]


def telegram(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data


def send_message(chat_id: int, text: str) -> None:
    # Если ответ длинный — режем на части.
    parts = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    for part in parts:
        telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


def handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()

    if not chat_id:
        return

    if not text:
        return

    if text.startswith("/start"):
        load_databases()
        total = len(all_records())
        send_message(
            chat_id,
            "👨‍🍳 <b>ChefCalc UZ — база цен HoReCa</b>\n\n"
            "Напиши название продукта.\n\n"
            "Например:\n"
            "• <code>мука</code>\n"
            "• <code>муку высший сорт</code>\n"
            "• <code>курица</code>\n"
            "• <code>говядина</code>\n"
            "• <code>сыр</code>\n"
            "• <code>картофель</code>\n\n"
            f"📦 В базе сейчас: <b>{total}</b> позиций."
        )
        return

    if text.startswith("/help"):
        send_message(
            chat_id,
            "🔎 <b>Поиск цен</b>\n\n"
            "Просто напиши товар обычным сообщением.\n"
            "Бот покажет все подходящие варианты, поставщика, цену, "
            "единицу и дату.\n\n"
            "Команда <code>/price мука</code> тоже работает."
        )
        return

    if text.startswith("/reload"):
        load_databases()
        send_message(
            chat_id,
            f"✅ База перечитана.\n"
            f"Старых товаров: {len(PRICES)}\n"
            f"Новых рыночных позиций: {len(MARKET_PRICES)}"
        )
        return

    query = re.sub(r"^/price\s*", "", text, flags=re.I).strip()
    if not query:
        send_message(chat_id, "Напиши товар после команды, например: <code>/price мука</code>.")
        return

    records = search_products(query)
    send_message(chat_id, make_search_response(query, records))


# ============================================================
# FLASK WEBHOOK
# ============================================================
@app.get("/")
def home():
    return jsonify(
        {
            "ok": True,
            "service": "ChefCalc UZ",
            "price_records": len(all_records()),
        }
    )


@app.get("/health")
def health():
    return "OK", 200


@app.post(f"/webhook/{webhook_path()}")
def webhook():
    update = request.get_json(silent=True) or {}

    try:
        message = update.get("message")
        if message:
            # Telegram gets HTTP 200 immediately.
            threading.Thread(
                target=handle_message,
                args=(message,),
                daemon=True,
            ).start()
    except Exception as exc:
        app.logger.exception("Webhook handling failed: %s", exc)

    return "OK", 200


def set_webhook():
    if not PUBLIC_URL:
        app.logger.warning(
            "RENDER_EXTERNAL_URL is not set; webhook not configured."
        )
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
    def worker():
        time.sleep(2)
        try:
            load_databases()
            set_webhook()
        except Exception:
            app.logger.exception("Failed to initialize Telegram webhook")

    threading.Thread(target=worker, daemon=True).start()


startup()
