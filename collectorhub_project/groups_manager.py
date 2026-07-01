"""
CollectorHub - groups_manager.py
Управление Facebook-группами.

v26:
- список групп больше НЕ зашит в Python-код;
- основной источник: SQLite + редактируемый файл data/facebook_groups.txt;
- файл поддерживает формат: Название группы | ссылка/ID;
- при запуске группы из файла добавляются/обновляются в базе;
- UI может сохранять название + ссылку.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from config import DATA_DIR, DATABASE_FILE

BASE_DIR = Path(__file__).resolve().parent
DB = DATABASE_FILE
GROUPS_FILE = DATA_DIR / "facebook_groups.txt"

GROUPS_TEMPLATE = """# CollectorHub Facebook groups v26
# Главный редактируемый список групп.
# Формат строки:
# Название группы | ссылка или ID группы
#
# Пример:
# Моя группа | https://www.facebook.com/groups/1271418521730582

Моя группа | 1271418521730582
Робота Свідниця / Работа Свидница | 928070684265382
Всі Свої Свідніца (Świdnica) 2.0 | 362464684262451
Świdnica - Ogłoszenia | Praca | Kupię | Sprzedam | Zamienię | 951748024974064
SOBÓTKA ŚWIDNICA ORAZ OKOLICE | 3188640361396390
ВРОЦЛАВ WROCŁAW | РАБОТА ПОДРАБОТКА | УСЛУГИ | 424165785522397
OGŁOSZENIA ŚWIDNICA | 807706018288951
Świdnica Ogłoszenia | 1533755107157323
ŚWIDNICA - OGŁOSZENIA | 1372825173526655
PRACA ŚWIDNICA, WAŁBRZYCH, NOWA RUDA - szukam/ zatrudnię/ ogłoszenia | 883663095173403
PRACA Świdnica i okolice szukam/zatrudnię | 212048401843482
Praca: Świdnica, Wałbrzych i okolice | https://www.facebook.com/groups/pracaswidnicaiwalbrzych/
Praca Świdnica | 778210792702084
Wałbrzych, Świdnica, Kamienna Góra, Świebodzice, Głuszyca - OGŁOSZENIA | 1725383154758085
Żarów, Świdnica i okolice- Ogłoszenia | Praca | Kupię | Sprzedam | Zamienię | 188148991597915
Ogłoszenia Świdnica-Wałbrzych-Jedlina Zdrój-Szczawno Zdrój-Boguszów Gorce | https://www.facebook.com/groups/dolnoslaski/
Świdnica wynajmę / sprzedam / kupię | https://www.facebook.com/groups/swidnicawynajme/
ŚWIDNICA - Spotted,Sprzedam,Kupię,Zamienię, Wynajmę,Informacje,Reklama | 1975016392756621
Świdnica Świebodzice Strzegom Dzierżoniów Żarów Jaworzyna- SPRZEDAM, KUPIĘ | 609061041377325
Praca | 163307837077402
PRACA | 1731418577071985
Świebodzice - Ogłoszenia | Praca | Kupię | Sprzedam | Zamienię | 1589832267801969
"""


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    if value.isdigit():
        return f"https://www.facebook.com/groups/{value}"

    if value.startswith("www."):
        value = "https://" + value

    if value.startswith("facebook.com/"):
        value = "https://" + value

    return value


def group_id_from_url(url: str) -> str:
    url = normalize_url(url)
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "groups":
            return parts[1]
    except Exception:
        pass
    return ""


def name_from_url(url: str) -> str:
    """Fallback for display name. Never returns numeric group ID as a visible name."""
    url = normalize_url(url)
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if parts:
            tail = parts[-1].strip()
            if tail and not tail.isdigit():
                return tail.replace("-", " ").replace("_", " ").strip()
    except Exception:
        pass

    return "Facebook Group"


def is_bad_group_name(name: str) -> bool:
    value = (name or "").strip()
    low = value.lower()
    if not value:
        return True
    if value.isdigit():
        return True
    if low.startswith("facebook group ") and low.split()[-1].isdigit():
        return True
    if "facebook.com/groups" in low:
        return True
    return False


def best_group_name(name: str, url: str) -> str:
    name = (name or "").strip()
    if not is_bad_group_name(name):
        return name
    return name_from_url(url)


def _looks_like_group_ref(value: str) -> bool:
    value = (value or "").strip().lower()
    return bool(value.isdigit() or "facebook.com/groups" in value or value.startswith("www.facebook.com/groups"))


def parse_group_line(line: str) -> tuple[str, str, str] | None:
    """Возвращает (name, group_id, url) из строки файла."""
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    # Формат: Название | ссылка/ID. Названия могут содержать |, поэтому берём последний разделитель.
    if "|" in raw:
        left, right = raw.rsplit("|", 1)
        name = left.strip()
        ref = right.strip()
    else:
        name = ""
        ref = raw

    url = normalize_url(ref)
    if not url:
        return None

    gid = group_id_from_url(url)
    name = best_group_name(name, url)

    return name, gid, url


class GroupsManager:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB)
        self.conn.row_factory = sqlite3.Row
        self._ensure_table()

    def _ensure_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS facebook_groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                group_id TEXT,
                url TEXT,
                enabled INTEGER DEFAULT 1
            )
        """)
        self.conn.commit()

    def ensure_groups_file(self):
        if not GROUPS_FILE.exists():
            GROUPS_FILE.write_text(GROUPS_TEMPLATE, encoding="utf-8")
            return

        # Старый файл v25 содержал только ссылки без названий.
        # Один раз переводим его в новый формат с названиями, старый сохраняем рядом.
        raw = GROUPS_FILE.read_text(encoding="utf-8", errors="replace")
        if "CollectorHub Facebook groups v26" not in raw:
            backup = GROUPS_FILE.with_suffix(".v25.backup.txt")
            try:
                if not backup.exists():
                    backup.write_text(raw, encoding="utf-8")
            except Exception:
                pass
            GROUPS_FILE.write_text(GROUPS_TEMPLATE, encoding="utf-8")

    def seed(self):
        """
        Группы больше не хардкодятся в коде.
        При первом запуске создаём data/facebook_groups.txt и импортируем его в SQLite.
        """
        self.ensure_groups_file()
        self.import_from_file(update_existing=True)
        self.cleanup_duplicates()
        self.export_to_file()

    def import_from_file(self, update_existing: bool = True):
        self.ensure_groups_file()

        raw = GROUPS_FILE.read_text(encoding="utf-8")
        items = []

        pending_name = ""
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line == "=======":
                continue

            parsed = parse_group_line(line)
            if parsed:
                name, gid, url = parsed

                # Поддержка старого формата: строка с названием, следующая строка с ID/URL.
                if not ("|" in line) and _looks_like_group_ref(line) and pending_name:
                    name = pending_name
                    pending_name = ""

                if _looks_like_group_ref(line) or "|" in line:
                    items.append((name, gid, url))
                else:
                    pending_name = line
                continue

            pending_name = line

        for name, gid, url in items:
            name = best_group_name(name, url)
            existing = self.conn.execute(
                """
                SELECT id FROM facebook_groups
                WHERE (group_id<>'' AND group_id=?) OR (url<>'' AND url=?)
                LIMIT 1
                """,
                (gid, url),
            ).fetchone()

            if existing:
                if update_existing:
                    self.conn.execute(
                        "UPDATE facebook_groups SET name=?, group_id=?, url=?, enabled=1 WHERE id=?",
                        (name, gid, url, existing["id"]),
                    )
            else:
                self.conn.execute(
                    "INSERT INTO facebook_groups(name, group_id, url, enabled) VALUES(?,?,?,1)",
                    (name, gid, url),
                )

        self.conn.commit()

    def export_to_file(self):
        rows = self.get_all()
        lines = [
            "# CollectorHub Facebook groups v26",
            "# Формат: Название группы | ссылка или ID группы",
            "# Можно редактировать через кнопку 'Группы Facebook' в программе.",
            "",
        ]

        for row in rows:
            url = row["url"] or (f"https://www.facebook.com/groups/{row['group_id']}" if row["group_id"] else "")
            name = best_group_name(row["name"], url)
            if url:
                lines.append(f"{name} | {url}")

        GROUPS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def get_all(self):
        cur = self.conn.execute(
            "SELECT * FROM facebook_groups ORDER BY id ASC"
        )
        return cur.fetchall()

    def get_enabled(self):
        self.import_from_file(update_existing=True)
        cur = self.conn.execute(
            "SELECT * FROM facebook_groups WHERE enabled=1 ORDER BY id ASC"
        )
        return cur.fetchall()

    def replace_all(self, groups: list):
        self.conn.execute("DELETE FROM facebook_groups")

        for item in groups:
            if isinstance(item, dict):
                name = (item.get("name") or "").strip()
                ref = (item.get("url") or item.get("group_id") or "").strip()
            else:
                name = ""
                ref = str(item or "").strip()

            url = normalize_url(ref)
            if not url:
                continue

            gid = group_id_from_url(url)
            name = best_group_name(name, url)

            self.conn.execute(
                "INSERT INTO facebook_groups(name, group_id, url, enabled) VALUES(?,?,?,1)",
                (name, gid, url),
            )

        self.conn.commit()
        self.export_to_file()

    def set_enabled(self, row_id: int, enabled: bool):
        self.conn.execute(
            "UPDATE facebook_groups SET enabled=? WHERE id=?",
            (1 if enabled else 0, row_id)
        )
        self.conn.commit()
        self.export_to_file()

    def add_or_update_group(self, name: str, url_or_id: str, enabled: bool = True) -> str:
        """
        Добавляет группу или обновляет существующую без дубля.
        Возвращает: "added" или "updated".
        В name никогда не записываем ID группы.
        """
        url = normalize_url(url_or_id)
        if not url:
            return "skipped"

        gid = group_id_from_url(url)
        name = best_group_name(name, url)

        existing = self.conn.execute(
            """
            SELECT * FROM facebook_groups
            WHERE (group_id<>'' AND group_id=?) OR (url<>'' AND url=?)
            ORDER BY id ASC
            LIMIT 1
            """,
            (gid, url),
        ).fetchone()

        if existing:
            old_name = existing["name"] or ""
            final_name = name if not is_bad_group_name(name) else best_group_name(old_name, url)
            self.conn.execute(
                "UPDATE facebook_groups SET name=?, group_id=?, url=?, enabled=? WHERE id=?",
                (final_name, gid, url, 1 if enabled else 0, existing["id"]),
            )
            self.conn.commit()
            self.cleanup_duplicates()
            self.export_to_file()
            return "updated"

        self.conn.execute(
            "INSERT INTO facebook_groups(name, group_id, url, enabled) VALUES(?,?,?,?)",
            (name, gid, url, 1 if enabled else 0),
        )
        self.conn.commit()
        self.cleanup_duplicates()
        self.export_to_file()
        return "added"

    def add_group(self, name: str, url_or_id: str, enabled: bool = True) -> bool:
        """Совместимость со старым кодом: True только если добавлена новая."""
        return self.add_or_update_group(name, url_or_id, enabled) == "added"

    def cleanup_duplicates(self):
        """Удаляет дубли по group_id/url, оставляя первую запись и нормальное название."""
        rows = self.conn.execute("SELECT * FROM facebook_groups ORDER BY id ASC").fetchall()
        seen = {}
        delete_ids = []

        for row in rows:
            url = normalize_url(row["url"] or (f"https://www.facebook.com/groups/{row['group_id']}" if row["group_id"] else ""))
            gid = group_id_from_url(url)
            key = gid.lower() if gid else url.rstrip("/").lower()
            if not key:
                continue

            name = best_group_name(row["name"], url)
            if key not in seen:
                seen[key] = row["id"]
                if name != row["name"] or url != (row["url"] or "") or gid != (row["group_id"] or ""):
                    self.conn.execute(
                        "UPDATE facebook_groups SET name=?, group_id=?, url=? WHERE id=?",
                        (name, gid, url, row["id"]),
                    )
            else:
                keep_id = seen[key]
                keep = self.conn.execute("SELECT * FROM facebook_groups WHERE id=?", (keep_id,)).fetchone()
                keep_name = best_group_name(keep["name"], url)
                final_name = keep_name
                if is_bad_group_name(keep_name) and not is_bad_group_name(name):
                    final_name = name
                self.conn.execute(
                    "UPDATE facebook_groups SET name=?, group_id=?, url=?, enabled=1 WHERE id=?",
                    (final_name, gid, url, keep_id),
                )
                delete_ids.append(row["id"])

        for row_id in delete_ids:
            self.conn.execute("DELETE FROM facebook_groups WHERE id=?", (row_id,))
        self.conn.commit()

    def has_group(self, url_or_id: str) -> bool:
        url = normalize_url(url_or_id)
        gid = group_id_from_url(url)
        row = self.conn.execute(
            """
            SELECT id FROM facebook_groups
            WHERE (group_id<>'' AND group_id=?) OR (url<>'' AND url=?)
            LIMIT 1
            """,
            (gid, url),
        ).fetchone()
        return bool(row)


if __name__ == "__main__":
    gm = GroupsManager()
    gm.seed()
    print(f"Loaded {len(gm.get_enabled())} groups")
