#!/usr/bin/env python3
"""ZeroTier GUI — a GTK4/libadwaita front end for the local ZeroTier One service."""

import html
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import unicodedata

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zt_api  # noqa: E402
import zt_central  # noqa: E402
import zt_discover  # noqa: E402

try:
    import zt_tray  # noqa: E402
except ImportError:
    zt_tray = None

APP_ID = "io.github.leifrossau.ZeroTierGUI"
HELPER = "/usr/local/libexec/zerotier-gui/zt-gui-helper"  # installed root-owned by install-system.sh
INSTALL_SYSTEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install-system.sh")
REFRESH_SECONDS = 3
CENTRAL_REFRESH_SECONDS = 60
CENTRAL_TOKEN_URL = "https://my.zerotier.com/account"

STATUS_LABELS = {
    "OK": ("Connected", "success"),
    "REQUESTING_CONFIGURATION": ("Waiting for config", "warning"),
    "ACCESS_DENIED": ("Not authorized", "error"),
    "NOT_FOUND": ("Network not found", "error"),
    "PORT_ERROR": ("Port error", "error"),
    "CLIENT_TOO_OLD": ("Client too old", "error"),
}

SETTINGS = [
    ("allowManaged", "Allow managed addresses", "Assign IPs and routes from the controller"),
    ("allowGlobal", "Allow global addresses", "Permit routes that overlap public IP space"),
    ("allowDefault", "Allow default route", "Send all internet traffic through this network"),
    ("allowDNS", "Allow DNS", "Use DNS servers pushed by the controller"),
]

CSS = """
.status-pill { padding: 2px 10px; border-radius: 999px; font-size: 0.85em; font-weight: bold; }
.status-pill.success { background: alpha(@success_color, 0.18); color: @success_color; }
.status-pill.warning { background: alpha(@warning_color, 0.18); color: @warning_color; }
.status-pill.error { background: alpha(@error_color, 0.18); color: @error_color; }
"""


def plain(text, limit=64):
    """Untrusted text (network names from controllers, device names) -> printable, bounded plain text."""
    text = "".join(c for c in str(text or "") if unicodedata.category(c) not in ("Cc", "Cf"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def helper_is_trusted(path):
    """True if path and every directory above it is root-owned and not writable by others,
    so no unprivileged process can swap the program that pkexec runs as root."""
    path = os.path.abspath(path)
    try:
        st = os.stat(path, follow_symlinks=False)
        if not os.path.isfile(path) or os.path.islink(path):
            return False
        while True:
            st = os.stat(path, follow_symlinks=False)
            if st.st_uid != 0 or st.st_mode & 0o022:
                return False
            parent = os.path.dirname(path)
            if parent == path:
                return True
            path = parent
    except OSError:
        return False


def run_async(fn, on_done, on_error=None):
    """Run fn() in a worker thread and deliver the result on the GTK main loop."""

    def worker():
        try:
            result = fn()
        except Exception as e:  # delivered to the UI
            if on_error:
                GLib.idle_add(on_error, e)
            return
        GLib.idle_add(on_done, result)

    threading.Thread(target=worker, daemon=True).start()


def copy_to_clipboard(text):
    # Wayland ignores clipboard requests from unfocused windows (e.g. from the tray),
    # so go through Plasma's clipboard manager when it's available.
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call_sync("org.kde.klipper", "/klipper", "org.kde.klipper.klipper", "setClipboardContents",
                      GLib.Variant("(s)", (text,)), None, Gio.DBusCallFlags.NONE, 1000)
        return
    except GLib.Error:
        pass
    Gdk.Display.get_default().get_clipboard().set(text)


CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "zerotier-gui")
KNOWN_FILE = os.path.join(CONFIG_DIR, "known_networks.json")
AUTOSTART_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "autostart", "zerotier-gui.desktop"
)


def load_known_networks():
    """Networks left via this app, {nwid: name}, so they can be rejoined from the tray."""
    try:
        with open(KNOWN_FILE) as f:
            data = json.load(f)
        return {k: str(v) for k, v in data.items() if zt_api.is_valid_network_id(k)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_known_networks(known):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = KNOWN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(known, f, indent=1)
    os.replace(tmp, KNOWN_FILE)


def make_pill(text, kind):
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("status-pill")
    label.add_css_class(kind)
    return label


def make_copy_button(get_text, on_copied):
    btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy")
    btn.add_css_class("flat")
    btn.connect("clicked", lambda _b: (copy_to_clipboard(get_text()), on_copied()))
    return btn


class NetworkRow(Adw.ExpanderRow):
    """One joined network; updated in place so expansion state survives refreshes."""

    def __init__(self, window, net):
        super().__init__()
        self.window = window
        self.nwid = net["nwid"]
        self._updating = False
        self._pill = None

        self.id_row = Adw.ActionRow(title="Network ID", subtitle_selectable=True)
        self.id_row.add_css_class("property")
        self.id_row.add_suffix(make_copy_button(lambda: self.nwid, lambda: window.toast("Network ID copied")))
        self.add_row(self.id_row)

        self.addr_row = Adw.ActionRow(title="Addresses", subtitle_selectable=True)
        self.addr_row.add_css_class("property")
        self.add_row(self.addr_row)

        self.iface_row = Adw.ActionRow(title="Interface", subtitle_selectable=True)
        self.iface_row.add_css_class("property")
        self.add_row(self.iface_row)

        self.switches = {}
        for key, title, subtitle in SETTINGS:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.connect("notify::active", self._on_switch, key)
            self.switches[key] = row
            self.add_row(row)

        self.authorize_row = Adw.ButtonRow(
            title="Authorize This Device", start_icon_name="emblem-ok-symbolic", visible=False
        )
        self.authorize_row.add_css_class("suggested-action")
        self.authorize_row.connect("activated", lambda _r: window.authorize(self.nwid))
        self.add_row(self.authorize_row)

        leave_row = Adw.ButtonRow(title="Leave Network", start_icon_name="system-log-out-symbolic")
        leave_row.add_css_class("destructive-action")
        leave_row.connect("activated", lambda _r: window.confirm_leave(self.nwid, self.display_name))
        self.add_row(leave_row)

        self.update(net)

    def update(self, net):
        self._updating = True
        name = plain(net.get("name") or self.window.account_names.get(self.nwid, ""))
        self.display_name = name or net["nwid"]
        self.set_title(GLib.markup_escape_text(self.display_name))
        addrs = net.get("assignedAddresses") or []
        status = net.get("status", "UNKNOWN")
        text, kind = STATUS_LABELS.get(status, (status.replace("_", " ").title(), "warning"))

        subtitle = ", ".join(a.split("/")[0] for a in addrs[:2]) or ("" if name else "")
        if name:
            subtitle = f"{net['nwid']}" + (f" · {subtitle}" if subtitle else "")
        self.set_subtitle(GLib.markup_escape_text(subtitle))

        if self._pill:
            self.remove(self._pill)
        self._pill = make_pill(text, kind)
        self.add_suffix(self._pill)

        self.id_row.set_subtitle(net["nwid"])
        self.addr_row.set_subtitle("\n".join(addrs) or "None assigned")
        dev = net.get("portDeviceName") or "—"
        self.iface_row.set_subtitle(f"{dev}  ·  MTU {net.get('mtu', '?')}  ·  {net.get('mac', '')}")
        for key, row in self.switches.items():
            row.set_active(bool(net.get(key)))
        self._updating = False
        self.status = status
        self.refresh_authorize()

    def refresh_authorize(self):
        """Offer authorization only for denied networks owned by the connected account."""
        self.authorize_row.set_visible(self.status == "ACCESS_DENIED" and self.nwid in self.window.account_ids)

    def _on_switch(self, row, _pspec, key):
        if self._updating:
            return
        value = row.get_active()
        self.window.change_setting(self.nwid, key, value, revert=lambda: self._revert(row, not value))

    def _revert(self, row, value):
        self._updating = True
        row.set_active(value)
        self._updating = False


WEB_PORTS = {80: "http", 443: "https", 8080: "http", 8096: "http", 32400: "http"}
TERMINALS = [["konsole", "-e"], ["kgx", "--"], ["gnome-terminal", "--"], ["xterm", "-e"]]


def uri_host(ip):
    return f"[{ip}]" if ":" in ip else ip


def open_uri(window, uri):
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except GLib.Error as e:
        window.toast(f"Couldn't open {uri}: {e.message}")


def open_in_terminal(window, argv):
    for term in TERMINALS:
        if shutil.which(term[0]):
            try:
                Gio.Subprocess.new([*term, *argv], Gio.SubprocessFlags.NONE)
            except GLib.Error as e:
                window.toast(f"Couldn't start {term[0]}: {e.message}")
            return
    window.toast("No terminal emulator found")


def pretty_hostname(name):
    """'OFFICE NAS' (NetBIOS) -> 'Office Nas'; 'nas.local' -> 'nas'."""
    if not name:
        return None
    name = name.removesuffix(".local")
    return name.title() if name.isupper() else name


class DeviceRow(Adw.ExpanderRow):
    """One discovered device with its info and tools."""

    def __init__(self, page, nwid, dev):
        super().__init__()
        self.page, self.window, self.nwid, self.dev = page, page.window, nwid, dev
        key = dev.node_id or dev.ip
        nickname = page.nicknames.get(key)
        description = getattr(dev, "description", None)
        name = nickname or pretty_hostname(dev.hostname) or description
        self.display_name = plain(name or dev.ip)
        self.set_title(GLib.markup_escape_text(self.display_name))
        bits = [dev.ip] if name else []
        if description and description != name:
            bits.append(description)
        if dev.latency_ms is not None:
            bits.append(f"{dev.latency_ms:.0f} ms")
        if dev.ports:
            bits.append(", ".join(zt_discover.COMMON_PORTS.get(p, str(p)) for p in dev.ports))
        self.set_subtitle(GLib.markup_escape_text(" · ".join(bits) or "No open services found"))

        if dev.is_controller:
            self.add_suffix(make_pill("Controller", "warning"))
        elif dev.peer_direct is True:
            self.add_suffix(make_pill("Direct", "success"))
        elif dev.peer_direct is False:
            self.add_suffix(make_pill("Relayed", "warning"))
        if dev.latency_ms is None:
            self.add_suffix(make_pill("No ping", "error"))

        def info(title, value, copy=False):
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value), subtitle_selectable=True)
            row.add_css_class("property")
            if copy:
                row.add_suffix(make_copy_button(lambda: value, lambda: self.window.toast(f"{title} copied")))
            self.add_row(row)

        info("IP Address", dev.ip, copy=True)
        if dev.node_id:
            info("Node ID", dev.node_id, copy=True)
        if dev.hostname:
            info("Hostname", dev.hostname, copy=True)
        if description:
            info("Device", description)
        if dev.node_id and (dev.peer_version or dev.peer_latency_ms is not None):
            path = {True: "direct path", False: "relayed via ZeroTier roots", None: "path unknown"}[dev.peer_direct]
            lat = f"{dev.peer_latency_ms} ms" if dev.peer_latency_ms not in (None, -1) else "—"
            info("ZeroTier", f"v{dev.peer_version or '?'} · {path} · {lat}")
        info(
            "Open Services",
            ", ".join(f"{zt_discover.COMMON_PORTS.get(p, p)} ({p})" for p in dev.ports) or "None of the common ports",
        )

        # Tools
        tools = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                            max_children_per_line=6, homogeneous=False)
        tools.set_margin_top(10)
        tools.set_margin_bottom(10)
        tools.set_margin_start(12)
        tools.set_margin_end(12)

        def tool(label, icon, cb, tip=None):
            content = Adw.ButtonContent(label=label, icon_name=icon)
            btn = Gtk.Button(child=content, tooltip_text=tip or label)
            btn.connect("clicked", lambda _b: cb())
            tools.append(btn)

        host = uri_host(dev.ip)
        tool("Ping", "network-wired-symbolic", self.ping, "Send 4 pings over ZeroTier")
        if 22 in dev.ports:
            tool("SSH", "utilities-terminal-symbolic", lambda: self.ask_user("ssh"), "Open an SSH session in a terminal")
            tool("Files (SFTP)", "folder-remote-symbolic", lambda: self.ask_user("sftp"), "Browse files over SSH")
        if 445 in dev.ports:
            tool("Shares (SMB)", "folder-remote-symbolic", lambda: open_uri(self.window, f"smb://{host}/"))
        for port in dev.ports:
            if port in WEB_PORTS:
                scheme = WEB_PORTS[port]
                default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
                url = f"{scheme}://{host}" + ("" if default else f":{port}") + "/"
                labels = {80: "Website", 443: "Website (HTTPS)", 8080: "Web :8080", 8096: "Jellyfin", 32400: "Plex"}
                tool(labels[port], "applications-internet-symbolic", lambda u=url: open_uri(self.window, u), url)
        if (3389 in dev.ports or 5900 in dev.ports) and shutil.which("krdc"):
            proto = "rdp" if 3389 in dev.ports else "vnc"
            tool("Remote Desktop", "computer-symbolic",
                 lambda: Gio.Subprocess.new(["krdc", f"{proto}://{host}"], Gio.SubprocessFlags.NONE))
        tool("Rename", "document-edit-symbolic", self.rename, "Give this device a local nickname")

        tools_row = Adw.PreferencesRow(activatable=False, child=tools)
        self.add_row(tools_row)

    def ping(self):
        self.page.ping(self.dev)

    def ask_user(self, kind):
        self.page.connect_shell(self.dev, kind, self.display_name)

    def rename(self):
        key = self.dev.node_id or self.dev.ip
        dialog = Adw.AlertDialog(heading="Rename Device", body="This name is only stored on this computer.")
        entry = Gtk.Entry(text=self.page.nicknames.get(key, ""), placeholder_text=self.dev.hostname or self.dev.ip,
                          activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_d, resp):
            if resp == "save":
                self.page.set_nickname(key, entry.get_text().strip())

        dialog.connect("response", on_response)
        dialog.present(self.window)


class DevicesPage(Adw.PreferencesPage):
    """Other devices found on joined networks, with tools to reach them."""

    STALE_SECONDS = 120

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.networks, self.peers = [], []
        self.results = {}  # nwid -> list[Device]
        self.groups = []
        self.scanning = False
        self.scanned_at = 0.0
        self.nicknames = zt_discover.load_nicknames()
        self.usernames = {}

        self.header = Adw.PreferencesGroup(
            title="Devices",
            description="Finds other devices on your networks by pinging them, then checks a few common services.",
        )
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(visible=False)
        self.scan_btn = Gtk.Button(label="Scan")
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self.scan())
        box.append(self.spinner)
        box.append(self.scan_btn)
        self.header.set_header_suffix(box)
        self.add(self.header)

    def on_data(self, networks, peers):
        self.networks, self.peers = networks, peers

    def scan_if_stale(self):
        if time.monotonic() - self.scanned_at > self.STALE_SECONDS:
            self.scan()

    def scan(self):
        if self.scanning:
            return
        nets = [n for n in self.networks if n.get("status") == "OK" and n.get("assignedAddresses")]
        if not nets:
            self.results = {}
            self.render()
            return
        self.scanning = True
        self.spinner.set_visible(True)
        self.scan_btn.set_sensitive(False)
        peers = list(self.peers)

        def work():
            out = {}
            for n in nets:
                out[n["nwid"]] = zt_discover.discover(n, peers)
            return out

        def done(results):
            self._finish()
            self.results = results
            self.render()
            self.window.get_application().update_tray()

        def failed(e):
            self._finish()
            self.window.toast(f"Scan failed: {e}")

        run_async(work, done, failed)

    def _finish(self):
        self.scanning = False
        self.scanned_at = time.monotonic()
        self.spinner.set_visible(False)
        self.scan_btn.set_sensitive(True)

    def ping(self, dev):
        ip = dev.ip

        def work():
            results = [zt_discover.ping(ip, timeout=1.0) for _ in range(4)]
            ok = [r for r in results if r is not None]
            return ok, len(results)

        def done(res):
            ok, total = res
            if ok:
                loss = 100 * (total - len(ok)) // total
                self.window.toast(f"{ip}: avg {sum(ok) / len(ok):.0f} ms, min {min(ok):.0f}, max {max(ok):.0f}, {loss}% loss")
            else:
                self.window.toast(f"{ip}: no reply (it may block ping)")

        self.window.toast(f"Pinging {ip}…")
        run_async(work, done, lambda e: self.window.toast(f"Ping failed: {e}"))

    def connect_shell(self, dev, kind, title):
        """Ask for the remote username, then open SSH or SFTP."""
        key = dev.node_id or dev.ip
        dialog = Adw.AlertDialog(
            heading="Connect via SSH" if kind == "ssh" else "Browse Files via SFTP",
            body=f"Username on {title}:",
        )
        entry = Gtk.Entry(text=self.usernames.get(key, os.environ.get("USER", "")), activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", "Connect")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("go")
        dialog.set_close_response("cancel")

        def on_response(_d, resp):
            user = entry.get_text().strip()
            if resp != "go":
                return
            if not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}", user):
                self.window.toast("Invalid username")
                return
            self.usernames[key] = user
            if kind == "ssh":
                open_in_terminal(self.window, ["ssh", "--", f"{user}@{dev.ip}"])
            else:
                open_uri(self.window, f"sftp://{user}@{uri_host(dev.ip)}/")

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def set_nickname(self, key, name):
        if name:
            self.nicknames[key] = name
        else:
            self.nicknames.pop(key, None)
        try:
            zt_discover.save_nicknames(self.nicknames)
        except OSError as e:
            self.window.toast(f"Couldn't save name: {e}")
        self.render()

    def render(self):
        for g in self.groups:
            self.remove(g)
        self.groups = []
        names = {n["nwid"]: n.get("name") or n["nwid"] for n in self.networks}
        if not self.results:
            g = Adw.PreferencesGroup()
            g.add(Adw.ActionRow(title="No connected networks to scan",
                                subtitle="Join a network on the Networks tab first"))
            self.add(g)
            self.groups.append(g)
            return
        stamp = time.strftime("%H:%M")
        for nwid, devices in self.results.items():
            others = [d for d in devices if not d.is_controller]
            g = Adw.PreferencesGroup(
                title=GLib.markup_escape_text(names.get(nwid, nwid)),
                description=f"{len(others)} device{'s' if len(others) != 1 else ''} found · scanned {stamp}",
            )
            if not devices:
                g.add(Adw.ActionRow(
                    title="No other devices answered",
                    subtitle="Devices may be offline or block ping. Your own device isn't listed.",
                ))
            for d in devices:
                g.add(DeviceRow(self, nwid, d))
            self.add(g)
            self.groups.append(g)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ZeroTier", default_width=560, default_height=680)
        self.client = zt_api.ZeroTierClient()
        self.network_rows = {}
        self.service_state, self.node_status = "loading", {}
        self.node_address = None
        self._refreshing = False

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        # Header
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="ZeroTier")
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        join_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Join Network")
        join_btn.connect("clicked", lambda _b: self.show_join_dialog())
        header.pack_start(join_btn)
        self.join_btn = join_btn

        menu = Gio.Menu()
        svc = Gio.Menu()
        svc.append("Start Service", "win.service-start")
        svc.append("Stop Service", "win.service-stop")
        svc.append("Grant Access to Service", "win.grant-access")
        menu.append_section(None, svc)
        other = Gio.Menu()
        account = Gio.Menu()
        account.append("Connect ZeroTier Account…", "win.account-connect")
        account.append("Disconnect Account", "win.account-disconnect")
        menu.append_section(None, account)
        other.append("Refresh", "win.refresh")
        other.append("Start at Login", "app.autostart")
        other.append("About ZeroTier GUI", "win.about")
        other.append("Quit", "app.quit")
        menu.append_section(None, other)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        toolbar.add_top_bar(header)

        for name, cb in [
            ("service-start", lambda *_: self.run_helper("setup", os.environ.get("USER", ""))),
            ("service-stop", lambda *_: self.run_helper("stop")),
            ("grant-access", lambda *_: self.run_helper("setup", os.environ.get("USER", ""))),
            ("refresh", lambda *_: self.refresh(force_account=True)),
            ("account-connect", lambda *_: self.show_token_dialog()),
            ("account-disconnect", lambda *_: self.disconnect_account()),
            ("about", lambda *_: self.show_about()),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        app.set_accels_for_action("win.refresh", ["F5", "<Control>r"])

        # Networks page
        self.net_page = Adw.PreferencesPage()
        self.node_group = Adw.PreferencesGroup(title="This Device")
        self.node_row = Adw.ActionRow(title="Node ID", subtitle_selectable=True)
        self.node_row.add_css_class("property")
        self.node_row.add_suffix(
            make_copy_button(lambda: self.node_address or "", lambda: self.toast("Node ID copied"))
        )
        self.node_status_row = Adw.ActionRow(title="Status")
        self.node_status_row.add_css_class("property")
        self.node_pill = None
        self.node_group.add(self.node_row)
        self.node_group.add(self.node_status_row)
        self.net_page.add(self.node_group)

        self.net_group = Adw.PreferencesGroup(title="Networks")
        join_suffix = Gtk.Button(label="Join…", valign=Gtk.Align.CENTER)
        join_suffix.add_css_class("flat")
        join_suffix.connect("clicked", lambda _b: self.show_join_dialog())
        self.net_group.set_header_suffix(join_suffix)
        self.empty_row = Adw.ActionRow(
            title="No networks joined", subtitle="Use “Join…” to connect to a network by its 16-digit ID"
        )
        self.net_group.add(self.empty_row)
        self.net_page.add(self.net_group)

        # ZeroTier Central account
        self.central = zt_central.CentralClient()
        self.account_networks = None  # None = not loaded yet
        self.account_ids = set()
        self.account_names = {}
        self.account_error = None
        self._account_loading = False
        self._account_fetched_at = 0.0
        self._account_render_key = None
        self.account_rows = []
        self.account_group = Adw.PreferencesGroup(title="Your ZeroTier Account", visible=False)
        self.net_page.add(self.account_group)

        # Devices page
        self.devices_page = DevicesPage(self)

        # Peers page
        self.peer_page = Adw.PreferencesPage()
        self.peer_group = Adw.PreferencesGroup(title="Peers")
        self.peer_page.add(self.peer_group)
        self.peer_rows = []
        self.last_networks, self.last_peers = [], []

        self.stack.add_titled_with_icon(self.net_page, "networks", "Networks", "network-vpn-symbolic")
        self.stack.add_titled_with_icon(self.devices_page, "devices", "Devices", "computer-symbolic")
        self.stack.add_titled_with_icon(self.peer_page, "peers", "Peers", "system-users-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_page_changed)

        # Error / setup state
        self.error_page = Adw.StatusPage(icon_name="network-offline-symbolic")
        self.error_button = Gtk.Button(halign=Gtk.Align.CENTER)
        self.error_button.add_css_class("pill")
        self.error_button.add_css_class("suggested-action")
        self.error_button.connect("clicked", self._on_error_button)
        self.error_page.set_child(self.error_button)
        self._error_action = None

        self.outer = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.outer.add_named(self.stack, "main")
        self.outer.add_named(self.error_page, "error")
        self.outer.add_named(Adw.Spinner(), "loading")
        self.outer.set_visible_child_name("loading")
        toolbar.set_content(self.outer)

        self.refresh()
        self._timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)
        self.connect("close-request", self._on_close)

    # ---- helpers -------------------------------------------------------

    def toast(self, text):
        text = plain(text, 200)
        if self.is_visible():
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))
        else:  # started from the tray: use a desktop notification (Plasma renders a markup subset)
            note = Gio.Notification.new("ZeroTier")
            note.set_body(html.escape(text))
            self.get_application().send_notification("status", note)

    def _tick(self):
        self.refresh()  # keep polling while hidden so the tray stays current
        return GLib.SOURCE_CONTINUE

    def _on_close(self, *_):
        if self.get_application().tray:
            self.set_visible(False)  # keep running in the tray
            return True
        GLib.source_remove(self._timer)
        return False

    # ---- data refresh --------------------------------------------------

    def refresh(self, force_account=False):
        if force_account or time.monotonic() - self._account_fetched_at > CENTRAL_REFRESH_SECONDS:
            self.refresh_account()
        if self._refreshing:
            return
        self._refreshing = True

        def fetch():
            client = self.client
            if client.token is None:  # token may have been granted since start
                client = self.client = zt_api.ZeroTierClient()
            return client.status(), client.networks(), client.peers()

        run_async(fetch, self._on_data, self._on_fetch_error)

    def _on_data(self, data):
        self._refreshing = False
        status, networks, peers = data
        self.outer.set_visible_child_name("main")
        self.join_btn.set_sensitive(True)
        self._update_node(status)
        self._update_networks(networks)
        self._update_peers(peers)
        self.last_networks, self.last_peers = networks, peers
        self.devices_page.on_data(networks, peers)
        self.service_state, self.node_status = "ok", status
        self.get_application().update_tray()

    def _on_page_changed(self, stack, _pspec):
        if stack.get_visible_child_name() == "devices":
            self.devices_page.scan_if_stale()

    def _on_fetch_error(self, err):
        self._refreshing = False
        self.join_btn.set_sensitive(False)
        self.service_state = "auth" if isinstance(err, zt_api.AuthError) else "down"
        self.get_application().update_tray()
        if isinstance(err, zt_api.AuthError):
            self._show_error(
                "dialog-password-symbolic",
                "Access Needed",
                "This app needs ZeroTier running and a copy of its auth token in your home folder.",
                "Grant Access",
                lambda: self.run_helper("setup", os.environ.get("USER", "")),
            )
        elif isinstance(err, zt_api.UntrustedListener):
            self._show_error(
                "dialog-warning-symbolic",
                "Unexpected Program on ZeroTier's Port",
                GLib.markup_escape_text(
                    f"{err} The ZeroTier service is probably not running, and another program is listening "
                    "where it should be. Your access token was not sent. Stop that program, then start ZeroTier."
                ),
                "Start Service",
                lambda: self.run_helper("setup", os.environ.get("USER", "")),
            )
        elif isinstance(err, zt_api.ServiceUnavailable):
            self._show_error(
                "network-offline-symbolic",
                "ZeroTier Is Not Running",
                "The zerotier-one service isn't reachable. Start and enable it to manage your networks.",
                "Start Service",
                lambda: self.run_helper("setup", os.environ.get("USER", "")),
            )
        else:
            self._show_error("dialog-error-symbolic", "Something Went Wrong", GLib.markup_escape_text(str(err)),
                             "Retry", self.refresh)

    def _show_error(self, icon, title, desc, button, action):
        self.error_page.set_icon_name(icon)
        self.error_page.set_title(title)
        self.error_page.set_description(desc)
        self.error_button.set_label(button)
        self._error_action = action
        self.outer.set_visible_child_name("error")

    def _on_error_button(self, _btn):
        if self._error_action:
            self._error_action()

    def _update_node(self, status):
        self.node_address = status.get("address")
        self.node_row.set_subtitle(self.node_address or "—")
        online = status.get("online")
        text = "Online" if online else "Offline"
        if status.get("tcpFallbackActive"):
            text += " (TCP relay)"
        self.node_status_row.set_subtitle(f"Version {status.get('version', '?')}")
        if self.node_pill:
            self.node_status_row.remove(self.node_pill)
        self.node_pill = make_pill(text, "success" if online else "error")
        self.node_status_row.add_suffix(self.node_pill)

    def _update_networks(self, networks):
        seen = set()
        for net in sorted(networks, key=lambda n: (n.get("name") or "~", n["nwid"])):
            nwid = net["nwid"]
            seen.add(nwid)
            row = self.network_rows.get(nwid)
            if row:
                row.update(net)
            else:
                row = self.network_rows[nwid] = NetworkRow(self, net)
                self.net_group.add(row)
        for nwid in list(self.network_rows):
            if nwid not in seen:
                self.net_group.remove(self.network_rows.pop(nwid))
        self.empty_row.set_visible(not self.network_rows)
        self._render_account()

    # ---- ZeroTier Central account --------------------------------------

    def refresh_account(self):
        if self._account_loading:
            return
        if self.central.token is None:
            self.central = zt_central.CentralClient()  # token may have been saved since
        self._account_fetched_at = time.monotonic()
        if self.central.token is None:
            self.account_networks, self.account_error = None, None
            self._set_account_ids(set())
            self._render_account()
            return
        self._account_loading = True

        def done(networks):
            self._account_loading = False
            self.account_networks, self.account_error = networks, None
            self.account_names = {n["id"]: (n.get("config") or {}).get("name") or "" for n in networks}
            self._set_account_ids({n["id"] for n in networks})
            self._render_account()

        def failed(e):
            self._account_loading = False
            self.account_error = e
            self._render_account()

        run_async(self.central.networks, done, failed)

    def _set_account_ids(self, ids):
        self.account_ids = ids
        for row in self.network_rows.values():
            row.refresh_authorize()

    def _render_account(self):
        joined = frozenset(self.network_rows)
        nets = self.account_networks
        key = (
            self.central.token is not None,
            repr(self.account_error),
            tuple((n["id"], (n.get("config") or {}).get("name"), n.get("onlineMemberCount")) for n in nets or []),
            joined,
        )
        if key == self._account_render_key:
            return
        self._account_render_key = key
        for row in self.account_rows:
            self.account_group.remove(row)
        self.account_rows = []

        def add(row):
            self.account_group.add(row)
            self.account_rows.append(row)

        # API tokens need a paid ZeroTier plan, so only show this section once connected (via the menu).
        self.account_group.set_visible(self.central.token is not None)
        if self.central.token is None:
            return

        if self.account_error is not None:
            if isinstance(self.account_error, zt_central.CentralAuthError):
                title, sub = "API token rejected", "Reconnect with a new token from my.zerotier.com"
            else:
                title, sub = "Couldn't reach ZeroTier Central", str(self.account_error)
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(sub))
            btn = Gtk.Button(label="Reconnect…", valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b: self.show_token_dialog())
            row.add_suffix(btn)
            add(row)
            return

        if nets is None:
            add(Adw.ActionRow(title="Loading networks…"))
            return
        if not nets:
            add(Adw.ActionRow(title="No networks in this account", subtitle="Create one at my.zerotier.com"))
            return

        self.account_group.set_description(f"{len(nets)} networks")
        for n in nets:
            nwid = n["id"]
            cfg = n.get("config") or {}
            online = n.get("onlineMemberCount")
            sub = nwid + (f" · {online} online" if online is not None else "")
            row = Adw.ActionRow(title=GLib.markup_escape_text(cfg.get("name") or nwid), subtitle=sub)
            if nwid in joined:
                row.add_suffix(make_pill("Joined", "success"))
            else:
                btn = Gtk.Button(label="Join", valign=Gtk.Align.CENTER)
                btn.add_css_class("suggested-action")
                btn.connect("clicked", lambda b, nwid=nwid: (b.set_sensitive(False), self.join(nwid, authorize=True)))
                row.add_suffix(btn)
            add(row)

    def _authorize_self(self, nwid):
        """Authorize this node on an account network. Runs in a worker thread."""
        node = self.node_address
        if not node:
            raise zt_central.CentralError("Node ID not known yet")
        member = self.central.member(nwid, node)
        if member and (member.get("config") or {}).get("authorized"):
            return
        # Name new members after this computer; keep any name set in Central.
        self.central.authorize_member(nwid, node, name=None if member else socket.gethostname())

    def authorize(self, nwid):
        def done(_):
            self.toast("Device authorized")
            self.refresh()

        run_async(lambda: self._authorize_self(nwid), done, lambda e: self.toast(f"Authorization failed: {e}"))

    def show_token_dialog(self):
        dialog = Adw.AlertDialog(
            heading="Connect ZeroTier Account",
            body=(
                f'Create an API token at <a href="{CENTRAL_TOKEN_URL}">my.zerotier.com</a> under '
                "<b>Account → API Access Tokens</b> and paste it below. "
                "It is stored in your home folder, readable only by you."
            ),
            body_use_markup=True,
        )
        entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True, placeholder_text="API token")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("connect", "Connect")
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("connect")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("connect", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled("connect", bool(e.get_text().strip())))

        def on_response(_d, response):
            if response != "connect":
                return
            token = entry.get_text().strip()

            def verify():
                zt_central.CentralClient(token=token).networks()  # raises if the token is bad
                zt_central.save_token(token)

            def done(_):
                self.central = zt_central.CentralClient()
                self.toast("Account connected")
                self.refresh_account()

            def failed(e):
                if isinstance(e, zt_central.CentralAuthError):
                    self.toast("That API token was rejected")
                else:
                    self.toast(f"Couldn't connect: {e}")

            run_async(verify, done, failed)

        dialog.connect("response", on_response)
        dialog.present(self)
        entry.grab_focus()

    def disconnect_account(self):
        try:
            zt_central.clear_token()
        except OSError as e:
            self.toast(f"Couldn't remove token: {e}")
            return
        self.central = zt_central.CentralClient()
        self.toast("Account disconnected")
        self.account_networks, self.account_error = None, None
        self._set_account_ids(set())
        self._render_account()

    def _update_peers(self, peers):
        for row in self.peer_rows:
            self.peer_group.remove(row)
        self.peer_rows = []
        order = {"LEAF": 0, "MOON": 1, "PLANET": 2}
        peers = sorted(peers, key=lambda p: (order.get(p.get("role"), 3), p.get("address", "")))
        self.peer_group.set_description(f"{len(peers)} known peers")
        for p in peers:
            paths = [x for x in p.get("paths") or [] if x.get("active")]
            preferred = next((x for x in paths if x.get("preferred")), paths[0] if paths else None)
            latency = p.get("latency", -1)
            lat_text = f"{latency} ms" if latency is not None and latency >= 0 else "no direct path"
            via = preferred["address"] if preferred else "relayed"
            row = Adw.ActionRow(
                title=p.get("address", "?"),
                subtitle=GLib.markup_escape_text(f"{via} · {lat_text} · v{p.get('version', '?')}"),
                subtitle_selectable=True,
            )
            role = p.get("role", "?")
            row.add_suffix(make_pill(role.title(), "success" if role == "LEAF" and paths else "warning"))
            self.peer_group.add(row)
            self.peer_rows.append(row)

    # ---- actions -------------------------------------------------------

    def show_join_dialog(self):
        dialog = Adw.AlertDialog(
            heading="Join Network",
            body="Enter the 16-character network ID from your ZeroTier controller.",
        )
        entry = Gtk.Entry(placeholder_text="e.g. 8056c2e21c000001", max_length=16, activates_default=True)
        entry.add_css_class("monospace")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("join", "Join")
        dialog.set_response_appearance("join", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("join")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("join", False)

        def on_changed(e):
            ok = zt_api.is_valid_network_id(e.get_text().strip())
            dialog.set_response_enabled("join", ok)
            if ok or not e.get_text():
                e.remove_css_class("error")
            else:
                e.add_css_class("error")

        entry.connect("changed", on_changed)

        # Prefill from clipboard if it holds a network ID.
        def on_clip(clip, res):
            try:
                text = (clip.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if zt_api.is_valid_network_id(text) and not entry.get_text():
                entry.set_text(text.lower())

        Gdk.Display.get_default().get_clipboard().read_text_async(None, on_clip)

        def on_response(_d, response):
            if response == "join":
                nwid = entry.get_text().strip().lower()
                self.join(nwid, authorize=nwid in self.account_ids)

        dialog.connect("response", on_response)
        dialog.present(self)
        entry.grab_focus()

    def join(self, nwid, authorize=False):
        """Join locally; for account networks, also authorize this device in Central."""

        def work():
            self.client.join(nwid)
            if authorize:
                try:
                    self._authorize_self(nwid)
                except zt_central.CentralError as e:
                    return f"Joined, but authorization failed: {e}"
            return f"Joining {nwid}…"

        def done(msg):
            self.toast(msg)
            self._account_render_key = None  # re-enable the Join button if joining failed
            self.refresh()

        def failed(e):
            self.toast(f"Join failed: {e}")
            self._account_render_key = None
            self._render_account()

        run_async(work, done, failed)

    def confirm_leave(self, nwid, title):
        dialog = Adw.AlertDialog(
            heading="Leave Network?",
            body=f"You will be disconnected from {title}. You can rejoin later with the network ID.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("leave", "Leave")
        dialog.set_response_appearance("leave", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "leave":
                self.leave(nwid)

        dialog.connect("response", on_response)
        dialog.present(self)

    def leave(self, nwid):
        name = next((n.get("name") for n in self.last_networks if n["nwid"] == nwid), "") or ""

        def done(_):
            known = load_known_networks()
            known[nwid] = name
            try:
                save_known_networks(known)
            except OSError:
                pass
            self.toast(f"Left {plain(name) or nwid}")
            self.refresh()

        run_async(lambda: self.client.leave(nwid), done, lambda e: self.toast(f"Leave failed: {e}"))

    def change_setting(self, nwid, key, value, revert):
        def failed(e):
            revert()
            self.toast(f"Could not change setting: {e}")

        run_async(lambda: self.client.update_network(nwid, **{key: value}), lambda _: self.refresh(), failed)

    def run_helper(self, *args):
        """Run the privileged helper through polkit, then refresh."""
        if not helper_is_trusted(HELPER):
            self.show_helper_missing()
            return
        try:
            proc = Gio.Subprocess.new(
                ["pkexec", HELPER, *args],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE,
            )
        except GLib.Error as e:
            self.toast(f"Could not run pkexec: {e.message}")
            return

        def finished(p, res):
            try:
                _ok, out, _err = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                self.toast(e.message)
                return
            code = p.get_exit_status()
            last = (out or "").strip().splitlines()[-1:] or [f"exit code {code}"]
            if code == 0:
                self.toast("Done")
            elif code == 126:  # pkexec: dialog dismissed
                self.toast("Authorization cancelled")
            else:  # 127 covers both "not authorized" and "cannot run helper"
                self.toast(f"Failed: {last[0]}")
            self.client = zt_api.ZeroTierClient()  # pick up a newly copied token
            self.refresh()

        proc.communicate_utf8_async(None, None, finished)

    def show_helper_missing(self):
        self.get_application().show_window()
        dialog = Adw.AlertDialog(
            heading="System Helper Not Installed",
            body=(
                "Starting ZeroTier and granting access need a small helper that runs as root. "
                "For safety it must be installed root-owned, outside your home folder. "
                "Run this once in a terminal:"
            ),
        )
        cmd = f"sudo {INSTALL_SYSTEM}"
        label = Gtk.Label(label=cmd, selectable=True, wrap=True, wrap_mode=2)
        label.add_css_class("monospace")
        dialog.set_extra_child(label)
        dialog.add_response("close", "Close")
        dialog.add_response("copy", "Copy Command")
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "copy" and (copy_to_clipboard(cmd), self.toast("Command copied")))
        dialog.present(self)

    def show_about(self):
        about = Adw.AboutDialog(
            application_name="ZeroTier GUI",
            application_icon="network-vpn",
            developer_name="leifrossau",
            version="1.0",
            website="https://github.com/leifrossau/zerotier-gui",
            issue_url="https://github.com/leifrossau/zerotier-gui/issues",
            comments="An unofficial manager for ZeroTier One networks. Not affiliated with ZeroTier, Inc.",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.tray = None
        self.window = None
        self.start_hidden = False
        self._tray_key = None
        self.add_main_option("hidden", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start in the system tray without opening the window", None)

    def do_handle_local_options(self, options):
        self.start_hidden = options.contains("hidden")
        return -1  # continue normal startup

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        autostart = Gio.SimpleAction.new_stateful("autostart", None, GLib.Variant("b", os.path.exists(AUTOSTART_FILE)))
        autostart.connect("activate", lambda *_: self.toggle_autostart())
        self.add_action(autostart)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit_app())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        if zt_tray:
            try:
                self.tray = zt_tray.TrayIcon(APP_ID, "ZeroTier", "network-vpn", self.toggle_window, self.build_tray_menu)
                self.hold()  # keep running when the window is closed
            except Exception as e:  # no session bus etc. — run without a tray
                print(f"Tray unavailable: {e}", file=sys.stderr)
                self.tray = None

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self)
            if self.start_hidden and self.tray:
                self.start_hidden = False
                return
        self.show_window()

    def show_window(self):
        self.window.set_visible(True)
        self.window.present()

    def toggle_window(self):
        if self.window.is_visible() and self.window.is_active():
            self.window.set_visible(False)
        else:
            self.show_window()

    def quit_app(self):
        if self.tray:
            self.tray.close()
            self.tray = None
            self.release()
        self.quit()

    # ---- tray ----------------------------------------------------------

    def update_tray(self):
        if not self.tray or not self.window:
            return
        w = self.window
        nets = w.last_networks if w.service_state == "ok" else []
        online = bool(w.node_status.get("online")) if w.service_state == "ok" else False
        devices = {nwid: [(d.ip, d.node_id, d.hostname, tuple(d.ports)) for d in devs]
                   for nwid, devs in w.devices_page.results.items()}
        key = (
            w.service_state, online,
            tuple((n["nwid"], n.get("name"), n.get("status"), tuple(n.get("assignedAddresses") or [])) for n in nets),
            repr(devices), repr(sorted(w.devices_page.nicknames.items())), os.path.exists(AUTOSTART_FILE),
        )
        if key == self._tray_key:
            return
        self._tray_key = key

        problems = [n for n in nets if n.get("status") != "OK"]
        if w.service_state != "ok":
            self.tray.set_icon("network-offline")
            self.tray.set_tooltip("ZeroTier", "Service not running" if w.service_state == "down" else "Access needed")
            self.tray.set_status("Active")
        else:
            self.tray.set_icon("network-vpn" if online else "network-offline",
                               "emblem-warning" if problems else "")
            lines = [f"{plain(n.get('name')) or n['nwid']}: "
                     + (", ".join(a.split("/")[0] for a in n.get("assignedAddresses") or [])
                        or STATUS_LABELS.get(n.get("status"), ("?",))[0])
                     for n in nets]
            self.tray.set_tooltip("ZeroTier — " + ("Online" if online else "Offline"),
                                  html.escape("\n".join(lines) or "No networks joined"))
            self.tray.set_status("NeedsAttention" if problems else "Active")
        self.tray.update_menu()

    def _with_window(self, fn):
        """Bring up the window, then run fn (for actions that need a dialog)."""
        def run():
            self.show_window()
            fn()
        return run

    def build_tray_menu(self):
        M = zt_tray.MenuItem
        w = self.window
        if not w:
            return [M("Quit", self.quit_app)]
        items = []

        if w.service_state == "ok":
            online = w.node_status.get("online")
            items.append(M(f"ZeroTier: {'Online' if online else 'Offline'} · {w.node_address or ''}", enabled=False))
        elif w.service_state == "loading":
            items.append(M("ZeroTier: connecting…", enabled=False))
        else:
            items.append(M("ZeroTier: service not running" if w.service_state == "down" else "ZeroTier: access needed",
                           enabled=False))
            items.append(M("Start ZeroTier Service…" if w.service_state == "down" else "Grant Access…",
                           lambda: w.run_helper("setup", os.environ.get("USER", ""))))
        items.append(M("Open ZeroTier", self.show_window, icon_name="network-vpn"))
        items.append(M(separator=True))

        joined = set()
        if w.service_state == "ok":
            for n in sorted(w.last_networks, key=lambda n: n.get("name") or n["nwid"]):
                nwid = n["nwid"]
                joined.add(nwid)
                name = plain(n.get("name") or w.account_names.get(nwid)) or nwid
                status = STATUS_LABELS.get(n.get("status"), (n.get("status", "?"),))[0]
                addrs = [a.split("/")[0] for a in n.get("assignedAddresses") or []]
                sub = [M(f"Copy IP {a}", lambda a=a: (copy_to_clipboard(a), w.toast(f"Copied {a}"))) for a in addrs]
                sub.append(M("Copy Network ID", lambda nwid=nwid: (copy_to_clipboard(nwid), w.toast("Network ID copied"))))
                if n.get("status") == "ACCESS_DENIED" and nwid in w.account_ids:
                    sub.append(M("Authorize This Device", lambda nwid=nwid: w.authorize(nwid)))
                sub.append(M(separator=True))
                sub.append(M("Leave Network", lambda nwid=nwid: w.leave(nwid), icon_name="system-log-out-symbolic"))
                items.append(M(f"{name} — {status}", children=sub,
                               icon_name="network-vpn" if n.get("status") == "OK" else "emblem-warning"))

            rejoin = [M(plain(name) or nwid, lambda nwid=nwid: w.join(nwid))
                      for nwid, name in load_known_networks().items() if nwid not in joined]
            if rejoin:
                items.append(M("Rejoin", children=rejoin))
            items.append(M("Join Network…", self._with_window(w.show_join_dialog), icon_name="list-add"))

            # Devices from the last scan
            page = w.devices_page
            dev_items = []
            for nwid, devs in page.results.items():
                for d in devs:
                    if d.is_controller:
                        continue
                    label = plain(page.nicknames.get(d.node_id or d.ip) or pretty_hostname(d.hostname)
                                  or getattr(d, "description", None) or d.ip)
                    host = uri_host(d.ip)
                    sub = [M(f"Copy IP {d.ip}", lambda d=d: (copy_to_clipboard(d.ip), w.toast(f"Copied {d.ip}"))),
                           M("Ping", lambda d=d: page.ping(d))]
                    if 22 in d.ports:
                        sub.append(M("SSH…", self._with_window(lambda d=d, t=label: page.connect_shell(d, "ssh", t)),
                                     icon_name="utilities-terminal"))
                        sub.append(M("Files (SFTP)…",
                                     self._with_window(lambda d=d, t=label: page.connect_shell(d, "sftp", t)),
                                     icon_name="folder-remote"))
                    if 445 in d.ports:
                        sub.append(M("Shares (SMB)", lambda h=host: open_uri(w, f"smb://{h}/"), icon_name="folder-remote"))
                    for port in d.ports:
                        if port in WEB_PORTS:
                            default = port in (80, 443)
                            url = f"{WEB_PORTS[port]}://{host}" + ("" if default else f":{port}") + "/"
                            sub.append(M(f"Open {url}", lambda u=url: open_uri(w, u), icon_name="applications-internet"))
                    dev_items.append(M(label if label != d.ip else d.ip, children=sub, icon_name="computer"))
            if page.scanning:
                dev_items.append(M("Scanning…", enabled=False))
            elif not page.results:
                dev_items.append(M("Not scanned yet", enabled=False))
            dev_items.append(M(separator=True))
            dev_items.append(M("Scan Now", page.scan))
            items.append(M("Devices", children=dev_items, icon_name="computer"))

        items.append(M(separator=True))
        items.append(M("Start at Login", self.toggle_autostart, checked=os.path.exists(AUTOSTART_FILE)))
        if w.service_state == "ok":
            items.append(M("Stop ZeroTier Service…", lambda: w.run_helper("stop")))
        items.append(M("Quit", self.quit_app, icon_name="application-exit"))
        return items

    def toggle_autostart(self):
        try:
            if os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
            else:
                os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
                exe = shutil.which("zerotier-gui") or f"{sys.executable} {os.path.abspath(__file__)}"
                with open(AUTOSTART_FILE, "w") as f:
                    f.write("[Desktop Entry]\nType=Application\nName=ZeroTier GUI\nIcon=network-vpn\n"
                            f"Exec={exe} --hidden\nX-GNOME-Autostart-enabled=true\n")
        except OSError as e:
            self.window.toast(f"Couldn't change autostart: {e}")
        self._tray_key = None
        self.update_tray()
        self.lookup_action("autostart").set_state(GLib.Variant("b", os.path.exists(AUTOSTART_FILE)))


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
