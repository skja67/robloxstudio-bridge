"""
Studio Bridge
-------------
A small host/join tool for running peer-to-peer Roblox Studio test sessions.

Connection methods, tried in this order:
  1. Direct (UPnP)   - the host's own router opens a port automatically.
  2. playit.gg       - fallback used only when UPnP isn't available
                       (CGNAT, locked-down router, etc). You run the free
                       playit.gg agent yourself and point it at the port
                       this app shows you; this app does not talk to any
                       relay server it doesn't control.

made by skja67 - https://github.com/skja67
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox, filedialog
import socket
import threading
import time
import subprocess
import uuid
import os
import atexit
import json
import secrets
import re
import webbrowser
import urllib.request
import xml.etree.ElementTree as ET

# ==================== IDENTITY ====================
APP_NAME = "Studio Bridge"
CREDIT_NAME = "skja67"
CREDIT_URL = "https://github.com/skja67"
PLAYIT_URL = "https://playit.gg"

# ==================== CONSTANTS ====================
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio_bridge_data.json")

PROXY_PORT = 55555          # local port the JOINER binds
HOST_RELAY_PORT = 55556     # fixed port the HOST relay binds -- forward this
                             # via UPnP, or point a playit.gg tunnel here
LEASE_SECONDS = 3600
TOKEN_LEN = 8                # hex chars

WIN_W, WIN_H = 760, 600


# ==================== JOIN TOKEN ====================
def generate_join_token() -> str:
    """Short-lived per-session secret. Only someone with this code gets
    forwarded through the host relay -- see start_host_relay()."""
    return secrets.token_hex(TOKEN_LEN // 2).upper()


# ==================== UPnP (IGD) ====================
def _local_tag(elem) -> str:
    return elem.tag.split("}")[-1]


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def upnp_discover(timeout: float = 3.0):
    """SSDP discovery for an IGD (Internet Gateway Device) on the LAN.
    Returns {"control_url", "service_type"} or None if nothing responds --
    expected on networks with UPnP disabled, CGNAT, or locked-down routers."""
    search_targets = [
        "urn:schemas-upnp-org:service:WANIPConnection:1",
        "urn:schemas-upnp-org:service:WANPPPConnection:1",
    ]
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: {st}\r\n\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    location = None
    try:
        for st in search_targets:
            try:
                sock.sendto(msg.format(st=st).encode(), ("239.255.255.250", 1900))
            except OSError:
                continue
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode(errors="ignore")
            m = re.search(r"LOCATION:\s*(.+)", text, re.IGNORECASE)
            if m:
                location = m.group(1).strip()
                break
    finally:
        sock.close()

    if not location:
        return None

    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
    except Exception:
        return None

    m = re.match(r"(https?://[^/]+)", location)
    base_url = m.group(1) if m else location

    for service in root.iter():
        if _local_tag(service) != "service":
            continue
        service_type, control_url = "", ""
        for child in service:
            lt = _local_tag(child)
            if lt == "serviceType":
                service_type = (child.text or "").strip()
            elif lt == "controlURL":
                control_url = (child.text or "").strip()
        if service_type in search_targets and control_url:
            full_url = control_url if control_url.startswith("http") else \
                base_url + (control_url if control_url.startswith("/") else "/" + control_url)
            return {"control_url": full_url, "service_type": service_type}
    return None


def _upnp_soap(control_url: str, service_type: str, action: str, params: dict = None, timeout: float = 4.0) -> bytes:
    params = params or {}
    args_xml = "".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">{args_xml}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    req = urllib.request.Request(
        control_url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def upnp_get_external_ip(gateway: dict):
    try:
        raw = _upnp_soap(gateway["control_url"], gateway["service_type"], "GetExternalIPAddress")
        m = re.search(rb"<NewExternalIPAddress>([^<]+)</NewExternalIPAddress>", raw)
        return m.group(1).decode() if m else None
    except Exception:
        return None


def upnp_add_port_mapping(gateway: dict, internal_port: int, external_port: int,
                           description: str = APP_NAME, protocol: str = "UDP",
                           lease: int = LEASE_SECONDS, internal_client: str = None):
    internal_client = internal_client or _get_local_ip()
    params = {
        "NewRemoteHost": "",
        "NewExternalPort": external_port,
        "NewProtocol": protocol,
        "NewInternalPort": internal_port,
        "NewInternalClient": internal_client,
        "NewEnabled": "1",
        "NewPortMappingDescription": description,
        "NewLeaseDuration": lease,
    }
    _upnp_soap(gateway["control_url"], gateway["service_type"], "AddPortMapping", params)


def upnp_delete_port_mapping(gateway: dict, external_port: int, protocol: str = "UDP"):
    try:
        _upnp_soap(gateway["control_url"], gateway["service_type"], "DeletePortMapping",
                   {"NewRemoteHost": "", "NewExternalPort": external_port, "NewProtocol": protocol})
    except Exception:
        pass


def try_upnp_direct_tunnel(local_port: int, log_fn):
    """Attempts the full automatic path: find the router, ask it to forward
    HOST_RELAY_PORT to us, and read back our public IP. Returns
    (external_ip, external_port) on success, or (None, None) if UPnP isn't
    available on this network -- the caller should fall back to playit.gg."""
    log_fn("Looking for a UPnP-capable router...")
    gateway = upnp_discover()
    if not gateway:
        log_fn("No UPnP router found.")
        return None, None
    try:
        upnp_add_port_mapping(gateway, local_port, local_port)
        ext_ip = upnp_get_external_ip(gateway)
        if not ext_ip:
            log_fn("Router did not report a public IP.")
            return None, None
        log_fn(f"UPnP mapping created: {ext_ip}:{local_port}")
        return ext_ip, local_port
    except Exception as e:
        log_fn(f"UPnP mapping failed: {e}")
        return None, None


# ==================== PERSISTENCE ====================
def load_data() -> dict:
    defaults = {
        "user_id": "0",
        "join_address": "",
        "join_token": "",
        "server_port": str(PROXY_PORT),
    }
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            return {**defaults, **stored}
    except Exception:
        pass
    return defaults


def save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ==================== GLOBAL PROXY STATE ====================
_proxy_running = threading.Event()
_proxy_stopped = threading.Event()
_udp_sockets = []
_proxy_lock = threading.Lock()
_proxy_thread = None
_active_port = None


# ==================== HELPERS ====================
def get_studio_path() -> str:
    try:
        cmd = (
            'powershell -Command "'
            'Get-ChildItem -Path $env:LOCALAPPDATA\\Roblox\\Versions '
            '-Filter RobloxStudioBeta.exe -Recurse | '
            'Select-Object -First 1 -ExpandProperty FullName"'
        )
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        result = subprocess.check_output(cmd, shell=True, creationflags=flags).decode(errors="ignore").strip()
        return result
    except Exception:
        return ""


def generate_guid() -> str:
    return str(uuid.uuid4()).upper()


def find_free_port(start_port: int, max_attempts: int = 100) -> int:
    for port in range(start_port, start_port + max_attempts):
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            test_sock.bind(("127.0.0.1", port))
            test_sock.close()
            return port
        except OSError:
            continue
    return -1


# ==================== UDP PROXY (JOIN SIDE) ====================
def warmup_udp_tunnel(dst_host: str, dst_port: int, log_fn, packets: int = 10,
                       delay: float = 0.05, join_token: str = None):
    """Sends a handful of throwaway UDP packets so a stateful NAT / relay on
    the remote side learns our return path before Studio sends real data."""
    log_fn(f"Warming up tunnel to {dst_host}:{dst_port}...")
    try:
        dst_ip = socket.gethostbyname(dst_host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        prefix = join_token.encode() if join_token else b""
        payload = prefix + b"\x00" * 64
        for _ in range(packets):
            try:
                sock.sendto(payload, (dst_ip, dst_port))
            except OSError:
                pass
            time.sleep(delay)
        sock.close()
        log_fn("Warm-up done.")
    except Exception as e:
        log_fn(f"Warm-up warning (non-fatal): {e}")


def start_udp_proxy(dst_host: str, dst_port: int, log_fn, join_token: str = None) -> tuple:
    """Runs on the JOINING machine. Bridges 127.0.0.1:<bound_port> to the
    host's address. If join_token is set, it's prefixed onto every outbound
    packet so the host's relay can verify it belongs to this session."""
    global _proxy_thread, _active_port
    if _proxy_running.is_set():
        stop_udp_proxy()
    token_prefix = join_token.encode() if join_token else b""

    _proxy_running.set()
    _proxy_stopped.clear()

    ready_event = threading.Event()
    error_box = [None]
    port_box = [None]

    def worker():
        client_sessions = {}
        local_sock = None
        try:
            dst_ip = socket.gethostbyname(dst_host)
            log_fn(f"Resolved {dst_host} -> {dst_ip}")
            bound_port = find_free_port(PROXY_PORT)
            if bound_port == -1:
                raise OSError(f"No free ports starting from {PROXY_PORT}")
            port_box[0] = bound_port
            if bound_port != PROXY_PORT:
                log_fn(f"Port {PROXY_PORT} busy, using {bound_port}")

            local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            local_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            local_sock.bind(("127.0.0.1", bound_port))
            local_sock.settimeout(0.3)
            with _proxy_lock:
                _udp_sockets.append(local_sock)
            log_fn(f"Local proxy bound on 127.0.0.1:{bound_port}")
            ready_event.set()

            while _proxy_running.is_set():
                try:
                    data, addr = local_sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if addr not in client_sessions:
                    log_fn(f"New session: {addr[0]}:{addr[1]}")
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(0.3)
                    client_sessions[addr] = s

                    def _listen(sock, client_addr, _ls=local_sock):
                        while _proxy_running.is_set() and client_addr in client_sessions:
                            try:
                                resp, _ = sock.recvfrom(65535)
                                payload = resp[len(token_prefix):] if token_prefix and resp[:len(token_prefix)] == token_prefix else resp
                                _ls.sendto(payload, client_addr)
                            except socket.timeout:
                                continue
                            except OSError:
                                break
                        try:
                            sock.close()
                        except Exception:
                            pass
                        client_sessions.pop(client_addr, None)

                    threading.Thread(target=_listen, args=(s, addr), daemon=True).start()

                try:
                    client_sessions[addr].sendto(token_prefix + data, (dst_ip, dst_port))
                except OSError as e:
                    log_fn(f"Send error: {e}")

        except OSError as e:
            error_box[0] = str(e)
            ready_event.set()
        except Exception as e:
            error_box[0] = str(e)
            ready_event.set()
        finally:
            for s in list(client_sessions.values()):
                try:
                    s.close()
                except Exception:
                    pass
            if local_sock:
                with _proxy_lock:
                    try:
                        _udp_sockets.remove(local_sock)
                    except ValueError:
                        pass
                try:
                    local_sock.close()
                except Exception:
                    pass
            log_fn("Local proxy stopped.")
            _proxy_stopped.set()

    _proxy_thread = threading.Thread(target=worker, daemon=True)
    _proxy_thread.start()
    ready_event.wait(timeout=5)

    if error_box[0]:
        log_fn(f"Proxy error: {error_box[0]}")
        _proxy_running.clear()
        return False, -1

    _active_port = port_box[0]
    warmup_udp_tunnel(dst_host, dst_port, log_fn, join_token=join_token)
    return True, port_box[0]


def stop_udp_proxy(wait: bool = True):
    global _proxy_thread, _active_port
    _proxy_running.clear()
    if _proxy_thread and _proxy_thread.is_alive():
        _proxy_stopped.wait(timeout=1.0)
    with _proxy_lock:
        for s in _udp_sockets:
            try:
                s.close()
            except Exception:
                pass
        _udp_sockets.clear()
    if wait and _proxy_thread and _proxy_thread.is_alive():
        _proxy_stopped.wait(timeout=3)
    _proxy_thread = None
    _active_port = None


atexit.register(lambda: stop_udp_proxy(wait=False))


# ==================== HOST RELAY (token-gated) ====================
def start_host_relay(local_target_port: int, token: str, log_fn, bind_port: int = None) -> tuple:
    """Runs on the HOST machine. Binds a UDP socket on 0.0.0.0:bind_port --
    this is the socket a UPnP mapping or a playit.gg tunnel forwards public
    traffic to -- and forwards a packet on to the local Studio server ONLY
    if it starts with the correct session token. Anything else is dropped
    silently, which keeps an open port from being usable as a scanning
    target or reflection point."""
    global _proxy_thread, _active_port
    if _proxy_running.is_set():
        stop_udp_proxy()

    _proxy_running.set()
    _proxy_stopped.clear()
    token_bytes = token.encode()

    ready_event = threading.Event()
    error_box = [None]
    port_box = [None]

    def worker():
        client_sessions = {}
        public_sock = None
        try:
            bound_port = bind_port or find_free_port(HOST_RELAY_PORT)
            if bound_port == -1:
                raise OSError("No free ports for host relay")
            port_box[0] = bound_port

            public_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            public_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            public_sock.bind(("0.0.0.0", bound_port))
            public_sock.settimeout(0.3)
            with _proxy_lock:
                _udp_sockets.append(public_sock)
            log_fn(f"Host relay listening on 0.0.0.0:{bound_port} (token-gated)")
            ready_event.set()

            while _proxy_running.is_set():
                try:
                    data, addr = public_sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break

                if len(data) < len(token_bytes) or data[:len(token_bytes)] != token_bytes:
                    continue
                payload = data[len(token_bytes):]

                if addr not in client_sessions:
                    log_fn(f"Validated join from {addr[0]}:{addr[1]}")
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(0.3)
                    client_sessions[addr] = s

                    def _listen(sock, client_addr, _ps=public_sock):
                        while _proxy_running.is_set() and client_addr in client_sessions:
                            try:
                                resp, _ = sock.recvfrom(65535)
                                _ps.sendto(token_bytes + resp, client_addr)
                            except socket.timeout:
                                continue
                            except OSError:
                                break
                        try:
                            sock.close()
                        except Exception:
                            pass
                        client_sessions.pop(client_addr, None)

                    threading.Thread(target=_listen, args=(s, addr), daemon=True).start()

                try:
                    client_sessions[addr].sendto(payload, ("127.0.0.1", local_target_port))
                except OSError as e:
                    log_fn(f"Relay send error: {e}")

        except OSError as e:
            error_box[0] = str(e)
            ready_event.set()
        except Exception as e:
            error_box[0] = str(e)
            ready_event.set()
        finally:
            for s in list(client_sessions.values()):
                try:
                    s.close()
                except Exception:
                    pass
            client_sessions.clear()
            if public_sock:
                with _proxy_lock:
                    try:
                        _udp_sockets.remove(public_sock)
                    except ValueError:
                        pass
                try:
                    public_sock.close()
                except Exception:
                    pass
            log_fn("Host relay stopped.")
            _proxy_stopped.set()

    _proxy_thread = threading.Thread(target=worker, daemon=True)
    _proxy_thread.start()
    ready_event.wait(timeout=5)

    if error_box[0]:
        log_fn(f"Host relay error: {error_box[0]}")
        _proxy_running.clear()
        return False, -1

    _active_port = port_box[0]
    return True, port_box[0]


# ==================== playit.gg helper ====================
def open_playit_site():
    """playit.gg support is a guided, manual fallback: this app has no
    account/API integration with playit.gg. Point their agent at the port
    this app shows you and paste back the address it gives you."""
    try:
        webbrowser.open(PLAYIT_URL)
    except Exception:
        pass


# ==================== STUDIO LAUNCHER ====================
def open_map_in_studio(studio: str, map_file: str):
    if not map_file:
        return
    subprocess.Popen([studio, map_file])


def launch_server(studio, port, user_id, parent_guid, play_guid):
    subprocess.Popen([
        studio,
        "-task", "StartServer",
        "-placeId", "0", "-universeId", "0", "-placeVersion", "0",
        "-port", port, "-creatorId", user_id, "-creatorType", "0",
        "-userid", user_id,
        "-numTestServerPlayersUponStartup", "0",
        "-parentSessionGuid", parent_guid,
        "-playTestSessionGuid", play_guid,
        "-instanceId", "StudioServer",
    ])


def launch_client(studio, server_ip, server_port, parent_guid, play_guid, instance_id):
    subprocess.Popen([
        studio,
        "-task", "StartClient",
        "-placeId", "0", "-universeId", "0", "-placeVersion", "0",
        "-server", server_ip, "-port", server_port,
        "-parentSessionGuid", parent_guid,
        "-playTestSessionGuid", play_guid,
        "-instanceId", instance_id,
    ])


# ==================== UI THEME (dark / minimal / grey) ====================
BG_COLOR = "#121214"
PANEL_COLOR = "#18181b"
CARD_COLOR = "#1e1e22"
BORDER_COLOR = "#2b2b30"

TEXT_COLOR = "#e9e9ec"
MUTED_COLOR = "#9a9aa0"
DIM_COLOR = "#5c5c62"

BTN_PRIMARY = "#3a3a40"
BTN_PRIMARY_HOVER = "#48484f"
BTN_DANGER = "#5a2a2a"
BTN_DANGER_HOVER = "#6e3434"
BTN_FLAT = "#232327"
BTN_FLAT_HOVER = "#2c2c31"

ACCENT_GREEN = "#5fbf7a"
ACCENT_RED = "#d9645f"
ACCENT_AMBER = "#c9a34e"

FONT_TITLE = ("Segoe UI", 20, "bold")
FONT_SUB = ("Segoe UI", 10)
FONT_LABEL = ("Segoe UI", 9, "bold")
FONT_BODY = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 8)
FONT_LOG = ("Consolas", 9)
FONT_BTN = ("Segoe UI", 10, "bold")


class Button(tk.Frame):
    """Flat, borderless button. Only feedback is an instant colour swap on
    hover/press -- no motion, no tweening."""

    def __init__(self, parent, text, command, base=BTN_PRIMARY, hover=BTN_PRIMARY_HOVER,
                 width=160, height=38, fg=TEXT_COLOR):
        super().__init__(parent, width=width, height=height, bg=base, highlightthickness=0)
        self.pack_propagate(False)
        self.base, self.hover = base, hover
        self.label = tk.Label(self, text=text, font=FONT_BTN, bg=base, fg=fg, cursor="hand2")
        self.label.pack(expand=True, fill="both")
        for widget in (self, self.label):
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
            widget.bind("<Button-1>", lambda e: command())

    def _on_enter(self, e):
        self.config(bg=self.hover)
        self.label.config(bg=self.hover)

    def _on_leave(self, e):
        self.config(bg=self.base)
        self.label.config(bg=self.base)


class Entry(tk.Entry):
    def __init__(self, parent, default=""):
        super().__init__(parent, font=FONT_BODY, bg=CARD_COLOR, fg=TEXT_COLOR,
                          insertbackground=TEXT_COLOR, relief="flat",
                          highlightthickness=1, highlightbackground=BORDER_COLOR,
                          highlightcolor=DIM_COLOR)
        if default:
            self.insert(0, default)


def make_log(parent, height=12) -> scrolledtext.ScrolledText:
    box = scrolledtext.ScrolledText(parent, height=height, font=FONT_LOG,
                                     bg="#0e0e10", fg="#c7c7cc", insertbackground="#c7c7cc",
                                     relief="flat", wrap="word", state="disabled")
    box.pack(fill="both", expand=True)
    return box


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.configure(bg=BG_COLOR)
        self.resizable(False, False)

        self._data = load_data()
        self.studio_path = get_studio_path()

        self.container = tk.Frame(self, bg=BG_COLOR)
        self.container.pack(fill="both", expand=True)

        self._show_welcome()

    # ---------- shared chrome ----------
    def _clear(self):
        for w in self.container.winfo_children():
            w.destroy()

    def _make_header(self, parent, title, subtitle):
        head = tk.Frame(parent, bg=BG_COLOR)
        head.pack(fill="x", padx=40, pady=(30, 10))
        tk.Label(head, text=title, font=FONT_TITLE, bg=BG_COLOR, fg=TEXT_COLOR).pack(anchor="w")
        tk.Label(head, text=subtitle, font=FONT_SUB, bg=BG_COLOR, fg=MUTED_COLOR).pack(anchor="w", pady=(2, 0))
        tk.Frame(parent, bg=BORDER_COLOR, height=1).pack(fill="x", padx=40, pady=(10, 0))

    def _make_footer(self, parent):
        foot = tk.Frame(parent, bg=BG_COLOR)
        foot.pack(side="bottom", fill="x", pady=(0, 10))
        tk.Label(foot, text=f"made by {CREDIT_NAME}", font=FONT_SMALL,
                 bg=BG_COLOR, fg=DIM_COLOR).pack()

    def _write_log(self, box, msg):
        def do():
            box.config(state="normal")
            box.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
            box.see("end")
            box.config(state="disabled")
        self.after(0, do)

    # ---------- welcome ----------
    def _show_welcome(self):
        self._clear()
        f = tk.Frame(self.container, bg=BG_COLOR)
        f.pack(fill="both", expand=True)

        mid = tk.Frame(f, bg=BG_COLOR)
        mid.pack(expand=True)

        tk.Label(mid, text=APP_NAME, font=("Segoe UI", 26, "bold"),
                 bg=BG_COLOR, fg=TEXT_COLOR).pack(pady=(0, 6))
        tk.Label(mid, text="Peer-to-peer Roblox Studio test sessions",
                 font=FONT_SUB, bg=BG_COLOR, fg=MUTED_COLOR).pack(pady=(0, 30))

        Button(mid, "HOST SESSION", self._show_host_setup,
               base=BTN_PRIMARY, hover=BTN_PRIMARY_HOVER, width=220, height=46).pack(pady=6)
        Button(mid, "JOIN SESSION", self._show_join_setup,
               base=BTN_FLAT, hover=BTN_FLAT_HOVER, width=220, height=46).pack(pady=6)

        if not self.studio_path:
            tk.Label(mid, text="Roblox Studio was not found automatically -- you can browse to it next.",
                     font=FONT_SMALL, bg=BG_COLOR, fg=DIM_COLOR).pack(pady=(20, 0))

        self._make_footer(f)

    # ---------- host setup ----------
    def _show_host_setup(self):
        self._clear()
        f = tk.Frame(self.container, bg=BG_COLOR)
        f.pack(fill="both", expand=True)
        self._make_header(f, "Host a session", "Set up a local server for others to join")

        body = tk.Frame(f, bg=BG_COLOR)
        body.pack(fill="both", expand=True, padx=40, pady=20)

        card = tk.Frame(body, bg=CARD_COLOR, highlightthickness=1, highlightbackground=BORDER_COLOR)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD_COLOR, padx=24, pady=20)
        inner.pack(fill="x")

        tk.Label(inner, text="ROBLOX STUDIO PATH", font=FONT_LABEL, bg=CARD_COLOR, fg=MUTED_COLOR).pack(anchor="w")
        path_row = tk.Frame(inner, bg=CARD_COLOR)
        path_row.pack(fill="x", pady=(5, 15))
        ent_path = Entry(path_row, self.studio_path)
        ent_path.pack(side="left", fill="x", expand=True)

        def browse():
            p = filedialog.askopenfilename(title="Locate RobloxStudioBeta.exe",
                                            filetypes=[("Executable", "*.exe")])
            if p:
                ent_path.delete(0, "end")
                ent_path.insert(0, p)

        Button(path_row, "BROWSE", browse, base=BTN_FLAT, hover=BTN_FLAT_HOVER,
               width=90, height=32).pack(side="left", padx=(10, 0))

        tk.Label(inner, text="YOUR USER ID", font=FONT_LABEL, bg=CARD_COLOR, fg=MUTED_COLOR).pack(anchor="w")
        ent_uid = Entry(inner, self._data.get("user_id", "0"))
        ent_uid.pack(fill="x", pady=(5, 15))

        tk.Label(inner, text="OPTIONAL MAP FILE (.rblx)", font=FONT_LABEL, bg=CARD_COLOR, fg=MUTED_COLOR).pack(anchor="w")
        map_row = tk.Frame(inner, bg=CARD_COLOR)
        map_row.pack(fill="x", pady=(5, 0))
        ent_map = Entry(map_row, "")
        ent_map.pack(side="left", fill="x", expand=True)

        def browse_map():
            p = filedialog.askopenfilename(title="Choose a place file", filetypes=[("Roblox place", "*.rblx")])
            if p:
                ent_map.delete(0, "end")
                ent_map.insert(0, p)

        Button(map_row, "BROWSE", browse_map, base=BTN_FLAT, hover=BTN_FLAT_HOVER,
               width=90, height=32).pack(side="left", padx=(10, 0))

        buttons = tk.Frame(body, bg=BG_COLOR)
        buttons.pack(pady=24)

        def start():
            studio = ent_path.get().strip()
            uid = ent_uid.get().strip() or "0"
            mapf = ent_map.get().strip()
            if not studio or not os.path.exists(studio):
                messagebox.showerror("Studio not found", "Please browse to RobloxStudioBeta.exe.")
                return
            self.studio_path = studio
            self._data["user_id"] = uid
            save_data(self._data)
            self._show_host_running(studio, uid, mapf)

        Button(buttons, "START HOSTING", start, width=180, height=42).pack(side="left", padx=6)
        Button(buttons, "BACK", self._show_welcome, base=BTN_FLAT, hover=BTN_FLAT_HOVER,
               width=100, height=42).pack(side="left", padx=6)

        self._make_footer(f)

    # ---------- host running ----------
    def _show_host_running(self, studio, user_id, map_file):
        self._clear()
        f = tk.Frame(self.container, bg=BG_COLOR)
        f.pack(fill="both", expand=True)
        self._make_header(f, "Session console", "Your hosted server")

        status_var = tk.StringVar(value="STARTING")
        status = tk.Label(f, textvariable=status_var, font=("Segoe UI", 11, "bold"),
                           bg=BG_COLOR, fg=MUTED_COLOR)
        status.pack(pady=(12, 4))

        info_var = tk.StringVar(value="")
        tk.Label(f, textvariable=info_var, font=FONT_BODY, bg=BG_COLOR, fg=TEXT_COLOR,
                 justify="left").pack(pady=(0, 10))

        # playit.gg fallback panel -- hidden unless UPnP fails
        fallback = tk.Frame(f, bg=CARD_COLOR, highlightthickness=1, highlightbackground=BORDER_COLOR)
        fb_inner = tk.Frame(fallback, bg=CARD_COLOR, padx=20, pady=16)
        fb_inner.pack(fill="x")
        tk.Label(fb_inner, text="UPnP wasn't available -- use playit.gg instead",
                 font=FONT_LABEL, bg=CARD_COLOR, fg=ACCENT_AMBER).pack(anchor="w")
        tk.Label(fb_inner,
                 text=(f"1. Install the free playit.gg agent and sign in.\n"
                       f"2. Create a UDP tunnel that forwards to 127.0.0.1:{HOST_RELAY_PORT}.\n"
                       f"3. Paste the address playit.gg gives you below."),
                 font=FONT_SMALL, bg=CARD_COLOR, fg=MUTED_COLOR, justify="left").pack(anchor="w", pady=(4, 10))
        pg_row = tk.Frame(fb_inner, bg=CARD_COLOR)
        pg_row.pack(fill="x")
        ent_playit = Entry(pg_row, "")
        ent_playit.pack(side="left", fill="x", expand=True)
        Button(pg_row, "OPEN PLAYIT.GG", open_playit_site, base=BTN_FLAT, hover=BTN_FLAT_HOVER,
               width=140, height=32).pack(side="left", padx=(8, 0))

        code_var = tk.StringVar(value="")
        addr_var = tk.StringVar(value="")

        def use_playit_address():
            addr = ent_playit.get().strip()
            if not addr:
                messagebox.showerror("No address", "Paste the address playit.gg gave you first.")
                return
            addr_var.set(addr)
            info_var.set(f"Join address: {addr}\nJoin code: {code_var.get()}")
            status_var.set("LIVE  •  via playit.gg")
            status.config(fg=ACCENT_GREEN)

        Button(fb_inner, "USE THIS ADDRESS", use_playit_address, width=160, height=32).pack(anchor="e", pady=(10, 0))

        log_area = make_log(f, height=11)

        buttons = tk.Frame(f, bg=BG_COLOR)
        buttons.pack(pady=10)

        def copy_join_info():
            self.clipboard_clear()
            self.clipboard_append(f"{addr_var.get()} {code_var.get()}")

        def stop_hosting():
            stop_udp_proxy(wait=True)
            self._show_welcome()

        Button(buttons, "COPY JOIN INFO", copy_join_info, base=BTN_FLAT, hover=BTN_FLAT_HOVER,
               width=160, height=36).pack(side="left", padx=5)
        Button(buttons, "STOP HOSTING", stop_hosting, base=BTN_DANGER, hover=BTN_DANGER_HOVER,
               width=150, height=36).pack(side="left", padx=5)

        def run():
            log = lambda msg: self._write_log(log_area, msg)
            port = str(HOST_RELAY_PORT - 1)  # local Studio server port
            p_guid, t_guid = generate_guid(), generate_guid()
            token = generate_join_token()
            self.after(0, lambda: code_var.set(token))

            log(f"Studio : {studio}")
            if map_file:
                log(f"Map    : {map_file}")
                try:
                    open_map_in_studio(studio, map_file)
                    time.sleep(2)
                except Exception as e:
                    log(f"Map open warning: {e}")

            log("Launching server process...")
            try:
                launch_server(studio, port, user_id, p_guid, t_guid)
                log("Server started. Waiting for it to initialize...")
                time.sleep(4)

                log(f"Starting host relay on 0.0.0.0:{HOST_RELAY_PORT}...")
                ok, _ = start_host_relay(int(port), token, log, bind_port=HOST_RELAY_PORT)
                if not ok:
                    log("Could not start the host relay.")
                    self.after(0, lambda: [status_var.set("FAILED"), status.config(fg=ACCENT_RED)])
                    return

                ext_ip, ext_port = try_upnp_direct_tunnel(HOST_RELAY_PORT, log)
                if ext_ip:
                    self.after(0, lambda: [
                        addr_var.set(f"{ext_ip}:{ext_port}"),
                        info_var.set(f"Join address: {ext_ip}:{ext_port}\nJoin code: {token}"),
                        status_var.set("LIVE  •  direct (UPnP)"),
                        status.config(fg=ACCENT_GREEN),
                    ])
                    log("Share the join address and join code with your player.")
                else:
                    log("Falling back to playit.gg -- see the panel below.")
                    self.after(0, lambda: [
                        fallback.pack(fill="x", padx=40, pady=(0, 10)),
                        status_var.set("WAITING  •  set up playit.gg"),
                        status.config(fg=ACCENT_AMBER),
                    ])
            except Exception as e:
                log(f"ERROR: {e}")
                self.after(0, lambda: [status_var.set("FAILED"), status.config(fg=ACCENT_RED)])

        threading.Thread(target=run, daemon=True).start()
        self._make_footer(f)

    # ---------- join setup ----------
    def _show_join_setup(self):
        self._clear()
        f = tk.Frame(self.container, bg=BG_COLOR)
        f.pack(fill="both", expand=True)
        self._make_header(f, "Join a session", "Connect to a hosted server")

        body = tk.Frame(f, bg=BG_COLOR)
        body.pack(fill="both", expand=True, padx=40, pady=20)

        card = tk.Frame(body, bg=CARD_COLOR, highlightthickness=1, highlightbackground=BORDER_COLOR)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD_COLOR, padx=24, pady=20)
        inner.pack(fill="x")

        tk.Label(inner, text="JOIN ADDRESS  (ip:port, from the host)", font=FONT_LABEL,
                 bg=CARD_COLOR, fg=MUTED_COLOR).pack(anchor="w")
        ent_addr = Entry(inner, self._data.get("join_address", ""))
        ent_addr.pack(fill="x", pady=(5, 15))

        tk.Label(inner, text="JOIN CODE", font=FONT_LABEL, bg=CARD_COLOR, fg=MUTED_COLOR).pack(anchor="w")
        ent_token = Entry(inner, self._data.get("join_token", ""))
        ent_token.pack(fill="x", pady=(5, 15))

        tk.Label(inner, text="ROBLOX STUDIO PATH", font=FONT_LABEL, bg=CARD_COLOR, fg=MUTED_COLOR).pack(anchor="w")
        path_row = tk.Frame(inner, bg=CARD_COLOR)
        path_row.pack(fill="x", pady=(5, 0))
        ent_path = Entry(path_row, self.studio_path)
        ent_path.pack(side="left", fill="x", expand=True)

        def browse():
            p = filedialog.askopenfilename(title="Locate RobloxStudioBeta.exe", filetypes=[("Executable", "*.exe")])
            if p:
                ent_path.delete(0, "end")
                ent_path.insert(0, p)

        Button(path_row, "BROWSE", browse, base=BTN_FLAT, hover=BTN_FLAT_HOVER, width=90, height=32).pack(side="left", padx=(10, 0))

        buttons = tk.Frame(body, bg=BG_COLOR)
        buttons.pack(pady=24)

        def join():
            addr = ent_addr.get().strip()
            token = ent_token.get().strip().upper()
            studio = ent_path.get().strip()
            if not addr or ":" not in addr:
                messagebox.showerror("Invalid address", "Enter the address as ip:port.")
                return
            if not token:
                messagebox.showerror("Invalid join code", "Enter the join code the host gave you.")
                return
            if not studio or not os.path.exists(studio):
                messagebox.showerror("Studio not found", "Please browse to RobloxStudioBeta.exe.")
                return
            self.studio_path = studio
            self._data["join_address"] = addr
            self._data["join_token"] = token
            save_data(self._data)
            host, port_s = addr.rsplit(":", 1)
            self._show_join_running(studio, host, int(port_s), token)

        Button(buttons, "CONNECT", join, width=170, height=44).pack(side="left", padx=6)
        Button(buttons, "BACK", self._show_welcome, base=BTN_FLAT, hover=BTN_FLAT_HOVER,
               width=100, height=40).pack(side="left", padx=6)

        self._make_footer(f)

    # ---------- join running ----------
    def _show_join_running(self, studio, host, port, token):
        self._clear()
        f = tk.Frame(self.container, bg=BG_COLOR)
        f.pack(fill="both", expand=True)
        self._make_header(f, "Connection console", "Tunnel status")

        status_var = tk.StringVar(value="CONNECTING")
        status = tk.Label(f, textvariable=status_var, font=("Segoe UI", 11, "bold"), bg=BG_COLOR, fg=MUTED_COLOR)
        status.pack(pady=(12, 8))

        log_area = make_log(f, height=15)

        buttons = tk.Frame(f, bg=BG_COLOR)
        buttons.pack(pady=12)

        def on_stop():
            self._write_log(log_area, "Disconnecting...")
            stop_udp_proxy(wait=True)
            self._show_welcome()

        Button(buttons, "DISCONNECT", on_stop, base=BTN_DANGER, hover=BTN_DANGER_HOVER,
               width=160, height=38).pack()

        def run():
            log = lambda msg: self._write_log(log_area, msg)
            log(f"Target : {host}:{port}")
            log("Connecting...")
            ok, bound_port = start_udp_proxy(host, port, log, join_token=token)
            if not ok:
                self.after(0, lambda: [status_var.set("FAILED"), status.config(fg=ACCENT_RED)])
                return

            self.after(0, lambda: [status_var.set(f"CONNECTED  •  local port {bound_port}"), status.config(fg=ACCENT_GREEN)])
            log("Launching Studio...")
            p_guid, t_guid = generate_guid(), generate_guid()
            try:
                launch_client(studio, "127.0.0.1", str(bound_port), p_guid, t_guid, "StudioPlayer")
            except Exception as e:
                log(f"ERROR: {e}")
                self.after(0, lambda: [status_var.set("STUDIO LAUNCH FAILED"), status.config(fg=ACCENT_RED)])
                stop_udp_proxy(wait=False)

        threading.Thread(target=run, daemon=True).start()
        self._make_footer(f)


# ==================== ENTRY POINT ====================
if __name__ == "__main__":
    app = App()
    app.mainloop()
    stop_udp_proxy(wait=True)
