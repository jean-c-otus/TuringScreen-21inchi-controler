"""Wayland/X11 tray using StatusNotifierItem + DBusMenu, without Qt or GTK."""
import asyncio
import logging
import os
import time

from dbus_next import Variant, Message, MessageType, PropertyAccess
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method, signal
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("turing.tray")
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"


def icon_pixmaps():
    result = []
    for size in (24, 48, 64):
        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=size // 4,
                               fill="#163b37", outline="#baf2d1", width=max(1, size // 24))
        draw.text((size / 2, size / 2), "T", font=ImageFont.load_default(size=int(size * .67)),
                  anchor="mm", fill="#baf2d1")
        red, green, blue, alpha = icon.split()
        result.append([size, size, Image.merge("RGBA", (alpha, red, green, blue)).tobytes()])
    return result


class TrayMenu(ServiceInterface):
    def __init__(self, controller):
        super().__init__("com.canonical.dbusmenu")
        self.controller = controller
        self.labels = {1: "打开 Turing Studio", 2: "查看日志", 3: "退出"}

    def properties(self, item_id):
        if item_id == 0:
            return {"children-display": Variant("s", "submenu")}
        return {"label": Variant("s", self.labels[item_id]),
                "enabled": Variant("b", True), "visible": Variant("b", True)}

    def dispatch(self, item_id, event):
        if event == "clicked":
            action = {1: self.controller.show_window, 2: self.controller.open_logs,
                      3: self.controller.request_exit}.get(item_id)
            if action:
                self.controller.dispatch(action)

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @method()
    def GetLayout(self, parentId: "i", recursionDepth: "i", propertyNames: "as") -> "u(ia{sv}av)":
        if parentId not in (0, *self.labels):
            raise ValueError("Unknown menu item")
        children = []
        if parentId == 0 and recursionDepth != 0:
            children = [Variant("(ia{sv}av)", [key, self.properties(key), []]) for key in self.labels]
        return [1, [parentId, self.properties(parentId), children]]

    @method()
    def GetGroupProperties(self, ids: "ai", propertyNames: "as") -> "a(ia{sv})":
        return [[key, self.properties(key)] for key in (ids or list(self.labels))
                if key in (0, *self.labels)]

    @method()
    def GetProperty(self, id: "i", name: "s") -> "v":
        return self.properties(id)[name]

    @method()
    def AboutToShow(self, id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @method()
    def Event(self, id: "i", eventId: "s", data: "v", timestamp: "u"):
        self.dispatch(id, eventId)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        invalid = []
        for item_id, event, data, timestamp in events:
            if item_id not in self.labels:
                invalid.append(item_id)
            else:
                self.dispatch(item_id, event)
        return invalid

    @signal()
    def LayoutUpdated(self, revision, parent) -> "ui":
        return [revision, parent]


class TrayItem(ServiceInterface):
    def __init__(self, controller):
        super().__init__("org.kde.StatusNotifierItem")
        self.controller = controller
        self.pixmaps = icon_pixmaps()
        self.detail = "后台运行；右键菜单可退出程序"

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "turing-studio"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "Turing Studio"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "u":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return self.pixmaps

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return ["", self.pixmaps, "Turing Studio", self.detail]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @method()
    def Activate(self, x: "i", y: "i"):
        self.controller.dispatch(self.controller.show_window)

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        self.controller.dispatch(self.controller.show_window)

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        # Hosts obtain and display the exported DBusMenu themselves.
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @signal()
    def NewToolTip(self):
        return None


class DesktopTray:
    def __init__(self, controller):
        self.controller = controller
        self.registered = False
        self.last_notification = 0
        self.bus = None
        self.loop = None

    def notify(self, message):
        now = time.monotonic()
        if now - self.last_notification < 60 or not self.bus or not self.loop:
            return
        self.last_notification = now
        asyncio.run_coroutine_threadsafe(self._notify(message), self.loop)

    async def _notify(self, message):
        try:
            await asyncio.wait_for(self.bus.call(Message(
                destination="org.freedesktop.Notifications", path="/org/freedesktop/Notifications",
                interface="org.freedesktop.Notifications", member="Notify",
                signature="susssasa{sv}i", body=["Turing Studio", 0, "dialog-warning",
                "屏幕连接异常，正在自动恢复", message, [], {}, 8000])), timeout=3)
        except Exception:
            log.debug("Desktop notification unavailable", exc_info=True)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        while not self.controller.stopping.is_set():
            try:
                self.bus = await MessageBus().connect()
                item = TrayItem(self.controller)
                self.bus.export(ITEM_PATH, item)
                self.bus.export(MENU_PATH, TrayMenu(self.controller))
                await self.bus.request_name(f"org.kde.StatusNotifierItem-{os.getpid()}-1")
                owner = None
                while not self.controller.stopping.is_set() and self.bus.connected:
                    reply = await asyncio.wait_for(self.bus.call(Message(
                        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus", member="GetNameOwner",
                        signature="s", body=[WATCHER])), timeout=3)
                    current = reply.body[0] if reply.message_type != MessageType.ERROR else None
                    if current != owner or not self.registered:
                        self.registered = False
                        if current:
                            result = await asyncio.wait_for(self.bus.call(Message(
                                destination=WATCHER, path="/StatusNotifierWatcher", interface=WATCHER,
                                member="RegisterStatusNotifierItem", signature="s",
                                body=[self.bus.unique_name])), timeout=3)
                            self.registered = result.message_type != MessageType.ERROR
                            if self.registered:
                                log.info("System tray registered")
                        owner = current
                    detail = self.controller.stream_status().get("message", "后台运行")
                    if item.detail != detail:
                        item.detail = detail
                        item.NewToolTip()
                    await asyncio.sleep(1)
            except Exception:
                log.exception("Tray connection unavailable; will reconnect")
                await asyncio.sleep(3)
            finally:
                self.registered = False
                if self.bus:
                    self.bus.disconnect()
                    self.bus = None
