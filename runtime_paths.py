"""Separate bundled read-only resources from persistent user data."""
from __future__ import annotations

import json
import ctypes.util
import os
import shutil
import sys
import uuid
from pathlib import Path

APP_ID = "turing-studio"
RESOURCE_ROOT = Path(__file__).resolve().parent


def data_root():
    override = os.environ.get("TURING_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_ID


def log_root():
    override = os.environ.get("TURING_LOG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP_ID


def system_environment():
    """Keep the frozen Python library directory out of system Chrome/FFmpeg."""
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        original = environment.pop("LD_LIBRARY_PATH_ORIG", None)
        if original:
            environment["LD_LIBRARY_PATH"] = original
        else:
            environment.pop("LD_LIBRARY_PATH", None)
    return environment


def usb_library(name="usb-1.0"):
    """Prefer the controlled bundled libusb build, with a system fallback."""
    if getattr(sys, "frozen", False):
        for filename in ("libusb-1.0.so.0", "libusb-1.0.so"):
            candidate = RESOURCE_ROOT / filename
            if candidate.is_file():
                return str(candidate)
    return ctypes.util.find_library(name)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_data(source, target=None):
    """Initialize an empty data directory transactionally, leaving originals intact.

    Never merge a different workspace over existing user data. A failed copy
    leaves the destination untouched and can be retried on the next launch.
    """
    source = Path(source).expanduser().resolve()
    target = Path(target or data_root()).resolve()
    if source == target or not (source / "config/state.json").is_file():
        return False
    if target.exists() and any(target.iterdir()):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / ("." + APP_ID + "-migrate-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        for name in ("config", "source", ".turing_data", "example"):
            if (source / name).is_dir():
                shutil.copytree(source / name, stage / name)
        atomic_json(stage / ".migration.json", {"source": str(source), "version": 1})
        # Publishing the whole workspace in one rename prevents a half-copied
        # config/source pair, including if the application is interrupted.
        if target.exists():
            target.rmdir()
        os.replace(stage, target)
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def migrate_development_data():
    # Frozen apps import explicitly through the installer/--import-data. Never
    # include the developer's personal media or absolute paths in a build.
    if not getattr(sys, "frozen", False) and not os.environ.get("TURING_DATA_DIR"):
        migrate_data(RESOURCE_ROOT)
