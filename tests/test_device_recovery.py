"""USB recovery unit tests; all devices and transfers are simulated."""
import threading
import unittest
from unittest.mock import Mock, patch

import usb.core
import device


class RecoveryTests(unittest.TestCase):
    def test_partial_write_marks_session_for_reset_without_replaying_payload(self):
        display = device.Display()
        display.dev = Mock()
        display.dev.write.return_value = 2048
        with self.assertRaisesRegex(RuntimeError, "2048/16000"):
            display._write(b"x" * 16000)
        self.assertTrue(display.needs_reset)
        self.assertEqual(display.dev.write.call_count, 1)
        self.assertEqual(display.bytes_sent, 2048)
        display.dev = None

    def test_timeout_marks_session_for_reset(self):
        display = device.Display()
        display.dev = Mock()
        display.dev.write.side_effect = usb.core.USBTimeoutError("timeout")
        with self.assertRaises(usb.core.USBTimeoutError):
            display._write(b"command")
        self.assertTrue(display.needs_reset)
        display.dev = None

    def test_reset_verifies_serial_and_rediscovers_changed_usb_address(self):
        display = device.Display()
        old = Mock(serial_number=device.SERIAL, bus=1, address=9)
        new = Mock(serial_number=device.SERIAL, bus=1, address=10)
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = False
        display.update_count = 6
        with patch.object(device, "libusb_backend", return_value=object()), \
             patch.object(device.usb.core, "find", side_effect=[old, new]), \
             patch.object(device.usb.util, "dispose_resources") as dispose:
            display.reset_usb(event)
        old.reset.assert_called_once_with()
        new.reset.assert_not_called()
        self.assertEqual(dispose.call_count, 2)
        self.assertEqual(display.update_count, 0)
        self.assertFalse(display.needs_reset)

    def test_reset_never_touches_a_different_serial(self):
        display = device.Display()
        other = Mock(serial_number="different-device")
        with patch.object(device, "libusb_backend", return_value=object()), \
             patch.object(device.usb.core, "find", return_value=other), \
             patch.object(device.usb.util, "dispose_resources"):
            with self.assertRaisesRegex(RuntimeError, "序列号不符"):
                display.reset_usb(threading.Event())
        other.reset.assert_not_called()

    def test_stop_cancels_reset_before_device_lookup(self):
        event = threading.Event()
        event.set()
        with patch.object(device.usb.core, "find") as find:
            with self.assertRaises(InterruptedError):
                device.Display().reset_usb(event)
        find.assert_not_called()

    def test_read_only_detection_reports_identity_and_avoids_bulk_writes(self):
        target = Mock(serial_number=device.SERIAL, bus=1, address=9)
        with patch.object(device, "libusb_backend", return_value=object()), \
             patch.object(device.usb.core, "find", return_value=target), \
             patch.object(device.usb.util, "dispose_resources"):
            details = device.probe_device()
        self.assertTrue(details["matched"])
        self.assertEqual(details["address"], 9)
        target.write.assert_not_called()
        target.reset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
