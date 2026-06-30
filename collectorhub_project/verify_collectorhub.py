"""
CollectorHub quick verifier.
Запускать из папки проекта:
python verify_collectorhub.py
"""
from __future__ import annotations

import importlib
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = [
    "main.py",
    "app.py",
    "collector_settings.py",
    "facebook_group_search.py",
    "facebook_forwarder.py",
    "groups_manager.py",
    "database.py",
]

print("CollectorHub verifier")
print("Project:", ROOT)
print()

ok = True
for name in FILES:
    path = ROOT / name
    if not path.exists():
        print(f"MISSING: {name}")
        if name in {"app.py", "main.py"}:
            ok = False
        continue
    try:
        py_compile.compile(str(path), doraise=True)
        print(f"OK syntax: {name}")
    except Exception as exc:
        ok = False
        print(f"BAD syntax: {name}: {exc}")

print()
for mod in ["collector_settings", "facebook_group_search", "groups_manager"]:
    try:
        importlib.import_module(mod)
        print(f"OK import: {mod}")
    except Exception as exc:
        ok = False
        print(f"BAD import: {mod}: {exc}")

print()
print("RESULT:", "OK" if ok else "PROBLEMS FOUND")
raise SystemExit(0 if ok else 1)
