"""Mock ZeroTier Central API server for testing."""

import argparse
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Seed data
NETWORKS = {
    '8056c2e21c111111': {
        'id': '8056c2e21c111111',
        'description': '',
        'onlineMemberCount': 1,
        'authorizedMemberCount': 1,
        'totalMemberCount': 1,
        'config': {
            'name': 'home-lab',
            'private': False,
        }
    },
    '8056c2e21c222222': {
        'id': '8056c2e21c222222',
        'description': '',
        'onlineMemberCount': 0,
        'authorizedMemberCount': 0,
        'totalMemberCount': 0,
        'config': {
            'name': 'office',
            'private': True,
        }
    },
    'a09acf0233333333': {
        'id': 'a09acf0233333333',
        'description': '',
        'onlineMemberCount': 0,
        'authorizedMemberCount': 0,
        'totalMemberCount': 0,
        'config': {
            'name': 'game-night',
            'private': False,
        }
    },
}

MEMBERS = {
    '8056c2e21c111111': {
        'a1b2c3d4e5': {
            'networkId': '8056c2e21c111111',
            'nodeId': 'a1b2c3d4e5',
            'name': '',
            'config': {
                'authorized': True,
                'ipAssignments': [],
            }
        }
    },
    '8056c2e21c222222': {},
    'a09acf0233333333': {},
}


class CentralHandler(BaseHTTPRequestHandler):
    """Handler for Central API requests."""

    def do_GET(self):
        """Handle GET requests."""
        if not self._check_auth():
            return

        path = self.path.split('?')[0]

        if path == '/api/v1/network':
            return self._list_networks()

        if path.startswith('/api/v1/network/') and '/member/' in path:
            parts = path.split('/')
            if len(parts) == 7 and parts[5] == 'member':
                nwid = parts[4]
                node_id = parts[6]
                return self._get_member(nwid, node_id)

        self._error(404, "Not found")

    def do_POST(self):
        """Handle POST requests."""
        if not self._check_auth():
            return

        path = self.path.split('?')[0]

        if path.startswith('/api/v1/network/') and '/member/' in path:
            parts = path.split('/')
            if len(parts) == 7 and parts[5] == 'member':
                nwid = parts[4]
                node_id = parts[6]
                return self._set_member(nwid, node_id)

        self._error(404, "Not found")

    def _check_auth(self) -> bool:
        """Check Authorization header."""
        auth = self.headers.get('Authorization', '')
        token = self.server.token

        if auth != f'token {token}':
            self._error(401, "Unauthorized")
            return False

        return True

    def _list_networks(self):
        """GET /api/v1/network"""
        networks = list(NETWORKS.values())
        self._json_response(200, networks)

    def _get_member(self, nwid: str, node_id: str):
        """GET /api/v1/network/<nwid>/member/<node_id>"""
        if nwid not in NETWORKS or node_id not in MEMBERS.get(nwid, {}):
            self._error(404, "Not found")
            return

        member = MEMBERS[nwid][node_id]
        self._json_response(200, member)

    def _set_member(self, nwid: str, node_id: str):
        """POST /api/v1/network/<nwid>/member/<node_id>"""
        if nwid not in NETWORKS:
            self._error(404, "Network not found")
            return

        # Parse request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._error(400, "Invalid JSON")
            return

        # Initialize members dict if needed
        if nwid not in MEMBERS:
            MEMBERS[nwid] = {}

        # Get or create member
        if node_id not in MEMBERS[nwid]:
            MEMBERS[nwid][node_id] = {
                'networkId': nwid,
                'nodeId': node_id,
                'name': '',
                'config': {
                    'authorized': False,
                    'ipAssignments': [],
                }
            }

        member = MEMBERS[nwid][node_id]

        # Update member
        if 'name' in data:
            member['name'] = data['name']

        if 'config' in data:
            if 'authorized' in data['config']:
                member['config']['authorized'] = data['config']['authorized']
            if 'ipAssignments' in data['config']:
                member['config']['ipAssignments'] = data['config']['ipAssignments']

        self._json_response(200, member)

    def _json_response(self, status: int, data):
        """Send JSON response."""
        response = json.dumps(data)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response.encode('utf-8'))

    def _error(self, status: int, message: str):
        """Send error response."""
        response = json.dumps({'error': message})
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response.encode('utf-8'))

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


class CentralServer(ThreadingHTTPServer):
    """Central API server with token."""

    def __init__(self, host, port, token):
        super().__init__((host, port), CentralHandler)
        self.token = token


def main():
    parser = argparse.ArgumentParser(description='Mock ZeroTier Central API')
    parser.add_argument('--port', type=int, default=19995, help='Server port')
    parser.add_argument('--token', default='centraltest', help='API token')
    args = parser.parse_args()

    server = CentralServer('127.0.0.1', args.port, args.token)
    logger.info(f"Mock Central API listening on http://127.0.0.1:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == '__main__':
    main()
