"""Discover devices on joined ZeroTier networks. Blocking, bounded, thread-safe, no GTK."""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

COMMON_PORTS: dict[int, str] = {
    22: "ssh", 80: "http", 443: "https", 445: "smb", 3389: "rdp",
    5900: "vnc", 8080: "http-alt", 8096: "jellyfin", 32400: "plex",
    25565: "minecraft",
}

_DEV_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}$")
_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


MAX_DEVICES = 256
DISCOVER_DEADLINE = 45.0
HTTP_READ_LIMIT = 16 * 1024
TITLE_WINDOW = 4 * 1024
_NICK_FILE_LIMIT = 256 * 1024
# Address space a (possibly hostile) controller may legitimately hand out for sweeping.
_SWEEPABLE_V4 = tuple(ipaddress.ip_network(n) for n in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


def clean_text(s: str | None, limit: int = 64) -> str | None:
    """Sanitise a remote string: drop control/format chars (incl. bidi, zero-width),
    collapse whitespace, truncate to `limit` (with an ellipsis); None if empty."""
    if s is None:
        return None
    s = "".join(" " if c.isspace() else c for c in str(s))
    s = "".join(c for c in s if unicodedata.category(c) not in ("Cc", "Cf"))
    s = " ".join(s.split())
    if not s:
        return None
    if len(s) > limit:
        s = s[:max(0, limit - 1)].rstrip() + "\u2026"
    return s


def _clean_host(name: str | None) -> str | None:
    """Sanitise a remote hostname: each label <= 63 chars, total <= 80."""
    if not name:
        return None
    name = clean_text(name, 4096)
    if not name:
        return None
    name = ".".join(label[:63] for label in name.split("."))
    return clean_text(name, 80)


def _mask(nwid: int) -> int:
    m = ((nwid >> 8) & 0xFF) << 32
    m ^= ((nwid >> 16) & 0xFF) << 24
    m ^= ((nwid >> 24) & 0xFF) << 16
    m ^= ((nwid >> 32) & 0xFF) << 8
    m ^= (nwid >> 40) & 0xFF
    return m


def _first_octet(nwid: int) -> int:
    first = (nwid & 0xFE) | 0x02
    return 0x32 if first == 0x52 else first


def node_to_mac(node: str, nwid: str) -> str:
    """MAC address ZeroTier assigns to `node` on network `nwid`."""
    n, w = int(node, 16), int(nwid, 16)
    m = (_first_octet(w) << 40) | n
    m ^= _mask(w)
    return ":".join(f"{(m >> s) & 0xFF:02x}" for s in range(40, -8, -8))


def mac_to_node(mac: str, nwid: str) -> str | None:
    """Node ID for a ZeroTier MAC on `nwid`, or None if it isn't one for this network."""
    try:
        m = int(mac.replace(":", "").replace("-", ""), 16)
    except ValueError:
        return None
    w = int(nwid, 16)
    if (m >> 40) & 0xFF != _first_octet(w):
        return None
    return f"{((m & 0xFFFFFFFFFF) ^ _mask(w)):010x}"


def rfc4193_address(nwid: str, node: str) -> str:
    """ZeroTier RFC4193 IPv6 address of `node` on `nwid`."""
    raw = b"\xfd" + int(nwid, 16).to_bytes(8, "big") + b"\x99\x93" + int(node, 16).to_bytes(5, "big")
    return str(ipaddress.IPv6Address(raw))


def controller_node(nwid: str) -> str:
    """Controller node ID (first 10 hex chars of the network ID)."""
    return nwid[:10].lower()


def ping(host: str, timeout: float = 1.0) -> float | None:
    """One ICMP echo; latency in ms or None."""
    ipaddress.ip_address(host)
    try:
        r = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", str(max(1, round(timeout))), "--", host],
            capture_output=True, text=True, timeout=max(1, round(timeout)) + 2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    m = _TIME_RE.search(r.stdout[:4096])
    return float(m.group(1)) if m else None


def neighbors(dev: str) -> dict[str, str]:
    """{ip: mac} from the kernel neighbour table for `dev`."""
    if not _DEV_RE.match(dev):
        raise ValueError(f"invalid device name: {dev!r}")
    try:
        r = subprocess.run(["ip", "-j", "neigh", "show", "dev", dev],
                           capture_output=True, text=True, timeout=5)
        out: dict[str, str] = {}
        for e in json.loads(r.stdout or "[]"):
            states = e.get("state") or []
            if isinstance(states, str):
                states = [states]
            if "lladdr" not in e or any(s in ("FAILED", "INCOMPLETE") for s in states):
                continue
            out[e["dst"]] = e["lladdr"].lower()
        return out
    except Exception:
        return {}


def _sweepable(net) -> bool:
    return net.version == 4 and net.is_private and any(net.subnet_of(p) for p in _SWEEPABLE_V4)


def sweep(cidr: str, exclude: set[str] = frozenset(), max_hosts: int = 1024,
          workers: int = 64) -> dict[str, float]:
    """Ping all hosts of an IPv4 network; {ip: latency_ms} for responders."""
    net = ipaddress.ip_interface(cidr).network
    if net.version != 4 or not _sweepable(net) or net.num_addresses - (2 if net.prefixlen < 31 else 0) > max_hosts:
        return {}
    hosts = [str(h) for h in net.hosts() if str(h) not in exclude]
    if not hosts:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as ex:
        results = list(ex.map(ping, hosts))
    return {h: r for h, r in zip(hosts, results) if r is not None}


def probe_ports(ip: str, ports=COMMON_PORTS, timeout: float = 0.6) -> list[int]:
    """Sorted list of ports accepting TCP connections."""
    ports = list(ports)
    if not ports:
        return []

    def check(p: int) -> int | None:
        try:
            with socket.create_connection((ip, p), timeout=timeout):
                return p
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=min(32, len(ports))) as ex:
        return sorted(p for p in ex.map(check, ports) if p is not None)


def netbios_name(ip: str, timeout: float = 1.5) -> str | None:
    """NetBIOS name (NBSTAT over UDP/137) of an IPv4 host, or None."""
    try:
        if ipaddress.ip_address(ip).version != 4:
            return None
        txid = int.from_bytes(os.urandom(2), "big")
        req = (struct.pack(">HHHHHH", txid, 0, 1, 0, 0, 0)
               + b"\x20" + b"CK" + b"A" * 30 + b"\x00" + struct.pack(">HH", 0x21, 1))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.connect((ip, 137))
            s.send(req)
            data = s.recv(1024)
        if len(data) < 57 or struct.unpack(">H", data[:2])[0] != txid:
            return None
        count = data[56]
        for i in range(count):
            off = 57 + i * 18
            if off + 18 > len(data):
                break
            suffix = data[off + 15]
            flags = struct.unpack(">H", data[off + 16:off + 18])[0]
            if suffix in (0x00, 0x20) and not flags & 0x8000:
                name = data[off:off + 15].decode("ascii", "replace").strip()
                name = _clean_host(name)
                if name:
                    return name
    except Exception:
        pass
    return None


def _dns_read_name(data: bytes, off: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name; returns (name, offset after it)."""
    labels: list[str] = []
    end = -1
    jumps = 0
    while True:
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if end < 0:
                end = off + 2
            jumps += 1
            if jumps > 16:
                raise ValueError("too many compression pointers")
            off = ((n & 0x3F) << 8) | data[off + 1]
            continue
        if n & 0xC0:
            raise ValueError("bad label")
        if off + 1 + n > len(data):
            raise ValueError("truncated label")
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if end >= 0 else off)


def _dns_ptr_query(ip: str, server_port: int, timeout: float) -> str | None:
    """Unicast DNS PTR query for `ip` sent straight to ip:server_port; target name or None."""
    try:
        addr = ipaddress.ip_address(ip)
        txid = int.from_bytes(os.urandom(2), "big")
        qname = b"".join(bytes([len(x)]) + x.encode("ascii")
                         for x in addr.reverse_pointer.split(".")) + b"\x00"
        req = struct.pack(">HHHHHH", txid, 0, 1, 0, 0, 0) + qname + struct.pack(">HH", 12, 1)
        fam = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        with socket.socket(fam, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.connect((ip, server_port))
            s.send(req)
            data = s.recv(4096)
        if len(data) < 12:
            return None
        rid, _flags, qd, an = struct.unpack(">HHHH", data[:8])
        if rid != txid:
            return None
        off = 12
        for _ in range(qd):
            _, off = _dns_read_name(data, off)
            off += 4
        for _ in range(an):
            _, off = _dns_read_name(data, off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            if off + rdlen > len(data):
                return None
            if rtype == 12:
                name, _ = _dns_read_name(data, off)
                return _clean_host(name.rstrip("."))
            off += rdlen
    except Exception:
        pass
    return None


def mdns_name(ip: str, timeout: float = 1.0) -> str | None:
    """Hostname via a unicast mDNS PTR query to ip:5353, or None."""
    return _dns_ptr_query(ip, 5353, timeout)


def llmnr_name(ip: str, timeout: float = 1.0) -> str | None:
    """Hostname via an LLMNR PTR query to ip:5355, or None."""
    return _dns_ptr_query(ip, 5355, timeout)


_SSH_DISTROS = ("Debian", "Ubuntu", "Raspbian", "Fedora", "FreeBSD", "NetBSD", "OpenBSD", "CentOS", "Alpine")
_OPENSSH_RE = re.compile(r"OpenSSH[_-](\d+(?:\.\d+)?)", re.I)


def _read_until(sock: socket.socket, deadline: float, limit: int, stop: bytes | None = None) -> bytes:
    """Read up to `limit` bytes until EOF, `stop` (case-insensitive) or the deadline."""
    buf = b""
    while len(buf) < limit:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            chunk = sock.recv(min(4096, limit - len(buf)))
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if stop and stop in buf.lower():
            break
    return buf[:limit]


def _ssh_description(ip: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((ip, 22), timeout=timeout) as s:
            raw = _read_until(s, deadline, 255, b"\n")
    except OSError:
        return None
    line = raw.split(b"\n", 1)[0].decode("ascii", "replace").strip()
    line = clean_text(line, 255) or ""
    if not line.startswith("SSH-"):
        return None
    soft = line.split("-", 2)[2] if line.count("-") >= 2 else line
    low = soft.lower()
    if "rosssh" in low:
        return "MikroTik RouterOS"
    m = _OPENSSH_RE.search(soft)
    if m:
        ver = f"OpenSSH {m.group(1)}"
        distro = next((d for d in _SSH_DISTROS if d.lower() in low), None)
        return f"{distro} \u00b7 {ver}" if distro else ver
    if "dropbear" in low:
        return "Dropbear SSH (embedded Linux)"
    if "cisco" in low:
        return "Cisco"
    return clean_text(soft, 40)


def _http_description(ip: str, port: int, timeout: float) -> list[str] | None:
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(max(0.01, deadline - time.monotonic()))
            s.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: zerotier-gui\r\n"
                      "Accept: */*\r\nConnection: close\r\n\r\n".encode("ascii"))
            raw = _read_until(s, deadline, HTTP_READ_LIMIT, b"</title")
    except OSError:
        return None
    return _parse_http(raw)


def _parse_http(raw: bytes) -> list[str] | None:
    """Title and Server strings from a raw HTTP response (linear time, no regex)."""
    raw = raw[:HTTP_READ_LIMIT]
    if not raw.startswith(b"HTTP/"):
        return None
    end = raw.find(b"\r\n\r\n")
    end2 = raw.find(b"\n\n")
    if end < 0 or 0 <= end2 < end:
        end = end2
    head = raw[:end] if end >= 0 else raw
    body = raw[end:] if end >= 0 else b""
    out: list[str] = []
    low = body.lower()  # bytes: same length as `body`, so offsets stay valid
    a = low.find(b"<title")
    if a >= 0:
        b = low.find(b">", a, a + 1024)
        if b >= 0:
            c = low.find(b"</title", b + 1, b + 1 + TITLE_WINDOW)
            chunk = body[b + 1:c] if c >= 0 else body[b + 1:b + 1 + TITLE_WINDOW]
            title = clean_text(html.unescape(chunk.decode("utf-8", "replace")), 60)
            if title:
                out.append(title)
    for line in head.decode("utf-8", "replace").split("\n"):
        if line.lower().startswith("server:"):
            server = clean_text(line[7:].strip().split("/")[0].split(" ")[0], 40)
            if server:
                out.append(server)
            break
    return out


def identify(ip: str, ports: list[int], timeout: float = 2.0) -> str | None:
    """Short device description from SSH/HTTP banners (only touches `ports`); None if unknown."""
    parts: list[str] = []
    if 22 in ports:
        d = _ssh_description(ip, timeout)
        if d:
            parts.append(d)
    for port in (80, 8080):
        if port in ports:
            r = _http_description(ip, port, timeout)
            if r is not None:
                parts.extend(r)
                break
    out: list[str] = []
    for p in parts:
        p = clean_text(p, 80)
        if not p:
            continue
        pl = p.lower()
        if any(pl in o.lower() or o.lower() in pl for o in out):
            # keep the longer, more specific wording
            for i, o in enumerate(out):
                if pl in o.lower() or o.lower() in pl:
                    if len(p) > len(o):
                        out[i] = p
                    break
            continue
        out.append(p)
    return clean_text(" \u00b7 ".join(out), 160)


def _usable_name(name: str | None, ip: str) -> str | None:
    if not name:
        return None
    name = _clean_host(name.rstrip("."))
    if not name:
        return None
    low = name.lower()
    if low == ip.lower() or low.endswith((".in-addr.arpa", ".ip6.arpa")):
        return None
    return name


def resolve_name(ip: str, timeout: float = 2.0) -> str | None:
    """Reverse DNS, NetBIOS, mDNS, LLMNR (in that priority), then avahi-resolve; None if unknown."""
    ipaddress.ip_address(ip)  # ValueError on anything that isn't an address (also blocks "-x")
    jobs = [
        lambda: socket.gethostbyaddr(ip)[0],
        lambda: netbios_name(ip, min(timeout, 1.5)),
        lambda: mdns_name(ip, min(timeout, 1.0)),
        lambda: llmnr_name(ip, min(timeout, 1.0)),
    ]
    slots: list[list] = [[] for _ in jobs]
    threads = []
    for job, slot in zip(jobs, slots):
        def run(job=job, slot=slot):
            try:
                slot.append(job())
            except Exception:
                pass
        # daemon threads: a hanging gethostbyaddr must not block interpreter exit
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout
    for t, slot in zip(threads, slots):
        t.join(max(0.0, deadline - time.monotonic()))
        if slot:
            name = _usable_name(slot[0], ip)
            if name:
                return name
    if shutil.which("avahi-resolve"):
        try:
            # avahi-resolve documents no "--"; `ip` is validated above instead.
            r = subprocess.run(["avahi-resolve", "-a", ip], capture_output=True,
                               text=True, timeout=min(timeout, 1.5))
            parts = r.stdout[:4096].strip().split("\t")
            if r.returncode == 0 and len(parts) >= 2:
                return _usable_name(parts[1], ip)
        except (OSError, subprocess.SubprocessError):
            pass
    return None


@dataclass
class Device:
    ip: str
    node_id: str | None
    mac: str | None
    latency_ms: float | None
    hostname: str | None = None
    description: str | None = None
    ports: list[int] = field(default_factory=list)
    is_controller: bool = False
    peer_latency_ms: int | None = None
    peer_direct: bool | None = None
    peer_version: str | None = None


def _sort_key(d: Device):
    a = ipaddress.ip_address(d.ip)
    return (a.version, int(a))


def _network_subnets(network: dict) -> list:
    """The network's own subnets, from its assigned addresses."""
    nets = []
    for a in network.get("assignedAddresses", []):
        try:
            n = ipaddress.ip_interface(a).network
        except ValueError:
            continue
        if n not in nets:
            nets.append(n)
    return nets


def _filter_ips(ips, nets) -> list[str]:
    """Keep only addresses inside one of `nets`; drop link-local, multicast, loopback, unspecified."""
    out: list[str] = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if "%" in str(ip) or a.is_link_local or a.is_multicast or a.is_loopback or a.is_unspecified:
            continue
        if any(a.version == n.version and a in n for n in nets):
            out.append(str(a))
    return out


def discover(network: dict, peers: list[dict], probe: bool = True) -> list[Device]:
    """Find other devices on one joined network."""
    t_end = time.monotonic() + DISCOVER_DEADLINE
    nwid = network["nwid"].lower()
    own_ips = {a.split("/")[0] for a in network.get("assignedAddresses", [])}
    own_node = mac_to_node(network["mac"], nwid) if network.get("mac") else None
    peer_by_node = {p["address"].lower(): p for p in peers if "address" in p}
    nets = _network_subnets(network)

    found: dict[str, float | None] = {}
    derived: dict[str, str] = {}  # ip -> node for RFC4193 hits

    for net in nets:
        if net.version == 4 and _sweepable(net):  # never ping public space a controller assigned
            res = sweep(str(net), exclude=own_ips)
            for ip in _filter_ips(res, nets):
                found[ip] = res[ip]

    if own_node and rfc4193_address(nwid, own_node) in own_ips:
        leafs = [p["address"].lower() for p in peers
                 if p.get("role") == "LEAF" and p["address"].lower() != own_node]
        cand = {rfc4193_address(nwid, n): n for n in leafs}
        cand = {ip: cand[ip] for ip in _filter_ips(cand, nets) if ip in cand}
        if cand and time.monotonic() < t_end:
            with ThreadPoolExecutor(max_workers=min(32, len(cand))) as ex:
                res = list(ex.map(ping, cand))
            for ip, r in zip(cand, res):
                if r is not None:
                    found[ip] = r
                    derived[ip] = cand[ip]

    nb = neighbors(network["portDeviceName"]) if network.get("portDeviceName") else {}
    nb = {ip: nb[ip] for ip in _filter_ips(nb, nets) if ip in nb}
    for ip in nb:
        if ip not in own_ips:
            found.setdefault(ip, None)

    # keep at most MAX_DEVICES, in IP order
    keep = sorted(found, key=lambda x: (ipaddress.ip_address(x).version, int(ipaddress.ip_address(x))))
    found = {ip: found[ip] for ip in keep[:MAX_DEVICES]}

    devices: list[Device] = []
    for ip, lat in found.items():
        mac = nb.get(ip)
        node = None
        if ip in derived:
            node = derived[ip]
            mac = mac or node_to_mac(node, nwid)
        elif mac:
            node = mac_to_node(mac, nwid)
        d = Device(ip=ip, node_id=node, mac=mac, latency_ms=lat,
                   is_controller=node == controller_node(nwid) if node else False)
        p = peer_by_node.get(node) if node else None
        if p:
            pl = p.get("latency")
            d.peer_latency_ms = pl if isinstance(pl, int) and pl >= 0 else None
            d.peer_direct = any(x.get("active") for x in p.get("paths", []))
            d.peer_version = clean_text(p.get("version"), 32)
        devices.append(d)

    if probe and devices:
        def work(d: Device):
            if time.monotonic() >= t_end:  # past the deadline: leave unprobed
                return
            with ThreadPoolExecutor(max_workers=1) as inner:
                nf = inner.submit(resolve_name, d.ip)
                d.ports = probe_ports(d.ip)
                try:
                    d.hostname = nf.result()
                except Exception:
                    d.hostname = None
            if time.monotonic() < t_end:
                d.description = identify(d.ip, d.ports)
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(work, devices))

    return sorted(devices, key=_sort_key)


NICKNAMES_FILE: str = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "zerotier-gui", "nicknames.json",
)


def load_nicknames() -> dict[str, str]:
    """Load nicknames; {} if missing or corrupt. Only str->str entries (<= 64 chars) are kept."""
    try:
        with open(NICKNAMES_FILE, "rb") as f:
            raw = f.read(_NICK_FILE_LIMIT + 1)
        if len(raw) > _NICK_FILE_LIMIT:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, str) and v
                    and len(k) <= 64 and len(v) <= 64}
    except (OSError, ValueError, RecursionError, TypeError):
        pass
    return {}


def save_nicknames(names: dict[str, str]) -> None:
    """Atomically write nicknames, dropping empty values."""
    d = os.path.dirname(NICKNAMES_FILE)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        if os.stat(d).st_uid == os.getuid():
            os.chmod(d, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in names.items() if v}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, NICKNAMES_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
