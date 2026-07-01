"""
CollectorHub - logger.py
Единый логгер проекта.

Исправлено для серверной панели: лог пишется в COLLECTOR_DATA_DIR/logs,
чтобы веб-интерфейс видел реальные события автопоиска, входа Facebook и запуска Collector.
"""

from pathlib import Path
import logging
import os

DATA_DIR = Path(os.getenv("COLLECTOR_DATA_DIR", Path(__file__).resolve().parent / "data")).resolve()
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "collectorhub.log"

logger = logging.getLogger("CollectorHub")
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

def info(message: str):
    logger.info(message)

def warning(message: str):
    logger.warning(message)

def error(message: str):
    logger.error(message)

if __name__ == "__main__":
    info("CollectorHub logger initialized")
    warning("Test warning")
    error("Test error")
    print(f"Log file: {LOG_FILE}")
