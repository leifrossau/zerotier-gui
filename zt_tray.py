"""Dependency-free system tray icon (StatusNotifierItem + dbusmenu) via Gio D-Bus.

Everything runs on the GLib main loop; no GTK widgets are touched.
"""
import os
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})"/><arg type="a(ia{s})"/>
    </signal>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
  </interface>
</node>
"""


@dataclass
class MenuItem:
    label: str = ""
    callback: Callable[[], None] | None = None
    enabled: bool = True
    visible: bool = True
    icon_name: str | None = None
    children: list["MenuItem"] | None = None  # non-empty -> submenu
    separator: bool = False
    checked: bool | None = None  # None = not a toggle


def _item_props(item: MenuItem | None, names=()) -> dict:
    """dbusmenu properties for one item (None = root), filtered by names if given."""
    props: dict[str, GLib.Variant] = {}
    if item is None:
        props["children-display"] = GLib.Variant("s", "submenu")
    elif item.separator:
        props["type"] = GLib.Variant("s", "separator")
        props["visible"] = GLib.Variant("b", item.visible)
    else:
        props["label"] = GLib.Variant("s", item.label.replace("_", "__"))
        props["enabled"] = GLib.Variant("b", item.enabled)
        props["visible"] = GLib.Variant("b", item.visible)
        if item.icon_name:
            props["icon-name"] = GLib.Variant("s", item.icon_name)
        if item.children:
            props["children-display"] = GLib.Variant("s", "submenu")
        if item.checked is not None:
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", 1 if item.checked else 0)
    if names:
        props = {k: v for k, v in props.items() if k in names}
    return props


class TrayIcon:
    """StatusNotifierItem with a dbusmenu context menu."""

    def __init__(self, item_id: str, title: str, icon_name: str,
                 on_activate: Callable[[], None],
                 build_menu: Callable[[], list[MenuItem]]):
        self._id = item_id
        self._title = title
        self._icon = icon_name
        self._overlay = ""
        self._tooltip = (title, "")
        self._status = "Active"
        self._on_activate = on_activate
        self._build_menu = build_menu

        self._revision = 1
        self._items: dict[int, MenuItem | None] = {}
        self._parents: dict[int, list[int]] = {}  # id -> child ids
        self._rebuild()

        self._service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._conn = None
        self._reg_ids: list[int] = []
        self._owner_id = 0
        self._watch_id = 0
        self._owned = False
        self._watcher_present = False

        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._reg_ids.append(self._conn.register_object(
                SNI_PATH, Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0],
                self._sni_call, self._sni_get, None))
            self._reg_ids.append(self._conn.register_object(
                MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
                self._menu_call, self._menu_get, None))
            self._owner_id = Gio.bus_own_name_on_connection(
                self._conn, self._service, Gio.BusNameOwnerFlags.NONE,
                self._on_name_acquired, self._on_name_lost)
            self._watch_id = Gio.bus_watch_name_on_connection(
                self._conn, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
                self._on_watcher_appeared, self._on_watcher_vanished)
        except GLib.Error as e:
            print(f"zt_tray: D-Bus setup failed: {e.message}")

    # -- public API ---------------------------------------------------------

    def set_icon(self, icon_name: str, overlay_icon_name: str = "") -> None:
        self._icon, self._overlay = icon_name, overlay_icon_name
        self._emit_sni("NewIcon")
        self._emit_sni("NewOverlayIcon")

    def set_tooltip(self, title: str, text: str = "") -> None:
        self._tooltip = (title, text)
        self._emit_sni("NewToolTip")

    def set_status(self, status: str) -> None:
        self._status = status
        self._emit_sni("NewStatus", GLib.Variant("(s)", (status,)))

    def update_menu(self) -> None:
        """Re-run build_menu, bump the revision and notify the host."""
        self._rebuild()
        self._revision += 1
        self._emit(MENU_PATH, MENU_IFACE, "LayoutUpdated",
                   GLib.Variant("(ui)", (self._revision, 0)))

    def close(self) -> None:
        if self._conn is None:
            return
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
        for rid in self._reg_ids:
            self._conn.unregister_object(rid)
        self._watch_id = self._owner_id = 0
        self._reg_ids = []
        self._conn = None

    # -- registration -------------------------------------------------------

    def _on_name_acquired(self, conn, name):
        self._owned = True
        self._register()

    def _on_name_lost(self, conn, name):
        self._owned = False

    def _on_watcher_appeared(self, conn, name, owner):
        self._watcher_present = True
        self._register()

    def _on_watcher_vanished(self, conn, name):
        self._watcher_present = False

    def _register(self):
        if not (self._owned and self._watcher_present and self._conn):
            return
        self._conn.call(
            WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._service,)), None,
            Gio.DBusCallFlags.NONE, -1, None, self._register_done)

    def _register_done(self, conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            print(f"zt_tray: registration failed: {e.message}")

    def _emit(self, path, iface, signal, params=None):
        if self._conn is not None:
            self._conn.emit_signal(None, path, iface, signal, params)

    def _emit_sni(self, signal, params=None):
        self._emit(SNI_PATH, SNI_IFACE, signal, params)

    # -- SNI interface ------------------------------------------------------

    def _sni_get(self, conn, sender, path, iface, prop):
        empty_px = GLib.Variant("a(iiay)", [])
        values = {
            "Category": GLib.Variant("s", "Communications"),
            "Id": GLib.Variant("s", self._id),
            "Title": GLib.Variant("s", self._title),
            "Status": GLib.Variant("s", self._status),
            "WindowId": GLib.Variant("u", 0),
            "IconName": GLib.Variant("s", self._icon),
            "OverlayIconName": GLib.Variant("s", self._overlay),
            "AttentionIconName": GLib.Variant("s", ""),
            "IconPixmap": empty_px,
            "OverlayIconPixmap": empty_px,
            "AttentionIconPixmap": empty_px,
            "ToolTip": GLib.Variant("(sa(iiay)ss)",
                                    (self._icon, [], *self._tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, inv):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._safe_call, self._on_activate)
        inv.return_value(None)  # ContextMenu / Scroll are no-ops

    @staticmethod
    def _safe_call(fn):
        try:
            fn()
        except Exception as e:  # keep the main loop alive
            print(f"zt_tray: callback error: {e!r}")
        return GLib.SOURCE_REMOVE

    # -- menu model ---------------------------------------------------------

    def _rebuild(self):
        """Assign sequential ids (root = 0) and index items by id."""
        self._items = {0: None}
        self._parents = {}
        counter = [0]

        def walk(parent_id, entries):
            ids = []
            for entry in entries:
                counter[0] += 1
                cid = counter[0]
                self._items[cid] = entry
                ids.append(cid)
                if entry.children:
                    walk(cid, entry.children)
            self._parents[parent_id] = ids

        walk(0, self._build_menu())

    def _layout(self, node_id, depth, names):
        children = []
        if depth != 0:
            children = [GLib.Variant("(ia{sv}av)", self._layout(c, depth - 1, names))
                        for c in self._parents.get(node_id, [])]
        return (node_id, _item_props(self._items[node_id], names), children)

    # -- dbusmenu interface -------------------------------------------------

    def _menu_get(self, conn, sender, path, iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, inv):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, names = args
            if parent not in self._items:
                return inv.return_dbus_error(
                    "com.canonical.dbusmenu.Error.UnknownId", f"Unknown id {parent}")
            inv.return_value(GLib.Variant(
                "(u(ia{sv}av))", (self._revision, self._layout(parent, depth, names))))
        elif method == "GetGroupProperties":
            ids, names = args
            ids = ids or list(self._items)
            inv.return_value(GLib.Variant("(a(ia{sv}))", (
                [(i, _item_props(self._items[i], names)) for i in ids if i in self._items],)))
        elif method == "GetProperty":
            item_id, name = args
            props = _item_props(self._items.get(item_id)) if item_id in self._items else None
            if props is None or name not in props:
                return inv.return_dbus_error(
                    "com.canonical.dbusmenu.Error.UnknownProperty",
                    f"No property {name} on id {item_id}")
            inv.return_value(GLib.Variant("(v)", (props[name],)))
        elif method == "Event":
            item_id, event_id, _data, _ts = args
            if item_id not in self._items:
                return inv.return_dbus_error(
                    "com.canonical.dbusmenu.Error.UnknownId", f"Unknown id {item_id}")
            self._handle_event(item_id, event_id)
            inv.return_value(None)
        elif method == "EventGroup":
            missing = []
            for item_id, event_id, _data, _ts in args[0]:
                if item_id in self._items:
                    self._handle_event(item_id, event_id)
                else:
                    missing.append(item_id)
            inv.return_value(GLib.Variant("(ai)", (missing,)))
        elif method == "AboutToShow":
            # Menu is rebuilt by update_menu(); nothing to refresh here.
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [i for i in args[0] if i not in self._items])))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _handle_event(self, item_id, event_id):
        item = self._items.get(item_id)
        if event_id == "clicked" and item and item.callback and item.enabled and item.visible:
            GLib.idle_add(self._safe_call, item.callback)
