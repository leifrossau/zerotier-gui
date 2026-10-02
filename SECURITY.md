# Security

## Reporting vulnerabilities

Please report security vulnerabilities privately using GitHub's "Report a vulnerability" feature (private security advisories) on the repository. Do not open a public issue.

## Security model

### Privileged helper

The `zt-gui-helper` script is the only code that runs as root. It:

- Requires polkit authentication (one password prompt per invocation)
- Is installed root-owned in `/usr/local/libexec/zerotier-gui` with restricted file permissions
- Performs only three operations: start/stop the `zerotier-one.service`, enable the service, and copy the auth token
- Validates all arguments strictly
- The app refuses to invoke it if the helper or any parent directory is writable by a non-root user
- Writes the token copy as the invoking user (verified via `PKEXEC_UID`), never as root, to prevent symlink attacks

### Local API access

The app verifies that the process listening on the API port runs as root or under the `zerotier-one` service user before sending the auth token. This prevents token theft if another program listens on the port while the service is stopped.

### Untrusted text handling

Text from network controllers, other devices, and remote services is treated as untrusted:

- Control and bidirectional characters are stripped
- Length is capped
- All text is escaped before display
- Banner parsing is bounded in size and time

### Device scanning

Device discovery scans are limited to:

- Private address ranges only (configured networks' own subnets)
- Maximum 256 devices per scan
- Maximum 45 seconds per scan

Scanning pings all devices on the network and probes listening ports; only scan networks where this is acceptable.

### Central API client

The Central API client follows strict security practices:

- HTTPS only (except on loopback for testing)
- No HTTP redirects followed
- API token never included in error messages

## Known and accepted limitations

- `~/.zeroTierOneAuthToken` grants any program running as your user full ZeroTier control, including routing settings. This is inherent to how ZeroTier enables non-root access.
- Any program with access to your session D-Bus can trigger tray menu actions.
- Device identity on the Devices tab (IP to node ID mapping, hostnames) is not cryptographically authenticated. Network members can spoof it.
