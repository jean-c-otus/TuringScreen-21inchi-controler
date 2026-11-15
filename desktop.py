"""Turing Studio desktop launcher: system Chrome, native tray, one backend."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from desktop_settings import load_settings, sync_autostart
from runtime_paths import (data_root, log_root, migrate_data,
                           migrate_development_data, system_environment, usb_library)

log = logging.getLogger("turing.desktop")


def find_chrome():
    for name in filter(None, (os.environ.get("TURING_CHROME"), "google-chrome-stable",
                              "google-chrome", "chromium", "chromium-browser")):
        found = shutil.which(name)
        if found:
            return found
    return None


def runtime_root():
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/turing-{os.getuid()}"))
    root = Path(os.environ.get("TURING_RUNTIME_DIR", base / "turing-studio"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise RuntimeError("运行目录不属于当前用户")
    root.chmod(0o700)
    return root


class SingleInstance:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "control.sock"
        self.lock = None
        self.server = None

    def acquire(self):
        self.lock = (self.root / "instance.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            return False

    def request(self, action, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(str(self.path))
                    client.sendall((action + "\n").encode())
                    with client.makefile("rb") as reply:
                        return json.loads(reply.readline(65536))
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("后台实例未响应，请查看日志") from None
                time.sleep(.1)

    def serve(self, controller):
        self.path.unlink(missing_ok=True)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                action = self.rfile.readline(128).decode().strip()
                if action == "show":
                    controller.dispatch(controller.show_window)
                    value = {"ok": True}
                elif action == "status":
                    value = {"ok": True, "url": controller.url, **controller.status()}
                elif action == "shutdown":
                    controller.request_exit()
                    value = {"ok": True}
                else:
                    value = {"ok": False, "error": "Unknown command"}
                self.wfile.write(json.dumps(value).encode() + b"\n")

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.path), Handler)
        self.path.chmod(0o600)
        threading.Thread(target=self.server.serve_forever, name="turing-control", daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.lock:
            self.path.unlink(missing_ok=True)
            self.lock.close()
            self.lock = None


class DesktopController:
    def __init__(self, backend):
        self.backend = backend
        self.stopping = threading.Event()
        self.url = ""
        self.tray = None
        self.browser_lock = threading.Lock()
        self.browsers = []
        self.profile = data_root() / "browser"

    def dispatch(self, action):
        def run():
            try:
                action()
            except Exception:
                log.exception("Desktop action failed")
        threading.Thread(target=run, daemon=True).start()

    def status(self):
        return {"settings": load_settings(), "tray_available": bool(self.tray and self.tray.registered),
                "log_path": str(log_root() / "studio.log")}

    def stream_status(self):
        return self.backend.current_job()

    def browser_api(self, path):
        # Verify the browser UUID as well as the ephemeral port, so a stale
        # DevToolsActivePort file cannot control another local application.
        try:
            lines = (self.profile / "DevToolsActivePort").read_text().splitlines()
            port = int(lines[0])
            if not 1 <= port <= 65535 or len(lines) < 2:
                return None
            endpoint = f"http://127.0.0.1:{port}"
            with urlopen(endpoint + "/json/version", timeout=.5) as response:
                version = json.load(response)
            if not version.get("webSocketDebuggerUrl", "").endswith(lines[1]):
                return None
            with urlopen(endpoint + path, timeout=1) as response:
                result = response.read()
                return json.loads(result) if path == "/json/list" else result
        except (OSError, ValueError, IndexError):
            return None

    def show_window(self):
        with self.browser_lock:
            if self.stopping.is_set():
                return
            targets = self.browser_api("/json/list") or []
            for target in targets:
                if target.get("type") == "page" and target.get("url", "").rstrip("/") == self.url.rstrip("/"):
                    self.browser_api("/json/activate/" + target["id"])
                    return
            # An app browser can stay alive briefly after its last window
            # closes. Finish that owned process before opening a fresh app
            # window, rather than delegating to a browser with no windows.
            self._stop_browser_processes()
            chrome = find_chrome()
            if not chrome:
                raise RuntimeError("未找到 Chrome/Chromium，请安装浏览器后重新启动")
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            process = subprocess.Popen([
                chrome, "--app=" + self.url, "--user-data-dir=" + str(self.profile),
                "--class=TuringStudio", "--no-first-run", "--no-default-browser-check",
                "--disable-background-mode", "--disable-extensions", "--remote-debugging-port=0",
                "--password-store=basic",
                "--autoplay-policy=no-user-gesture-required",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=system_environment(), start_new_session=True)
            self.browsers = [proc for proc in self.browsers if proc.poll() is None]
            self.browsers.append(process)
            log.info("Opened Chrome application window")
            # Serialize rapid tray clicks through initial browser startup too.
            for _ in range(30):
                if self.stopping.is_set() or process.poll() is not None:
                    break
                if any(target.get("url", "").rstrip("/")==self.url.rstrip("/")
                       for target in self.browser_api("/json/list") or []):
                    break
                time.sleep(.1)

    def _close_pages(self):
        for target in self.browser_api("/json/list") or []:
            if target.get("type") == "page":
                self.browser_api("/json/close/" + target["id"])

    def hide_window(self):
        timer = threading.Timer(.2, self._hide_window)
        timer.daemon = True
        timer.start()

    def _hide_window(self):
        self.close_browser()

    def open_logs(self):
        subprocess.Popen(["xdg-open", str(log_root())], env=system_environment(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def notify_error(self, message):
        if self.tray:
            self.tray.notify(message)

    def request_exit(self):
        log.info("Exit requested")
        self.stopping.set()
        self.backend.closing_event.set()
        self.backend.stop_event.set()

    def close_browser(self):
        with self.browser_lock:
            self._close_pages()
            self._stop_browser_processes()

    def _stop_browser_processes(self):
        for process in self.browsers:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        self.browsers.clear()


def configure_logging():
    folder = log_root()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(folder / "studio.log", maxBytes=2 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # Routine 900 ms status polling must not crowd out USB errors in the log.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def diagnostics():
    chrome = find_chrome()
    ffmpeg = shutil.which("ffmpeg")
    libusb = usb_library()
    from device import libusb_backend
    backend_ready = libusb_backend() is not None
    result = {"chrome": chrome, "ffmpeg": ffmpeg, "libusb": libusb, "libusb_backend_ready": backend_ready,
              "data_dir": str(data_root()), "log_dir": str(log_root()),
              "session_bus": bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if chrome and ffmpeg and libusb and backend_ready else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Turing Studio desktop application")
    parser.add_argument("--autostart", action="store_true", help="Started by the desktop login session")
    parser.add_argument("--silent", action="store_true", help="Start in the system tray")
    parser.add_argument("--show", action="store_true", help="Open the editor even when silent start is enabled")
    parser.add_argument("--check", action="store_true", help="Check runtime dependencies without accessing USB")
    parser.add_argument("--status", action="store_true", help="Print the running desktop instance status")
    parser.add_argument("--import-data", metavar="DIRECTORY", help="Copy old workspace data on first launch")
    parser.add_argument("--shutdown", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--register-desktop", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.check:
        return diagnostics()
    if args.register_desktop:
        from desktop_settings import register_desktop_entry
        print(register_desktop_entry())
        return 0
    instance = SingleInstance(runtime_root())
    if not instance.acquire():
        if args.import_data:
            parser.error("导入旧数据前请先从托盘退出程序")
        action = ("shutdown" if args.shutdown else "status" if args.status else "show" if args.show
                  else "status" if (args.autostart or args.silent) else "show")
        result = instance.request(action)
        if args.status:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        if args.status or args.shutdown:
            print(json.dumps({"running": False}))
            return 0
        if args.import_data:
            migrated = migrate_data(args.import_data)
            print("已导入现有配置、素材和字体" if migrated else "保留现有数据；没有执行导入")
            return 0
        migrate_development_data()
        configure_logging()
        if not find_chrome():
            raise RuntimeError("未找到系统 Chrome/Chromium，请先安装浏览器")
        from werkzeug.serving import make_server
        import app as backend
        from desktop_tray import DesktopTray
        controller = DesktopController(backend)
        backend.desktop_controller = controller
        server = make_server("127.0.0.1", 0, backend.app, threaded=True)
        controller.url = f"http://127.0.0.1:{server.server_port}"
        instance.serve(controller)
        threading.Thread(target=server.serve_forever, name="turing-http", daemon=True).start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: controller.request_exit())
        controller.tray = DesktopTray(controller)
        def run_tray():
            asyncio.run(controller.tray.run())
        tray_thread = threading.Thread(target=run_tray, name="turing-tray", daemon=True)
        tray_thread.start()
        try:
            sync_autostart()
            # Start once from the backend lifecycle; page reloads cannot duplicate it.
            backend.resume_last_stream()
            settings = load_settings()
            if args.show or not (args.silent or settings["silent_start"]):
                controller.show_window()
            log.info("Desktop ready at %s; data=%s", controller.url, data_root())
            controller.stopping.wait()
        finally:
            controller.request_exit()
            backend.shutdown()
            server.shutdown()
            server.server_close()
            controller.close_browser()
            tray_thread.join(timeout=4)
        return 0
    finally:
        instance.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("Desktop startup failed")
        print("Turing Studio 启动失败，请查看日志：" + str(log_root() / "studio.log"), file=sys.stderr)
        raise SystemExit(1)
