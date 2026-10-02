import urllib.parse
"""Client for ZeroTier Central web API (v1)."""

import json
import os
import socket
import tempfile
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from typing import Optional

# Token file location with proper XDG_CONFIG_HOME handling
_xdg_config_home = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
_config_base = (
    _xdg_config_home
    if os.path.isabs(_xdg_config_home)
    else os.path.expanduser('~/.config')
)
TOKEN_FILE: str = os.path.join(_config_base, 'zerotier-gui', 'central_token')


class _NoRedirectHandler(HTTPRedirectHandler):
    """Custom HTTP redirect handler that blocks all redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Block all redirect requests."""
        return None


class CentralError(Exception):
    """Base exception for Central API errors."""
    pass


class CentralAuthError(CentralError):
    """Authentication error (401/403 or missing token)."""
    pass


class CentralUnavailable(CentralError):
    """Network or connection error."""
    pass


def load_token() -> Optional[str]:
    """Load token from env ZT_CENTRAL_TOKEN or TOKEN_FILE.

    Returns None if not found, empty, or invalid (whitespace/control chars).
    File read is capped at 4096 bytes.
    """
    token = os.environ.get('ZT_CENTRAL_TOKEN')
    if token:
        token = token.strip()
        # Reject if empty or contains whitespace/control characters
        if token and not any(c.isspace() or not c.isprintable() for c in token):
            return token
        return None

    try:
        with open(TOKEN_FILE, 'r') as f:
            token = f.read(4096 + 1)  # Read 4096 + 1 to detect oversized files
            if len(token) > 4096:
                return None
            token = token.strip()
            # Reject if empty or contains whitespace/control characters
            if token and not any(c.isspace() or not c.isprintable() for c in token):
                return token
            return None
    except (OSError, UnicodeDecodeError):
        return None


def save_token(token: str) -> None:
    """Save token to TOKEN_FILE with mode 0o600.

    Raises ValueError if token is empty or contains whitespace/control characters.
    """
    token = token.strip()
    if not token or any(c.isspace() or not c.isprintable() for c in token):
        raise ValueError("Token must not be empty or contain whitespace/control characters")

    parent = os.path.dirname(TOKEN_FILE)
    os.makedirs(parent, mode=0o700, exist_ok=True)

    # chmod parent dir to 0o700 if it already exists and is owned by current user
    try:
        stat_info = os.stat(parent)
        if stat_info.st_uid == os.getuid():
            os.chmod(parent, 0o700)
    except OSError:
        pass

    # Write atomically; track fd ownership after fdopen takes control
    fd, temp_path = tempfile.mkstemp(dir=parent)
    fd_owned = True
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            fd_owned = False  # fdopen took ownership
            f.write(token)
        os.replace(temp_path, TOKEN_FILE)
    except BaseException:
        # Clean up: if fdopen didn't take ownership yet, we must close fd
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def clear_token() -> None:
    """Remove TOKEN_FILE if present."""
    try:
        os.unlink(TOKEN_FILE)
    except FileNotFoundError:
        pass


class CentralClient:
    """Client for ZeroTier Central API."""

    def __init__(self, token: Optional[str] = None, base_url: Optional[str] = None, timeout: float = 10.0):
        """Initialize client.

        Args:
            token: API token (None to load from environment/file)
            base_url: API base URL (must be HTTPS, except localhost/127.0.0.1/[::1] for testing)
            timeout: Request timeout in seconds

        Raises:
            ValueError: base_url is not HTTPS (except for allowed loopback addresses)
        """
        if token is None:
            token = load_token()
        self.token = token
        self.base_url = base_url or os.environ.get('ZT_CENTRAL_URL', 'https://api.zerotier.com/api/v1')
        self.timeout = timeout

        # Validate base_url: HTTPS only, except plain http to a loopback host (local mock server)
        parsed = urllib.parse.urlsplit(self.base_url)
        loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1") and not parsed.username
        if not (parsed.scheme == "https" and parsed.hostname) and not (parsed.scheme == "http" and loopback):
            raise ValueError("base_url must use HTTPS (plain http is only allowed to 127.0.0.1/localhost/[::1])")

        # Build opener with redirect blocking
        self._opener = build_opener(_NoRedirectHandler())

    def _validate_id(self, value: str, length: int) -> str:
        """Validate hex id and return lowercase."""
        if not isinstance(value, str) or len(value) != length:
            raise ValueError(f"Expected {length}-character hex string")
        if not all(c in '0123456789abcdefABCDEF' for c in value):
            raise ValueError("Invalid hex string")
        return value.lower()

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Make HTTP request to API.

        Response body is capped at 2 MiB.
        """
        if not self.token:
            raise CentralAuthError("No API token")

        url = f"{self.base_url}{path}"
        headers = {
            'Authorization': f'token {self.token}',
            'User-Agent': 'zerotier-gui/1.0',
        }

        if body is not None:
            headers['Content-Type'] = 'application/json'

        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')

        req = Request(url, data=data, headers=headers, method=method)

        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                # Check for redirect responses before read
                if 300 <= response.status < 400:
                    raise CentralError("Unexpected redirect (HTTP 30x)")

                # Cap response at 2 MiB
                response_data = response.read(2 * 1024 * 1024 + 1)
                if len(response_data) > 2 * 1024 * 1024:
                    raise CentralError("Response body too large (> 2 MiB)")

                return json.loads(response_data) if response_data else {}
        except HTTPError as e:
            if e.code in (401, 403):
                raise CentralAuthError("Authentication failed")
            if 300 <= e.code < 400:
                raise CentralError("Unexpected redirect (HTTP 30x)")
            try:
                body_snippet = e.read(101).decode('utf-8', errors='replace')[:100]
                # Strip control characters from body snippet
                body_snippet = ''.join(c for c in body_snippet if c.isprintable() or c in '\n\t')
            except Exception:
                body_snippet = ""
            raise CentralError(f"HTTP {e.code}: {body_snippet}") from e
        except HTTPError:
            raise
        except (URLError, socket.timeout, socket.gaierror, ConnectionError, TimeoutError) as e:
            # Map DNS errors and network errors to CentralUnavailable
            exc_class = type(e).__name__
            raise CentralUnavailable(f"Network error ({exc_class})") from e
        except Exception as e:
            # Unexpected exception: don't expose details
            exc_class = type(e).__name__
            raise CentralError(f"Unexpected error ({exc_class})") from e

    def networks(self) -> list:
        """List networks for the account.

        Returns list sorted by name (case-insensitive), then by id.
        Only includes network objects with a valid 16-hex id.
        """
        response = self._request('GET', '/network')

        # Must be a list
        if not isinstance(response, list):
            return []

        networks = []
        for item in response:
            # Skip non-dict items
            if not isinstance(item, dict):
                continue

            # Validate id is a 16-hex string
            item_id = item.get('id')
            if not isinstance(item_id, str) or len(item_id) != 16:
                continue
            if not all(c in '0123456789abcdefABCDEF' for c in item_id):
                continue

            # Coerce config to dict if needed, name to str if needed
            if not isinstance(item.get('config'), dict):
                item['config'] = {}
            if not isinstance(item.get('name'), str):
                item['name'] = ''

            networks.append(item)

        # Sort by config.name (case-insensitive), then by id
        networks.sort(key=lambda n: (
            (n.get('config', {}).get('name', '') or '').lower(),
            n.get('id', '')
        ))
        return networks

    def member(self, nwid: str, node_id: str) -> Optional[dict]:
        """Get a network member.

        Args:
            nwid: Network ID (16 hex characters)
            node_id: Node ID (10 hex characters)

        Returns:
            Member dict (or None if response is not a dict), or None if not found (404).

        Raises:
            ValueError: Invalid ID format
            CentralAuthError: Authentication failed
            CentralError: Other HTTP errors
            CentralUnavailable: Network error
        """
        nwid = self._validate_id(nwid, 16)
        node_id = self._validate_id(node_id, 10)

        if not self.token:
            raise CentralAuthError("No API token")

        url = f"{self.base_url}/network/{nwid}/member/{node_id}"
        headers = {
            'Authorization': f'token {self.token}',
            'User-Agent': 'zerotier-gui/1.0',
        }

        req = Request(url, headers=headers, method='GET')

        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                # Check for redirect responses before read
                if 300 <= response.status < 400:
                    raise CentralError("Unexpected redirect (HTTP 30x)")

                # Cap response at 2 MiB
                response_data = response.read(2 * 1024 * 1024 + 1)
                if len(response_data) > 2 * 1024 * 1024:
                    raise CentralError("Response body too large (> 2 MiB)")

                if response_data:
                    result = json.loads(response_data)
                    # Return None unless response is a dict
                    return result if isinstance(result, dict) else None
                return None
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (401, 403):
                raise CentralAuthError("Authentication failed")
            if 300 <= e.code < 400:
                raise CentralError("Unexpected redirect (HTTP 30x)")
            try:
                body_snippet = e.read(101).decode('utf-8', errors='replace')[:100]
                # Strip control characters from body snippet
                body_snippet = ''.join(c for c in body_snippet if c.isprintable() or c in '\n\t')
            except Exception:
                body_snippet = ""
            raise CentralError(f"HTTP {e.code}: {body_snippet}") from e
        except HTTPError:
            raise
        except (URLError, socket.timeout, socket.gaierror, ConnectionError, TimeoutError) as e:
            # Map DNS errors and network errors to CentralUnavailable
            exc_class = type(e).__name__
            raise CentralUnavailable(f"Network error ({exc_class})") from e
        except Exception as e:
            # Unexpected exception: don't expose details
            exc_class = type(e).__name__
            raise CentralError(f"Unexpected error ({exc_class})") from e

    def authorize_member(self, nwid: str, node_id: str, name: Optional[str] = None) -> dict:
        """Create or authorize a network member.

        Args:
            nwid: Network ID (16 hex characters)
            node_id: Node ID (10 hex characters)
            name: Member name (optional)

        Returns:
            Member dict with config.authorized set to True.

        Raises:
            ValueError: Invalid ID format
            CentralAuthError: Authentication failed
            CentralError: Other HTTP errors
            CentralUnavailable: Network error
        """
        nwid = self._validate_id(nwid, 16)
        node_id = self._validate_id(node_id, 10)

        body = {'config': {'authorized': True}}
        if name is not None:
            body['name'] = name

        return self._request('POST', f'/network/{nwid}/member/{node_id}', body)
