"""ZeroTier One local service JSON API client."""

import os
import json
import pwd
import socket
import urllib.request
import urllib.error
from typing import Optional


class ZeroTierError(Exception):
    """Base exception for ZeroTier API errors."""
    pass


class AuthError(ZeroTierError):
    """Authentication or authorization error (401/403)."""
    pass


class ServiceUnavailable(ZeroTierError):
    """Service is unavailable (connection refused, timeout, etc)."""
    pass


class UntrustedListener(ServiceUnavailable):
    """The API port is held by a process that isn't the ZeroTier service; the token is not sent."""


def listener_uids(port: int) -> set[int]:
    """UIDs of all processes listening on TCP `port` (any address), from /proc/net/tcp{,6}."""
    uids = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with open(table) as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) > 7 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                    uids.add(int(fields[7]))
    return uids


def trusted_service_uids() -> set[int]:
    """root plus the account the ZeroTier service runs as (it owns its data directory)."""
    uids = {0}
    try:
        uids.add(pwd.getpwnam("zerotier-one").pw_uid)
    except KeyError:
        pass
    try:
        uids.add(os.stat("/var/lib/zerotier-one/identity.public").st_uid)
    except OSError:
        pass
    return uids


TOKEN_PATHS = [
    os.path.expanduser("~/.zeroTierOneAuthToken"),
    "/var/lib/zerotier-one/authtoken.secret",
]


def find_token() -> Optional[str]:
    """Find auth token from env var or readable token files.

    Checks ZT_TOKEN env var first, then reads first accessible file in TOKEN_PATHS.
    Returns stripped token or None if not found.
    """
    token = os.environ.get("ZT_TOKEN")
    if token:
        return token.strip()

    for path in TOKEN_PATHS:
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            continue

    return None


def default_port() -> int:
    """Get default port from /var/lib/zerotier-one/zerotier-one.port or 9993."""
    try:
        with open("/var/lib/zerotier-one/zerotier-one.port", "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 9993


def is_valid_network_id(nwid: str) -> bool:
    """Check if nwid is exactly 16 hex characters."""
    if len(nwid) != 16:
        return False
    try:
        int(nwid, 16)
        return True
    except ValueError:
        return False


class ZeroTierClient:
    """Client for ZeroTier One local JSON API."""

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None,
                 token: Optional[str] = None, timeout: float = 3.0):
        """Initialize client.

        Args:
            host: API host (default 127.0.0.1)
            port: API port (default from env ZT_PORT or default_port())
            token: Auth token (default from find_token())
            timeout: Request timeout in seconds
        """
        self.host = host
        # Only verify the listener for the real service; explicit ports/hosts are for tests and mocks.
        self.verify_listener = port is None and "ZT_PORT" not in os.environ and host == "127.0.0.1"
        self.port = port if port is not None else int(os.environ.get("ZT_PORT", default_port()))
        self.token = token if token is not None else find_token()
        self.timeout = timeout

    def _request(self, method: str, path: str, data: Optional[dict] = None) -> dict:
        """Make authenticated HTTP request to ZeroTier API.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: API path (e.g. "/status")
            data: JSON body for POST requests

        Returns:
            Parsed JSON response or empty dict if body is empty

        Raises:
            AuthError: On 401/403 or missing token
            ZeroTierError: On other HTTP errors
            ServiceUnavailable: On connection/timeout errors
        """
        if self.token is None:
            raise AuthError("No auth token found")

        if self.verify_listener:
            self._check_listener()

        url = f"http://{self.host}:{self.port}{path}"

        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("X-ZT1-Auth", self.token)

            if data is not None:
                req.add_header("Content-Type", "application/json")
                req.data = json.dumps(data).encode("utf-8")

            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}

        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"Authentication failed: {e.code}")
            snippet = e.read().decode("utf-8", errors="ignore")[:200]
            raise ZeroTierError(f"HTTP {e.code}: {snippet}")

        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
            raise ServiceUnavailable(f"Service unavailable: {e}")

    def _check_listener(self) -> None:
        """Refuse to send the token unless the port is held by the ZeroTier service itself.

        When zerotier-one is stopped, any local program (including sandboxed apps that share
        the host network) could listen on its port and collect the token.
        """
        try:
            uids = listener_uids(self.port)
        except (OSError, ValueError) as e:
            raise ServiceUnavailable(f"Can't verify the ZeroTier service ({type(e).__name__})")
        if not uids:
            raise ServiceUnavailable("ZeroTier service is not listening")
        untrusted = uids - trusted_service_uids()
        if untrusted:
            raise UntrustedListener(
                f"Port {self.port} is held by another program (uid {', '.join(map(str, sorted(untrusted)))}); "
                "not sending the ZeroTier token"
            )

    def status(self) -> dict:
        """Get ZeroTier service status."""
        return self._request("GET", "/status")

    def networks(self) -> list:
        """List all joined networks."""
        return self._request("GET", "/network")

    def network(self, nwid: str) -> dict:
        """Get network details."""
        return self._request("GET", f"/network/{nwid}")

    def join(self, nwid: str) -> dict:
        """Join a network.

        Args:
            nwid: Network ID (16 hex chars)

        Raises:
            ValueError: If nwid is invalid
        """
        if not is_valid_network_id(nwid):
            raise ValueError(f"Invalid network ID: {nwid}")

        nwid = nwid.lower()
        return self._request("POST", f"/network/{nwid}", {})

    def leave(self, nwid: str) -> None:
        """Leave a network."""
        self._request("DELETE", f"/network/{nwid}")

    def update_network(self, nwid: str, **settings) -> dict:
        """Update network settings.

        Args:
            nwid: Network ID
            allowManaged: Allow managed IPs
            allowGlobal: Allow global routes
            allowDefault: Allow default route
            allowDNS: Allow managed DNS

        Raises:
            ValueError: If unknown settings are provided
        """
        allowed_keys = {"allowManaged", "allowGlobal", "allowDefault", "allowDNS"}
        invalid_keys = set(settings.keys()) - allowed_keys
        if invalid_keys:
            raise ValueError(f"Invalid settings: {invalid_keys}")

        return self._request("POST", f"/network/{nwid}", settings)

    def peers(self) -> list:
        """Get peer list."""
        return self._request("GET", "/peer")
