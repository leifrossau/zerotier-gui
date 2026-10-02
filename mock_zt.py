"""Mock ZeroTier One service for testing."""

import argparse
import json
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


class MockZTHandler(BaseHTTPRequestHandler):
    """HTTP request handler for mock ZeroTier API."""

    # Class-level state shared across requests
    _networks = {}
    _peers = []
    _token = "test"
    _join_times = {}

    @classmethod
    def initialize_state(cls, token="test"):
        """Initialize mock state with default data."""
        cls._token = token
        cls._join_times = {}

        # Initialize with 2 networks: one OK, one ACCESS_DENIED
        cls._networks = {
            "8056c2e21c111111": {
                "id": "8056c2e21c111111",
                "nwid": "8056c2e21c111111",
                "name": "home-lab",
                "status": "OK",
                "type": "PRIVATE",
                "mac": "12:b0:a3:df:36:27",
                "mtu": 10000,
                "portDeviceName": "zt8056c2e2",
                "assignedAddresses": ["10.147.17.5/24", "fd80::17:5/88"],
                "routes": [{"target": "10.147.0.0/16", "via": None}],
                "allowManaged": True,
                "allowGlobal": False,
                "allowDefault": False,
                "allowDNS": True,
                "bridge": False,
                "broadcastEnabled": False,
                "dns": {"domain": "", "servers": []},
            },
            "8056c2e21c222222": {
                "id": "8056c2e21c222222",
                "nwid": "8056c2e21c222222",
                "name": "",
                "status": "ACCESS_DENIED",
                "type": "PRIVATE",
                "mac": "22:83:90:df:36:27",
                "mtu": 10000,
                "portDeviceName": "zt8056c2e3",
                "assignedAddresses": [],
                "routes": [],
                "allowManaged": False,
                "allowGlobal": False,
                "allowDefault": False,
                "allowDNS": False,
                "bridge": False,
                "broadcastEnabled": False,
                "dns": {"domain": "", "servers": []},
            },
        }

        # Initialize peers (documentation-only IP ranges; leaf IDs match tools/demo.py)
        def peer(address, role, latency, version, path=None):
            paths = [{"address": path, "active": True, "preferred": True}] if path else []
            return {"address": address, "role": role, "latency": latency, "version": version, "paths": paths}

        cls._peers = [
            peer("62f865ae71", "PLANET", 38, "1.14.2", "192.0.2.10/9993"),
            peer("778cde7190", "PLANET", 112, "1.14.2", "192.0.2.20/9993"),
            peer("8056c2e21c", "LEAF", 27, "1.14.2", "198.51.100.7/9993"),
            peer("3f9c1a7b22", "LEAF", 4, "1.14.2", "203.0.113.21/41641"),
            peer("7d20e5c913", "LEAF", 11, "1.14.2", "203.0.113.34/9993"),
            peer("b84f0d6a55", "LEAF", -1, "1.12.2"),
            peer("e1a7c3940f", "LEAF", 23, "1.14.1", "203.0.113.60/21004"),
        ]

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass

    def _check_auth(self) -> bool:
        """Check X-ZT1-Auth header. Return False and send 401 if missing/invalid."""
        token = self.headers.get("X-ZT1-Auth")
        if token != self._token:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"Unauthorized"}')
            return False
        return True

    def _send_json(self, status: int, data):
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _check_transition(self, nwid):
        """Check if a network should transition from REQUESTING_CONFIGURATION to OK."""
        if nwid in self._join_times and nwid in self._networks:
            net = self._networks[nwid]
            if net.get("status") == "REQUESTING_CONFIGURATION":
                elapsed = time.time() - self._join_times[nwid]
                if elapsed >= 4.0:
                    net["status"] = "OK"
                    net["name"] = "new-net"
                    net["assignedAddresses"] = [f"10.{(hash(nwid) % 256)}.0.1/24"]

    def _read_json_body(self):
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        return json.loads(body) if body else {}

    def do_GET(self):
        """Handle GET requests."""
        if not self._check_auth():
            return

        path = urlparse(self.path).path

        if path == "/status":
            status_data = {
                "address": "a1b2c3d4e5",
                "online": True,
                "version": "1.14.2",
                "tcpFallbackActive": False,
                "planetWorldId": 149604618,
                "config": {"settings": {"primaryPort": 9993}},
            }
            self._send_json(200, status_data)

        elif path == "/network":
            # Check transitions for all networks
            for nwid in list(self._networks.keys()):
                self._check_transition(nwid)
            self._send_json(200, list(self._networks.values()))

        elif path.startswith("/network/"):
            nwid = path.split("/")[-1]
            if nwid in self._networks:
                self._check_transition(nwid)
                self._send_json(200, self._networks[nwid])
            else:
                self._send_json(404, {"error": "Network not found"})

        elif path == "/peer":
            self._send_json(200, self._peers)

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        """Handle POST requests (join/update network)."""
        if not self._check_auth():
            return

        path = urlparse(self.path).path

        if not path.startswith("/network/"):
            self._send_json(404, {"error": "Not found"})
            return

        nwid = path.split("/")[-1]
        if len(nwid) != 16 or not all(c in "0123456789abcdefABCDEF" for c in nwid):
            self._send_json(400, {"error": "Invalid network ID"})
            return

        nwid = nwid.lower()
        body = self._read_json_body()

        if nwid not in self._networks:
            # Create new network with REQUESTING_CONFIGURATION status
            self._networks[nwid] = {
                "id": nwid,
                "nwid": nwid,
                "name": "",
                "status": "REQUESTING_CONFIGURATION",
                "type": "PRIVATE",
                "mac": "00:aa:bb:cc:dd:ee",
                "mtu": 10000,
                "portDeviceName": f"zt{nwid[:8]}",
                "assignedAddresses": [],
                "routes": [],
                "allowManaged": body.get("allowManaged", False),
                "allowGlobal": body.get("allowGlobal", False),
                "allowDefault": body.get("allowDefault", False),
                "allowDNS": body.get("allowDNS", False),
                "bridge": False,
                "broadcastEnabled": False,
                "dns": {"domain": "", "servers": []},
            }
            self._join_times[nwid] = time.time()
        else:
            # Update existing network settings
            for key in ["allowManaged", "allowGlobal", "allowDefault", "allowDNS"]:
                if key in body:
                    self._networks[nwid][key] = body[key]

        # Check if we should transition from REQUESTING_CONFIGURATION to OK
        self._check_transition(nwid)

        self._send_json(200, self._networks[nwid])

    def do_DELETE(self):
        """Handle DELETE requests (leave network)."""
        if not self._check_auth():
            return

        path = urlparse(self.path).path

        if not path.startswith("/network/"):
            self._send_json(404, {"error": "Not found"})
            return

        nwid = path.split("/")[-1].lower()

        if nwid not in self._networks:
            self._send_json(404, {"error": "Network not found"})
            return

        del self._networks[nwid]
        if nwid in self._join_times:
            del self._join_times[nwid]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()


def run_mock_server(port=19993, token="test"):
    """Run mock ZeroTier server on specified port."""
    MockZTHandler.initialize_state(token)
    server = ThreadingHTTPServer(("127.0.0.1", port), MockZTHandler)
    print(f"Mock ZeroTier server running on http://127.0.0.1:{port}")
    print(f"Auth token: {token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock ZeroTier One service")
    parser.add_argument("--port", type=int, default=19993, help="Port to listen on")
    parser.add_argument("--token", default="test", help="Auth token")
    args = parser.parse_args()

    run_mock_server(args.port, args.token)
