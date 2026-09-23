# ChefCalc UZ — Telegram price bot

Простой бот: пользователь пишет название товара и получает цену из `prices.json`.

## Render
- Runtime: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Environment variable: `TELEGRAM_BOT_TOKEN` = токен BotFather

Render автоматически предоставляет `RENDER_EXTERNAL_URL`; приложение использует его для установки Telegram webhook.

## Данные
База содержит 134 товара из отчёта о закупках пользователя за период 16.08.2026–13.09.2026.
