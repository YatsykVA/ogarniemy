CLEANED PROJECT

Удалено только то, что является мусором/временными файлами/локальными данными разработки:

- .git/ — история Git, на сервер для запуска не нужна
- __pycache__/ и *.pyc — кэш Python
- *.log — временные логи проверки
- server.py.backup_before_encoding_fix_20260624 — старая резервная копия
- collectorhub_project/data/playwright_profile/ — локальный профиль браузера Playwright/Chrome, кэш и потенциальные сессии
- collectorhub_project/data/facebook_groups.v25.backup.txt — старая резервная копия списка групп
- collectorhub_project/data/debug_posts.txt — отладочный runtime-файл

Оставлено:
- server.py
- marketing_bot.py
- HTML/CSS/JS
- assets/
- downloads/
- requirements.txt
- Procfile
- .railwayignore
- .gitignore
- server.db
- ogarniemy_data/server.db
- collectorhub_project/ с исходниками и настройками

Важно:
- Если CollectorHub должен работать через уже залогиненный Facebook-профиль, после удаления playwright_profile нужно будет снова выполнить вход.
- Базы данных не удалялись, чтобы не потерять рабочие данные.
