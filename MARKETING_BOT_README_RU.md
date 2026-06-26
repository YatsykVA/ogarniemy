# Маркетинговый userbot Ogarniemy

Telegram-часть работает не через BotFather-бота, а через личный Telegram-аккаунт. Сообщения в группы, ответы по ключевым словам и рассылки отправляются от имени этого аккаунта.

Важно: userbot использует официальный Telegram API для пользовательских аккаунтов. Не делайте массовый спам и не пишите незнакомым людям в личку без согласия, иначе Telegram может ограничить аккаунт.

## Первый запуск Telegram

1. Установить зависимость:

```powershell
python -m pip install telethon
```

2. Открыть https://my.telegram.org, войти по номеру телефона и создать приложение в разделе API development tools.
3. Скопировать `api_id` и `api_hash`.
4. Открыть на сервере страницу:

```text
https://ваш-сервер/telegram-login
```

5. Ввести админ-пароль сайта, `api_id`, `api_hash` и номер личного Telegram-аккаунта.
6. Нажать "Отправить код", дождаться кода в Telegram и ввести его на этой же странице.

После успешного входа сервер сохранит:

```text
marketing_bot_config.json
ogarniemy_userbot.session
```

Эти файлы не нужно отправлять кому-либо: они дают доступ к личному аккаунту.

## Консольный запасной вариант

Если страница недоступна, можно авторизоваться через консоль:

```powershell
$env:TELEGRAM_API_ID="123456"
$env:TELEGRAM_API_HASH="ваш_api_hash"
$env:TELEGRAM_SESSION="ogarniemy_userbot"
$env:ADMIN_TELEGRAM_IDS="ваш_telegram_id"
$env:PRESENTATION_URL="https://ogarniemy.pro"
```

5. Один раз авторизовать личный аккаунт:

```powershell
python marketing_bot.py --telegram-login
```

## Запуск

Отдельно:

```powershell
python marketing_bot.py --telegram
```

Вместе с основным сервером:

```powershell
$env:MARKETING_BOT_ENABLED="1"
python server.py
```

## Команды

Для пользователей:

```text
/start
/client
/worker
/city Warszawa
/stop
```

Для администратора:

```text
/stats
/broadcast all текст
/broadcast clients текст
/broadcast workers текст
/postgroups текст
/groups
```

В группе:

```text
/watch Warszawa | мастер, сантехник, электрик, ремонт, уборка, переезд
```

После `/watch` userbot сохраняет группу и ключевые слова. Когда в группе появляется сообщение с ключевым словом, userbot публично отвечает рекламным текстом или материалом, выбранным в админке. Частота ответов ограничена переменной `KEYWORD_REPLY_COOLDOWN`, по умолчанию один раз в 6 часов на одно ключевое слово в одной группе.

## Facebook Messenger

Facebook-часть осталась прежней: нужен Page Access Token и webhook в настройках Meta Developers.

```powershell
$env:FACEBOOK_PAGE_ACCESS_TOKEN="токен_страницы"
$env:FACEBOOK_VERIFY_TOKEN="любой_секретный_текст"
python marketing_bot.py --facebook-webhook --port 8080
```

Webhook URL:

```text
https://ваш-сервер/facebook/webhook
```
