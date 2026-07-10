# adlanTgBot

MVP-каркас Telegram-бота для доступа в одну приватную группу через разовые платежи Lava.

## Стек

- Python 3.12
- aiogram 3
- FastAPI
- SQLite + aiosqlite
- httpx
- pydantic-settings
- простой async scheduler без APScheduler
- без SQLAlchemy/Alembic/Django

## Docker quick start

Локально или на сервере можно запустить API и polling-бота через Docker Compose. API и bot собираются из одного Docker image:

```bash
cp .env.example .env
mkdir -p /root/adlanbot/data
```

Заполните `.env`: `BOT_TOKEN`, `TELEGRAM_GROUP_ID`, `ADMIN_IDS`, параметры Lava и `PAYMENT_PROVIDER`.
Для локальной проверки без Lava поставьте `PAYMENT_PROVIDER=mock` и `APP_BASE_URL=http://localhost:8000`.
Для временной проверки на сервере без Lava поставьте `PAYMENT_PROVIDER=mock` и `APP_BASE_URL=http://rmzn.net:8000`. Порт API `8000:8000` публикуется на host напрямую.

Запуск:

```bash
docker compose up --build -d
```

Проверка API:

```bash
curl http://localhost:8000/healthz
```

Проверка на сервере:

```bash
curl http://rmzn.net:8000/healthz
```

Логи:

```bash
docker compose logs -f api
docker compose logs -f bot
```

Остановка:

```bash
docker compose down
```

Compose использует общий bind-volume `/root/adlanbot/data:/app/data` и задает `DATABASE_PATH=/app/data/bot.sqlite3`, поэтому SQLite-файл сохраняется на сервере в `/root/adlanbot/data/bot.sqlite3`. Сервисы читают общий `.env`; API перед стартом применяет миграции, bot также применяет миграции при запуске.

Текущий server/mock сценарий временный и работает без HTTPS. Для реальной Lava позже нужен публичный HTTPS URL в `APP_BASE_URL`, например `https://rmzn.net`, и reverse proxy/сертификаты.

## Deploy на новый сервер rmzn.net

1. В DNS создайте A record: `rmzn.net -> <IP сервера>`.
2. Откройте порт API на сервере. Если используется `ufw`:

```bash
sudo ufw allow 8000/tcp
```

3. Установите Docker и Docker Compose plugin, затем склонируйте или обновите репозиторий:

```bash
git clone <repo-url> /root/adlanbot/app
cd /root/adlanbot/app
# или для существующей копии:
git pull
```

4. Создайте `.env`:

```bash
cp .env.example .env
```

Минимальные значения для server/mock сценария:

```env
BOT_TOKEN=123456:replace-me
TELEGRAM_GROUP_ID=-1001234567890
ADMIN_IDS=123456789,987654321
APP_BASE_URL=http://rmzn.net:8000
PAYMENT_PROVIDER=mock
```

5. Создайте директорию для SQLite:

```bash
mkdir -p /root/adlanbot/data
```

6. Запустите app-сервисы:

```bash
docker compose up --build -d
```

7. Смотрите логи:

```bash
docker compose logs -f api bot
```

8. Проверьте healthcheck:

```bash
curl http://rmzn.net:8000/healthz
```

В mock-режиме ссылки оплаты должны быть вида:

```text
http://rmzn.net:8000/mock/payments/<order_id>/pay
```

Это временный mock-сценарий без HTTPS. Для реальных платежей Lava позже нужен HTTPS и reverse proxy.

## Запуск без Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Заполните `.env`: `BOT_TOKEN`, `TELEGRAM_GROUP_ID`, `ADMIN_IDS`, параметры Lava и публичный `APP_BASE_URL`.
По умолчанию используется реальный провайдер `PAYMENT_PROVIDER=lava`.

Применить миграции:

```bash
python3 -m app.db.migrations
```

Запустить API:

```bash
uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

Polling для локальной разработки:

```bash
python3 -m app.main
```

## Локальная проверка без Lava

1. Создайте окружение и `.env`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

2. В `.env` заполните минимум `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_PATH`, `APP_BASE_URL=http://localhost:8000` и `PAYMENT_PROVIDER=mock`. Параметры Lava для этой проверки не нужны.
3. Примените миграции:

```bash
python3 -m app.db.migrations
```

4. Запустите API:

```bash
uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

5. В отдельном терминале запустите polling:

```bash
python3 -m app.main
```

6. Добавьте бота в приватную группу и выдайте ему права на создание invite-ссылок. Напишите любое сообщение в группе и скопируйте `chat_id` из логов polling/API в строках вида `telegram.message chat_id=...` в `TELEGRAM_GROUP_ID` в `.env`, затем перезапустите API и polling, чтобы процессы перечитали `.env`.
7. В Telegram отправьте боту `/start`. Telegram ID администратора для `ADMIN_IDS` можно получить внешними способами Telegram или временно взять из логов `user_id=...` после сообщения администратора в личке с ботом или в группе.
8. Создайте тариф для проверки витрины: `/tariff_set week "7 дней" 500 7`.
9. В Telegram нажмите кнопку `💳 Тарифы`, затем кнопку покупки тарифа. Бот пришлет mock-ссылку вида:

```text
http://localhost:8000/mock/payments/<order_id>/pay
```

10. Откройте ссылку в браузере или выполните:

```bash
curl -X POST http://localhost:8000/mock/payments/<order_id>/pay
```

Endpoint работает только при `PAYMENT_PROVIDER=mock` или `MOCK_PAYMENTS_ENABLED=true`. Он применяет оплату через ту же идемпотентную обработку, что и Lava webhook, и не подтверждает реальные Lava-платежи. Повторный вызов не продлевает доступ второй раз.
11. Проверьте доступ кнопкой `🔐 Мой доступ`.

## Webhooks

- Telegram webhook: `POST /telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}`
- Lava webhook: `POST /lava/webhook`
- Healthcheck: `GET /healthz`

Success redirect от Lava можно вести на `GET /lava/success`: он показывает только нейтральный ответ и не подтверждает оплату.

## Логи Telegram событий

При `python3 -m app.main` или Telegram webhook бот пишет входящие события в stdout/stderr через стандартный logging. Для сообщений ищите строки вида:

```text
telegram.message chat_id=... chat_type=... user_id=... username=... message_type=...
```

Полный текст сообщений и payload webhook не логируются. Для получения `TELEGRAM_GROUP_ID` используйте `chat_id` из этих логов: в группе бот не отвечает на команды, но входящие сообщения продолжат логироваться. Для получения ID администратора используйте внешние способы Telegram или временно возьмите `user_id` из этих логов после сообщения администратора в личке с ботом или в группе.

## Меню и команды бота

Основной пользовательский UX построен на Reply-клавиатуре в личном чате. При `/start` бот регистрирует пользователя и показывает кнопки:

- `💳 Тарифы` - активные тарифы и покупка доступа;
- `🔐 Мой доступ` - текущий доступ и повторная выдача invite-ссылки, если пользователь еще не в группе;
- `📄 Документы` - публичная оферта, политика конфиденциальности, условия возврата и правила сообщества;
- `🛟 Поддержка` - контакт поддержки `@gymvash`.

Для администраторов из `ADMIN_IDS` клавиатура дополнительно показывает:

- `Админ: список тарифов` - список всех тарифов;
- `Админ: отключить тариф` - выбор активного тарифа через inline-кнопки и подтверждение отключения.

При старте polling бот устанавливает минимальное меню команд Telegram через `setMyCommands`:

- private chat scope для всех личных чатов получает только `/start`;
- group chat scopes получают пустой список команд, поэтому меню команд в группах не показывается;
- для каждого ID из `ADMIN_IDS` в private chat scope назначаются `/start`, `/tariff_set` и `/grant_access`;
- если `ADMIN_IDS` пустой, устанавливаются только private и пустые group scopes.

После изменения команд или `.env` перезапустите polling-бота, чтобы он перечитал настройки и обновил меню команд в Telegram. Admin-aware Reply-клавиатура в `/start` строится через `settings.is_admin`, поэтому учитывает `ADMIN_IDS` и username fallback из `ADMIN_USERNAMES`.

Slash-команды оставлены как технический fallback и для локальной разработки. В меню Telegram обычного пользователя показывается только `/start`; в меню администратора дополнительно показываются команды, которые не закрыты простой клавиатурой:

- `/start` - регистрация и список действий.
- `/tariff_set CODE "Название" PRICE DURATION_DAYS [CURRENCY] [sort_order] [description]` - создать/обновить тариф, например `/tariff_set week "7 дней" 500 7` или `/tariff_set month "30 дней" 1500 30 RUB 20`.
- `/grant_access <telegram_id> <days>` или `/grant_access <days>` - вручную выдать доступ пользователю или себе.

Админы проверяются по Telegram ID из `ADMIN_IDS`; username используется только как fallback для выполнения команд.
Все пользовательские и админские действия обрабатываются только в личном чате с ботом. В группе бот молчит на команды, но продолжает логировать сообщения и обрабатывать `chat_member` события для invite/access flow.

## Правила доступа

- `access_until` хранится в UTC ISO.
- В пользовательских сообщениях даты доступа показываются в МСК (UTC+3) в человекочитаемом формате.
- Продление: `base = max(current access_until, paid_at/now)`, затем `base + duration_days`.
- `payments.applied_at` делает применение платежа идемпотентным.
- Invite-ссылки персональные: `member_limit=1`, TTL 24 часа.
- Перед повторной выдачей ссылки бот вызывает `getChatMember`.
- `chat_member` update проверяет, тот ли пользователь вошел по ссылке. Чужой пользователь удаляется через `banChatMember` + `unbanChatMember`, ссылка отзывается, админы уведомляются.
- Бот не удаляет пользователей из `ADMIN_IDS`, а также Telegram-администраторов и владельца группы. Это правило применяется и при истечении доступа, и при проверке входа по чужой персональной ссылке. Если статус участника перед удалением не удалось проверить, удаление пропускается.
- За 3 дня до окончания доступа отправляется предупреждение.
- После окончания доступа пользователь удаляется из группы через `banChatMember` + `unbanChatMember`.

## Lava

Выбор платежного адаптера задается переменной `PAYMENT_PROVIDER`:

- `lava` - production default, создание invoice и проверка статусов идут через Lava.
- `mock` - локальная разработка без внешнего API; invoice хранится в базе с `provider=mock`, а подтверждение идет через `/mock/payments/{order_id}/pay`.

Также поддерживается явный dev-флаг `MOCK_PAYMENTS_ENABLED=true`, но для новых локальных запусков предпочтительнее `PAYMENT_PROVIDER=mock`.

`LavaClient` содержит точки расширения:

- `create_invoice`
- `get_invoice_status`
- `verify_webhook`
- `normalize_webhook_payload`

Клиент использует Lava Business API: `POST /business/invoice/create`, `POST /business/invoice/status`, подпись исходящих запросов в заголовке `Signature`, проверка webhook-подписи из `Authorization`. Локальный `payments.order_id` передается в Lava как `orderId`, а `payments.invoice_id` хранит `invoice_id` Lava.

## Проверки

```bash
python3 -m compileall app tests
pytest
```
