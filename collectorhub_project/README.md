# CollectorHub

CollectorHub --- Windows-приложение для автоматического поиска
объявлений в Facebook-группах и пересылки подходящих объявлений в
Telegram.

## Возможности

-   Сбор публикаций из выбранных Facebook-групп через Playwright.
-   Сохранение объявлений в SQLite.
-   Фильтрация по ключевым словам и словам-исключениям.
-   Отправка подходящих объявлений в Telegram.
-   Подготовка к подключению AI (можно включать/выключать).
-   Подготовка к автоматической публикации в собственную
    Facebook-группу.

## Структура

``` text
main.py
app.py
config.py
database.py
collector.py
facebook_session.py
extractor.py
parser.py
filters.py
telegram_sender.py
groups_manager.py
logger.py
data/
```

## Установка

``` bash
python -m venv .venv

# Windows
.venv\Scripts\activate

pip install -r requirements.txt

playwright install chromium
```

## Первый запуск

1.  Запусти:

``` bash
python facebook_session.py
```

2.  Войди в Facebook вручную.
3.  После появления своей ленты нажми Enter в консоли.
4.  Затем запускай:

``` bash
python main.py
```

## План разработки

-   Улучшение извлечения автора, телефона и ссылок.
-   Публикация объявлений в собственную Facebook-группу.
-   Подключаемый AI.
-   Расписание автоматического запуска.
-   Красивый интерфейс управления.
