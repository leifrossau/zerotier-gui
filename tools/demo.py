#!/usr/bin/env python3
"""Run ZeroTier GUI against the mock service with made-up devices, e.g. for screenshots.

    python3 tools/demo.py [--page networks|devices|peers] [--dark] [--expand]

Nothing here touches a real ZeroTier service, real devices or your settings: it starts
mock_zt.py on a free port, uses a throwaway config directory and fakes the device scan.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--page", choices=["networks", "devices", "peers"], default="networks")
    ap.add_argument("--dark", action="store_true", help="force the dark style")
    ap.add_argument("--expand", action="store_true", help="expand the first row on the page")
    ap.add_argument("--width", type=int, default=600)
    ap.add_argument("--height", type=int, default=840)
    args = ap.parse_args()

    port = free_port()
    mock = subprocess.Popen([sys.executable, os.path.join(ROOT, "mock_zt.py"), "--port", str(port), "--token", "demo"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    config = tempfile.mkdtemp(prefix="zerotier-gui-demo-")
    os.makedirs(os.path.join(config, "zerotier-gui"))
    with open(os.path.join(config, "zerotier-gui", "nicknames.json"), "w") as f:
        f.write('{"7d20e5c913": "Gaming PC"}')
    os.environ.update(XDG_CONFIG_HOME=config, ZT_PORT=str(port), ZT_TOKEN="demo")
    os.environ.pop("ZT_CENTRAL_TOKEN", None)
    signal.signal(signal.SIGTERM, lambda *_: (mock.terminate(), os._exit(0)))
    time.sleep(0.5)

    import zt_discover
    from zt_discover import Device

    def fake_discover(network, peers, probe=True):
        if network.get("nwid") != "8056c2e21c111111":
            return []
        time.sleep(0.8)
        return [
            Device("10.147.17.21", "3f9c1a7b22", None, 4.2, "nas.local", "Synology DiskStation · nginx",
                   [22, 80, 443, 445], False, 4, True, "1.14.2"),
            Device("10.147.17.34", "7d20e5c913", None, 11.0, "GAMING-PC", None, [445, 3389], False, 11, True, "1.14.2"),
            Device("10.147.17.48", "b84f0d6a55", None, 96.5, None, "Debian · OpenSSH 10.0", [22, 8096],
                   False, -1, False, "1.12.2"),
            Device("10.147.17.60", "e1a7c3940f", None, 23.4, "raspberrypi.local", "Raspbian · OpenSSH 9.2",
                   [22, 25565], False, 23, True, "1.14.1"),
        ]

    zt_discover.discover = fake_discover

    import zerotier_gui as z
    from gi.repository import Adw, GLib

    z.APP_ID = "io.github.leifrossau.ZeroTierGUI.Demo"
    z.zt_tray = None  # no second tray icon
    app = z.App()

    def setup():
        w = app.window
        if not w:
            return True
        if args.dark:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        w.set_default_size(args.width, args.height)
        w.stack.set_visible_child_name(args.page)
        if args.expand:
            GLib.timeout_add(2500, expand, w)
        return False

    def expand(w):
        page = {"networks": w.net_page, "devices": w.devices_page, "peers": w.peer_page}[args.page]

        def walk(widget):
            if isinstance(widget, Adw.ExpanderRow):
                widget.set_expanded(True)
                return True
            child = widget.get_first_child()
            while child:
                if walk(child):
                    return True
                child = child.get_next_sibling()
            return False

        walk(page)
        return False

    GLib.timeout_add(300, setup)
    try:
        return app.run([sys.argv[0]])
    finally:
        mock.terminate()


if __name__ == "__main__":
    sys.exit(main())
