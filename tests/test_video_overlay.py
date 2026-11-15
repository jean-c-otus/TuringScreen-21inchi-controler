import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from device import Display
from scene import compose, compose_overlay

from video_overlay import build_overlay_packet


class VideoOverlayTests(unittest.TestCase):
    def test_transparent_layer_composition_matches_host_composite(self):
        layers = [{"type": "text", "text": "TOP", "x": 30, "y": 30,
                   "color": "#00ff00", "opacity": 70}]
        overlay = compose_overlay(layers, {})
        self.assertEqual(overlay.getpixel((0, 0)), (0, 0, 0, 0))
        background = Image.new("RGBA", (480, 480), "blue")
        background.alpha_composite(overlay)
        self.assertEqual(background.convert("RGB").tobytes(),
                         compose(Image.new("RGB", (480, 480), "blue"), layers, {}).tobytes())

    def test_native_overlay_never_sends_background_and_skips_unchanged_pixels(self):
        lcd = Display()
        commands, payloads = [], []
        def write(data):
            lcd.bytes_sent += len(data)
            payloads.append(data)
        image = Image.new("RGBA", (480, 480), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((10, 20, 19, 29), fill="red")
        with patch.object(lcd, "_command", side_effect=commands.append), \
             patch.object(lcd, "_write", side_effect=write), \
             patch.object(lcd, "_read_matching_reply", return_value=b"needReSend:0|renderCnt:1"), \
             patch("device.time.sleep"), \
             patch.object(lcd, "draw", side_effect=AssertionError("No host background frames")):
            lcd.initialize_video_overlay(90)
            commands.clear()
            result = lcd.update_video_overlay(image, 90)
            self.assertEqual([c[0] for c in commands], [0xCF, 0xCA, 0xD0])
            self.assertEqual(int.from_bytes(commands[1][3:7], "big"), 480*480*4)
            self.assertEqual(result["visible_pixels"], 100)
            sent_count = len(payloads)
            self.assertEqual(lcd.update_video_overlay(image, 90)["method"], "skip")
            self.assertEqual(len(payloads), sent_count)
            result = lcd.update_video_overlay(Image.new("RGBA", (480,480)), 90)
            self.assertEqual(result["visible_pixels"], 0)
            self.assertEqual(commands[-1][0], 0xCC)
            self.assertEqual(int.from_bytes(commands[-1][14:18], "big"), 0)
            self.assertEqual(result["method"], "overlay-delta")
            self.assertGreater(result["changed_pixels"], 0)
            self.assertLess(result["bytes"], 480*480*4)

    def test_unchanged_overlay_is_skipped(self):
        blank = Image.new("RGBA", (480, 480), (0, 0, 0, 0))
        self.assertIsNone(build_overlay_packet(blank, blank, 90, 0))

    def test_changed_pixels_and_visibility_mask(self):
        blank = Image.new("RGBA", (480, 480), (0, 0, 0, 0))
        current = blank.copy()
        ImageDraw.Draw(current).text((20, 20), "CPU 45C", fill=(255, 255, 255, 255))
        packet = build_overlay_packet(current, blank, 90, 0)
        self.assertIsNotNone(packet)
        self.assertGreater(packet.changed_pixels, 0)
        self.assertGreater(packet.visible_pixels, 0)
        self.assertEqual(packet.header[:3], bytes.fromhex("CC EF 69"))

    def test_clearing_overlay_sends_empty_mask(self):
        blank = Image.new("RGBA", (480, 480), (0, 0, 0, 0))
        current = blank.copy()
        ImageDraw.Draw(current).text((20, 20), "CPU 45C", fill=(255, 255, 255, 255))
        packet = build_overlay_packet(blank, current, 90, 1)
        self.assertIsNotNone(packet)
        self.assertEqual(packet.visible_pixels, 0)
        self.assertEqual(packet.header[-4:], (0).to_bytes(4, "big"))


if __name__ == "__main__":
    unittest.main()
