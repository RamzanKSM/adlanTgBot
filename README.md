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

Локально или на сервере можно запустить API и polling-бота двумя сервисами из одного Docker image:

```bash
cp .env.example .env
mkdir -p data
```

Заполните `.env`: `BOT_TOKEN`, `TELEGRAM_GROUP_ID`, `ADMIN_IDS`, параметры Lava и `PAYMENT_PROVIDER`.
Для локальной проверки без Lava поставьте `PAYMENT_PROVIDER=mock` и `APP_BASE_URL=http://localhost:8000`.

Запуск:

```bash
docker compose up --build -d
```

Проверка API:

```bash
curl http://localhost:8000/healthz
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

Compose использует общий bind-volume `./data:/app/data` и задает `DATABASE_PATH=/app/data/bot.sqlite3`, поэтому SQLite-файл сохраняется на хосте в `data/`. Сервисы читают общий `.env`; API перед стартом применяет миграции, bot также применяет миграции при запуске.

Для Lava в production нужен публичный HTTPS reverse proxy на порт `8000` и `APP_BASE_URL=https://your-domain.example`. Внешний proxy в этот MVP не добавлен намеренно.

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
9. Откройте `/tariffs`, нажмите кнопку покупки тарифа. Бот пришлет mock-ссылку вида:

```text
http://localhost:8000/mock/payments/<internal_invoice_id>/pay
```

10. Откройте ссылку в браузере или выполните:

```bash
curl -X POST http://localhost:8000/mock/payments/<internal_invoice_id>/pay
```

Endpoint работает только при `PAYMENT_PROVIDER=mock` или `MOCK_PAYMENTS_ENABLED=true`. Он применяет оплату через ту же идемпотентную обработку, что и Lava webhook, и не подтверждает реальные Lava-платежи. Повторный вызов не продлевает доступ второй раз.
11. Проверьте доступ командой `/access`.

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

## Команды бота

При старте polling бот устанавливает меню команд Telegram через `setMyCommands`:

- private chat scope для всех личных чатов получает пользовательские команды;
- group chat scopes получают пустой список команд, поэтому меню команд в группах не показывается;
- для каждого ID из `ADMIN_IDS` в private chat scope назначаются пользовательские и админские команды;
- если `ADMIN_IDS` пустой, устанавливаются только private и пустые group scopes.

После изменения команд или `.env` перезапустите polling-бота, чтобы он перечитал настройки и обновил меню команд в Telegram. `ADMIN_USERNAMES` недостаточно для админского меню команд: Bot API `BotCommandScopeChat` принимает только `chat_id`. Username fallback продолжает работать для доступа к handler-ам, но меню Telegram с админскими командами назначается только администраторам из `ADMIN_IDS`.

Пользовательские:

- `/start` - регистрация и список действий.
- `/tariffs` - активные тарифы.
- `/access` - текущий доступ и повторная выдача invite-ссылки, если пользователь еще не в группе.

Админские:

- `/admin_tariffs` - список тарифов.
- `/tariff_set CODE "Название" PRICE DURATION_DAYS [CURRENCY] [sort_order] [description]` - создать/обновить тариф, например `/tariff_set week "7 дней" 500 7` или `/tariff_set month "30 дней" 1500 30 RUB 20`.
- `/tariff_disable CODE` - отключить тариф.
- `/grant_access <telegram_id> <days>` или `/grant_access <days>` - вручную выдать доступ пользователю или себе.

Админы проверяются по Telegram ID из `ADMIN_IDS`; username используется только как fallback для выполнения команд.
Все пользовательские и админские команды обрабатываются только в личном чате с ботом. В группе бот молчит на команды, но продолжает логировать сообщения и обрабатывать `chat_member` события для invite/access flow.

## Правила доступа

- `access_until` хранится в UTC ISO.
- В пользовательских сообщениях даты доступа показываются в МСК (UTC+3) в человекочитаемом формате.
- Продление: `base = max(current access_until, paid_at/now)`, затем `base + duration_days`.
- `payments.applied_at` делает применение платежа идемпотентным.
- Invite-ссылки персональные: `member_limit=1`, TTL 24 часа.
- Перед повторной выдачей ссылки бот вызывает `getChatMember`.
- `chat_member` update проверяет, тот ли пользователь вошел по ссылке. Чужой пользователь удаляется через `banChatMember` + `unbanChatMember`, ссылка отзывается, админы уведомляются.
- Бот не удаляет пользователей из `ADMIN_IDS`, а также Telegram-администраторов и владельца группы; если статус участника перед удалением не удалось проверить, удаление пропускается.
- За 3 дня до окончания доступа отправляется предупреждение.
- После окончания доступа пользователь удаляется из группы через `banChatMember` + `unbanChatMember`.

## Lava

Выбор платежного адаптера задается переменной `PAYMENT_PROVIDER`:

- `lava` - production default, создание invoice и проверка статусов идут через Lava.
- `mock` - локальная разработка без внешнего API; invoice хранится в базе с `provider=mock`, а подтверждение идет через `/mock/payments/{internal_invoice_id}/pay`.

Также поддерживается явный dev-флаг `MOCK_PAYMENTS_ENABLED=true`, но для новых локальных запусков предпочтительнее `PAYMENT_PROVIDER=mock`.

`LavaClient` содержит точки расширения:

- `create_invoice`
- `get_invoice_status`
- `verify_webhook`
- `normalize_webhook_payload`

Текущий клиент намеренно толерантен к форме payload и не привязан к последнему полному контракту Lava. Перед production нужно сверить URL, поля запроса/ответа и формат подписи с актуальной документацией Lava.

## Проверки

```bash
python3 -m compileall app tests
pytest
```
