"""Persistent desktop options and per-user XDG login registration."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from runtime_paths import RESOURCE_ROOT, atomic_json, data_root

DEFAULTS = {"start_at_login": False, "silent_start": False}
_lock = threading.RLock()


def settings_path():
    return data_root() / "config/desktop.json"


def autostart_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart/turing-studio.desktop"


def load_settings():
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    return {key: value.get(key, default) if isinstance(value.get(key), bool) else default
            for key, default in DEFAULTS.items()}


def launch_command():
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).absolute()), str(Path(__file__).with_name("desktop.py"))]


def desktop_exec(arguments):
    # Desktop Entry Exec escaping is distinct from shell escaping. Percent is
    # a field-code marker even inside a quoted argument.
    def quote(value):
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError("启动路径不能包含换行")
        for old, new in (("\\", "\\\\"), ('"', '\\"'), ("`", "\\`"), ("$", "\\$"), ("%", "%%")):
            value = value.replace(old, new)
        return '"' + value + '"'
    # General Desktop Entry string unescaping consumes one layer of backslashes.
    return " ".join(quote(arg) for arg in arguments).replace("\\", "\\\\")


def sync_autostart(settings=None):
    settings = settings or load_settings()
    path = autostart_path()
    if not settings["start_at_login"]:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("[Desktop Entry]\nType=Application\nName=Turing Studio\n"
            "Comment=Restore the last display stream at login\nTerminal=false\n"
            "Exec=" + desktop_exec([*launch_command(), "--autostart"]) + "\n")
    temp = path.with_suffix(".desktop.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_settings(changes):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("桌面设置无效")
    if any(not isinstance(value, bool) for value in changes.values()):
        raise ValueError("桌面选项必须是布尔值")
    with _lock:
        previous = load_settings()
        updated = {**previous, **changes}
        sync_autostart(updated)
        try:
            atomic_json(settings_path(), updated)
        except OSError:
            sync_autostart(previous)
            raise
        return updated


def register_desktop_entry():
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "turing-studio.desktop"
    text = ("[Desktop Entry]\nType=Application\nName=Turing Studio\n"
            "Comment=Local editor and streaming for the Turing display\n"
            "Exec=" + desktop_exec([*launch_command(), "--show"]) + "\n"
            "Icon=" + str(RESOURCE_ROOT / "static/turing-studio.svg") + "\n"
            "Terminal=false\nCategories=Utility;HardwareSettings;\nStartupWMClass=TuringStudio\n")
    temp = entry.with_suffix(".desktop.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, entry)
    finally:
        temp.unlink(missing_ok=True)
    sync_autostart()
    return entry
