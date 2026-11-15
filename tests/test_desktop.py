"""Desktop lifecycle tests with isolated user directories and simulated USB."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from PIL import Image

import desktop_settings
import runtime_paths
import workspace


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.environment = patch.dict(os.environ, {
            "TURING_DATA_DIR": str(self.data), "TURING_LOG_DIR": str(self.root / "logs"),
            "XDG_CONFIG_HOME": str(self.root / "settings"),
            "XDG_DATA_HOME": str(self.root / "share"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        paths = {"ROOT": self.data, "CONFIG": self.data / "config",
                 "PROJECTS": self.data / "config/projects", "DRAFTS": self.data / "config/drafts",
                 "SOURCE": self.data / "source", "DRAFT_SOURCE": self.data / "source/drafts",
                 "FONTS": self.data / "source/fonts", "STATE": self.data / "config/state.json"}
        for key, path in paths.items():
            if key != "STATE":
                path.mkdir(parents=True, exist_ok=True)
        changed = patch.multiple(workspace, **paths)
        changed.start()
        self.addCleanup(changed.stop)

    def fixture(self):
        key = uuid.uuid4().hex
        folder = workspace.draft_assets(key)
        folder.mkdir()
        for name in ("original.png", "base.png", "preview.png"):
            Image.new("RGB", (480, 480), "#123456").save(folder / name)
        item = {"id": key, "name": "Sent scene", "kind": "image", "source": folder / "original.png",
                "base": folder / "base.png", "preview": folder / "preview.png", "draft": True,
                "settings": {"fit": "cover"}, "layers": [], "device": workspace.DEFAULT_PREFERENCES.copy(),
                "info": {"fps": 0, "frames": 1, "duration": 0}, "revision": 1, "base_project": None}
        workspace.write_draft(item)
        return item

    def test_resume_preserves_sent_media_and_controls_after_unsent_edits(self):
        item = self.fixture()
        workspace.remember_stream(item, 180, "partial", "strict")
        item["settings"]["fit"] = "contain"
        Image.new("RGB", (480, 480), "red").save(item["base"])
        workspace.write_draft(item)
        restored = workspace.restore_stream()
        self.assertEqual(restored["settings"]["fit"], "cover")
        self.assertEqual(restored["device"]["transport"], "partial")
        self.assertEqual(restored["device"]["mount_ccw"], 180)
        self.assertEqual(Image.open(restored["base"]).getpixel((0, 0)), (18, 52, 86))
        self.assertNotEqual(restored["id"], item["id"])
        self.assertEqual(workspace.list_projects(), [])

    def test_failed_checkpoint_keeps_previous_successful_snapshot(self):
        item = self.fixture()
        workspace.remember_stream(item, 90, "full", "auto")
        previous = (workspace.CONFIG / "last-stream.json").read_bytes()
        with patch.object(workspace, "_copy_assets", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                workspace.remember_stream(item, 0, "partial", "auto")
        self.assertEqual((workspace.CONFIG / "last-stream.json").read_bytes(), previous)
        self.assertEqual(workspace.restore_stream()["device"]["transport"], "full")

    def test_login_and_silent_start_are_independent_and_persistent(self):
        command = ["/tmp/My Studio/odd $name`100%\\studio", "--autostart"]
        with patch.object(desktop_settings, "launch_command", return_value=command[:1]):
            self.assertEqual(desktop_settings.load_settings(), {"start_at_login": False, "silent_start": False})
            desktop_settings.save_settings({"silent_start": True})
            self.assertFalse(desktop_settings.autostart_path().exists())
            desktop_settings.save_settings({"start_at_login": True})
            path = desktop_settings.autostart_path()
            text = path.read_text()
            self.assertIn("--autostart", text)
            self.assertIn("100%%", text)
            if shutil.which("desktop-file-validate"):
                subprocess.run(["desktop-file-validate", str(path)], check=True, capture_output=True)
            desktop_settings.save_settings({"start_at_login": False})
            self.assertFalse(path.exists())
            self.assertTrue(desktop_settings.load_settings()["silent_start"])

    def test_autostart_write_failure_does_not_claim_setting_was_saved(self):
        with patch.object(desktop_settings, "sync_autostart", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                desktop_settings.save_settings({"start_at_login": True})
        self.assertFalse(desktop_settings.load_settings()["start_at_login"])

    def test_bundled_libusb_is_preferred_with_system_fallback(self):
        library = self.root / "libusb-1.0.so.0"
        library.write_bytes(b"bundled")
        with patch.object(runtime_paths.sys, "frozen", True, create=True), \
             patch.object(runtime_paths, "RESOURCE_ROOT", self.root), \
             patch.object(runtime_paths.ctypes.util, "find_library", return_value="system-usb") as find:
            self.assertEqual(runtime_paths.usb_library(), str(library))
            find.assert_not_called()
            library.unlink()
            self.assertEqual(runtime_paths.usb_library(), "system-usb")

    def test_libusb_load_failure_retries_with_system_library(self):
        import device
        fallback = object()
        with patch.object(device.usb.backend.libusb1, "get_backend", side_effect=[None, fallback]) as load:
            self.assertIs(device.libusb_backend(), fallback)
        self.assertIs(load.call_args_list[0].kwargs["find_library"], runtime_paths.usb_library)
        self.assertIs(load.call_args_list[1].kwargs["find_library"], runtime_paths.ctypes.util.find_library)

    def test_migration_copies_assets_fonts_and_keeps_both_workspaces(self):
        old = self.root / "old"
        (old / "config").mkdir(parents=True)
        (old / "source/fonts").mkdir(parents=True)
        (old / "config/state.json").write_text('{"active_draft": null}')
        (old / "source/fonts/test.ttf").write_bytes(b"font")
        target = self.root / "migrated"
        self.assertTrue(runtime_paths.migrate_data(old, target))
        self.assertEqual((target / "source/fonts/test.ttf").read_bytes(), b"font")
        self.assertTrue((old / "config/state.json").is_file())
        (old / "source/fonts/test.ttf").write_bytes(b"changed")
        self.assertFalse(runtime_paths.migrate_data(old, target))
        self.assertEqual((target / "source/fonts/test.ttf").read_bytes(), b"font")

    def test_migration_copy_failure_does_not_create_partial_workspace(self):
        old = self.root / "old"
        (old / "config").mkdir(parents=True)
        (old / "config/state.json").write_text("{}")
        target = self.root / "migrated"
        with patch.object(runtime_paths.shutil, "copytree", side_effect=OSError("out of space")):
            with self.assertRaises(OSError):
                runtime_paths.migrate_data(old, target)
        self.assertFalse(target.exists())

    def backend(self):
        # Import after the test's data paths have been isolated. No device
        # connection happens on import, and all worker tests use a fake LCD.
        import app
        old_job = copy.deepcopy(app.job)
        old_stop = app.stop_event
        app.stop_event = threading.Event()
        app.reset_requested.clear()
        app.closing_event.clear()
        self.addCleanup(lambda: app.job.update(old_job))
        self.addCleanup(setattr, app, "stop_event", old_stop)
        return app

    def test_send_error_reconnects_and_checkpoints_only_successful_frame(self):
        backend = self.backend()
        item = self.fixture()
        result = {"bytes": 100, "method": "full", "changed_pixels": None,
                  "frame_hash": "simulated", "regions": 1, "seconds": .01}
        lcd = Mock(connected=True)
        lcd.draw.side_effect = [OSError("USB timeout"), result]

        class ImmediateRetry(threading.Event):
            def wait(self, timeout=None):
                self.waited = getattr(self,"waited",0) + timeout
                return False

        event = ImmediateRetry()
        with patch.object(backend, "lcd", lcd), patch.object(backend, "stop_event", event), \
             patch.object(backend, "sensor_snapshot", return_value={}), \
             self.assertLogs("turing", level="ERROR"):
            backend.worker("send", item, 90, "full", "auto")
        self.assertEqual(lcd.connect.call_count, 2)
        self.assertEqual(lcd.close.call_count, 1)
        self.assertEqual(event.waited, 2)
        self.assertEqual(backend.job["state"], "done")
        self.assertIn("USB timeout", backend.job["last_error"])
        self.assertTrue((workspace.CONFIG / "last-stream.json").is_file())
        for call in lcd.draw.call_args_list:
            self.assertEqual(call.kwargs["mode"], "full")
            self.assertFalse(call.kwargs["skip_unchanged"])

    def test_stop_during_retry_cancels_further_connections(self):
        backend = self.backend()
        item = self.fixture()
        lcd = Mock()
        lcd.connect.side_effect = OSError("disconnected")

        class CancelRetry(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True

        with patch.object(backend, "lcd", lcd), patch.object(backend, "stop_event", CancelRetry()), \
             self.assertLogs("turing", level="ERROR"):
            backend.worker("send", item, 90, "partial", "auto")
        self.assertEqual(lcd.connect.call_count, 1)
        self.assertEqual(backend.job["state"], "stopped")
        self.assertFalse((workspace.CONFIG / "last-stream.json").exists())

    def test_failed_reconnect_escalates_to_usb_reset_before_resuming(self):
        backend = self.backend()
        item = self.fixture()
        lcd = Mock(connected=False,needs_reset=True)
        lcd.connect.side_effect = [OSError("HELLO timeout"),OSError("HELLO timeout"),None]
        lcd.draw.return_value = {"bytes":100,"method":"full","changed_pixels":None,
                                 "frame_hash":"recovered","regions":1,"seconds":.01}
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = False
        with patch.object(backend,"lcd",lcd),patch.object(backend,"stop_event",event), \
             patch.object(backend,"sensor_snapshot",return_value={}),self.assertLogs("turing",level="ERROR"):
            backend.worker("send",item,90,"full","auto")
        self.assertEqual(lcd.connect.call_count,3)
        lcd.reset_usb.assert_called_once_with(event)
        self.assertEqual(backend.job["state"],"done")
        self.assertEqual(lcd.draw.call_args.kwargs["mode"],"full")

    def test_manual_reconnect_really_resets_and_handshakes(self):
        backend = self.backend()
        lcd = Mock()
        lcd.connection_details.return_value = {"display_id":"verified","bus":1,"address":10,"handshake":True}
        with patch.object(backend,"lcd",lcd):
            backend.worker("reconnect",None,90,"full","auto")
        lcd.reset_usb.assert_called_once_with(backend.stop_event)
        lcd.connect.assert_called_once_with()
        lcd.draw.assert_not_called()
        self.assertTrue(backend.job["device_check"]["handshake"])

    def test_manual_reconnect_interrupts_retry_wait(self):
        backend = self.backend()
        backend.job["state"]="retrying"
        with patch.object(backend,"lcd") as lcd,patch.object(backend,"worker_thread",Mock()):
            result=backend.app.test_client().post("/api/reconnect",json={},
                headers={"X-Turing-Token":backend.TOKEN})
        self.assertEqual(result.status_code,200)
        self.assertTrue(backend.reset_requested.is_set())
        lcd.close.assert_not_called()
        backend.reset_requested.clear()

    def test_notification_failure_does_not_kill_usb_recovery(self):
        backend=self.backend()
        lcd=Mock(connected=True)
        lcd.draw.side_effect=[OSError("USB timeout"),{"bytes":100,"method":"full",
            "changed_pixels":None,"frame_hash":"recovered","regions":1,"seconds":.01}]
        event=Mock()
        event.is_set.return_value=False
        event.wait.return_value=False
        tray=Mock()
        tray.notify_error.side_effect=RuntimeError("notification service unavailable")
        with patch.object(backend,"lcd",lcd),patch.object(backend,"stop_event",event), \
             patch.object(backend,"desktop_controller",tray),patch.object(backend,"sensor_snapshot",return_value={}), \
             self.assertLogs("turing",level="ERROR"):
            backend.worker("send",self.fixture(),90,"full","auto")
        self.assertEqual(backend.job["state"],"done")
        self.assertEqual(lcd.connect.call_count,2)

    def test_detection_queues_a_handshake_when_idle_but_stays_read_only_while_sending(self):
        backend = self.backend()
        client=backend.app.test_client()
        headers={"X-Turing-Token":backend.TOKEN}
        identity={"present":True,"matched":True,"bus":1,"address":9}
        with patch.object(backend,"probe_device",return_value=identity), \
             patch.object(backend,"start_job") as start:
            backend.job["state"]="idle"
            result=client.post("/api/device/check",json={},headers=headers)
            self.assertTrue(result.json["checking"])
            self.assertEqual(start.call_args.args[0],"check")
            start.reset_mock()
            backend.job["state"]="live"
            result=client.post("/api/device/check",json={},headers=headers)
            self.assertFalse(result.json["checking"])
            start.assert_not_called()

    def test_resume_starts_saved_job_without_any_page_request(self):
        backend = self.backend()
        item = self.fixture()
        workspace.remember_stream(item, 270, "partial", "auto")
        with patch.object(backend, "start_job") as start:
            self.assertTrue(backend.resume_last_stream())
        self.assertEqual(start.call_args.args[0], "send")
        self.assertEqual(start.call_args.args[2:], (270, "partial", "auto"))

    def test_desktop_settings_endpoint_requires_write_token(self):
        backend = self.backend()
        controller = Mock()
        controller.status.return_value = {"settings": desktop_settings.load_settings(), "tray_available": True}
        with patch.object(backend, "desktop_controller", controller):
            client = backend.app.test_client()
            self.assertTrue(client.get("/api/desktop").json["available"])
            self.assertEqual(client.post("/api/desktop", json={"silent_start": True}).status_code, 403)
            response = client.post("/api/desktop", json={"silent_start": True},
                                   headers={"X-Turing-Token": backend.TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["settings"]["silent_start"])

    def test_single_instance_reuses_backend_and_releases_after_exit(self):
        from desktop import SingleInstance
        folder = self.root / "runtime"
        folder.mkdir()
        first, second = SingleInstance(folder), SingleInstance(folder)
        controller = Mock(url="http://127.0.0.1:1234")
        controller.status.return_value = {"tray_available": True}
        self.assertTrue(first.acquire())
        self.addCleanup(first.close)
        first.serve(controller)
        self.assertFalse(second.acquire())
        self.assertTrue(second.request("status")["tray_available"])
        second.request("show")
        controller.dispatch.assert_called_once_with(controller.show_window)
        first.close()
        self.assertTrue(second.acquire())
        second.close()

    def test_ffmpeg_loop_decodes_more_frames_than_the_original_clip(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        from media import iter_video_frames
        clip = self.root / "two-frames.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=480x480:r=2", "-frames:v", "2", "-pix_fmt", "yuv420p",
                        str(clip)], check=True)
        event = threading.Event()
        stream = iter_video_frames(clip, event, loop=True)
        try:
            for index in range(7):
                self.assertEqual(next(stream).size, (480, 480))
            event.set()
            with self.assertRaises(StopIteration):
                next(stream)
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
