# Build on Arch x86_64; include controlled libusb, reuse system Chrome/FFmpeg.
from pathlib import Path
import os

root = Path(SPECPATH).parent
libusb = Path("/usr/lib/libusb-1.0.so.0")
if not libusb.is_file():
    raise RuntimeError("Install libusb before building")
os.environ["PYINSTALLER_USB_HOOK_SKIP_PYUSB_DISCOVERY"] = "1"
a = Analysis(
    [str(root / "desktop.py")], pathex=[str(root)],
    binaries=[(str(libusb), ".")], datas=[(str(root / "static"), "static")],
    hiddenimports=["usb.backend.libusb1", "dbus_next.aio"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="turing-studio",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True)
collect = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="turing-studio")
