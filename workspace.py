"""Persistent saved projects and independent, recoverable editing drafts.

Immutable saved snapshots:  config/projects/<id>.json + source/<id>/
Temporary drafts:           config/drafts/<id>.json + source/drafts/<id>/
Current draft and defaults:  config/state.json
Uploaded font library:       source/fonts/

The ONLY function that creates a saved project is commit_draft().
Loading a saved project clones both files and control state into a new draft.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from runtime_paths import data_root, migrate_development_data

migrate_development_data()
ROOT = data_root()
CONFIG = ROOT / "config"
PROJECTS = CONFIG / "projects"
DRAFTS = CONFIG / "drafts"
SOURCE = ROOT / "source"
DRAFT_SOURCE = SOURCE / "drafts"
FONTS = SOURCE / "fonts"
STATE = CONFIG / "state.json"
lock = threading.RLock()
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
SUFFIX_PATTERN = re.compile(r"^\.[a-z0-9]{2,6}$")

for folder in (CONFIG, PROJECTS, DRAFTS, SOURCE, DRAFT_SOURCE, FONTS):
    folder.mkdir(parents=True, exist_ok=True)

DEFAULT_PREFERENCES = {
    "mount_ccw": 90, "transport": "device", "policy": "auto",
}


def valid_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("无效的方案编号")
    return value


def _json_write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json_read(path: Path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def project_assets(project_id):
    return SOURCE / valid_id(project_id)


def draft_assets(draft_id):
    return DRAFT_SOURCE / valid_id(draft_id)


def config_path(project_id):
    return PROJECTS / (valid_id(project_id) + ".json")


def draft_path(draft_id):
    return DRAFTS / (valid_id(draft_id) + ".json")


def validate_preferences(value):
    if not isinstance(value, dict):
        raise ValueError("设备偏好格式无效")
    prefs = {**DEFAULT_PREFERENCES, **value}
    try:
        prefs["mount_ccw"] = int(prefs["mount_ccw"])
    except (ValueError, TypeError) as exc:
        raise ValueError("安装角度无效") from exc
    if prefs["mount_ccw"] not in (0, 90, 180, 270):
        raise ValueError("安装角度无效")
    if prefs["transport"] not in ("full", "partial", "device"):
        raise ValueError("USB 刷新策略无效")
    if prefs.get("playback_mode", "host") not in ("host", "device"):
        raise ValueError("播放模式无效")
    if prefs.get("device_dir", "/mnt/SDCARD/video/") not in (
            "/mnt/SDCARD/video/", "/root/video/"):
        raise ValueError("设备视频目录无效")
    if prefs["policy"] not in ("auto", "strict"):
        raise ValueError("帧率策略无效")
    return prefs


def get_state():
    with lock:
        data = _json_read(STATE) or {}
        try:
            prefs = validate_preferences(data.get("preferences", {}))
        except ValueError:
            prefs = DEFAULT_PREFERENCES.copy()
        active_draft = data.get("active_draft")
        if not isinstance(active_draft, str) or not ID_PATTERN.fullmatch(active_draft):
            active_draft = None
        # Older V3.2 stored a saved project id in "active"; migrate lazily.
        old_saved = data.get("active")
        if not isinstance(old_saved, str) or not ID_PATTERN.fullmatch(old_saved):
            old_saved = None
        return {
            "active_draft": active_draft,
            "active": old_saved,
            "preferences": prefs,
        }


def save_state(active_draft=None, change_active_draft=False,
               preferences=None, active=None, change_active=False):
    with lock:
        state = get_state()
        if change_active_draft:
            state["active_draft"] = valid_id(active_draft) if active_draft else None
        if change_active:
            state["active"] = valid_id(active) if active else None
        if preferences is not None:
            state["preferences"] = validate_preferences({
                **state["preferences"], **preferences
            })
        _json_write(STATE, state)
        return state


def _record(item, draft=False):
    original = Path(item["source"])
    suffix = original.suffix.lower()
    if not SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError("素材后缀无效")
    info = {
        "version": 2,
        "id": valid_id(item["id"]),
        "name": str(item.get("name", "未命名方案"))[:120],
        "kind": item["kind"],
        "suffix": suffix,
        "settings": copy.deepcopy(item["settings"]),
        "layers": copy.deepcopy(item["layers"]),
        "info": copy.deepcopy(item["info"]),
        "device": validate_preferences(item.get("device", get_state()["preferences"])),
        "revision": int(item.get("revision", 1)),
        "updated_at": time.time(),
    }
    if draft:
        info["draft"] = True
        info["base_project"] = item.get("base_project")
        info["edit_seq"] = int(item.get("edit_seq", 0))
    return info


def write_project(item):
    """Legacy/migration entrypoint. Normal UI never calls this directly."""
    entry = _record(item, draft=False)
    with lock:
        _json_write(config_path(entry["id"]), entry)
    return entry


def write_draft(item):
    entry = _record(item, draft=True)
    with lock:
        _json_write(draft_path(entry["id"]), entry)
    return entry


def _load_record(project_id, draft=False):
    project_id = valid_id(project_id)
    path = draft_path(project_id) if draft else config_path(project_id)
    row = _json_read(path)
    if not row or row.get("id") != project_id:
        return None
    suffix = row.get("suffix", "")
    if not isinstance(suffix, str) or not SUFFIX_PATTERN.fullmatch(suffix):
        return None
    kind = row.get("kind")
    if kind not in ("image", "video"):
        return None
    folder = draft_assets(project_id) if draft else project_assets(project_id)
    original = folder / ("original" + suffix)
    if not original.is_file():
        return None
    ext = ".png" if kind == "image" else ".mp4"
    try:
        device = validate_preferences(row.get("device", get_state()["preferences"]))
    except ValueError:
        device = get_state()["preferences"]
    return {
        "id": project_id,
        "name": row.get("name", project_id),
        "kind": kind,
        "source": original,
        "base": folder / ("base" + ext),
        "preview": folder / ("preview" + ext),
        "device_video": folder / "device.mp4",
        "settings": row.get("settings", {}),
        "layers": row.get("layers", []),
        "info": row.get("info", {}),
        "device": device,
        "revision": int(row.get("revision", 1)),
        "edit_seq": int(row.get("edit_seq", 0)) if draft else 0,
        "base_project": row.get("base_project") if draft else project_id,
        "draft": draft,
        "updated_at": row.get("updated_at", 0),
    }


def load_project(project_id):
    return _load_record(project_id, draft=False)


def load_draft(draft_id):
    return _load_record(draft_id, draft=True)


def list_projects():
    with lock:
        entries = []
        for path in PROJECTS.glob("*.json"):
            try:
                project = load_project(path.stem)
            except (ValueError, TypeError):
                continue
            if project:
                entries.append({
                    "id": project["id"], "name": project["name"],
                    "kind": project["kind"],
                    "revision": project["revision"],
                    "updated_at": project["updated_at"],
                })
        return sorted(entries, key=lambda row: row["updated_at"], reverse=True)


def _copy_assets(source: Path, target: Path):
    """Copy, never hardlink: editing a draft cannot alter a saved snapshot."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("base.png", "base.mp4", "preview.png", "preview.mp4", "device.mp4"):
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, target / name)
    originals = [p for p in source.iterdir()
                 if p.is_file() and p.name.startswith("original.")]
    if len(originals) != 1:
        raise ValueError("方案原始素材缺失或不唯一")
    if originals[0].stat().st_size > 80 * 1024 * 1024:
        raise ValueError("原始素材过大")
    shutil.copy2(originals[0], target / originals[0].name)


def create_draft_from_project(project_id):
    """Every selection creates a fresh temp copy, leaving saved files untouched."""
    project = load_project(project_id)
    if project is None:
        raise ValueError("已保存方案不存在")
    draft_id = uuid.uuid4().hex
    folder = draft_assets(draft_id)
    try:
        _copy_assets(project_assets(project_id), folder)
        copy_item = {
            **project,
            "id": draft_id,
            "source": folder / project["source"].name,
            "base": folder / project["base"].name,
            "preview": folder / project["preview"].name,
            "device_video": folder / "device.mp4",
            "base_project": project_id,
            "draft": True,
            "edit_seq": 0,
        }
        write_draft(copy_item)
        save_state(active_draft=draft_id, change_active_draft=True)
        return load_draft(draft_id)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        draft_path(draft_id).unlink(missing_ok=True)
        raise


def make_draft_asset_path(draft_id, suffix):
    if not SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError("文件格式不支持")
    folder = draft_assets(draft_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / ("original" + suffix)


def commit_draft(item, name):
    """ONLY explicit Save Name call creates a persistent saved scheme.

    Never overwrite an existing saved snapshot. The current draft survives
    with its same ID, but now records the new snapshot as its base_project.
    Any future edit modifies only that draft.
    """
    name = str(name or "").strip()[:120]
    if not name:
        raise ValueError("请填写方案名称")
    draft_id = valid_id(item["id"])
    if not item.get("draft") or load_draft(draft_id) is None:
        raise ValueError("只能保存正在编辑的临时草稿")
    saved_id = uuid.uuid4().hex
    target = project_assets(saved_id)
    try:
        _copy_assets(draft_assets(draft_id), target)
        saved_item = {
            **item,
            "id": saved_id,
            "name": name,
            "source": target / Path(item["source"]).name,
            "base": target / Path(item["base"]).name,
            "preview": target / Path(item["preview"]).name,
            "device_video": target / "device.mp4",
            "draft": False,
            "base_project": saved_id,
        }
        # Atomic manifest write happens only after assets are copied.
        write_project(saved_item)
    except Exception:
        config_path(saved_id).unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        raise

    item["name"] = name
    item["base_project"] = saved_id
    write_draft(item)
    save_state(active_draft=draft_id, change_active_draft=True)
    return load_project(saved_id)


def remove_project(project_id):
    project_id = valid_id(project_id)
    with lock:
        config_path(project_id).unlink(missing_ok=True)
        shutil.rmtree(project_assets(project_id), ignore_errors=True)
        # Drafts cloned from this snapshot remain independent and editable.
        if get_state()["active"] == project_id:
            save_state(active=None, change_active=True)


def remember_stream(item, mount, mode, policy):
    """Publish an independent snapshot only after a frame was sent successfully.

    This is a playback checkpoint, not a user-named saved project. Later edits
    or deletion of a named project cannot alter its assets or controls.
    """
    with lock:
        snapshot_id = uuid.uuid4().hex
        folder = SOURCE / "resume" / snapshot_id
        manifest = CONFIG / "last-stream.json"
        previous = _json_read(manifest) or {}
        try:
            _copy_assets(Path(item["source"]).parent, folder)
            entry = _record({**item, "id": snapshot_id,
                             "device": {**item["device"], "mount_ccw": mount,
                                        "transport": mode, "policy": policy}})
            entry["base_project"] = item.get("base_project")
            _json_write(manifest, entry)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        old_id = previous.get("id")
        if isinstance(old_id, str) and ID_PATTERN.fullmatch(old_id) and old_id != snapshot_id:
            shutil.rmtree(SOURCE / "resume" / old_id, ignore_errors=True)


def restore_stream():
    """Clone the last sent checkpoint into an editable draft for this session."""
    with lock:
        row = _json_read(CONFIG / "last-stream.json")
        if not row:
            return None
        snapshot_id = valid_id(row.get("id"))
        suffix = row.get("suffix", "")
        kind = row.get("kind")
        if not SUFFIX_PATTERN.fullmatch(suffix) or kind not in ("image", "video"):
            raise ValueError("上次发送配置损坏")
        origin = SOURCE / "resume" / snapshot_id
        draft_id = uuid.uuid4().hex
        folder = draft_assets(draft_id)
        ext = ".png" if kind == "image" else ".mp4"
        try:
            _copy_assets(origin, folder)
            item = {
                **row, "id": draft_id, "source": folder / ("original" + suffix),
                "base": folder / ("base" + ext), "preview": folder / ("preview" + ext),
                "device_video": folder / "device.mp4",
                "draft": True, "edit_seq": 0,
            }
            write_draft(item)
            save_state(active_draft=draft_id, change_active_draft=True,
                       preferences=item["device"])
            return load_draft(draft_id)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            draft_path(draft_id).unlink(missing_ok=True)
            raise


def migrate_legacy_fonts():
    """Copy pre-V3.2 uploads, never delete old files."""
    old = ROOT / ".turing_data" / "fonts"
    if not old.is_dir() or old.resolve() == FONTS.resolve():
        return
    for file in old.iterdir():
        if not file.is_file():
            continue
        if file.name == "index.json" or (
            re.fullmatch(r"[a-f0-9]{32}\.(ttf|otf|ttc)", file.name)
            and file.stat().st_size <= 8 * 1024 * 1024
        ):
            target = FONTS / file.name
            if not target.exists():
                shutil.copy2(file, target)


def migrate_legacy_media():
    """Recover pre-V3.2 original sources as saved projects.

    Original edit controls were never persisted in those older releases;
    defaults are used rather than claiming the historical settings exist.
    """
    legacy = ROOT / ".turing_data"
    if not legacy.is_dir():
        return 0
    count = 0
    for folder in legacy.iterdir():
        if not folder.is_dir() or not ID_PATTERN.fullmatch(folder.name):
            continue
        if config_path(folder.name).is_file():
            continue
        originals = [f for f in folder.iterdir()
                     if f.is_file() and f.name.startswith(("source.", "original."))]
        if not originals:
            continue
        old = originals[0]
        if old.stat().st_size > 80 * 1024 * 1024:
            continue
        suffix = old.suffix.lower()
        kind = ("image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
                else "video" if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".gif"}
                else None)
        if kind is None:
            continue
        saved = project_assets(folder.name)
        saved.mkdir(parents=True, exist_ok=True)
        original = saved / ("original" + suffix)
        if not original.exists():
            shutil.copy2(old, original)
        ext = ".png" if kind == "image" else ".mp4"
        for file in ("base" + ext, "preview" + ext):
            target = saved / file
            if not target.exists() and (folder / file).is_file():
                shutil.copy2(folder / file, target)
        entry = {
            "id": folder.name, "name": "恢复素材 " + folder.name[:8],
            "kind": kind, "source": original,
            "settings": {
                "fit": "cover", "rotation": 0, "background": "#101827",
                "fps": 2, "zoom": 1.0, "offset_x": 0, "offset_y": 0,
            },
            "layers": [],
            "info": {"duration": 0, "frames": 1, "fps": 0} if kind == "image"
                    else {"duration": 0, "frames": 0, "fps": 2},
            "device": get_state()["preferences"],
            "revision": 1,
        }
        write_project(entry)
        count += 1
    return count
