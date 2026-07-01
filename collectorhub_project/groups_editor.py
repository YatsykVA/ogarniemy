"""
CollectorHub - groups_editor.py
Окно редактирования Facebook-групп.

v26:
- видно название группы и ссылку;
- добавить / изменить / удалить / вверх / вниз / сохранить оставлены;
- добавлена кнопка копирования ссылки;
- группы сохраняются в SQLite и data/facebook_groups.txt.
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QDialog,
    QFormLayout,
    QLineEdit,
    QDialogButtonBox,
    QMessageBox,
    QHeaderView,
)

from groups_manager import GroupsManager, normalize_url, group_id_from_url, name_from_url


class GroupDialog(QDialog):
    def __init__(self, parent=None, name: str = "", url: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Группа Facebook")
        self.resize(640, 180)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(name)
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://www.facebook.com/groups/... или ID группы")

        form.addRow("Название группы:", self.name_edit)
        form.addRow("Ссылка / ID:", self.url_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        url = normalize_url(self.url_edit.text())
        name = self.name_edit.text().strip() or name_from_url(url)
        return name, url


class GroupsEditor(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Группы Facebook")
        self.resize(1050, 720)

        self.manager = GroupsManager()
        self.manager.seed()

        root = QVBoxLayout(self)

        title = QLabel("Группы Facebook")
        title.setObjectName("title")

        subtitle = QLabel("Здесь видно название группы и ссылку. Эти группы использует Collector.")
        subtitle.setObjectName("subtitle")

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Название группы", "Ссылка"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        buttons = QHBoxLayout()

        self.add_btn = QPushButton("➕ Добавить")
        self.edit_btn = QPushButton("✏️ Изменить")
        self.delete_btn = QPushButton("🗑 Удалить")
        self.copy_btn = QPushButton("📋 Копировать ссылку")
        self.up_btn = QPushButton("⬆️ Вверх")
        self.down_btn = QPushButton("⬇️ Вниз")
        self.save_btn = QPushButton("💾 Сохранить")
        self.close_btn = QPushButton("Закрыть")

        for btn in [
            self.add_btn,
            self.edit_btn,
            self.delete_btn,
            self.copy_btn,
            self.up_btn,
            self.down_btn,
            self.save_btn,
            self.close_btn,
        ]:
            buttons.addWidget(btn)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addWidget(self.table, 1)
        root.addLayout(buttons)

        self.add_btn.clicked.connect(self.add_group)
        self.edit_btn.clicked.connect(self.edit_group)
        self.delete_btn.clicked.connect(self.delete_group)
        self.copy_btn.clicked.connect(self.copy_link)
        self.up_btn.clicked.connect(self.move_up)
        self.down_btn.clicked.connect(self.move_down)
        self.save_btn.clicked.connect(self.save_groups)
        self.close_btn.clicked.connect(self.close)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_group())

        self.load_groups()
        self.apply_style()

    def apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #1f252d;
                color: #e8eef7;
                font-family: Segoe UI;
                font-size: 14px;
            }

            QLabel#title {
                font-size: 28px;
                font-weight: 800;
                margin-bottom: 4px;
            }

            QLabel#subtitle {
                color: #aab8ca;
                margin-bottom: 12px;
            }

            QTableWidget {
                background: #111820;
                alternate-background-color: #151e29;
                border: 1px solid #334155;
                border-radius: 12px;
                gridline-color: #263445;
                font-size: 14px;
            }

            QHeaderView::section {
                background: #243244;
                color: #e8eef7;
                border: 0;
                padding: 8px;
                font-weight: 800;
            }

            QTableWidget::item {
                padding: 8px;
            }

            QTableWidget::item:selected {
                background: #2f80ed;
                color: white;
            }

            QPushButton {
                min-height: 46px;
                min-width: 105px;
                border-radius: 12px;
                border: 1px solid #4b6b97;
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3d8df5,
                    stop:1 #2467bd
                );
                color: white;
                font-weight: 700;
                padding: 8px 12px;
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
                padding-top: 10px;
            }
        """)

    def _selected_row(self) -> int:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Нет выбора", "Сначала выбери группу.")
        return row

    def _set_row(self, row: int, name: str, url: str):
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(url))

    def _get_row(self, row: int) -> tuple[str, str]:
        name_item = self.table.item(row, 0)
        url_item = self.table.item(row, 1)
        return (
            name_item.text().strip() if name_item else "",
            url_item.text().strip() if url_item else "",
        )

    def load_groups(self):
        self.table.setRowCount(0)

        for row in self.manager.get_all():
            url = row["url"] or (f"https://www.facebook.com/groups/{row['group_id']}" if row["group_id"] else "")
            if not url:
                continue
            name = row["name"] or name_from_url(url)
            table_row = self.table.rowCount()
            self.table.insertRow(table_row)
            self._set_row(table_row, name, url)

    def add_group(self):
        dialog = GroupDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return

        name, url = dialog.values()
        if not url:
            return

        row = self.table.rowCount()
        self.table.insertRow(row)
        self._set_row(row, name, url)

    def edit_group(self):
        row = self._selected_row()
        if row < 0:
            return

        old_name, old_url = self._get_row(row)
        dialog = GroupDialog(self, old_name, old_url)
        if dialog.exec() != QDialog.Accepted:
            return

        name, url = dialog.values()
        if url:
            self._set_row(row, name, url)

    def delete_group(self):
        row = self._selected_row()
        if row < 0:
            return

        name, url = self._get_row(row)
        answer = QMessageBox.question(
            self,
            "Удалить группу",
            f"Удалить группу?\n\n{name}\n{url}",
        )

        if answer == QMessageBox.Yes:
            self.table.removeRow(row)

    def copy_link(self):
        row = self._selected_row()
        if row < 0:
            return

        _name, url = self._get_row(row)
        if url:
            QGuiApplication.clipboard().setText(url)
            QMessageBox.information(self, "Скопировано", "Ссылка группы скопирована.")

    def move_up(self):
        row = self._selected_row()
        if row <= 0:
            return

        current = self._get_row(row)
        above = self._get_row(row - 1)
        self._set_row(row - 1, *current)
        self._set_row(row, *above)
        self.table.selectRow(row - 1)

    def move_down(self):
        row = self._selected_row()
        if row < 0 or row >= self.table.rowCount() - 1:
            return

        current = self._get_row(row)
        below = self._get_row(row + 1)
        self._set_row(row + 1, *current)
        self._set_row(row, *below)
        self.table.selectRow(row + 1)

    def save_groups(self):
        groups = []
        seen = set()

        for i in range(self.table.rowCount()):
            name, url = self._get_row(i)
            url = normalize_url(url)
            if not url or url in seen:
                continue
            seen.add(url)
            groups.append({"name": name or name_from_url(url), "url": url})

        self.manager.replace_all(groups)
        self.load_groups()

        QMessageBox.information(
            self,
            "Сохранено",
            f"Сохранено групп: {len(groups)}",
        )


def main():
    app = QApplication(sys.argv)
    window = GroupsEditor()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
