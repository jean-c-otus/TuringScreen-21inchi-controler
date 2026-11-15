"""Regression coverage for animated uploads, compositing and arbitrary rotation.

Uses a temporary workspace and real FFmpeg codecs; never opens a USB device.
"""
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageChops, ImageStat

from media import (defaults, image_frame, iter_video_frames, render, render_device_video,
                   run_ffmpeg, device_video_compensation)


class RotationTests(unittest.TestCase):
    def test_device_compensation_subtracts_player_rotation(self):
        for mount, expected in ((0,270),(90,0),(180,90),(270,180)):
            self.assertEqual(device_video_compensation(mount),expected)
        with self.assertRaises(ValueError):
            device_video_compensation(45)

    def test_decimal_negative_and_multiple_turn_angles(self):
        for value, expected in (("31.5", 31.5), ("-17.5", 342.5), ("742.25", 22.25)):
            with self.subTest(value=value):
                self.assertEqual(defaults({"rotation": value})["rotation"], expected)

    def test_invalid_angles_are_rejected(self):
        for value in ("invalid", "nan", "inf", -math.inf, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                defaults({"rotation": value})

    def test_fractional_rotation_keeps_transparent_corners_over_background(self):
        source = Image.new("RGB", (80, 60), "red")
        options = {"rotation": 31.5, "fit": "contain", "background": "#0000ff"}
        result = image_frame(source, options)
        self.assertEqual(result.size, (480, 480))
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(result.getpixel((240, 240)), (255, 0, 0))
        self.assertNotEqual(result.tobytes(), image_frame(source, {**options, "rotation": 0}).tobytes())


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class AnimatedUploadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="turing-media-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TURING_DATA_DIR": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        import workspace
        import app
        import scene
        self.workspace = workspace
        self.backend = app
        paths = {"ROOT": self.root / "data", "CONFIG": self.root / "data/config",
                 "PROJECTS": self.root / "data/config/projects", "DRAFTS": self.root / "data/config/drafts",
                 "SOURCE": self.root / "data/source", "DRAFT_SOURCE": self.root / "data/source/drafts",
                 "FONTS": self.root / "data/source/fonts", "STATE": self.root / "data/config/state.json"}
        for key, path in paths.items():
            if key != "STATE":
                path.mkdir(parents=True, exist_ok=True)
        for replacement in (patch.multiple(workspace, **paths), patch.object(app, "items", {}),
                            patch.object(app, "job", {**app.job, "state": "idle"}),
                            patch.object(scene, "sensor_snapshot", return_value={})):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = app.app.test_client()
        self.headers = {"X-Turing-Token": app.TOKEN}
        self.gif = self.root / "animated.gif"
        frames = [Image.new("RGB", (80, 60), color) for color in ("red", "blue")]
        frames[0].save(self.gif, save_all=True, append_images=frames[1:], duration=500, loop=0)

    def upload(self, path, **settings):
        return self.client.post("/api/upload", headers=self.headers, data={
            "file": (io.BytesIO(path.read_bytes()), path.name), **settings,
        })

    def test_device_clip_uses_selected_fps_without_overlay(self):
        destination = self.root / "device.mp4"
        options = {"fps": 24, "rotation": 17.5, "fit": "contain"}
        info = render_device_video(self.gif, destination, options, mount_ccw=90)
        self.assertEqual(info["fps"], 24)
        self.assertEqual(info["frames"], 24)
        decoded = list(iter_video_frames(destination, threading.Event()))
        self.assertEqual(len(decoded), 24)
        channels = decoded[0].convert("RGB").tobytes()
        # No green text/overlay can become baked into the device background.
        self.assertFalse(any(g > r+60 and g > b+60 for r,g,b in zip(
            channels[::3], channels[1::3], channels[2::3])))

    def test_gif_and_mp4_orientation_for_each_mount_and_material_rotation(self):
        # Asymmetric coloured regions expose clockwise/counterclockwise,
        # double-rotation and mirror errors that solid-frame tests cannot.
        pattern = Image.new("RGB", (960,960), "#202020")
        draw = ImageDraw.Draw(pattern)
        for box,colour in (((40,40,380,310),"red"),((560,90,910,400),"lime"),
                           ((80,570,420,910),"blue"),((590,590,820,790),"yellow")):
            draw.rectangle(box,fill=colour)
        png,gif,mp4=(self.root/name for name in ("orientation.png","orientation.gif","orientation.mp4"))
        pattern.save(png)
        pattern.save(gif,save_all=True,append_images=[pattern.transpose(Image.Transpose.FLIP_LEFT_RIGHT)],
                     duration=500,loop=0)
        run_ffmpeg(["-loop","1","-i",str(png),"-t","1","-r","2",
                    "-c:v","libx264","-crf","0","-pix_fmt","yuv444p",str(mp4)])
        for source in (gif,mp4):
            for angle in (0,90,30,-17.5):
                options={"rotation":angle,"fit":"contain","fps":2}
                expected_preview=image_frame(pattern,options)
                for mount in (0,90,180,270):
                    with self.subTest(source=source.suffix,angle=angle,mount=mount):
                        destination=self.root/"oriented-device.mp4"
                        render_device_video(source,destination,options,mount_ccw=mount)
                        actual=list(iter_video_frames(destination,threading.Event()))[0]
                        # Undo the requested remaining compensation; the MCU
                        # itself provides the other 90 degrees at playback.
                        compensation={0:270,90:0,180:90,270:180}[mount]
                        restored=actual.rotate(compensation)
                        error=ImageStat.Stat(ImageChops.difference(restored,expected_preview)).mean
                        self.assertLess(max(error),4.0,(source.suffix,angle,mount,error))

    def test_gif_upload_keeps_animation_and_layers_above_each_frame(self):
        layers = [{"type": "text", "text": "TOP", "x": 20, "y": 20,
                   "size": 60, "color": "#00ff00"}]
        response = self.upload(self.gif, rotation="22.5", layers=json.dumps(layers))
        self.assertEqual(response.status_code, 200, response.json)
        project = response.json["project"]
        self.assertEqual(project["kind"], "video")
        self.assertEqual(project["info"]["frames"], 2)
        draft = self.workspace.load_draft(project["id"])
        self.assertEqual(draft["settings"]["rotation"], 22.5)
        decoded = list(iter_video_frames(draft["preview"], threading.Event()))
        self.assertEqual(len(decoded), 2)
        for frame in decoded:
            pixels = frame.crop((0, 0, 180, 110)).convert("RGB").tobytes()
            channels = zip(pixels[0::3], pixels[1::3], pixels[2::3])
            self.assertGreater(sum(g > r + 60 and g > b + 60 for r, g, b in channels), 100)
        red, _, blue = decoded[0].getpixel((240, 240))
        self.assertGreater(red, 200)
        self.assertLess(blue, 20)
        red, _, blue = decoded[1].getpixel((240, 240))
        self.assertGreater(blue, 200)
        self.assertLess(red, 20)
        source = self.client.get(project["source"])
        self.assertEqual(source.mimetype, "image/gif")
        source.close()
        preview = self.client.get(project["preview"], headers={"Range": "bytes=0-63"})
        self.assertEqual(preview.status_code, 206)
        self.assertEqual(preview.mimetype, "video/mp4")
        preview.close()
        with patch.object(self.backend, "start_job") as start:
            sent = self.client.post("/api/send", json={"id": project["id"]}, headers=self.headers)
        self.assertEqual(sent.status_code, 200, sent.json)
        self.assertEqual(start.call_args.args[0], "send")

    def test_mp4_upload_and_rerender_support_decimal_angles(self):
        mp4 = self.root / "animated.mp4"
        render(self.gif, "video", mp4, {})
        response = self.upload(mp4, rotation="-17.5")
        self.assertEqual(response.status_code, 200, response.json)
        project = response.json["project"]
        self.assertEqual(project["settings"]["rotation"], 342.5)
        response = self.client.post("/api/render", headers=self.headers, json={
            "id": project["id"], "settings": {"rotation": 47.25}, "sequence": 1,
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["project"]["settings"]["rotation"], 47.25)
        self.assertEqual(response.json["project"]["info"]["frames"], 2)

    def test_invalid_upload_logs_reason_and_preserves_active_draft(self):
        good = self.upload(self.gif)
        self.assertEqual(good.status_code, 200, good.json)
        current = self.workspace.get_state()["active_draft"]
        draft_files = sorted(self.workspace.DRAFTS.iterdir())
        self.gif.write_bytes(b"invalid animated input")
        with self.assertLogs("turing", level="ERROR") as logged:
            bad = self.upload(self.gif)
        self.assertEqual(bad.status_code, 400)
        self.assertIn("FFmpeg", bad.json["error"])
        self.assertIn("Draft creation failed", "\n".join(logged.output))
        self.assertEqual(self.workspace.get_state()["active_draft"], current)
        self.assertEqual(sorted(self.workspace.DRAFTS.iterdir()), draft_files)

    def test_gif_frame_rate_setting_is_saved_and_used_after_rerender(self):
        response=self.upload(self.gif,fps="5")
        self.assertEqual(response.status_code,200,response.json)
        project=response.json["project"]
        self.assertEqual(project["info"]["fps"],5)
        rendered=self.client.post("/api/render",headers=self.headers,json={
            "id":project["id"],"settings":{"fps":3},"sequence":1})
        self.assertEqual(rendered.status_code,200,rendered.json)
        self.assertEqual(rendered.json["project"]["info"]["fps"],3)
        self.assertEqual(self.workspace.load_draft(project["id"])["settings"]["fps"],3)
        with patch.object(self.backend,"start_job") as start:
            self.client.post("/api/send",headers=self.headers,json={"id":project["id"]})
        self.assertEqual(start.call_args.args[1]["info"]["fps"],3)


if __name__ == "__main__":
    unittest.main()
