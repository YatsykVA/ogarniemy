"""
CollectorHub - main.py
GUI launcher.

v25:
- добавлена кнопка "Группы Facebook";
- кнопки главного окна сделаны крупнее, квадратнее и аккуратнее;
- группы редактируются через отдельное окно groups_editor.py.
"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QStatusBar,
    QTextEdit,
    QSpinBox,
    QComboBox,
    QInputDialog,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("CollectorHub")
        self.resize(1200, 900)

        self.process = None
        self.mode = None
        self.project_dir = Path(__file__).resolve().parent
        self.data_dir = self.project_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        root = QWidget()
        layout = QVBoxLayout(root)

        title = QLabel("CollectorHub")
        title.setObjectName("title")

        subtitle = QLabel("Facebook → фильтры → Telegram")
        subtitle.setObjectName("subtitle")

        button_grid = QGridLayout()
        button_grid.setSpacing(14)

        collector_buttons = QHBoxLayout()
        collector_buttons.setSpacing(14)

        settings_row = QHBoxLayout()
        settings_row.setSpacing(10)

        self.posts_limit_label = QLabel("Постов на группу:")
        self.posts_limit_label.setObjectName("settingsLabel")
        self.posts_limit_spin = QSpinBox()
        self.posts_limit_spin.setRange(1, 1000)
        self.posts_limit_spin.setSingleStep(10)

        try:
            from config import load_config
            self.posts_limit_spin.setValue(int(load_config().max_posts_per_group or 100))
        except Exception:
            self.posts_limit_spin.setValue(100)

        self.open_keywords_btn = QPushButton("🟢\nКлючевые слова")
        self.open_exclusions_btn = QPushButton("🔴\nСлова-исключения")
        self.groups_btn = QPushButton("👥\nГруппы Facebook")
        self.fb_search_btn = QPushButton("🔍\nАвтопоиск Facebook-групп")

        self.open_env_btn = QPushButton("⚙️\nTelegram .env")
        self.test_telegram_btn = QPushButton("📨\nТест Telegram")
        self.resend_last_btn = QPushButton("🧪\nПовторить последнее")

        self.clear_posts_btn = QPushButton("🧹\nОчистить посты")
        self.login_btn = QPushButton("🔐\nВойти Facebook")
        self.login_done_btn = QPushButton("✅\nЯ вошёл")
        self.fb_refresh_btn = QPushButton("🔄\nОбновить список групп")

        self.start_btn = QPushButton("▶\nЗапустить Collector")
        self.stop_btn = QPushButton("■\nОстановить")

        self.login_done_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.send_to_label = QLabel("Куда отправлять:")
        self.send_to_label.setObjectName("settingsLabel")
        self.send_to_combo = QComboBox()
        self.send_to_combo.addItem("Telegram", "telegram")
        self.send_to_combo.addItem("Facebook", "facebook")
        self.send_to_combo.addItem("Telegram + Facebook", "both")

        self.fb_target_label = QLabel("FB-группа куда публиковать:")
        self.fb_target_label.setObjectName("settingsLabel")
        self.fb_target_combo = QComboBox()
        self.fb_target_combo.setMinimumWidth(360)

        try:
            from config import load_config
            cfg = load_config()
            dest = (getattr(cfg, "send_destination", "telegram") or "telegram").lower()
            idx = self.send_to_combo.findData(dest)
            if idx >= 0:
                self.send_to_combo.setCurrentIndex(idx)
        except Exception:
            pass

        settings_row.addWidget(self.posts_limit_label)
        settings_row.addWidget(self.posts_limit_spin)
        settings_row.addSpacing(18)
        settings_row.addWidget(self.send_to_label)
        settings_row.addWidget(self.send_to_combo)
        settings_row.addSpacing(18)
        settings_row.addWidget(self.fb_target_label)
        settings_row.addWidget(self.fb_target_combo, 1)
        settings_row.addStretch(1)

        self.start_btn.setObjectName("startCollector")
        self.stop_btn.setObjectName("stopCollector")

        buttons = [
            self.open_keywords_btn,
            self.open_exclusions_btn,
            self.groups_btn,
            self.open_env_btn,
            self.test_telegram_btn,
            self.resend_last_btn,
            self.clear_posts_btn,
            self.login_btn,
            self.login_done_btn,
            self.fb_search_btn,
            self.fb_refresh_btn,
        ]

        positions = [
            (0, 0), (0, 1), (0, 2),
            (1, 0), (1, 1), (1, 2),
            (2, 0), (2, 1), (2, 2),
            (3, 0), (3, 1),
        ]

        for btn, pos in zip(buttons, positions):
            btn.setMinimumHeight(76)
            button_grid.addWidget(btn, *pos)

        for btn in [self.start_btn, self.stop_btn]:
            btn.setMinimumHeight(86)
            collector_buttons.addWidget(btn, 1)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(12)
        layout.addLayout(settings_row)
        layout.addSpacing(10)
        layout.addLayout(button_grid)
        layout.addSpacing(14)
        layout.addLayout(collector_buttons)
        layout.addSpacing(16)
        layout.addWidget(self.log_box, 1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Готов к работе")

        self.open_keywords_btn.clicked.connect(self.open_keywords)
        self.open_exclusions_btn.clicked.connect(self.open_exclusions)
        self.groups_btn.clicked.connect(self.open_groups_editor)
        self.fb_search_btn.clicked.connect(self.search_facebook_groups)
        self.fb_refresh_btn.clicked.connect(self.load_facebook_target_groups)
        self.send_to_combo.currentIndexChanged.connect(self.save_send_destination)
        self.fb_target_combo.currentIndexChanged.connect(self.save_facebook_target_from_combo)
        self.open_env_btn.clicked.connect(self.open_env)
        self.test_telegram_btn.clicked.connect(self.test_telegram)
        self.resend_last_btn.clicked.connect(self.resend_last)
        self.clear_posts_btn.clicked.connect(self.clear_posts_db)

        self.login_btn.clicked.connect(self.login_facebook)
        self.login_done_btn.clicked.connect(self.finish_login)
        self.start_btn.clicked.connect(self.start_collector)
        self.stop_btn.clicked.connect(self.stop_process)
        self.posts_limit_spin.valueChanged.connect(self.save_posts_limit)

        self.ensure_files()
        self.load_facebook_target_groups()

    def ensure_files(self):
        try:
            from words_manager import ensure_word_files
            ensure_word_files()
        except Exception as exc:
            self.log(f"Не удалось создать файлы слов: {exc}")

        try:
            from groups_manager import GroupsManager
            GroupsManager().seed()
        except Exception as exc:
            self.log(f"Не удалось подготовить группы Facebook: {exc}")

        env_file = self.project_dir / ".env"
        if not env_file.exists():
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=8730157974:AAGCB6RGEyfPRzmWmYpEujY-p2zZVb3ce54\n"
                "TELEGRAM_GROUP_ID=-1004294928850\n",
                encoding="utf-8",
            )

    def open_file(self, path: Path):
        self.ensure_files()
        try:
            os.startfile(str(path))
            self.log(f"Открыт файл: {path}")
        except Exception as exc:
            self.log(f"Не удалось открыть файл {path}: {exc}")

    def open_keywords(self):
        self.open_file(self.data_dir / "keywords.txt")

    def open_exclusions(self):
        self.open_file(self.data_dir / "exclusions.txt")

    def open_env(self):
        self.open_file(self.project_dir / ".env")

    def save_posts_limit(self, value: int):
        try:
            from config import update_config
            update_config(max_posts_per_group=int(value))
            self.statusBar().showMessage(f"Лимит сохранён: {value} постов на группу")
        except Exception as exc:
            self.log(f"Не удалось сохранить лимит постов: {exc}")

    def log(self, text: str):
        self.log_box.append(text.rstrip())
        self.statusBar().showMessage(text.rstrip()[:120])

    def run_script(self, script_name: str, mode: str, extra_env: dict | None = None):
        if self.process and self.process.state() != QProcess.NotRunning:
            self.log("Уже запущен процесс. Сначала останови его.")
            return

        script_file = self.project_dir / script_name

        if not script_file.exists():
            self.log(f"Ошибка: не найден файл: {script_file}")
            return

        self.process = QProcess(self)
        self.mode = mode

        self.process.setWorkingDirectory(str(self.project_dir))
        self.process.setProgram(sys.executable)
        self.process.setArguments(["-u", str(script_file)])

        env = self.process.processEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        if extra_env:
            for key, value in extra_env.items():
                env.insert(str(key), str(value))
        self.process.setProcessEnvironment(env)

        self.process.readyReadStandardOutput.connect(self.read_stdout)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)

        self.set_busy(True)

        if mode == "login":
            self.login_done_btn.setEnabled(True)
        else:
            self.login_done_btn.setEnabled(False)

        self.log(f"Запускаю: {script_name}")
        self.process.start()

    def set_busy(self, busy: bool):
        self.login_btn.setEnabled(not busy)
        self.start_btn.setEnabled(not busy)
        self.test_telegram_btn.setEnabled(not busy)
        self.resend_last_btn.setEnabled(not busy)
        self.clear_posts_btn.setEnabled(not busy)
        self.groups_btn.setEnabled(not busy)
        self.fb_search_btn.setEnabled(not busy)
        self.fb_refresh_btn.setEnabled(not busy)
        self.send_to_combo.setEnabled(not busy)
        self.fb_target_combo.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def open_groups_editor(self):
        self.ensure_files()
        self.log("Открываю редактор Facebook-групп...")
        self.run_script("groups_editor.py", "groups_editor")

    def save_send_destination(self):
        try:
            from config import update_config
            destination = self.send_to_combo.currentData() or "telegram"
            update_config(send_destination=destination, facebook_publish_enabled=destination in {"facebook", "both"})
            self.statusBar().showMessage(f"Куда отправлять сохранено: {self.send_to_combo.currentText()}")
        except Exception as exc:
            self.log(f"Не удалось сохранить направление отправки: {exc}")

    def load_facebook_target_groups(self):
        """Заполняет выпадающий список группами из CollectorHub Facebook groups."""
        try:
            from config import load_config
            from groups_manager import GroupsManager, best_group_name

            self.ensure_files()
            cfg = load_config()
            saved_url = (getattr(cfg, "facebook_target_group_url", "") or "").strip()
            saved_name = (getattr(cfg, "facebook_target_group_name", "") or "").strip()

            self.fb_target_combo.blockSignals(True)
            self.fb_target_combo.clear()
            self.fb_target_combo.addItem("— не выбрано —", "")

            rows = GroupsManager().get_all()
            selected_index = 0
            for row in rows:
                name = (row["name"] or "Facebook Group").strip()
                url = (row["url"] or "").strip()
                gid = (row["group_id"] or "").strip()
                if not url and gid:
                    url = f"https://www.facebook.com/groups/{gid}"
                if not url:
                    continue
                name = best_group_name(name, url)
                self.fb_target_combo.addItem(name, url)
                if saved_url and url.rstrip("/") == saved_url.rstrip("/"):
                    selected_index = self.fb_target_combo.count() - 1
                elif saved_name and name.casefold() == saved_name.casefold():
                    selected_index = self.fb_target_combo.count() - 1

            self.fb_target_combo.setCurrentIndex(selected_index)
            self.fb_target_combo.blockSignals(False)

            if self.fb_target_combo.count() <= 1:
                self.log("В списке Facebook-групп пока нет групп для выбора получателя.")
            else:
                self.statusBar().showMessage(f"Список FB-групп обновлён: {self.fb_target_combo.count() - 1}")
        except Exception as exc:
            try:
                self.fb_target_combo.blockSignals(False)
            except Exception:
                pass
            self.log(f"Не удалось загрузить список Facebook-групп: {exc}")

    def save_facebook_target_from_combo(self):
        try:
            from config import update_config
            url = (self.fb_target_combo.currentData() or "").strip()
            name = self.fb_target_combo.currentText().strip() if url else ""
            update_config(
                facebook_target_group_url=url,
                facebook_target_group_name=name,
                facebook_publish_enabled=bool(url),
            )
            if url:
                self.statusBar().showMessage(f"FB-группа для публикации выбрана: {self.fb_target_combo.currentText()}")
            else:
                self.statusBar().showMessage("FB-группа для публикации не выбрана")
        except Exception as exc:
            self.log(f"Не удалось сохранить Facebook-группу для публикации: {exc}")

    def search_facebook_groups(self):
        self.ensure_files()
        text, ok = QInputDialog.getText(
            self,
            "Автопоиск Facebook-групп",
            "Введи запросы в одну строку через запятую:\nнапример: Świdnica praca, Wałbrzych ogłoszenia, Українці Świdnica",
        )
        if not ok or not text.strip():
            self.log("Автопоиск Facebook-групп отменён")
            return
        self.log("Запускаю автопоиск Facebook-групп. Запросы через запятую будут обработаны по одному.")
        self.run_script("facebook_group_search.py", "facebook_group_search", extra_env={"FB_GROUP_SEARCH_QUERIES": text.strip()})

    def test_telegram(self):
        self.ensure_files()
        self.log("Отправляю тестовое сообщение в Telegram...")
        self.run_script("telegram_test.py", "telegram_test")

    def resend_last(self):
        self.ensure_files()
        self.log("Тест оформления: отправляю последнее сохранённое объявление ещё раз...")
        self.run_script("telegram_resend_last.py", "resend_last")

    def clear_posts_db(self):
        self.ensure_files()
        self.log("Очищаю базу сохранённых постов. Группы и слова не трогаю...")
        self.run_script("reset_posts_db.py", "clear_posts")

    def login_facebook(self):
        self.log("Откроется Chrome для входа в Facebook.")
        self.log("Collector НЕ запущен, страница не будет прыгать по группам.")
        self.log("Войди в Facebook, затем нажми кнопку: Я вошёл в Facebook.")
        self.run_script("facebook_login.py", "login")

    def finish_login(self):
        self.log("Сохраняю Facebook-сессию и закрываю окно входа...")
        self.stop_process()

    def start_collector(self):
        self.ensure_files()
        self.log("Перед запуском Collector читает data/keywords.txt, data/exclusions.txt и список групп из базы")
        self.log(f"Лимит поиска: {self.posts_limit_spin.value()} постов на группу")
        self.run_script("app.py", "collector")

    def stop_process(self):
        if not self.process or self.process.state() == QProcess.NotRunning:
            self.log("Процесс уже остановлен")
            return

        self.log("Останавливаю процесс...")
        self.process.terminate()

        if not self.process.waitForFinished(5000):
            self.log("Процесс не остановился мягко, принудительно закрываю...")
            self.process.kill()
            self.process.waitForFinished(3000)

    def read_stdout(self):
        data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        if data.strip():
            self.log(data)

    def read_stderr(self):
        data = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
        if data.strip():
            self.log(data)

    def process_finished(self, exit_code, exit_status):
        self.set_busy(False)
        self.login_done_btn.setEnabled(False)

        if self.mode == "login":
            self.log("Вход в Facebook завершён. Теперь можно запускать Collector.")
        elif self.mode == "groups_editor":
            self.log("Редактор групп закрыт. Обновляю выпадающий список FB-групп.")
            self.load_facebook_target_groups()
        elif self.mode == "facebook_group_search":
            if exit_code == 0:
                self.log("Автопоиск Facebook-групп завершён. Новые группы добавлены в список, если были найдены.")
                self.load_facebook_target_groups()
            else:
                self.log("Автопоиск Facebook-групп завершился с ошибкой или требует ручного действия в Facebook.")
                self.load_facebook_target_groups()
        elif self.mode == "telegram_test":
            if exit_code == 0:
                self.log("Тест Telegram прошёл успешно.")
            else:
                self.log("Тест Telegram не прошёл. Проверь .env, токен и ID группы.")
        elif self.mode == "resend_last":
            if exit_code == 0:
                self.log("Последнее сообщение повторно отправлено в Telegram.")
            else:
                self.log("Не удалось отправить последнее сообщение. Проверь .env и наличие постов в базе.")
        elif self.mode == "clear_posts":
            if exit_code == 0:
                self.log("База постов очищена. Теперь Collector заново будет считать посты новыми.")
            else:
                self.log("Не удалось очистить базу постов.")
        elif exit_code == 0:
            self.log("Collector завершил работу нормально")
        else:
            self.log(f"Процесс завершился/остановлен. Код: {exit_code}")

        self.mode = None

    def process_error(self, error):
        self.log(f"Ошибка QProcess: {error}")

    def closeEvent(self, event):
        if self.process and self.process.state() != QProcess.NotRunning:
            self.stop_process()
        event.accept()


def main():
    app = QApplication(sys.argv)

    app.setStyleSheet("""
        QMainWindow {
            background: #1f252d;
        }

        QLabel {
            color: white;
        }

        QLabel#title {
            font-size: 32px;
            font-weight: 900;
        }

        QLabel#subtitle {
            color: #aab8ca;
            font-size: 15px;
        }

        QTextEdit {
            background: #111820;
            color: #d7e3f4;
            border: 1px solid #334155;
            border-radius: 14px;
            padding: 10px;
            font-family: Consolas;
            font-size: 12px;
        }

        QPushButton {
            background: qlineargradient(
                x1:0, y1:0, x2:0, y2:1,
                stop:0 #3d8df5,
                stop:1 #2467bd
            );
            color: white;
            border: 1px solid #4b6b97;
            border-radius: 14px;
            padding: 10px;
            font-size: 14px;
            font-weight: 800;
        }

        QPushButton:hover {
            background: qlineargradient(
                x1:0, y1:0, x2:0, y2:1,
                stop:0 #5aa2ff,
                stop:1 #2f80ed
            );
        }

        QPushButton:pressed {
            background: #1d4f91;
            padding-top: 13px;
        }

        QLabel#settingsLabel {
            color: #d7e3f4;
            font-size: 15px;
            font-weight: 800;
        }

        QComboBox, QLineEdit {
            background: #111820;
            color: #e8eef7;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 8px;
            font-size: 14px;
            font-weight: 700;
        }

        QSpinBox {
            background: #111820;
            color: #e8eef7;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 8px;
            font-size: 15px;
            font-weight: 800;
            min-width: 110px;
        }


        /* Human-readable dialogs and popup menus */
        QDialog, QInputDialog, QMessageBox {
            background: #1f252d;
            color: #e8eef7;
        }

        QDialog QLabel, QInputDialog QLabel, QMessageBox QLabel {
            color: #e8eef7;
            font-size: 14px;
            font-weight: 700;
        }

        QInputDialog QLineEdit, QDialog QLineEdit {
            background: #f8fafc;
            color: #0f172a;
            border: 2px solid #60a5fa;
            border-radius: 10px;
            padding: 10px;
            font-size: 15px;
            font-weight: 800;
            selection-background-color: #2563eb;
            selection-color: white;
        }

        QComboBox {
            background: #f8fafc;
            color: #0f172a;
            border: 2px solid #60a5fa;
            border-radius: 10px;
            padding: 8px 30px 8px 10px;
            font-size: 14px;
            font-weight: 800;
        }

        QComboBox:hover {
            border: 2px solid #93c5fd;
            background: #ffffff;
        }

        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 26px;
            border-left: 1px solid #bfdbfe;
            background: #dbeafe;
            border-top-right-radius: 8px;
            border-bottom-right-radius: 8px;
        }

        QComboBox QAbstractItemView {
            background: #ffffff;
            color: #0f172a;
            selection-background-color: #2563eb;
            selection-color: #ffffff;
            border: 2px solid #60a5fa;
            outline: 0;
            padding: 4px;
            font-size: 14px;
            font-weight: 700;
        }

        QSpinBox, QSpinBox QLineEdit {
            background: #f8fafc;
            color: #0f172a;
            selection-background-color: #2563eb;
            selection-color: white;
        }

        QPushButton#startCollector, QPushButton#stopCollector {
            background: qlineargradient(
                x1:0, y1:0, x2:0, y2:1,
                stop:0 #34d399,
                stop:1 #059669
            );
            border: 1px solid #6ee7b7;
            color: white;
            font-size: 18px;
            font-weight: 900;
            border-radius: 16px;
        }

        QPushButton#startCollector:hover, QPushButton#stopCollector:hover {
            background: qlineargradient(
                x1:0, y1:0, x2:0, y2:1,
                stop:0 #6ee7b7,
                stop:1 #10b981
            );
        }

        QPushButton#startCollector:disabled, QPushButton#stopCollector:disabled {
            background: #475569;
            color: #cbd5e1;
            border: 1px solid #64748b;
        }

        QPushButton:disabled {
            background: #475569;
            color: #cbd5e1;
            border: 1px solid #64748b;
        }

        QStatusBar {
            color: white;
        }
    """)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
