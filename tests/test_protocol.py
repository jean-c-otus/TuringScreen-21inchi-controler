import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock
import threading

from device_protocol import frame_stream, path_command, upload_command, pad_command
from device_media import DeviceMedia


class ProtocolTests(unittest.TestCase):
    def test_command_padding(self):
        self.assertEqual(len(pad_command(b"x")), 250)
        self.assertEqual(len(pad_command(b"x" * 250)), 250)

    def test_payload_framing(self):
        self.assertEqual(len(frame_stream(b"x" * 249)), 250)
        self.assertEqual(len(frame_stream(b"x" * 250)), 500)
        self.assertEqual(frame_stream(b"x" * 249)[-1], 0)

    def test_path_command(self):
        command = path_command(0x78, "/root/video/demo.mp4")
        self.assertEqual(command[0:3], bytes.fromhex("78 EF 69"))
        self.assertEqual(command[6], len("/root/video/demo.mp4"))

    def test_upload_size_is_little_endian(self):
        command = upload_command("/root/video/demo.mp4", 0x12345678)
        self.assertTrue(command.endswith(bytes.fromhex("78 56 34 12")))

    def media(self):
        lcd = Mock(io_lock=threading.RLock())
        media = DeviceMedia(lcd)
        media._read_reply = Mock()
        return media, lcd

    def test_device_play_sets_loop_flag_and_requires_ack(self):
        media, lcd = self.media()
        media.get_file_size = Mock(return_value=123)
        media._read_reply.return_value = b"play_video_success"
        media.play_video("/mnt/SDCARD/video/example.mp4")
        command = lcd._command.call_args.args[0]
        self.assertEqual(command[:3], bytes.fromhex("78 EF 69"))
        self.assertEqual(command[7], 1)
        media._read_reply.return_value = b"error"
        with self.assertRaises(RuntimeError):
            media.play_video("/mnt/SDCARD/video/example.mp4")

    def test_empty_size_reply_is_error_instead_of_reuploading_blindly(self):
        media, lcd = self.media()
        media._read_reply.return_value = b""
        with self.assertRaises(RuntimeError):
            media.get_file_size("/mnt/SDCARD/video/example.mp4")

    def test_upload_frames_and_size_verification(self):
        media, lcd = self.media()
        raw = bytes(range(256)) * 65
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "device.mp4"
            clip.write_bytes(raw)
            media._read_reply.side_effect = [b"create_success", b"file_rev_done"]
            media.get_file_size = Mock(return_value=len(raw))
            result = media.upload_file(clip, "/mnt/SDCARD/video/example.mp4")
        framed = b"".join(c.args[0] for c in lcd._write.call_args_list)
        self.assertEqual(framed, frame_stream(raw))
        self.assertTrue(result["verified"])
        self.assertFalse(lcd.needs_reset)

    def test_failed_upload_requires_reset_and_cannot_start_playback(self):
        media, lcd = self.media()
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "device.mp4"
            clip.write_bytes(b"clip")
            media._read_reply.return_value = b"create_success"
            lcd._write.side_effect = OSError("short USB write")
            with self.assertRaises(OSError):
                media.upload_file(clip, "/mnt/SDCARD/video/example.mp4")
        self.assertTrue(lcd.needs_reset)
        self.assertFalse(any(c.args[0][0] == 0x78 for c in lcd._command.call_args_list))


if __name__ == "__main__":
    unittest.main()
