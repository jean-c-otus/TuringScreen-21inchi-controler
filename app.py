"""Turing Studio: local Web editor, persisted projects, fresh frame pipeline.

- Immutable saved schemes: config/projects/<id>.json + source/<id>/.
- Editable draft: config/drafts/<id>.json + source/drafts/<id>/.
- Only an explicit "保存名称" operation commits the draft as a NEW saved snapshot.
- Live sensors recompose from the saved *base* on every tick.
- USB mode is STRICT: 'full' uses only 0xC8, 'partial' only 0xCC.
"""
from __future__ import annotations

import atexit
import logging
import math
import mimetypes
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from runtime_paths import RESOURCE_ROOT

from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image

import workspace
from device import Display, available, probe_device
from device_jobs import run_device_video_job
from media import IMAGE_EXT, VIDEO_EXT, defaults, image_frame, render, iter_video_frames
from scene import (compose, font_file, font_list, install_font,
                   MAX_FONT_BYTES, normalize_layers, render_preview,
                   sensor_snapshot, SENSOR_NAMES)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("turing")
Image.MAX_IMAGE_PIXELS = 36_000_000

ROOT=RESOURCE_ROOT
app=Flask(__name__,static_folder="static")
app.config["MAX_CONTENT_LENGTH"]=80*1024*1024
TOKEN=__import__("secrets").token_urlsafe(32)
lock=threading.RLock()
render_lock=threading.Lock()
stop_event=threading.Event()
reset_requested=threading.Event()
closing_event=threading.Event()
worker_thread=None
desktop_controller=None
lcd=Display()
last_bench=None
job={"state":"idle","progress":0,"total":0,"message":"选择已保存方案或上传素材",
     "fps":None,"bytes":0,"method":"","benchmark":None,
     "estimate":None,"changed_pixels":None,"frame_hash":"","regions":0,
     "scene_revision":0,"error_count":0,"last_error":"","retry_in":0,
     "recovery_phase":"","recovery_attempt":0,"device_check":None,
     "requested_fps":None,"actual_fps":None,"last_write_at":None,
     "overlay_interval_ms":None,"overlay_sample_ms":None,"overlay_render_ms":None,"overlay_send_ms":None,
     "overlay_transfer_ms":None,"overlay_wait_ms":None,"overlay_encode_ms":None,
     "overlay_last_bytes":0,"overlay_pixel_bytes":0,"overlay_poslen_bytes":0,
     "transport":workspace.DEFAULT_PREFERENCES["transport"],"active_id":None,"mount_ccw":90,"media_kind":None}
BUSY={"running","benchmark","live","retrying","reconnecting","checking","preparing","uploading"}
items={}

workspace.migrate_legacy_fonts()
legacy_count=workspace.migrate_legacy_media()
if legacy_count:
    log.info("Recovered %d legacy source assets (old unsaved editing state unavailable)",legacy_count)


def update(**fields):
    with lock:
        job.update(fields)


def current_job():
    with lock:
        return dict(job,connected=lcd.connected,benchmark=last_bench)


@app.get("/api/desktop")
def desktop_status():
    if desktop_controller is None:
        return jsonify(available=False)
    return jsonify(available=True, **desktop_controller.status())


@app.post("/api/desktop")
def desktop_preferences():
    if desktop_controller is None:
        return jsonify(error="请使用桌面启动器设置登录启动选项"),409
    try:
        from desktop_settings import save_settings
        settings=save_settings(request.get_json(silent=True))
        return jsonify(settings=settings)
    except (ValueError,OSError) as exc:
        return jsonify(error=str(exc)),400


@app.post("/api/desktop/hide")
def hide_desktop():
    if desktop_controller is None:
        return jsonify(error="桌面模式未启动"),409
    desktop_controller.hide_window()
    return jsonify(ok=True)


def materialize(draft_id):
    """Only return editable drafts. Never edit a persisted scheme in place."""
    if not draft_id:
        return None
    with lock:
        item=items.get(draft_id)
        if item is None:
            item=workspace.load_draft(draft_id)
            if item is None:
                return None
            item["layers"]=normalize_layers(item["layers"])
            item["settings"]=defaults(item["settings"])
            item["revision"]=int(item.get("revision",1))
            items[draft_id]=item
    if not item["base"].is_file() or not item["preview"].is_file():
        with render_lock:
            if not item["base"].is_file() or not item["preview"].is_file():
                regenerate(item,item["settings"],item["layers"])
    return item


def describe(item):
    """All editing controls are restored from the project's actual snapshot."""
    return {
        "id":item["id"],"name":item["name"],"kind":item["kind"],
        "settings":item["settings"],"layers":item["layers"],
        "device":item["device"],
        "base_project":item.get("base_project"),
        "draft":True,"edit_seq":item.get("edit_seq",0),
        "info":item["info"],"revision":item["revision"],
        "preview":f"/api/preview/{item['id']}",
        "source":f"/api/source/{item['id']}",
    }


def regenerate(item, options, layers, sequence=None, device=None):
    """Only update a temporary draft; atomically replace Python preview.

    The editor sends a monotonically increasing sequence number, rejecting
    stale render requests that arrive after a newer edit or explicit commit.
    """
    if not item.get("draft"):
        raise ValueError("已保存方案不可直接修改，请先创建临时草稿")
    if sequence is not None:
        sequence=int(sequence)
        if sequence<item.get("edit_seq",0):
            return item
    options=defaults(options)
    layers=normalize_layers(layers)
    device=workspace.validate_preferences(device if device is not None else item["device"])
    with lock:
        if (job["state"]=="live" and job.get("transport")=="device" and
                job.get("active_id")==item["id"] and
                (options!=item["settings"] or device["mount_ccw"]!=job["mount_ccw"] or
                 device["transport"]!="device")):
            raise ValueError("MCU 播放中可修改额外图层；修改背景、播放帧率、安装角度或刷新策略前请先停止")
    version=uuid.uuid4().hex[:12]
    ext=".png" if item["kind"]=="image" else ".mp4"
    tmp_base=item["base"].with_name("base-"+version+ext)
    tmp_preview=item["preview"].with_name("preview-"+version+ext)
    settings_changed=not item["base"].is_file() or item["settings"]!=options
    try:
        if settings_changed:
            info=render(item["source"],item["kind"],tmp_base,options)
            current_base=tmp_base
        else:
            info=item["info"]
            current_base=item["base"]
        render_preview(current_base,tmp_preview,item["kind"],layers,info["fps"])
        if settings_changed:
            os.replace(tmp_base,item["base"])
        os.replace(tmp_preview,item["preview"])
        with lock:
            item["settings"]=options
            item["layers"]=layers
            item["info"]=info
            item["device"]=device
            item["revision"]=int(item.get("revision",0))+1
            item["edit_seq"]=max(int(item.get("edit_seq",0)),
                                 sequence if sequence is not None else 0)
            workspace.write_draft(item)
    finally:
        tmp_base.unlink(missing_ok=True)
        tmp_preview.unlink(missing_ok=True)
    return item


@app.before_request
def protect_write():
    if request.method in ("POST","PUT","DELETE"):
        if request.headers.get("X-Turing-Token")!=TOKEN:
            return jsonify(error="未经授权的操作"),403


@app.errorhandler(413)
def upload_limit(_):
    return jsonify(error="单个文件不能超过 80 MB"),413


@app.get("/")
def index():
    return send_from_directory(ROOT/"static","index.html")


@app.get("/api/token")
def token():
    return jsonify(token=TOKEN)


@app.get("/api/device")
def device_status():
    return jsonify(awake=available(),connected=lcd.connected,
                   id="1d6b:0121",resolution="480×480",
                   state=job["state"],last_error=job.get("last_error",""))


@app.get("/api/status")
def status():
    return jsonify(current_job())


@app.get("/api/sensors")
def sensor_values():
    return jsonify(values=sensor_snapshot(),names=SENSOR_NAMES)


@app.get("/api/config")
def config():
    state=workspace.get_state()
    selected=None
    try:
        draft_id=state["active_draft"]
        if draft_id:
            selected=materialize(draft_id)
        if selected is None and state["active"]:
            # One-time migration of V3.2's saved "active" selection.
            selected=workspace.create_draft_from_project(state["active"])
            items[selected["id"]]=selected
        if selected is not None:
            selected=describe(selected)
    except Exception:
        log.exception("Unable to restore selected draft")
    return jsonify(projects=workspace.list_projects(),
                   active=selected.get("base_project") if selected else None,
                   preferences=state["preferences"],project=selected)


@app.post("/api/config/select")
def select_project():
    """Selecting a saved snapshot ALWAYS clones its assets/config into draft."""
    project_id=(request.get_json(silent=True) or {}).get("id")
    with lock:
        if job["state"] in BUSY:
            return jsonify(error="先停止正在运行的任务，再切换方案"),409
    try:
        draft=workspace.create_draft_from_project(project_id)
        draft["settings"]=defaults(draft["settings"])
        draft["layers"]=normalize_layers(draft["layers"])
        with lock:
            items[draft["id"]]=draft
        return jsonify(project=describe(draft),projects=workspace.list_projects())
    except (ValueError,OSError) as exc:
        return jsonify(error=str(exc)),400


@app.post("/api/config/preferences")
def save_preferences():
    payload=request.get_json(silent=True) or {}
    prefs=payload.get("preferences",{})
    try:
        with lock:
            if job["state"] in BUSY:
                raise ValueError("请先停止当前任务，再修改设备控件")
        state=workspace.save_state(preferences=prefs)
        if payload.get("id"):
            item=materialize(payload["id"])
            if item is None:
                return jsonify(error="临时草稿不存在"),404
            with lock:
                item["device"]=workspace.validate_preferences({
                    **item["device"],**prefs
                })
                workspace.write_draft(item)
            return jsonify(preferences=item["device"],active_draft=item["id"])
        return jsonify(state)
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)),400


@app.post("/api/config/commit")
def commit_project():
    """Explicitly save a NEW immutable scheme. Never rename/overwrite old one."""
    payload=request.get_json(silent=True) or {}
    with lock:
        if job["state"] in BUSY and job["state"]!="live":
            return jsonify(error="播放或测速期间无法保存方案"),409
    try:
        item=materialize(payload.get("id"))
        if item is None:return jsonify(error="临时方案不存在"),404
        name=str(payload.get("name","")).strip()[:120]
        if not name:return jsonify(error="请输入要保存的方案名称"),400
        with render_lock:
            incoming=payload.get("sequence")
            if incoming is not None and int(incoming)<item.get("edit_seq",0):
                return jsonify(error="编辑版本已变化，请重试保存"),409
            options=defaults(payload.get("settings",item["settings"]))
            layers=normalize_layers(payload.get("layers",item["layers"]))
            device=workspace.validate_preferences(
                payload.get("device",item["device"])
            )
            if (options!=item["settings"] or layers!=item["layers"]
                    or device!=item["device"]):
                regenerate(item,options,layers,sequence=incoming,device=device)
            elif incoming is not None:
                item["edit_seq"]=max(item["edit_seq"],int(incoming))
            saved=workspace.commit_draft(item,name)
        return jsonify(project=describe(item),saved={
            "id":saved["id"],"name":saved["name"]},
            projects=workspace.list_projects())
    except (ValueError,TypeError,OSError) as exc:
        return jsonify(error=str(exc)),400


@app.post("/api/config/rename")
def legacy_rename():
    # Old UI URLs must not silently overwrite an already-saved project.
    return jsonify(error="请更新前端并使用「保存名称」创建新的持久方案"),409


@app.post("/api/config/delete")
def delete_project():
    payload=request.get_json(silent=True) or {}
    key=payload.get("id")
    with lock:
        if job["state"] in BUSY:
            return jsonify(error="先停止 USB 任务再删除"),409
    try:
        workspace.remove_project(key)
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    # The current draft stays intact even when its source snapshot is deleted.
    return jsonify(projects=workspace.list_projects(),
                   active_draft=workspace.get_state()["active_draft"])


@app.get("/api/fonts")
def fonts_list():
    return jsonify(fonts=font_list())


@app.post("/api/fonts")
def upload_font():
    uploaded=request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="请上传 TTF / OTF / TTC 字体"),400
    try:
        record=install_font(uploaded.filename,
                            uploaded.stream.read(MAX_FONT_BYTES+1))
        return jsonify(font=record)
    except ValueError as exc:
        return jsonify(error=str(exc)),400


@app.get("/api/fonts/<font_id>")
def get_font(font_id):
    try:
        return send_file(font_file(font_id),
                         mimetype="application/octet-stream",
                         conditional=True,max_age=86400)
    except ValueError:
        return jsonify(error="字体不存在"),404


@app.get("/api/source/<project_id>")
def source_asset(project_id):
    try:
        item=materialize(project_id)
    except ValueError:
        item=None
    if not item:return jsonify(error="素材不存在"),404
    mimetype=mimetypes.guess_type(str(item["source"]))[0]
    return send_file(item["source"],mimetype=mimetype or "application/octet-stream",
                     conditional=True)


@app.get("/api/preview/<project_id>")
def preview_asset(project_id):
    try:item=materialize(project_id)
    except ValueError:item=None
    if not item:return jsonify(error="预览不存在"),404
    response=send_file(item["preview"],
       mimetype="image/png" if item["kind"]=="image" else "video/mp4",
       conditional=True)
    response.headers["Cache-Control"]="no-store"
    return response


@app.post("/api/upload")
def upload():
    with lock:
        if job["state"] in BUSY:
            return jsonify(error="USB 正在发送视频或测速，请先停止"),409
    uploaded=request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="请选择图片或视频"),400
    suffix=Path(uploaded.filename).suffix.lower()
    kind=("image" if suffix in IMAGE_EXT else
          "video" if suffix in VIDEO_EXT else None)
    if kind is None:return jsonify(error="不支持该文件格式"),400
    key=uuid.uuid4().hex
    folder=workspace.draft_assets(key)
    try:
        settings=defaults(request.form.to_dict())
        layers=normalize_layers(request.form.get("layers","[]"))
        original=workspace.make_draft_asset_path(key,suffix)
        uploaded.save(original)
        if original.stat().st_size>80*1024*1024:
            raise ValueError("上传文件过大")
        suffix_out=".png" if kind=="image" else ".mp4"
        item={
            "id":key,"name":Path(uploaded.filename).name[:120],"kind":kind,
            "source":original,"base":folder/("base"+suffix_out),
            "preview":folder/("preview"+suffix_out),
            "device_video":folder/"device.mp4",
            "settings":{},"layers":[],"revision":0,"edit_seq":0,
            "draft":True,"base_project":None,
            "device":workspace.get_state()["preferences"],
            "info":{"frames":1,"fps":0,"duration":0},
        }
        with render_lock:
            regenerate(item,settings,layers)
        with lock:
            items[key]=item
            workspace.save_state(active_draft=key,change_active_draft=True)
        return jsonify(project=describe(item),projects=workspace.list_projects())
    except Exception as exc:
        log.exception("Draft creation failed for %s",uploaded.filename)
        shutil.rmtree(folder,ignore_errors=True)
        workspace.draft_path(key).unlink(missing_ok=True)
        return jsonify(error=str(exc)),400


@app.post("/api/render")
def rerender():
    req=request.get_json(silent=True) or {}
    with lock:
        if job["state"] in BUSY and job["state"]!="live":
            return jsonify(error="视频或测速进行中，请先停止后编辑"),409
    try:
        item=materialize(req.get("id"))
        if item is None:return jsonify(error="找不到保存的方案"),404
        settings=defaults(req.get("settings",item["settings"]))
        layers=normalize_layers(req.get("layers",item["layers"]))
        with render_lock:
            regenerate(item,settings,layers,
                       sequence=req.get("sequence"),
                       device=req.get("device",item["device"]))
        return jsonify(project=describe(item),projects=workspace.list_projects())
    except (ValueError,OSError,Exception) as exc:
        log.exception("Render failed")
        return jsonify(error=f"重新渲染失败：{exc}"),400


# Benchmark example resource optional: ./example/<user-image>
EXAMPLE=workspace.ROOT/"example"


def example_image():
    if not EXAMPLE.is_dir():
        return None
    for file in sorted(EXAMPLE.iterdir()):
        if not file.is_file() or file.suffix.lower() not in IMAGE_EXT:
            continue
        if file.stat().st_size>30*1024*1024:
            continue
        try:
            with Image.open(file) as img:
                img.verify()
            return file
        except (OSError,ValueError):
            continue
    return None


@app.get("/api/example")
def example_status():
    path=example_image()
    return jsonify(found=bool(path),name=path.name if path else None)


def baseline(item, readings=None):
    """Reopen the current base every time. No cached rendered preview here."""
    with Image.open(item["base"]) as image:
        return compose(image,item["layers"],readings)


def first_media_frame(item, readings=None):
    if item["kind"]=="image":
        return baseline(item,readings)
    stream=iter_video_frames(item["base"],threading.Event())
    try:
        return compose(next(stream),item["layers"],readings)
    except StopIteration as exc:
        raise RuntimeError("视频无法解码出第一帧") from exc
    finally:
        stream.close()


def bench_valid(mode,mount):
    with lock:
        report=last_bench
    return (
        report is not None and lcd.connected
        and report["mode"]==mode and report["mount"]==mount
        and time.time()-report["measured_at"]<600
    )


def measure(item,mount,mode):
    global last_bench
    lcd.connect()
    path=example_image()
    if path is not None:
        with Image.open(path) as image:
            background=image_frame(image,{"fit":"cover"})
        label=f"example/{path.name}"
    elif item is not None:
        background=first_media_frame(item,sensor_snapshot(force=True))
        label=item["name"]
    else:
        raise RuntimeError("请先上传素材或在 example/ 里放入图片")
    result=lcd.benchmark(background,mount_ccw=mount,mode=mode)
    result.update(mount=mount,source=label)
    with lock:
        last_bench=result
    return result


def estimate_video(item,mount,mode):
    if mode=="full":
        return {"largest_bytes":926500,"average_bytes":926500,"samples":1}
    examples=[]
    stream=iter_video_frames(item["base"],threading.Event())
    sensors=sensor_snapshot()
    try:
        for frame in stream:
            examples.append(compose(frame,item["layers"],sensors))
            if len(examples)>=10:break
    finally:
        stream.close()
    if len(examples)<2:
        return {"largest_bytes":700000,"average_bytes":700000,"samples":len(examples)}
    payloads=[]
    prev=examples[0].rotate(-mount,expand=True)
    for frame in examples[1:]:
        current=frame.rotate(-mount,expand=True)
        _,length=Display.estimate_regions(prev,current)
        payloads.append(length)
        prev=current
    return {"largest_bytes":max(payloads),
            "average_bytes":int(sum(payloads)/len(payloads)),
            "samples":len(examples)}


def stream_attempt(kind,item,mount,mode,policy):
    global last_bench
    total_bytes=0
    sent=0
    saved_revision=None
    frame_times=[]

    def record_sent(result):
        if result["method"]=="skip":
            return
        frame_times.append(time.monotonic())
        del frame_times[:-32]
        actual=(round((len(frame_times)-1)/max(.001,frame_times[-1]-frame_times[0]),2)
                if len(frame_times)>1 else None)
        update(last_write_at=time.time(),actual_fps=actual,recovery_phase="",recovery_attempt=0)

    def remember_sent(revision):
        nonlocal saved_revision
        if revision==saved_revision:
            return
        # A live edit may finish while USB is writing. Only checkpoint a
        # revision whose assets still match the frame that actually arrived.
        with render_lock,lock:
            if revision!=item["revision"]:
                return
            try:
                workspace.remember_stream(item,mount,mode,policy)
                saved_revision=revision
            except Exception:
                log.exception("Unable to save last sent configuration")
        update(retry_in=0)

    try:
        if kind=="send" and mode=="device":
            if item is None or item["kind"] not in ("image","video"):
                raise ValueError("内部 MCU 为主刷新需要图片、GIF 或视频素材")
            def device_ready(revision, written=True):
                if written:
                    update(last_write_at=time.time(),recovery_phase="",recovery_attempt=0)
                remember_sent(revision)
            run_device_video_job(item,lcd,stop_event,closing_event,update,mount,
                                 device_dir=item["device"].get("device_dir","/mnt/SDCARD/video/"),
                                 state_lock=lock,on_ready=device_ready)
            return
        if kind=="check":
            lcd.close()
            lcd.connect(preserve_media=True)
        else:
            lcd.connect()
        if kind in ("check","reconnect"):
            details=lcd.connection_details()
            update(state="done",device_check=details,recovery_phase="",retry_in=0,
                   message=("设备握手成功" if kind=="check" else "屏幕 USB 已复位并重新握手")+
                           f" · {details['display_id']} · USB {details['bus']}:{details['address']}")
            return
        if kind=="benchmark":
            result=measure(item,mount,mode)
            update(state="done",estimate=result,method=mode,
                   message=f"只测 {mode}：{result['bps']/1024/1024:.2f} MiB/s。"
                           "请确认屏幕测试色块真的出现并还原")
            return
        if item is None:
            raise RuntimeError("没有选中方案")

        is_sensor=any(x["type"]=="sensor" for x in item["layers"])
        if item["kind"]=="image":
            with render_lock,lock:
                revision=item["revision"]
                frame=baseline(item,sensor_snapshot(force=True))
            result=lcd.draw(frame,mount_ccw=mount,mode=mode,
                            skip_unchanged=False)
            record_sent(result)
            remember_sent(revision)
            total_bytes+=result["bytes"]
            sent+=1
            update(progress=1,method=result["method"],bytes=total_bytes,
                   changed_pixels=result["changed_pixels"],
                   frame_hash=result["frame_hash"],regions=result["regions"],
                   scene_revision=item["revision"],
                   message=f"已发送第 {item['revision']} 版画面 · {mode} · "
                           f"SHA {result['frame_hash']}")
            if not is_sensor:
                update(state="done",message="图片发送完成，修改控件后会自动重新生成预览；"
                                           "再次发送使用最新方案。")
                return
            update(state="live",message="传感器实时运行：每秒重新读取数据、"
                                        "从背景重新合成画面")
            last_revision=item["revision"]
            while not stop_event.wait(1.0):
                with render_lock,lock:
                    layer_snapshot=[dict(row) for row in item["layers"]]
                    scene_revision=item["revision"]
                    with Image.open(item["base"]) as background:
                        updated=compose(background,layer_snapshot,
                                        sensor_snapshot(force=True))
                result=lcd.draw(updated,mount_ccw=mount,mode=mode)
                record_sent(result)
                remember_sent(scene_revision)
                total_bytes+=result["bytes"]
                if result["method"]!="skip": sent+=1
                update(progress=sent,method=result["method"],bytes=total_bytes,
                       changed_pixels=result["changed_pixels"],
                       frame_hash=result["frame_hash"],regions=result["regions"],
                       scene_revision=scene_revision,
                       message=f"实时第 {scene_revision} 版 · "
                               f"{result['changed_pixels']} 像素变化 · "
                               f"{result['method']} · SHA {result['frame_hash']}")
                last_revision=scene_revision
            update(state="stopped",message=f"已停止实时传感器 · 共更新 {sent} 帧")
            return

        if not bench_valid(mode,mount):
            update(state="benchmark",message=f"使用固定 {mode} 模式执行播放预检")
            measure(item,mount,mode)
        with lock:
            benchmark=dict(last_bench)
        estimate=estimate_video(item,mount,mode)
        requested=int(item["info"]["fps"])
        maxbytes=max(1,estimate["largest_bytes"])
        speed=max(1,benchmark["bps"])
        safe=max(.2,min(8,0.70/max(0.02,maxbytes/speed + 0.025)))
        safe=round(safe,2)
        if policy=="strict" and safe<requested:
            update(state="blocked",estimate={
                **estimate,"safe_fps":safe,"requested_fps":requested
            },message=f"测速预计 {safe}FPS，低于目标 {requested}FPS；"
                       "已阻止播放，不会切换 USB 协议。")
            return
        step=max(1,math.ceil(requested/max(.2,safe)))
        fps=requested/step
        update(state="running",fps=round(fps,2),
               estimate={**estimate,"safe_fps":safe,
                         "requested_fps":requested,"effective_fps":fps},
               message=f"以固定 {mode} 模式播放 · {fps:.2f}FPS")
        next_due=time.monotonic()
        extra_skips=0
        for index,frame in enumerate(iter_video_frames(item["base"],stop_event,loop=True)):
            if stop_event.is_set():break
            if index%step:continue
            if extra_skips:
                extra_skips-=1
                continue
            if stop_event.wait(max(0,next_due-time.monotonic())):break
            pixels=compose(frame,item["layers"],sensor_snapshot(force=True))
            result=lcd.draw(pixels,mount_ccw=mount,mode=mode)
            record_sent(result)
            remember_sent(item["revision"])
            total_bytes+=result["bytes"]
            if result["method"]!="skip":sent+=1
            update(progress=index%max(1,item["info"]["frames"])+1,
                   method=result["method"],bytes=total_bytes,
                   frame_hash=result["frame_hash"],
                   changed_pixels=result["changed_pixels"],
                   regions=result["regions"],
                   message=f"第 {index+1} 帧 · {mode} · "
                           f"{result['changed_pixels']} 变化像素 · "
                           f"SHA {result['frame_hash']}")
            extra_skips=max(0,math.ceil(result["seconds"]*fps)-1)
            next_due=max(next_due+1/max(.2,fps),time.monotonic())
        update(state="stopped" if stop_event.is_set() else "done",
               message=f"视频播放{'已停止' if stop_event.is_set() else '完成'} · "
                       f"{sent} 帧 · {total_bytes//1024} KiB")
    except Exception:
        raise


def worker(kind,item,mount,mode,policy):
    """Keep a send job alive across disconnects; Stop/Exit cancels retry waits."""
    global last_bench
    failures=0
    while not stop_event.is_set():
        attempt_started=time.time()
        try:
            must_reset=(kind=="reconnect" or reset_requested.is_set() or
                        (failures>=2 and getattr(lcd,"needs_reset",False) is True))
            if must_reset:
                reset_requested.clear()
                update(state="reconnecting",recovery_phase="usb_reset",retry_in=0,
                       message="正在定向复位图灵屏幕 USB 设备…")
                lcd.reset_usb(stop_event)
            if stop_event.is_set():
                break
            if failures or must_reset or kind=="check":
                update(state="checking" if kind=="check" else "reconnecting",
                       recovery_phase="handshake",message="正在执行屏幕 HELLO 握手…")
            stream_attempt(kind,item,mount,mode,policy)
            return
        except Exception as exc:
            if (job.get("last_write_at") or 0)>attempt_started:
                failures=0
            failures+=1
            log.exception("USB %s attempt %d failed; mode=%s",kind,failures,mode)
            try:
                lcd.close()
            except Exception:
                log.exception("USB release failed during recovery")
            with lock:
                last_bench=None
                error_count=job.get("error_count",0)+1
            if stop_event.is_set():
                break
            delay=min(30,2**min(failures,5))
            keep_retrying=kind in ("send","reconnect")
            message=(f"{mode} 传输异常：{exc}；已记录日志，{delay} 秒后重连继续发送"
                     if keep_retrying else f"{'设备检测' if kind=='check' else '测速'}失败：{exc}")
            if kind in ("check","reconnect"):
                update(device_check={**(job.get("device_check") or {}),
                                     "handshake":False,"error":str(exc)})
            update(state="retrying" if keep_retrying else "error",
                   last_error=str(exc),error_count=error_count,
                   retry_in=delay if keep_retrying else 0,actual_fps=None,
                   recovery_phase="waiting" if keep_retrying else "",recovery_attempt=failures,
                   message=message)
            if desktop_controller is not None:
                try:
                    desktop_controller.notify_error(message)
                except Exception:
                    log.exception("Desktop notification failed; USB recovery will continue")
            if not keep_retrying:
                return
            for remaining in range(delay,0,-1):
                update(retry_in=remaining)
                if reset_requested.is_set() or stop_event.wait(1):
                    break
            if stop_event.is_set():
                break
            update(state="running",message="正在重新连接屏幕并恢复发送…",retry_in=0)
    update(state="stopped",message="发送已停止",retry_in=0,recovery_phase="")


def start_job(kind,item,mount,mode,policy):
    global worker_thread
    with lock:
        if closing_event.is_set():
            raise ValueError("程序正在退出")
        if job["state"] in BUSY or (worker_thread and worker_thread.is_alive()):
            raise ValueError("已有 USB 任务")
        if kind=="send" and item:
            item["device"]=workspace.validate_preferences({**item["device"],
                "mount_ccw":mount,"transport":mode,"policy":policy})
            workspace.write_draft(item)
        stop_event.clear()
        reset_requested.clear()
        job.update(state={"benchmark":"benchmark","check":"checking","reconnect":"reconnecting"}.get(kind,"running"),
                   message="开始发送…",progress=0,
                   total=item["info"]["frames"] if item else 0,
                   fps=None,actual_fps=None,bytes=0,method=mode,
                   overlay_interval_ms=None,overlay_sample_ms=None,overlay_render_ms=None,overlay_send_ms=None,
                   overlay_transfer_ms=None,overlay_wait_ms=None,overlay_encode_ms=None,
                   overlay_last_bytes=0,overlay_pixel_bytes=0,overlay_poslen_bytes=0,
                   estimate=None,frame_hash="",changed_pixels=None,regions=0,
                   recovery_phase="",recovery_attempt=0,retry_in=0,
                   transport=mode,active_id=item["id"] if item else None,mount_ccw=mount,
                   media_kind=item["kind"] if item else None,
                   requested_fps=item["info"]["fps"] if item and item["kind"]=="video" else None)
        worker_thread=threading.Thread(target=worker,args=(kind,item,mount,mode,policy),
                                       name="turing-usb",daemon=True)
        worker_thread.start()


def resume_last_stream():
    try:
        item=workspace.restore_stream()
        if item is None:
            update(message="首次使用：发送一次方案后，下次启动将自动恢复")
            return False
        item["settings"]=defaults(item["settings"])
        item["layers"]=normalize_layers(item["layers"])
        with lock:
            items[item["id"]]=item
        if not item["base"].is_file() or not item["preview"].is_file():
            with render_lock:
                regenerate(item,item["settings"],item["layers"])
        prefs=item["device"]
        start_job("send",item,prefs["mount_ccw"],prefs["transport"],prefs["policy"])
        return True
    except Exception as exc:
        log.exception("Unable to restore last stream")
        update(state="error",message=f"恢复上次发送失败：{exc}",last_error=str(exc))
        if desktop_controller is not None:
            desktop_controller.notify_error(f"恢复上次发送失败：{exc}")
        return False


def job_args(payload):
    preferences=workspace.get_state()["preferences"]
    mount=int(payload.get("mount_ccw",preferences["mount_ccw"]))
    mode=str(payload.get("transport",preferences["transport"]))
    policy=str(payload.get("policy",preferences["policy"]))
    if mount not in (0,90,180,270):
        raise ValueError("安装角度不合法")
    if mode not in ("full","partial","device"):
        raise ValueError("USB 刷新策略无效")
    if policy not in ("auto","strict"):
        raise ValueError("帧率策略不合法")
    return mount,mode,policy


@app.post("/api/benchmark")
def benchmark():
    payload=request.get_json(silent=True) or {}
    try:
        item=materialize(payload.get("id")) if payload.get("id") else None
        if item is None and example_image() is None:
            return jsonify(error="请上传素材，或在 example/ 内放入示例图片"),400
        mount,mode,policy=job_args(payload)
        if mode=="device":
            raise ValueError("内部 MCU 刷新无需逐帧 USB 测速；请直接发送素材")
        start_job("benchmark",item,mount,mode,policy)
        return jsonify(ok=True,mode=mode)
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)),409


@app.post("/api/send")
def send():
    payload=request.get_json(silent=True) or {}
    try:
        item=materialize(payload.get("id"))
        if not item:return jsonify(error="请先选择已保存的方案"),400
        mount,mode,policy=job_args(payload)
        if mode!="device" and item["kind"]=="video" and item["info"]["fps"]>8:
            raise ValueError("电脑逐帧发送最高支持 8 FPS；请降低帧率或选择内部 MCU 为主刷新")
        start_job("send",item,mount,mode,policy)
        return jsonify(ok=True,revision=item["revision"],mode=mode)
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)),409


@app.post("/api/stop")
def stop():
    stop_event.set()
    return jsonify(ok=True,message=("停止请求已发出；若正在上传文件，将完成当前文件后停止，避免屏幕协议错位"
                                   if job.get("transport")=="device" else
                                   "停止请求已发出，将在当前帧完成后停止"))


@app.post("/api/reconnect")
def reconnect():
    global last_bench
    with lock:
        if job["state"]=="retrying":
            if worker_thread and worker_thread.is_alive():
                if stop_event.is_set():
                    return jsonify(error="上个任务正在停止，请稍后再执行 USB 重连"),409
                reset_requested.set()
                job.update(message="已请求立即执行屏幕 USB 定向复位…")
                return jsonify(ok=True,message=job["message"],queued=True)
            job.update(state="idle")
        if job["state"] in BUSY:
            return jsonify(error="请先停止发送任务"),409
        last_bench=None
    try:
        start_job("reconnect",None,*job_args({}))
        return jsonify(ok=True,message="正在复位屏幕 USB 并重新握手，结果将在传输状态中显示")
    except ValueError as exc:
        return jsonify(error=str(exc)),409


@app.post("/api/device/check")
def check_device():
    details=probe_device()
    with lock:
        details["connected"]=lcd.connected
        details["last_error"]=job.get("last_error","")
        job["device_check"]=details
        if job["state"] in BUSY or (worker_thread and worker_thread.is_alive()):
            return jsonify(device=details,checking=False,
                           message="已读取 USB 设备信息；任务运行中，握手状态请查看传输状态")
    try:
        start_job("check",None,*job_args({}))
        return jsonify(device=details,checking=True,message="已读取设备信息，正在执行真实握手检测…")
    except ValueError as exc:
        return jsonify(error=str(exc)),409


@atexit.register
def shutdown():
    closing_event.set()
    stop_event.set()
    if worker_thread and worker_thread.is_alive():
        # An accepted file upload must finish its declared byte count. Keep
        # the process/session alive until completion (or a transfer error),
        # rather than killing a daemon worker in the middle of the file.
        worker_thread.join(timeout=None if job.get("transport")=="device" else 15)
        if worker_thread.is_alive():
            log.warning("USB worker still finishing its current transfer at exit")
            return
    try:lcd.close()
    except Exception:log.exception("USB release at shutdown failed")
