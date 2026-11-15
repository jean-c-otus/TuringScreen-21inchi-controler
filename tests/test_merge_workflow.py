"""Offline regression tests for the V3.3 draft / Scheme C media merge.

No USB panel, Flask server, or FFmpeg executable is needed to run these.
"""
from __future__ import annotations

import hashlib
import json
import importlib.util
import sys
import types
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from media import defaults, device_video_signature

import workspace


class SavedDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        paths = {
            "ROOT": folder,
            "CONFIG": folder / "config",
            "PROJECTS": folder / "config" / "projects",
            "DRAFTS": folder / "config" / "drafts",
            "SOURCE": folder / "source",
            "DRAFT_SOURCE": folder / "source" / "drafts",
            "FONTS": folder / "source" / "fonts",
            "STATE": folder / "config" / "state.json",
        }
        for p in paths.values():
            if p.suffix != ".json":
                p.mkdir(parents=True, exist_ok=True)
        self.patch = patch.multiple(workspace, **paths)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.id = uuid.uuid4().hex

    def fixture(self, kind="video"):
        folder = workspace.project_assets(self.id)
        folder.mkdir()
        suffix = ".mp4" if kind == "video" else ".png"
        original = folder / ("original" + suffix)
        if kind == "video":
            original.write_bytes(b"sample-original")
            (folder / "base.mp4").write_bytes(b"sample-base")
            (folder / "preview.mp4").write_bytes(b"sample-preview")
            (folder / "device.mp4").write_bytes(b"device-content")
        else:
            for name in ("original.png", "base.png", "preview.png"):
                Image.new("RGB", (480, 480), "#abccef").save(folder / name)
        item = {
            "id": self.id,
            "name": "initial snapshot",
            "kind": kind,
            "source": original,
            "settings": {"fit": "cover", "rotation": 0, "background": "#101827",
                         "fps": 2, "zoom": 1.0, "offset_x": 0, "offset_y": 0},
            "layers": [],
            "info": {"fps": 2, "frames": 2, "duration": 1.0},
            "revision": 1,
            "device": {"mount_ccw": 90, "transport": "full", "policy": "auto",
                       "playback_mode": "device", "device_dir": "/mnt/SDCARD/video/"},
        }
        if kind == "video":
            item["info"]["device_video_signature"] = device_video_signature(original, item["settings"], 90)
        workspace.write_project(item)
        return workspace.load_project(self.id)

    def test_select_edit_save_never_changes_old_project(self):
        self.fixture(kind="image")
        original_manifest = workspace.config_path(self.id).read_bytes()
        original_image = (workspace.project_assets(self.id) / "original.png").read_bytes()
        draft = workspace.create_draft_from_project(self.id)
        self.assertNotEqual(draft["id"], self.id)
        self.assertEqual(draft["base_project"], self.id)
        self.assertEqual(workspace.get_state()["active_draft"], draft["id"])
        draft["settings"]["rotation"] = 90
        draft["layers"].append({"type": "text", "text": "DRAFT"})
        draft["device"]["transport"] = "partial"
        workspace.write_draft(draft)
        self.assertEqual(workspace.config_path(self.id).read_bytes(), original_manifest)
        saved = workspace.commit_draft(draft, "new version")
        self.assertEqual(saved["name"], "new version")
        self.assertNotEqual(saved["id"], self.id)
        self.assertEqual(saved["settings"]["rotation"], 90)
        self.assertEqual(saved["device"]["transport"], "partial")
        draft["settings"]["rotation"] = 180
        workspace.write_draft(draft)
        self.assertEqual(workspace.load_project(saved["id"])["settings"]["rotation"], 90)
        self.assertEqual(workspace.config_path(self.id).read_bytes(), original_manifest)
        self.assertEqual((workspace.project_assets(self.id) / "original.png").read_bytes(), original_image)

    def test_device_video_cache_survives_snapshot_and_clone(self):
        self.fixture()
        first = workspace.create_draft_from_project(self.id)
        self.assertTrue(first["device_video"].is_file())
        self.assertEqual(first["device_video"].read_bytes(), b"device-content")
        first["device"]["device_dir"] = "/root/video/"
        workspace.write_draft(first)
        saved = workspace.commit_draft(first, "revision-2")
        self.assertTrue(saved["device_video"].is_file())
        second = workspace.create_draft_from_project(saved["id"])
        self.assertEqual(second["device_video"].read_bytes(), b"device-content")
        self.assertEqual(second["device"]["device_dir"], "/root/video/")

    def test_device_video_runner_reuses_uploaded_media_without_overwriting_saved(self):
        saved = self.fixture()
        draft = workspace.create_draft_from_project(self.id)
        digest = hashlib.sha256(draft["device_video"].read_bytes()).hexdigest()
        expected_path = "/mnt/SDCARD/video/known.mp4"
        draft["info"]["device_upload_hash"] = digest
        draft["info"]["device_path"] = expected_path
        workspace.write_draft(draft)
        original_manifest = workspace.config_path(self.id).read_bytes()
        stopped = threading.Event()
        actions = []

        class LCD:
            def connect(self):
                actions.append("connect")
            def initialize_video_overlay(self, mount):
                actions.append(("overlay", mount))

        class FakeDeviceMedia:
            def __init__(self, lcd):
                pass
            def get_file_size(self, path):
                self_path = path
                actions.append(("size", self_path))
                return len(b"device-content")
            def upload_file(self, *args, **kwargs):
                raise AssertionError("Already-uploaded clip must not be uploaded again")
            def play_video(self, path):
                actions.append(("play", path))
                stopped.set()
            def stop_video(self):
                actions.append("stop")

        # Load the actual runner against a fake USB/media module, keeping the
        # exercise runnable without PyUSB or a physical Rev C display.
        mock_media = types.ModuleType("device_media")
        mock_media.DeviceMedia = FakeDeviceMedia
        mock_media.VIDEO_DIR_INTERNAL = "/mnt/SDCARD/video/"
        mock_media.VIDEO_DIR_COMPAT = "/root/video/"
        mock_scene = types.ModuleType("scene")
        mock_scene.compose_overlay = lambda *args: None
        mock_scene.sensor_snapshot = lambda **kwargs: {}
        spec = importlib.util.spec_from_file_location(
            "test_device_jobs_runtime", Path(__file__).resolve().parents[1] / "device_jobs.py"
        )
        runner = importlib.util.module_from_spec(spec)
        statuses = []
        with patch.dict(sys.modules, {"device_media": mock_media, "scene": mock_scene}):
            spec.loader.exec_module(runner)
            runner.run_device_video_job(
                draft, LCD(), stopped, threading.Event(),
                lambda **kwargs: statuses.append(kwargs), 90)
        self.assertIn(("size", expected_path), actions)
        self.assertIn(("play", expected_path), actions)
        self.assertIn("stop", actions)
        self.assertEqual(statuses[-1]["state"], "stopped")
        self.assertEqual(workspace.config_path(self.id).read_bytes(), original_manifest)

    def test_invalid_playback_preferences_are_rejected(self):
        with self.assertRaises(ValueError):
            workspace.validate_preferences({"device_dir": "/root/../tmp/"})
        with self.assertRaises(ValueError):
            workspace.validate_preferences({"playback_mode": "other"})

    def native_fixture(self):
        self.fixture()
        item = workspace.create_draft_from_project(self.id)
        item["device"]["transport"] = "device"
        workspace.write_draft(item)
        return item

    def test_native_checkpoint_restores_strategy_clip_and_remote_cache(self):
        item = self.native_fixture()
        item["info"].update(device_path="/mnt/SDCARD/video/known.mp4", device_upload_hash="verified")
        workspace.remember_stream(item, 90, "device", "auto")
        restored = workspace.restore_stream()
        self.assertEqual(restored["device"]["transport"], "device")
        self.assertEqual(restored["device_video"].read_bytes(), b"device-content")
        self.assertEqual(restored["info"]["device_upload_hash"], "verified")

    def test_native_send_bypasses_all_host_frames_and_bandwidth_tests(self):
        import app
        item = self.native_fixture()
        with patch.object(app, "run_device_video_job") as native, \
             patch.object(app, "lcd") as lcd, \
             patch.object(app, "measure", side_effect=AssertionError("No frame benchmark")), \
             patch.object(app, "iter_video_frames", side_effect=AssertionError("No frame decoding")):
            app.stream_attempt("send", item, 90, "device", "strict")
        native.assert_called_once()
        lcd.draw.assert_not_called()
        lcd.connect.assert_not_called()

    def test_native_api_accepts_images_and_rejects_frame_benchmark(self):
        import app
        item = self.native_fixture()
        headers = {"X-Turing-Token": app.TOKEN}
        client = app.app.test_client()
        with patch.object(app, "materialize", return_value=item), patch.object(app, "start_job") as start:
            sent = client.post("/api/send", headers=headers, json={"id": item["id"], "transport":"device"})
            self.assertEqual(sent.status_code, 200, sent.json)
            self.assertEqual(start.call_args.args[3], "device")
            start.reset_mock()
            bench = client.post("/api/benchmark", headers=headers, json={"id":item["id"],"transport":"device"})
            self.assertEqual(bench.status_code, 409)
            item["kind"]="image"
            sent = client.post("/api/send", headers=headers, json={"id":item["id"],"transport":"device"})
            self.assertEqual(sent.status_code, 200, sent.json)
            self.assertEqual(start.call_args.args[1]["kind"], "image")

    def test_native_overlay_edit_does_not_invalidate_clip_but_background_change_is_rejected(self):
        import app
        item = self.native_fixture()
        with patch.object(app, "job", {**app.job,"state":"live","transport":"device",
                                      "active_id":item["id"],"mount_ccw":90}), \
             patch.object(app, "render_preview", side_effect=lambda base,dest,*args: dest.write_bytes(b"preview")):
            signature = item["info"]["device_video_signature"]
            app.regenerate(item,item["settings"],[{"type":"text","text":"NEW"}])
            self.assertEqual(item["info"]["device_video_signature"],signature)
            self.assertEqual(item["device_video"].read_bytes(),b"device-content")
            with self.assertRaisesRegex(ValueError,"先停止"):
                app.regenerate(item,{**item["settings"],"rotation":17.5},item["layers"])

    def test_native_upload_live_overlay_edits_and_clear_do_not_reupload_background(self):
        import device_jobs
        item = self.native_fixture()
        stopped, closing = threading.Event(), threading.Event()
        lcd = Mock()
        lcd.update_video_overlay.return_value = {"method":"video-overlay","bytes":30,"changed_pixels":1,"regions":1}
        media = Mock()
        media.get_file_size.return_value = 0
        media.upload_file.return_value = {"verified":True}
        statuses, checkpoints = [], []
        class EditSequence:
            count = 0
            def is_set(self):
                return stopped.is_set()
            def wait(self, seconds):
                self.count += 1
                if self.count == 1:
                    item["layers"]=[{"type":"text","text":"LIVE"}]
                    item["revision"]+=1
                elif self.count == 2:
                    item["layers"]=[]
                    item["revision"]+=1
                else:
                    stopped.set()
                return stopped.is_set()
        with patch.object(device_jobs,"DeviceMedia",return_value=media), \
             patch.object(device_jobs,"render_device_video",side_effect=AssertionError("Reuse encoded clip")):
            device_jobs.run_device_video_job(item,lcd,EditSequence(),closing,
                lambda **fields: statuses.append(fields),90,on_ready=lambda revision,*_:checkpoints.append(revision))
        media.upload_file.assert_called_once()
        media.play_video.assert_called_once()
        lcd.draw.assert_not_called()
        self.assertEqual(lcd.update_video_overlay.call_count,3)
        cleared = lcd.update_video_overlay.call_args.args[0]
        self.assertIsNone(cleared.getbbox())
        self.assertEqual(checkpoints,[1,2,3])
        self.assertEqual(statuses[-1]["state"],"stopped")

    def test_failed_native_upload_never_plays_or_checkpoints(self):
        import device_jobs
        item = self.native_fixture()
        media, lcd, ready = Mock(), Mock(), Mock()
        media.get_file_size.return_value=0
        media.upload_file.side_effect=OSError("USB timeout")
        with patch.object(device_jobs,"DeviceMedia",return_value=media):
            with self.assertRaises(OSError):
                device_jobs.run_device_video_job(item,lcd,threading.Event(),threading.Event(),Mock(),90,on_ready=ready)
        ready.assert_not_called()
        media.play_video.assert_not_called()
        lcd.initialize_video_overlay.assert_not_called()

    def test_native_background_change_rebuilds_encoded_cache(self):
        import device_jobs
        item = self.native_fixture()
        item["settings"]["rotation"]=22.5
        stopped = threading.Event()
        stopped.set()
        def encode(source,dest,settings,**kwargs):
            Path(dest).write_bytes(b"new-background")
            return {"fps":2,"frames":2,"duration":1}
        with patch.object(device_jobs,"render_device_video",side_effect=encode) as render, \
             patch.object(device_jobs,"DeviceMedia",side_effect=AssertionError("Stopped before USB")):
            lcd=Mock()
            device_jobs.run_device_video_job(item,lcd,stopped,threading.Event(),Mock(),90)
        render.assert_called_once()
        lcd.connect.assert_not_called()

    def test_native_mount_change_invalidates_background_cache(self):
        import device_jobs
        item=self.native_fixture()
        signature=item["info"]["device_video_signature"]
        signatures=set()
        def encode(source,dest,settings,**kwargs):
            Path(dest).write_bytes(b"corrected-compensation")
            return {"fps":2,"frames":2,"duration":1}
        for mount in (0,90,180,270):
            with self.subTest(mount=mount):
                signatures.add(device_video_signature(item["source"],item["settings"],mount))
                item["info"]["device_video_signature"]=signature
                stopped=threading.Event()
                stopped.set()
                with patch.object(device_jobs,"render_device_video",side_effect=encode) as render:
                    lcd=Mock()
                    device_jobs.run_device_video_job(item,lcd,stopped,threading.Event(),Mock(),mount)
                self.assertEqual(render.call_count,0 if mount==90 else 1)
                lcd.connect.assert_not_called()
        self.assertEqual(len(signatures),4)

    def test_legacy_compensated_video_cache_is_regenerated(self):
        import device_jobs
        item=self.native_fixture()
        stopped=threading.Event()
        stopped.set()
        def encode(source,dest,settings,**kwargs):
            Path(dest).write_bytes(b"corrected-compensation")
            return {"fps":2,"frames":2,"duration":1}
        for version in (1,2):
            legacy=hashlib.sha256(item["source"].read_bytes())
            old={"version":version,"settings":defaults(item["settings"])}
            old.update({"mount":90} if version==1 else {"orientation":"material-only"})
            legacy.update(json.dumps(old,sort_keys=True).encode())
            item["info"]["device_video_signature"]=legacy.hexdigest()
            item["info"]["device_upload_hash"]="old-upload"
            with patch.object(device_jobs,"render_device_video",side_effect=encode) as render:
                lcd=Mock()
                device_jobs.run_device_video_job(item,lcd,stopped,threading.Event(),Mock(),90)
            render.assert_called_once()
            lcd.connect.assert_not_called()
        restored=workspace.load_draft(item["id"])
        self.assertEqual(restored["device_video"].read_bytes(),b"corrected-compensation")
        self.assertEqual(restored["info"]["device_video_signature"],
                         device_video_signature(item["source"],item["settings"],90))
        self.assertNotIn("device_upload_hash",restored["info"])

    def test_overlay_period_includes_processing_time_without_extra_one_second_wait(self):
        import device_jobs
        item=self.native_fixture()
        item["layers"]=[{"type":"sensor","metric":"clock"}]
        item["info"].update(device_upload_hash=hashlib.sha256(item["device_video"].read_bytes()).hexdigest(),
                             device_path="/mnt/SDCARD/video/known.mp4")
        for usb_seconds in (.4,1.2):
            with self.subTest(usb_seconds=usb_seconds):
                clock=[0.0]
                waits,completions,status=[],[],{}
                class StopAfterThreeWaits:
                    stopped=False
                    def is_set(self):
                        return self.stopped
                    def wait(self,timeout):
                        self_timeout=timeout
                        waits.append(self_timeout)
                        clock[0]+=self_timeout
                        if len(waits)>=3:
                            self.stopped=True
                        return self.stopped
                def sample(**kwargs):
                    clock[0]+=.1
                    return {}
                def compose(*args):
                    clock[0]+=.1
                    return Image.new("RGBA",(480,480))
                def send(*args):
                    clock[0]+=usb_seconds
                    completions.append(clock[0])
                    return {"method":"video-overlay","bytes":100,"regions":1,"changed_pixels":1}
                media,lcd=Mock(),Mock()
                media.get_file_size.return_value=item["device_video"].stat().st_size
                lcd.update_video_overlay.side_effect=send
                with patch.object(device_jobs,"DeviceMedia",return_value=media), \
                     patch.object(device_jobs.time,"monotonic",side_effect=lambda:clock[0]), \
                     patch.object(device_jobs,"sensor_snapshot",side_effect=sample), \
                     patch.object(device_jobs,"compose_overlay",side_effect=compose):
                    device_jobs.run_device_video_job(item,lcd,StopAfterThreeWaits(),threading.Event(),
                        lambda **fields:status.update(fields),90)
                target=max(1.0,usb_seconds+.2)
                self.assertEqual(len(completions),3)
                for before,after in zip(completions,completions[1:]):
                    self.assertAlmostEqual(after-before,target)
                self.assertAlmostEqual(waits[0],max(0,1-usb_seconds-.2))
                self.assertEqual(status["overlay_interval_ms"],round(1000*target))
                self.assertEqual(status["overlay_send_ms"],round(1000*usb_seconds))
                media.upload_file.assert_not_called()

    def test_stop_during_native_upload_finishes_file_without_starting_player(self):
        import device_jobs
        item = self.native_fixture()
        stopped = threading.Event()
        media = Mock()
        media.get_file_size.return_value=0
        def upload(*args,**kwargs):
            stopped.set()
            return {"verified":True}
        media.upload_file.side_effect=upload
        with patch.object(device_jobs,"DeviceMedia",return_value=media):
            device_jobs.run_device_video_job(item,Mock(),stopped,threading.Event(),Mock(),90)
        media.play_video.assert_not_called()
        self.assertIn("device_upload_hash",workspace.load_draft(item["id"])["info"])

    def test_exit_leaves_native_background_playing_but_user_stop_stops_player(self):
        import device_jobs
        item = self.native_fixture()
        for quitting in (True,False):
            with self.subTest(quitting=quitting):
                stopped,closing=threading.Event(),threading.Event()
                if quitting:
                    closing.set()
                media=Mock()
                media.get_file_size.return_value=0
                media.upload_file.return_value={"verified":True}
                media.play_video.side_effect=lambda *_:stopped.set()
                with patch.object(device_jobs,"DeviceMedia",return_value=media):
                    device_jobs.run_device_video_job(item,Mock(),stopped,closing,Mock(),90)
                # One STOP before changing media; only user-stop sends another
                # after playback starts. Closing the process leaves it alone.
                self.assertEqual(media.stop_video.call_count,1 if quitting else 2)


if __name__ == "__main__":
    unittest.main()
