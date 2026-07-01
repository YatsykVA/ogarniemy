CollectorHub Update 31
======================

Что добавлено:

1) Выбор направления отправки объявлений
---------------------------------------
В GUI появилась кнопка:

    📤 Куда отправлять: Telegram / Facebook / Telegram + Facebook

Нажатие переключает режим по кругу:

    Telegram -> Facebook -> Telegram + Facebook -> Telegram

Настройки хранятся тут:

    data/collector_settings.json

Если хочешь публиковать в Facebook, открой этот файл и заполни хотя бы:

    facebook_target_group_name

или:

    facebook_target_group_url

Пример:

{
    "send_mode": "both",
    "facebook_target_group_name": "МОЯ ГРУППА",
    "facebook_target_group_url": "https://www.facebook.com/groups/...."
}

2) Telegram
-----------
Работает как раньше. Collector берёт подходящие объявления и отправляет их в Telegram.

3) Facebook-forward
-------------------
Facebook-режим НЕ переписывает текст. Он пытается открыть оригинальный пост и нажать Share / Поделиться / Udostępnij.

Важно: Facebook часто меняет интерфейс. Если он покажет окно, где нужен ручной шаг, капча, подтверждение, выбор группы или вопрос — программа это НЕ обходит. Окно останется открытым, а в логах будет видно, что нужна ручная помощь.

4) Поиск Facebook Group
-----------------------
В GUI появилась кнопка:

    🔍 Поиск Facebook Group

Как работает:

- ты вводишь новые ключевые слова;
- программа ищет группы Facebook;
- пропускает группы, которые уже есть в CollectorHub;
- пробует нажать Вступить / Join / Dołącz;
- если уже состоим или вступление принято сразу — добавляет группу в активные;
- если Facebook просит вопросы — добавляет группу со статусом pending_questions и выключает автопоиск до ручного подтверждения.

Группы хранятся в SQLite и экспортируются сюда:

    data/facebook_groups.txt

Статусы групп:

    active              — включена в автоматический обход
    join_requested      — заявка отправлена, нужно ждать/проверить
    pending_questions   — нужно вручную ответить на вопросы
    not_joined          — программа не смогла вступить

5) Как установить
-----------------
Закрой CollectorHub.
Скопируй все файлы из этого архива в папку проекта:

    C:\Users\Viktor\Documents\GitHub\CollectorHub

Соглашайся на замену файлов.
Потом запускай:

    start_collectorhub.bat

6) Файлы в архиве
-----------------
main.py
collector.py
database.py
groups_manager.py
collector_settings.py
facebook_forwarder.py
facebook_group_search.py
README_UPDATE_31.txt
