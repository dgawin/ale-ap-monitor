"""
ALE OmniAccess Stellar – AP Status Monitor
============================================
Requires:  pip install paramiko

Layout:
  Left  : AP list (IP, status indicator, client count)
  Right : Tabs → System | Clients | Wireless | Network
  Bottom: IP input, refresh controls, log bar
"""

APP_VERSION = "0.1.0"
APP_NAME    = "ALE OmniAccess Stellar – AP Status Monitor"

STATIC_CACHE_TTL = 600   # seconds — static AP data refreshed every 10 min
_static_cache: dict = {}  # {ip: {"ts": float, "data": dict}}

import tkinter as tk
from tkinter import ttk, messagebox
import paramiko
import threading
import queue
import time
import re
import configparser
import os
import sys
import socket
import base64
import glob
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Settings / Profile system ───────────────────────────────────────────────
# Resolve the directory where the EXE (or .py script) lives.
# sys.executable points to the EXE when frozen by PyInstaller --onefile,
# so INI files are always written next to the EXE, not into the temp unpack dir.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "ap_monitor.ini")   # current active profile
LOG_FILE      = os.path.join(APP_DIR, "ap_monitor_debug.log")

# ─── Debug logger ────────────────────────────────────────────────────────────
import threading as _threading
_log_lock    = _threading.Lock()
_debug_enabled = False   # toggled at runtime via Settings

def set_debug_logging(enabled: bool):
    global _debug_enabled
    _debug_enabled = bool(enabled)

def debug_log(ap_ip: str, cmd: str, output: str):
    """Append one SSH exchange to the debug log file."""
    if not _debug_enabled:
        return
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = "─" * 72
    block = (
        f"\n{sep}\n"
        f"[{ts}]  AP: {ap_ip}\n"
        f"CMD: {cmd}\n"
        f"{sep}\n"
        f"{output}\n"
    )
    with _log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(block)


def _obfuscate(pw: str) -> str:
    """Lightweight obfuscation — not encryption, just not plaintext."""
    return "b64:" + base64.b64encode(pw.encode("utf-8")).decode("ascii")

def _deobfuscate(raw: str) -> str:
    if raw.startswith("b64:"):
        try:
            return base64.b64decode(raw[4:]).decode("utf-8")
        except Exception:
            return raw
    return raw  # legacy plaintext

def list_profiles():
    """Return sorted list of profile names (= INI basenames without extension)."""
    files = glob.glob(os.path.join(APP_DIR, "ap_monitor_*.ini")) +             glob.glob(os.path.join(APP_DIR, "ap_monitor.ini"))
    names = []
    for f in files:
        base = os.path.basename(f)
        if base == "ap_monitor.ini":
            names.append("default")
        else:
            names.append(base[len("ap_monitor_"):-len(".ini")])
    return sorted(set(names))

def profile_path(profile: str) -> str:
    if profile == "default":
        return os.path.join(APP_DIR, "ap_monitor.ini")
    return os.path.join(APP_DIR, f"ap_monitor_{profile}.ini")

def load_settings(path=None):
    if path is None:
        path = SETTINGS_FILE
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    raw_pw = cfg.get("credentials", "password", fallback="aos2016")
    return {
        "username":   cfg.get("credentials", "username",  fallback="support"),
        "password":   _deobfuscate(raw_pw),
        "timeout":    cfg.getint("connection", "timeout",  fallback=10),
        "workers":    cfg.getint("connection", "workers",  fallback=10),
        "ip_start":   cfg.get("scan",         "ip_start", fallback="192.168.85.1"),
        "ip_end":     cfg.get("scan",         "ip_end",   fallback="192.168.85.10"),
        "ip_list":    cfg.get("scan",         "ip_list",  fallback=""),
        "interval":   cfg.getint("scan",      "interval", fallback=60),
        "theme":      cfg.get("ui",           "theme",    fallback="dark"),
        "dns_server": cfg.get("dns",          "server",   fallback=""),
    }

def save_settings(d, path=None):
    global SETTINGS_FILE
    if path is None:
        path = SETTINGS_FILE
    cfg = configparser.ConfigParser()
    cfg["credentials"] = {"username": d["username"],
                          "password": _obfuscate(d["password"])}
    cfg["connection"]  = {"timeout":  str(d["timeout"]), "workers": str(d["workers"])}
    cfg["scan"]        = {
        "ip_start": d["ip_start"], "ip_end": d["ip_end"],
        "ip_list":  d["ip_list"],  "interval": str(d["interval"]),
    }
    cfg["ui"]  = {"theme": d.get("theme", "dark")}
    cfg["dns"] = {"server": d.get("dns_server", "")}
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)

# ─── Runtime credentials (updated from settings) ──────────────────────────────
SSH_USERNAME   = "support"
SSH_PASSWORD   = "aos2016"
SSH_TIMEOUT    = 10
MAX_WORKERS    = 10
DNS_SERVER     = ""   # custom DNS for client reverse lookups; "" = system default

# ─── Themes ───────────────────────────────────────────────────────────────────
THEMES = {
    "dark": {
        "bg":          "#0f1117",
        "surface":     "#1a1d27",
        "surface2":    "#22263a",
        "border":      "#2e3350",
        "accent":      "#7b5ea7",
        "accent2":     "#5b3d87",
        "ok":          "#3ecf8e",
        "warn":        "#f5a623",
        "err":         "#e05c5c",
        "text":        "#e8eaf0",
        "text_dim":    "#7b82a0",
        "text_bright": "#ffffff",
        "selected":    "#3a2060",
        "hover":       "#221540",
        "btn_fg":      "#ffffff",
        "tree_bg":     "#1a1d27",
        "field_bg":    "#22263a",
    },
    "light": {
        "bg":          "#f0edf6",
        "surface":     "#ffffff",
        "surface2":    "#e8e2f4",
        "border":      "#bdb0d8",
        "accent":      "#6b2fa0",
        "accent2":     "#4e1f7a",
        "ok":          "#1a7a52",
        "warn":        "#b86a00",
        "err":         "#b52828",
        "text":        "#1a1428",
        "text_dim":    "#5c4e7a",
        "text_bright": "#0a0614",
        "selected":    "#d0bef0",
        "hover":       "#e4d8f8",
        "btn_fg":      "#ffffff",
        "tree_bg":     "#faf8ff",
        "field_bg":    "#eeeaf8",
    },
}

CURRENT_THEME = "dark"

def get_theme(name=None):
    return THEMES.get(name or CURRENT_THEME, THEMES["dark"])

C = get_theme("dark")

FONT_MONO  = ("Courier New", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_BODY  = ("Segoe UI", 10)
FONT_BOLD  = ("Segoe UI", 10, "bold")
FONT_HEAD  = ("Segoe UI", 11, "bold")
FONT_TITLE = ("Segoe UI", 13, "bold")


# ─── SSH helpers ──────────────────────────────────────────────────────────────

def ssh_connect(ip):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=SSH_USERNAME, password=SSH_PASSWORD,
              timeout=SSH_TIMEOUT, look_for_keys=False, allow_agent=False)
    return c


def ssh_exec(client, cmd, timeout=10, _ap_ip=""):
    """Execute cmd over SSH. Logs to debug log if enabled."""
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="ignore").strip()
        err = stderr.read().decode("utf-8", errors="ignore").strip()
        log_out = out if out else ("(empty)")
        if err:
            log_out += f"\n[STDERR] {err}"
        debug_log(_ap_ip, cmd, log_out)
        return out
    except Exception as e:
        debug_log(_ap_ip, cmd, f"[EXCEPTION] {e}")
        return ""


# ─── Parsers ──────────────────────────────────────────────────────────────────

def parse_showsysinfo(raw):
    """
    Parse 'showsysinfo' output.
    Real ALE format uses right-aligned labels with colon separator:
        Company Name:ALE USA Inc.
             SN:SSZ202101464
    """
    d = {}
    for line in raw.splitlines():
        m = re.match(r"\s*([^:]+?)\s*:\s*(.+)$", line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        if not value:
            continue
        if label in ("sn",) or "serial" in label:
            d.setdefault("serial", value)
        elif "device model" in label or label == "model":
            d.setdefault("model", value)
        elif label == "mac":
            d.setdefault("mac", value)
        elif "country" in label:
            d.setdefault("country", value)
        elif "software version" in label:
            d.setdefault("firmware", value)
        elif "hardware version" in label:
            d.setdefault("hw_version", value)
        elif "essid prefix" in label:
            d.setdefault("essid_prefix", value)
        elif "cluster describe" in label:
            d.setdefault("cluster_describe", value)
    return d


def parse_uptime(raw):
    # e.g. "16:26:13 up 1 day, 23:17,  load average: 0.04, 0.18, 0.21"
    m = re.search(r"up\s+(.+?),\s+\d+\s+user", raw)
    uptime_str = m.group(1).strip() if m else raw.split("up")[-1].split(",")[0].strip() if "up" in raw else raw
    m2 = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", raw)
    load = (m2.group(1), m2.group(2), m2.group(3)) if m2 else ("?", "?", "?")
    return {"uptime": uptime_str, "load1": load[0], "load5": load[1], "load15": load[2]}


def parse_free(raw):
    result = {}
    for line in raw.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("Mem"):
            # total used free [shared buffers]
            try:
                total = int(parts[1])
                used  = int(parts[2])
                free  = int(parts[3])
                result = {
                    "mem_total": total,
                    "mem_used":  used,
                    "mem_free":  free,
                    "mem_pct":   round(used / total * 100) if total else 0,
                }
            except (IndexError, ValueError):
                pass
    return result


def parse_getmode(raw):
    raw = raw.strip().lower()
    if "cluster" in raw:
        return "Cluster"
    if "ov" in raw or "omnivista" in raw:
        return "OmniVista"
    if "cloud" in raw:
        return "Cloud"
    return raw or "—"


def parse_cluster_self(raw):
    # ClusterID  MAC              role    priority  status
    lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("Cluster")]
    result = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 4:
            result["cluster_id"] = parts[0]
            result["cluster_mac"] = parts[1]
            result["cluster_role"] = parts[2]
            result["cluster_status"] = parts[-1]
    return result


def parse_sta_list(raw):
    """
    Parse sta_list output. Handles:
    - Empty IPv6 column
    - Concatenated Final_role values like "160336760032Employee" or "1601895758109arp"
    - Duplicate SSID blocks in cluster mode (deduplicated by MAC)
    - Empty SSID blocks
    """
    clients      = []
    seen         = set()
    current_ssid = ""
    MAC_RE  = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
    IPV6_RE = re.compile(r"^[0-9a-fA-F]{1,4}::?")

    for line in raw.splitlines():
        stripped = line.strip()

        if stripped.startswith("SSID:"):
            current_ssid = stripped[5:].strip()
            continue

        if stripped.startswith("STA_MAC") or not stripped:
            continue

        if not MAC_RE.match(stripped):
            continue

        parts = stripped.split()
        if len(parts) < 7:
            continue

        idx = 1
        ip4 = parts[idx] if len(parts) > idx else "—"; idx += 1

        # Skip IPv6 field if present
        if len(parts) > idx and (IPV6_RE.match(parts[idx]) or parts[idx].count(":") >= 2):
            idx += 1

        online = parts[idx] if len(parts) > idx else "—"; idx += 1
        rx     = parts[idx] if len(parts) > idx else "—"; idx += 1
        tx     = parts[idx] if len(parts) > idx else "—"; idx += 1
        freq   = parts[idx] if len(parts) > idx else "—"; idx += 1
        auth   = parts[idx] if len(parts) > idx else "—"; idx += 1

        # Final_role in cluster mode has a numeric prefix e.g. "160336760032Employee"
        # Strip the leading digits to get the actual role name
        role_raw   = parts[idx] if len(parts) > idx else "—"; idx += 1
        role_clean = re.sub(r"^\d+", "", role_raw).strip() or role_raw

        # VLANID is the next column
        vlan_id    = parts[idx] if len(parts) > idx else ""
        try:
            vlan_int = int(vlan_id)
            if vlan_int > 0:
                role_clean = f"{role_clean} (VLAN {vlan_int})"
        except (ValueError, TypeError):
            pass

        mac = parts[0]
        if mac in seen:
            continue
        seen.add(mac)

        clients.append({
            "mac":    mac,
            "ip":     ip4,
            "online": online,
            "rx":     rx,
            "tx":     tx,
            "freq":   freq,
            "auth":   auth,
            "role":   role_clean,
            "ssid":   current_ssid,
            # Phase-1 radio fields (filled later by merge_client_radio_data)
            "rssi":          None,
            "snr":           None,
            "txrate":        None,
            "rxrate":        None,
            "band":          None,
            "mode":          None,
            "assoctime":     None,
            "health_score":  None,
            "health_status": None,
            # Phase-2 stubs
            "last_seen":     None,
            "ap_name":       None,
        })

    return clients


def parse_iwconfig(raw):
    """Parse iwconfig output into list of interface dicts."""
    interfaces = []
    current = None
    for line in raw.splitlines():
        m = re.match(r"^(\w+)\s+IEEE", line)
        if m:
            if current:
                interfaces.append(current)
            std_m = re.search(r"IEEE\s+(\S+)", line)
            essid_m = re.search(r'ESSID:"([^"]*)"', line)
            current = {
                "iface":   m.group(1),
                "std":     std_m.group(1) if std_m else "—",
                "essid":   essid_m.group(1) if essid_m else "—",
                "freq":    "—", "channel": "—", "bitrate": "—",
                "txpower": "—", "txpower_cur": "—",
                "signal":  "—", "noise":   "—", "quality": "—",
            }
        elif current:
            if "Frequency:" in line:
                fm = re.search(r"Frequency:([\d.]+\s*\w+)", line)
                if fm:
                    current["freq"] = fm.group(1).strip()
                # Channel is often on same line: "Frequency:2.437 GHz  (Channel 6)"
                cm = re.search(r"Channel\s+(\d+)", line)
                if cm:
                    current["channel"] = cm.group(1)
            if "Bit Rate" in line:
                bm = re.search(r"Bit Rate[=:]([\d.]+\s*Mb/s)", line)
                if bm:
                    current["bitrate"] = bm.group(1).strip()
            if "Tx-Power=" in line:
                pm = re.search(r"Tx-Power=([\d]+)\s*dBm", line)
                if pm:
                    current["txpower"] = pm.group(1) + " dBm"
            if "Link Quality=" in line:
                qm = re.search(r"Link Quality=(\S+)", line)
                sm = re.search(r"Signal level=([-\d]+\s*\S+)", line)
                nm = re.search(r"Noise level=([-\d]+\s*\S+)", line)
                if qm:
                    current["quality"] = qm.group(1)
                if sm:
                    current["signal"] = sm.group(1).strip()
                if nm:
                    current["noise"] = nm.group(1).strip()
    if current:
        interfaces.append(current)
    # Filter out non-wireless (br-wan etc.)
    return [i for i in interfaces if i["essid"] != "—" or i["std"] != "—"]


def parse_adme_show(raw):
    """
    Parse 'adme show'. Handles two formats:
    - With tenantId:    mac ip ip6 ov_ip tenantId state name version radiocnt ...
    - Without tenantId: mac ip ip6 ov_ip          state name version radiocnt ...
    state is always a single digit 0-4; we find it by scanning past ip4/ip6/ov_ip fields.
    """
    STATE_LABELS = {"0":"active","1":"offline","2":"wireless ageing",
                    "3":"static active","4":"static offline"}
    MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
    IP4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    neighbors = []
    current   = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("[state]") or stripped.startswith("mac"):
            continue

        if MAC_RE.match(stripped):
            if current:
                neighbors.append(_adme_finalize(current))
            parts = stripped.split()
            idx = 1
            # skip ipv4
            if len(parts) > idx and IP4_RE.match(parts[idx]): idx += 1
            # skip ipv6 (contains multiple colons)
            if len(parts) > idx and parts[idx].count(":") >= 2: idx += 1
            # skip ov_ip
            if len(parts) > idx and IP4_RE.match(parts[idx]): idx += 1
            # skip tenantId if it's not a single digit (e.g. "OVNG")
            if len(parts) > idx and not re.match(r"^\d$", parts[idx]): idx += 1
            # now: state, name, version, radiocnt, [radioid, channel, freq, bw, rssi, txpower]
            state_raw = parts[idx] if len(parts) > idx else "0"; idx += 1
            name      = parts[idx] if len(parts) > idx else "—"; idx += 1
            version   = parts[idx] if len(parts) > idx else "—"; idx += 1
            idx += 1  # skip radiocnt

            current = {
                "mac":     parts[0],
                "ip":      parts[1] if len(parts) > 1 else "—",
                "state":   STATE_LABELS.get(state_raw, state_raw),
                "name":    name,
                "version": version,
                "radios":  [],
            }
            # First radio may be on same line: radioid channel freq bw rssi txpower
            if len(parts) >= idx + 5:
                current["radios"].append({
                    "freq": parts[idx+2] if len(parts) > idx+2 else "0",
                    "rssi": parts[idx+4] if len(parts) > idx+4 else "0",
                })

        elif current is not None:
            parts = stripped.split()
            if len(parts) >= 6:
                try:
                    int(parts[0])
                    current["radios"].append({"freq": parts[2], "rssi": parts[4]})
                except ValueError:
                    pass

    if current:
        neighbors.append(_adme_finalize(current))
    return neighbors


def _adme_finalize(ap):
    """Pick best RSSI per band from collected radios."""
    rssi_2g, rssi_5g = "—", "—"
    for r in ap.get("radios", []):
        try:
            freq = int(r.get("freq", 0))
            rssi = int(r.get("rssi", 0))
            if freq == 0:
                continue
            if freq < 3000:
                if rssi_2g == "—" or rssi > int(rssi_2g):
                    rssi_2g = str(rssi)
            else:
                if rssi_5g == "—" or rssi > int(rssi_5g):
                    rssi_5g = str(rssi)
        except (ValueError, TypeError):
            pass
    ap["rssi_2g"] = rssi_2g
    ap["rssi_5g"] = rssi_5g
    return ap


def parse_iwlist_txpower(raw):
    """
    Parse 'iwlist txpower' (no interface arg).
    Interface block starts at col 0; skip lines with 'no transmit-power information'.
    """
    results       = []
    current_iface = None
    current_power = "—"
    levels        = []

    for line in raw.splitlines():
        # New interface: starts at col 0, non-space
        if line and not line[0].isspace():
            # Save previous if useful
            if current_iface and current_power != "—":
                results.append({
                    "iface":   current_iface,
                    "current": current_power,
                    "levels":  ", ".join(levels),
                })
            m = re.match(r"^([\w\-\.]+)", line)
            current_iface = m.group(1) if m else None
            current_power = "—"
            levels        = []

        elif current_iface:
            if "Current Tx-Power=" in line:
                m = re.search(r"(\d+)\s*dBm", line)
                if m:
                    current_power = m.group(1) + " dBm"
            elif re.match(r"\s+\d+\s+dBm", line):
                m = re.search(r"(\d+)\s+dBm", line)
                if m:
                    levels.append(m.group(1) + " dBm")

    if current_iface and current_power != "—":
        results.append({
            "iface":   current_iface,
            "current": current_power,
            "levels":  ", ".join(levels),
        })
    return results


def parse_ifconfig_brwan(raw):
    result = {}
    m = re.search(r"HWaddr\s+(\S+)", raw)
    if m:
        result["mac"] = m.group(1)
    m = re.search(r"inet addr:([\d.]+)", raw)
    if m:
        result["ip"] = m.group(1)
    m = re.search(r"Mask:([\d.]+)", raw)
    if m:
        result["mask"] = m.group(1)
    m = re.search(r"RX bytes:(\d+)", raw)
    if m:
        result["rx_bytes"] = int(m.group(1))
    m = re.search(r"TX bytes:(\d+)", raw)
    if m:
        result["tx_bytes"] = int(m.group(1))
    return result


def parse_wlanconfig(raw):
    """
    Parse 'wlanconfig athXX list' output.

    Real ALE format — header + positional data rows, followed by
    indented key:value continuation lines per client:

      ADDR  AID  CHAN  TXRATE  RXRATE  RSSI  MINRSSI  MAXRSSI  IDLE  TXSEQ  RXSEQ
        CAPS  XCAPS  ACAPS  ERP  STATE  MAXRATE(DOT11)  HTCAPS  VHTCAPS  ASSOCTIME
        IEs  MODE  PSMODE  RXNSS  TXNSS
      1c:ce:51:57:30:21  1  48  173M  78M  28  19  54  2  0  65535  Es  BOf  0  b  0
        APM  1gTRs  02:38:24  WME  IEEE80211_MODE_11AC_VHT20  0 2 2 ...
       SNR                            : 28
       Operating band                 : 5GHz

    Returns dict keyed by lowercase MAC address.
    """
    result      = {}
    current_mac = None
    current     = {}
    # Column indices resolved from the header line (collected across wrapped lines)
    col_names   = []
    header_done = False

    MAC_RE = re.compile(
        r"^([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # ── Header accumulation ───────────────────────────────────────────
        # The header can span multiple lines; it starts with "ADDR" and
        # ends when we see the first MAC-address data line.
        if not header_done:
            if re.match(r"^ADDR\b", stripped, re.I):
                col_names = stripped.split()
                continue
            # Continuation of header (no MAC yet): pure UPPERCASE tokens
            if col_names and not MAC_RE.match(stripped):
                col_names.extend(stripped.split())
                continue

        # ── Data line: starts with a MAC address ──────────────────────────
        if MAC_RE.match(stripped):
            header_done = True
            # Save previous client
            if current_mac:
                result[current_mac] = current

            current_mac = MAC_RE.match(stripped).group(1).lower()
            current     = {}

            # Split the entire (possibly wrapped) data into tokens.
            # We treat the line as a token stream and map to col_names.
            tokens = stripped.split()
            # Map tokens to column names (best-effort, columns may wrap)
            col_map = {}
            for i, col in enumerate(col_names):
                if i < len(tokens):
                    col_map[col.upper()] = tokens[i]

            # Extract the fields we care about
            def _tok(key):
                return col_map.get(key.upper(), "")

            txrate = _tok("TXRATE")
            rxrate = _tok("RXRATE")
            rssi   = _tok("RSSI")
            assoc  = _tok("ASSOCTIME")
            mode   = _tok("MODE")

            if txrate: current["txrate"]    = txrate
            if rxrate: current["rxrate"]    = rxrate
            # ASSOCTIME from col_map is unreliable (col count drifts due to multi-word headers)
            # Always prefer the direct HH:MM:SS regex match
            assoc_re = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", stripped)
            if assoc_re:
                current["assoctime"] = assoc_re.group(1)
            elif assoc:
                current["assoctime"] = assoc
            mode_re = re.search(r"(IEEE80211_MODE_\S+)", stripped)
            if mode_re:
                current["mode"] = mode_re.group(1)
            elif mode and mode.startswith("IEEE"):
                current["mode"] = mode
            # TXRATE / RXRATE: regex fallback for "173M" style tokens
            rate_tokens = re.findall(r"\b(\d+M)\b", stripped)
            if len(rate_tokens) >= 2:
                if not current.get("txrate"): current["txrate"] = rate_tokens[0]
                if not current.get("rxrate"): current["rxrate"] = rate_tokens[1]
            elif len(rate_tokens) == 1:
                if not current.get("txrate"): current["txrate"] = rate_tokens[0]
            try:
                r = int(rssi)
                # RSSI from wlanconfig is a positive SNR-like value (not dBm).
                # Store raw; dBm conversion: dBm = raw - 96 for ALE.
                # We keep the raw value here and convert in health/display.
                # RSSI column on ALE Stellar == SNR value, not a dBm figure.
                # Store as snr_raw; authoritative SNR comes from continuation line.
                current["snr_raw"] = r
            except (ValueError, TypeError):
                pass
            continue

        # ── Continuation lines (indented key : value) ─────────────────────
        if current_mac is None:
            continue

        # These lines look like "  SNR                            : 28"
        kv = re.match(r"^(.+?)\s*:\s*(.+)$", stripped)
        if kv:
            key = kv.group(1).strip().upper()
            val = kv.group(2).strip()
            if key == "SNR":
                try:
                    current["snr"] = int(val)
                except (ValueError, TypeError):
                    pass
            elif key == "OPERATING BAND":
                # Normalize "2.4 GHz" -> "2.4GHz", "5 GHz" -> "5GHz"
                            current["band"] = re.sub(r"\s+", "", val)
            elif key == "MODE" and val.startswith("IEEE"):
                current.setdefault("mode", val)

    if current_mac:
        result[current_mac] = current

    # Derive band from mode name if not explicitly set
    for mac, data in result.items():
        if not data.get("band"):
            mode = data.get("mode", "").upper()
            if any(x in mode for x in ("11NG", "11G", "11B", "HT20", "HT40")):
                # 11NG / 11NA both contain "11N" — check G/A suffix
                if "11NA" not in mode:
                    data["band"] = "2.4GHz"
                else:
                    data["band"] = "5GHz"
            if any(x in mode for x in ("11NA", "11AC", "11AX", "VHT", "HE")):
                data["band"] = "5GHz"

    return result


def merge_client_radio_data(clients, wlan_clients):
    """
    Merge wlanconfig data into client list.
    Matches on MAC address (case-insensitive).
    Adds last_seen and ap_name stubs for Phase 2.
    """
    if not wlan_clients:
        return clients
    for client in clients:
        mac = client.get("mac", "").lower()
        radio = wlan_clients.get(mac)
        if radio:
            client.update({k: v for k, v in radio.items() if v is not None})
        # Phase-2 stubs
        client.setdefault("last_seen", None)
        client.setdefault("ap_name",   None)
    return clients


def calculate_health(client):
    """
    Compute a 0-100 health score.

    Metrics used (ALE Stellar specifics):
      - SNR (dB)     max 50 pts  — primary signal quality indicator
                                   (RSSI column in wlanconfig == SNR, not dBm)
      - Band         max 15 pts
      - TX Rate      max 20 pts
      - RX Rate      max 15 pts

    Returns (score: int, status: str, breakdown: list[dict])
    breakdown entry: {label, points, max, note, ok}
    """
    score     = 0
    breakdown = []

    # ── SNR (max 50 pts) ───────────────────────────────────────────────────────────────────────────
    # Use continuation-line SNR (reliable). Fall back to snr_raw (== RSSI column).
    snr = client.get("snr") or client.get("snr_raw")
    if snr is not None:
        try:
            s = int(snr)
            if s > 30:
                pts = 50; note = f"{s} dB  (excellent, > 30)"
            elif s > 20:
                pts = 35; note = f"{s} dB  (good, 20–30)"
            elif s > 15:
                pts = 20; note = f"{s} dB  (ok, 15–20)"
            elif s > 10:
                pts = 10; note = f"{s} dB  (poor, 10–15)"
            else:
                pts = 0;  note = f"{s} dB  (bad, ≤ 10)"
            score += pts
            breakdown.append({"label": "SNR", "points": pts, "max": 50, "note": note, "ok": pts >= 35})
        except (ValueError, TypeError):
            breakdown.append({"label": "SNR", "points": 0, "max": 50, "note": "parse error", "ok": False})
    else:
        breakdown.append({"label": "SNR", "points": 0, "max": 50,
                          "note": "no data — wlanconfig interface not found?", "ok": False})

    # ── Band (max 15 pts) ───────────────────────────────────────────────────────────────────────────
    band = (client.get("band") or "").upper()
    if "5" in band:
        pts = 15; note = "5GHz"
    elif "2.4" in band or "2G" in band:
        pts = 5;  note = "2.4GHz"
    else:
        pts = 0;  note = "unknown"
    score += pts
    breakdown.append({"label": "Band", "points": pts, "max": 15, "note": note, "ok": pts >= 15})

    # ── TX Rate (max 20 pts) ─────────────────────────────────────────────────────────────────────────
    txrate_raw = client.get("txrate") or ""
    try:
        tx_mbps = int(re.sub(r"[^\d]", "", str(txrate_raw)))
        if tx_mbps >= 300:
            pts = 20; note = f"{txrate_raw}  (≥ 300 Mbps)"
        elif tx_mbps >= 150:
            pts = 15; note = f"{txrate_raw}  (150–300 Mbps)"
        elif tx_mbps >= 54:
            pts = 8;  note = f"{txrate_raw}  (54–150 Mbps)"
        else:
            pts = 0;  note = f"{txrate_raw}  (< 54 Mbps)"
        score += pts
        breakdown.append({"label": "TX Rate", "points": pts, "max": 20, "note": note, "ok": pts >= 15})
    except (ValueError, TypeError):
        breakdown.append({"label": "TX Rate", "points": 0, "max": 20,
                          "note": txrate_raw or "no data", "ok": False})

    # ── RX Rate (max 15 pts) ─────────────────────────────────────────────────────────────────────────
    rxrate_raw = client.get("rxrate") or ""
    try:
        rx_mbps = int(re.sub(r"[^\d]", "", str(rxrate_raw)))
        if rx_mbps >= 300:
            pts = 15; note = f"{rxrate_raw}  (≥ 300 Mbps)"
        elif rx_mbps >= 150:
            pts = 10; note = f"{rxrate_raw}  (150–300 Mbps)"
        elif rx_mbps >= 54:
            pts = 5;  note = f"{rxrate_raw}  (54–150 Mbps)"
        else:
            pts = 0;  note = f"{rxrate_raw}  (< 54 Mbps)"
        score += pts
        breakdown.append({"label": "RX Rate", "points": pts, "max": 15, "note": note, "ok": pts >= 10})
    except (ValueError, TypeError):
        breakdown.append({"label": "RX Rate", "points": 0, "max": 15,
                          "note": rxrate_raw or "no data", "ok": False})

    if score >= 80:
        status = "Healthy"
    elif score >= 50:
        status = "Warning"
    else:
        status = "Critical"

    return score, status, breakdown

def parse_route(raw):
    routes = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 8 and re.match(r"\d+\.\d+", parts[0]):
            routes.append({
                "dest":    parts[0],
                "gateway": parts[1],
                "mask":    parts[2],
                "iface":   parts[7],
            })
    return routes


def fmt_bytes(b):
    if b is None:
        return "—"
    try:
        b = int(b)
    except (ValueError, TypeError):
        return str(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def fmt_mb(b):
    """Format bytes as MB with 2 decimal places."""
    try:
        b = int(b)
    except (ValueError, TypeError):
        return str(b) if b else "—"
    return f"{b / 1_000_000:.2f} MB"


def fmt_online(seconds):
    """Convert seconds to D:HH:MM:SS string."""
    try:
        s = int(seconds)
    except (ValueError, TypeError):
        return str(seconds) if seconds else "—"
    days    = s // 86400
    s      %= 86400
    hours   = s // 3600
    s      %= 3600
    minutes = s // 60
    secs    = s % 60
    if days > 0:
        return f"{days}d {hours:02}:{minutes:02}:{secs:02}"
    return f"{hours:02}:{minutes:02}:{secs:02}"


def fmt_assoctime(raw):
    """
    Normalize association time to HH:MM:SS.
    Accepts: "HH:MM:SS" (from wlanconfig), integer seconds (from sta_list),
    or any string — returned as-is if unrecognised.
    """
    if not raw or raw in ("—", "None", "none"):
        return "—"
    # Already HH:MM:SS — convert to "1D h:mm:ss" if hours >= 24
    m_hms = re.match(r"^(\d+):(\d{2}):(\d{2})$", str(raw))
    if m_hms:
        h_total = int(m_hms.group(1))
        mm = int(m_hms.group(2)); ss = int(m_hms.group(3))
        if h_total >= 24:
            d = h_total // 24; h_r = h_total % 24
            return f"{d}D {h_r}:{mm:02}:{ss:02}"
        return str(raw)
    # Integer seconds
    try:
        s    = int(raw)
        h    = s // 3600
        m    = (s % 3600) // 60
        sec  = s % 60
        if h >= 24:
            d = h // 24; h %= 24
            return f"{d}D {h}:{m:02}:{sec:02}"
        return f"{h:02}:{m:02}:{sec:02}"
    except (ValueError, TypeError):
        return str(raw)


def rssi_to_dbm(rssi_val):
    """Convert ALE RSSI value to dBm. Formula: dBm = RSSI - 96."""
    try:
        r = int(rssi_val)
        if r <= 0:
            return None
        return r - 96
    except (ValueError, TypeError):
        return None


def fmt_rssi(rssi_val):
    """Format RSSI as 'XX (−YY dBm)'. Returns plain '—' if invalid."""
    dbm = rssi_to_dbm(rssi_val)
    if dbm is None:
        return "—"
    return f"{rssi_val} ({dbm} dBm)"


def rssi_tag(rssi_val):
    """Return color tag based on RSSI value per ALE chart."""
    try:
        r = int(rssi_val)
        if r >= 29:   return "rssi_ok"    # ≥ -67 dBm  — Perfect (green)
        if r >= 21:   return "rssi_warn"  # -75..-68   — OK (orange)
        return "rssi_bad"                 # ≤ -76 dBm  — Bad (red)
    except (ValueError, TypeError):
        return "rssi_bad"


def resolve_hostname(ip, dns_server=None, timeout=2.0):
    """
    Reverse DNS lookup for ip.
    If dns_server is set, queries that server directly via dnspython (PTR record).
    Falls back to system resolver via socket if dnspython is unavailable or no
    custom server is configured.
    Returns hostname string or '—'.
    """
    if not ip or ip == "—":
        return "—"

    if dns_server:
        try:
            import dns.resolver
            import dns.reversename
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [dns_server]
            resolver.lifetime = timeout
            rev = dns.reversename.from_address(ip)
            answer = resolver.resolve(rev, "PTR")
            return str(answer[0]).rstrip(".")
        except ImportError:
            pass  # dnspython not installed — fall through to socket
        except Exception:
            return "—"

    # System resolver fallback
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        host, _, _ = socket.gethostbyaddr(ip)
        return host
    except (socket.herror, socket.gaierror, OSError):
        return "—"
    finally:
        socket.setdefaulttimeout(old_timeout)


def resolve_client_hostnames(clients, max_workers=20):
    """
    Resolve hostnames for a list of client dicts in parallel.
    Adds a 'hostname' key to each client dict in-place.
    Uses DNS_SERVER global if set, otherwise system resolver.
    Returns the same list.
    """
    if not clients:
        return clients

    ips = [c.get("ip", "—") for c in clients]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(resolve_hostname, ip, DNS_SERVER): i for i, ip in enumerate(ips)}
        results = ["—"] * len(clients)
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                results[idx] = "—"

    for client, hostname in zip(clients, results):
        client["hostname"] = hostname

    return clients


# ─── Data fetcher ─────────────────────────────────────────────────────────────

def fetch_ap_data(ip, force_static=False):
    """
    SSH into AP and return structured data dict.

    Two-tier refresh strategy:
      Static  (TTL = STATIC_CACHE_TTL, default 10 min):
          showsysinfo, getmode, ap_name, resolv.conf, cluster_mgt
          — change only after firmware upgrade / reconfig.
      Dynamic (every scan cycle):
          sta_list, adme, iwconfig, wlanconfig, free, uptime,
          ifconfig br-wan, route-n
          — change while AP is running.

    force_static=True bypasses the cache (first scan / manual Refresh button).
    """
    import time as _time

    result = {"ip": ip, "status": "error", "error": None}
    ssh = None

    try:
        ssh = ssh_connect(ip)
        result["status"] = "ok"

        def run(cmd, timeout=10):
            return ssh_exec(ssh, cmd, timeout=timeout, _ap_ip=ip)

        # ── Static data (cached) ───────────────────────────────────────────
        now       = _time.monotonic()
        cache     = _static_cache.get(ip, {})
        cache_age = now - cache.get("ts", 0)
        use_cache = (not force_static) and bool(cache) and (cache_age < STATIC_CACHE_TTL)

        if use_cache:
            static = cache["data"]
        else:
            raw_sys  = run("showsysinfo")
            sysinfo  = parse_showsysinfo(raw_sys)
            raw_mode = run("getmode")
            # cluster_mgt returns empty on some models — treat as optional
            raw_self = run("cluster_mgt -x show=self", timeout=8)

            # AP hostname from SSH banner
            ap_name = ""
            try:
                transport = ssh.get_transport()
                chan = transport.open_session()
                chan.get_pty()
                chan.invoke_shell()
                import time as _t; _t.sleep(0.8)
                banner = b""
                while chan.recv_ready():
                    banner += chan.recv(1024)
                chan.close()
                m_banner = re.search(r"@([^:$\s]+)[:\$]",
                                     banner.decode("utf-8", errors="ignore"))
                if m_banner:
                    ap_name = m_banner.group(1).strip()
            except Exception:
                pass

            # /etc/cluster_config/system.conf — Permission denied on AP1431,
            # missing on AP1201 — fully optional, never surfaced as error
            raw_preconfig = run("cat /etc/cluster_config/system.conf")
            location = "—"
            if raw_preconfig and "[STDERR]" not in raw_preconfig:
                try:
                    import json as _json
                    pcfg = _json.loads(raw_preconfig)
                    location = (pcfg.get("setLocation", {})
                                    .get("switch_location", "") or "—")
                except Exception:
                    m_loc = re.search(r'"switch_location"\s*:\s*"([^"]+)"',
                                      raw_preconfig)
                    if m_loc:
                        location = m_loc.group(1)

            # DNS — nameservers rarely change at runtime
            raw_dns = run("cat /etc/resolv.conf")
            dns_list = re.findall(r"nameserver\s+([\d.]+)", raw_dns)

            static = {
                "sysinfo":  sysinfo,
                "mode":     parse_getmode(raw_mode),
                "cluster":  parse_cluster_self(raw_self),
                "ap_name":  ap_name,
                "location": location,
                "dns":      ", ".join(dns_list) if dns_list else "—",
            }
            _static_cache[ip] = {"ts": now, "data": static}

        # ── Dynamic data (every cycle) ─────────────────────────────────────
        raw_up   = run("uptime")
        raw_free = run("free")
        raw_date = run("date")

        result["system"] = {
            **static["sysinfo"],
            **parse_uptime(raw_up),
            **parse_free(raw_free),
            "mode":     static["mode"],
            **static["cluster"],
            "date":     raw_date,
            "ap_name":  static["ap_name"],
            "location": static["location"],
        }
        result["ap_name"] = static["ap_name"]
        result["location"] = static["location"]

        # Clients
        raw_sta = run("ssudo sta_list")
        clients = parse_sta_list(raw_sta)
        resolve_client_hostnames(clients)

        # Discover active ath* interfaces via iwconfig + /sys/class/net.
        # Exclude:
        #   -untag  → VLAN-untagged bridge interfaces, not real VAPs
        #   athscan → background scan interfaces, never have associated clients
        raw_iw_early = run("iwconfig")
        ath_ifaces = [
            m.group(1)
            for m in re.finditer(r"^(ath\w+)\s+IEEE", raw_iw_early, re.MULTILINE)
        ]
        raw_netdev = run("ls /sys/class/net/")
        for token in raw_netdev.split():
            if token.startswith("ath") and token not in ath_ifaces:
                ath_ifaces.append(token)

        wlan_clients = {}
        for iface in ath_ifaces:
            if "-untag" in iface or iface.startswith("athscan"):
                continue
            out = run(f"wlanconfig {iface} list")
            if out.strip() and "ADDR" in out:
                wlan_clients.update(parse_wlanconfig(out))

        merge_client_radio_data(clients, wlan_clients)
        for c in clients:
            score, status, breakdown = calculate_health(c)
            c["health_score"]     = score
            c["health_status"]    = status
            c["health_breakdown"] = breakdown

        result["clients"]      = clients
        result["client_count"] = len(clients)

        # Wireless — re-use iwconfig already fetched above
        result["wireless"] = parse_iwconfig(raw_iw_early)

        # TX Power — iwlist without interface also hits gre0 and prints stderr.
        # That is harmless (parse_iwlist_txpower filters by iface), but we keep
        # the call as-is since the parser already handles it correctly.
        raw_txpower  = run("iwlist txpower")
        txpower_map  = {t["iface"]: t for t in parse_iwlist_txpower(raw_txpower)}

        for iface in result.get("wireless", []):
            ifname = iface["iface"]
            tp = txpower_map.get(ifname, {})
            if tp:
                iface["txpower_cur"]   = tp.get("current", "—")
                iface["txpower_avail"] = tp.get("levels",  "—")
            raw_ch = run(f"iwlist {ifname} channel")
            cm = re.search(r"Current Frequency.*?Channel\s+(\d+)", raw_ch)
            if cm:
                iface["channel"] = cm.group(1)

        # MESH
        raw_adme       = run("adme show")
        result["mesh"] = {"neighbors": parse_adme_show(raw_adme)}

        # Network (dynamic — WAN IP / routes can change)
        raw_ifc   = run("ifconfig br-wan")
        raw_route = run("route -n")
        net               = parse_ifconfig_brwan(raw_ifc)
        net["routes"]     = parse_route(raw_route)
        net["dns"]        = static["dns"]
        result["network"] = net

    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)
    finally:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
    return result


# ─── GUI ──────────────────────────────────────────────────────────────────────


# ─── Settings Dialog ──────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, current, on_save):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()
        self._on_save = on_save

        pad = {"padx": 12, "pady": 6}

        # ── Credentials
        cred_f = ttk.LabelFrame(self, text="SSH Credentials", padding=12)
        cred_f.pack(fill=tk.X, padx=16, pady=(16, 8))

        ttk.Label(cred_f, text="Username", style="Dim.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self._user_var = tk.StringVar(value=current.get("username", "support"))
        ttk.Entry(cred_f, textvariable=self._user_var, width=24).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(cred_f, text="Password", style="Dim.TLabel").grid(row=1, column=0, sticky="w", **pad)
        self._pass_var = tk.StringVar(value=current.get("password", ""))
        self._pass_entry = ttk.Entry(cred_f, textvariable=self._pass_var, width=24, show="●")
        self._pass_entry.grid(row=1, column=1, sticky="w", **pad)
        self._show_pw = tk.BooleanVar(value=False)
        ttk.Checkbutton(cred_f, text="Show", variable=self._show_pw,
                        command=self._toggle_pw).grid(row=1, column=2, sticky="w")

        # ── Connection
        conn_f = ttk.LabelFrame(self, text="Connection", padding=12)
        conn_f.pack(fill=tk.X, padx=16, pady=(0, 8))

        ttk.Label(conn_f, text="SSH Timeout (s)", style="Dim.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self._timeout_var = tk.IntVar(value=current.get("timeout", 10))
        ttk.Spinbox(conn_f, from_=3, to=60, textvariable=self._timeout_var, width=6).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(conn_f, text="Parallel Workers", style="Dim.TLabel").grid(row=1, column=0, sticky="w", **pad)
        self._workers_var = tk.IntVar(value=current.get("workers", 10))
        ttk.Spinbox(conn_f, from_=1, to=50, textvariable=self._workers_var, width=6).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(conn_f, text="DNS Server (Client-Auflösung)", style="Dim.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self._dns_var = tk.StringVar(value=current.get("dns_server", ""))
        dns_frame = ttk.Frame(conn_f)
        dns_frame.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        ttk.Entry(dns_frame, textvariable=self._dns_var, width=18).pack(side=tk.LEFT)
        ttk.Label(dns_frame, text="  leer = System-DNS", style="Dim.TLabel",
                  font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(6, 0))

        # ── Scan defaults
        scan_f = ttk.LabelFrame(self, text="Scan Defaults", padding=12)
        scan_f.pack(fill=tk.X, padx=16, pady=(0, 8))

        ttk.Label(scan_f, text="IP Range Start", style="Dim.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self._ip_start_var = tk.StringVar(value=current.get("ip_start", ""))
        ttk.Entry(scan_f, textvariable=self._ip_start_var, width=18).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(scan_f, text="IP Range End", style="Dim.TLabel").grid(row=1, column=0, sticky="w", **pad)
        self._ip_end_var = tk.StringVar(value=current.get("ip_end", ""))
        ttk.Entry(scan_f, textvariable=self._ip_end_var, width=18).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(scan_f, text="IP List", style="Dim.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self._ip_list_var = tk.StringVar(value=current.get("ip_list", ""))
        ttk.Entry(scan_f, textvariable=self._ip_list_var, width=36).grid(row=2, column=1, columnspan=2, sticky="w", **pad)

        ttk.Label(scan_f, text="Auto-Refresh (s)", style="Dim.TLabel").grid(row=3, column=0, sticky="w", **pad)
        self._interval_var = tk.IntVar(value=current.get("interval", 60))
        ttk.Spinbox(scan_f, from_=10, to=600, increment=10,
                    textvariable=self._interval_var, width=6).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(scan_f, text="Static Cache (s)", style="Dim.TLabel").grid(row=4, column=0, sticky="w", **pad)
        self._static_ttl_var = tk.IntVar(value=current.get("static_ttl", 600))
        ttk.Spinbox(scan_f, from_=60, to=3600, increment=60,
                    textvariable=self._static_ttl_var, width=6).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(scan_f, text="← Name, Model, DNS (Intervall für statische Daten)",
                  style="Dim.TLabel", font=("Segoe UI", 8)).grid(row=4, column=2, sticky="w")

        # ── Display
        disp_f = ttk.LabelFrame(self, text="AP List Display", padding=12)
        disp_f.pack(fill=tk.X, padx=16, pady=(0, 8))

        ttk.Label(disp_f, text="Show in list", style="Dim.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 16), pady=4)
        self._list_show_var = tk.StringVar(value=current.get("list_show", "both"))
        for col, (val, lbl) in enumerate([("name", "Name only"), ("ip", "IP only"), ("both", "Name + IP")]):
            ttk.Radiobutton(disp_f, text=lbl, value=val,
                            variable=self._list_show_var).grid(row=0, column=col+1, sticky="w", padx=4)

        ttk.Label(disp_f, text="Sort by", style="Dim.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 16), pady=4)
        self._list_sort_var = tk.StringVar(value=current.get("list_sort", "ip"))
        for col, (val, lbl) in enumerate([("ip", "IP address"), ("name", "AP name")]):
            ttk.Radiobutton(disp_f, text=lbl, value=val,
                            variable=self._list_sort_var).grid(row=1, column=col+1, sticky="w", padx=4)

        # ── Debug Logging
        dbg_f = ttk.LabelFrame(self, text="Debug", padding=12)
        dbg_f.pack(fill=tk.X, padx=16, pady=(0, 8))

        self._debug_var = tk.BooleanVar(value=bool(current.get("debug_logging", False)))
        ttk.Checkbutton(
            dbg_f, text="SSH Debug Logging aktivieren",
            variable=self._debug_var
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(dbg_f,
            text=f"Log: {LOG_FILE}",
            style="Dim.TLabel", font=("Segoe UI", 8)
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        btn_log_row = ttk.Frame(dbg_f)
        btn_log_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(btn_log_row, text="Log öffnen",
                   command=self._open_log).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_log_row, text="Log leeren",
                   command=self._clear_log,
                   style="Ghost.TButton").pack(side=tk.LEFT)

        # ── Info label: where the ini is saved
        ini_path = SETTINGS_FILE
        ttk.Label(self, text=f"Saved to: {ini_path}", style="Dim.TLabel",
                  font=("Segoe UI", 8)).pack(padx=16, pady=(0, 4), anchor="w")

        # ── Buttons
        btn_row = ttk.Frame(self)
        btn_row.pack(fill=tk.X, padx=16, pady=(4, 16))
        ttk.Button(btn_row, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="Cancel", command=self.destroy,
                   style="Ghost.TButton").pack(side=tk.RIGHT)

        self.update_idletasks()
        # Center over parent
        x = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _toggle_pw(self):
        self._pass_entry.config(show="" if self._show_pw.get() else "●")

    def _open_log(self):
        import subprocess
        if not os.path.exists(LOG_FILE):
            messagebox.showinfo("Debug Log", "Noch keine Log-Datei vorhanden.\n"
                                "Aktiviere Debug Logging und starte einen Scan.", parent=self)
            return
        try:
            if sys.platform == "win32":
                os.startfile(LOG_FILE)
            else:
                subprocess.Popen(["xdg-open", LOG_FILE])
        except Exception as e:
            messagebox.showerror("Fehler", str(e), parent=self)

    def _clear_log(self):
        if not os.path.exists(LOG_FILE):
            return
        if messagebox.askyesno("Log leeren",
                               "Debug-Log wirklich leeren?", parent=self):
            with open(LOG_FILE, "w", encoding="utf-8") as fh:
                fh.write("")
            messagebox.showinfo("Log geleert", "Debug-Log wurde geleert.", parent=self)

    def _save(self):
        d = {
            "username":   self._user_var.get().strip(),
            "password":   self._pass_var.get(),
            "timeout":    self._timeout_var.get(),
            "workers":    self._workers_var.get(),
            "dns_server": self._dns_var.get().strip(),
            "ip_start":   self._ip_start_var.get().strip(),
            "ip_end":     self._ip_end_var.get().strip(),
            "ip_list":    self._ip_list_var.get().strip(),
            "interval":   self._interval_var.get(),
            "list_show":    self._list_show_var.get(),
            "list_sort":    self._list_sort_var.get(),
            "debug_logging": self._debug_var.get(),
            "static_ttl":   self._static_ttl_var.get(),
        }
        if not d["username"]:
            messagebox.showerror("Error", "Username cannot be empty.", parent=self)
            return
        self._on_save(d)
        self.destroy()

class APMonitor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"ALE Stellar – AP Status Monitor  v{APP_VERSION}")
        self.geometry("1200x780")
        self.minsize(900, 600)
        self.configure(bg=C["bg"])

        # Load persisted settings
        self._settings = load_settings()
        self._apply_settings(self._settings)

        # State
        self._ap_data    = {}       # ip -> data dict
        self._selected   = None     # currently selected IP
        self._refresh_id = None     # after() handle
        self._scanning   = False
        self._q          = queue.Queue()

        self._build_styles()
        self._build_ui()
        self._poll_queue()

    def _apply_settings(self, d):
        global SSH_USERNAME, SSH_PASSWORD, SSH_TIMEOUT, MAX_WORKERS, DNS_SERVER, CURRENT_THEME, C
        SSH_USERNAME    = d["username"]
        SSH_PASSWORD    = d["password"]
        SSH_TIMEOUT     = d["timeout"]
        MAX_WORKERS     = d["workers"]
        DNS_SERVER      = d.get("dns_server", "").strip()
        self._list_show = d.get("list_show", "both")
        self._list_sort = d.get("list_sort", "ip")
        set_debug_logging(d.get("debug_logging", False))
        global STATIC_CACHE_TTL
        STATIC_CACHE_TTL = int(d.get("static_ttl", 600))
        if "theme" in d:
            CURRENT_THEME = d["theme"]
            C = get_theme(CURRENT_THEME)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".", background=C["bg"], foreground=C["text"],
                    fieldbackground=C["surface"], bordercolor=C["border"],
                    troughcolor=C["surface2"], selectbackground=C["accent"],
                    selectforeground=C["text_bright"], font=FONT_BODY)

        s.configure("TFrame",       background=C["bg"])
        s.configure("Surface.TFrame", background=C["surface"])
        s.configure("Surface2.TFrame", background=C["surface2"])

        s.configure("TLabel",       background=C["bg"],      foreground=C["text"])
        s.configure("Dim.TLabel",   background=C["bg"],      foreground=C["text_dim"])
        s.configure("Surf.TLabel",  background=C["surface"], foreground=C["text"])
        s.configure("Surf2.TLabel", background=C["surface2"],foreground=C["text"])
        s.configure("Head.TLabel",  background=C["bg"],      foreground=C["text_bright"], font=FONT_HEAD)
        s.configure("Title.TLabel", background=C["bg"],      foreground=C["text_bright"], font=FONT_TITLE)
        s.configure("Ok.TLabel",    background=C["bg"],      foreground=C["ok"])
        s.configure("Err.TLabel",   background=C["bg"],      foreground=C["err"])
        s.configure("Warn.TLabel",  background=C["bg"],      foreground=C["warn"])
        s.configure("Accent.TLabel",background=C["bg"],      foreground=C["accent"])

        s.configure("TButton",
                    background=C["accent"], foreground=C["text_bright"],
                    borderwidth=0, padding=(12, 6), font=FONT_BOLD, relief="flat")
        s.map("TButton",
              background=[("active", "#3a7ae8"), ("disabled", C["surface2"])],
              foreground=[("disabled", C["text_dim"])])

        s.configure("Ghost.TButton",
                    background=C["surface"], foreground=C["text"],
                    borderwidth=1, padding=(10, 5), font=FONT_BODY, relief="flat")
        s.map("Ghost.TButton",
              background=[("active", C["surface2"])])

        s.configure("TEntry",
                    fieldbackground=C["field_bg"], foreground=C["text"],
                    insertcolor=C["text"], bordercolor=C["border"],
                    lightcolor=C["border"], darkcolor=C["border"], padding=5)

        s.configure("TSpinbox",
                    fieldbackground=C["field_bg"], foreground=C["text"],
                    bordercolor=C["border"], arrowcolor=C["text_dim"], padding=4)

        s.configure("TNotebook",
                    background=C["bg"], bordercolor=C["border"], tabmargins=[0,0,0,0])
        s.configure("TNotebook.Tab",
                    background=C["surface"], foreground=C["text_dim"],
                    padding=(16, 8), font=FONT_BODY, borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", C["surface2"]), ("active", C["hover"])],
              foreground=[("selected", C["text_bright"]), ("active", C["text"])])

        s.configure("Treeview",
                    background=C["tree_bg"], foreground=C["text"],
                    fieldbackground=C["tree_bg"], bordercolor=C["border"],
                    rowheight=28, font=FONT_BODY)
        s.configure("Treeview.Heading",
                    background=C["surface2"], foreground=C["text"],
                    borderwidth=0, font=FONT_BOLD, relief="flat")
        s.map("Treeview.Heading",
              background=[("active", C["border"])],
              foreground=[("active", C["text_bright"])])
        s.map("Treeview",
              background=[("selected", C["selected"])],
              foreground=[("selected", C["text_bright"])])

        s.configure("TScrollbar",
                    background=C["surface2"], troughcolor=C["bg"],
                    bordercolor=C["bg"], arrowcolor=C["text_dim"],
                    width=8)

        s.configure("TScale",
                    background=C["bg"], troughcolor=C["surface2"],
                    sliderlength=16, sliderrelief="flat")

        s.configure("TProgressbar",
                    background=C["accent"], troughcolor=C["surface2"],
                    bordercolor=C["bg"], lightcolor=C["accent"], darkcolor=C["accent"])

        s.configure("TLabelframe",
                    background=C["bg"], bordercolor=C["border"], relief="solid")
        s.configure("TLabelframe.Label",
                    background=C["bg"], foreground=C["text_dim"], font=FONT_SMALL)

        s.configure("TRadiobutton",
                    background=C["surface"], foreground=C["text_dim"],
                    indicatorcolor=C["accent"], font=FONT_SMALL)
        s.map("TRadiobutton",
              background=[("active", C["hover"])],
              foreground=[("selected", C["accent"]), ("active", C["text"])])

        # Update root window background
        try:
            self.configure(bg=C["bg"])
        except Exception:
            pass

    # ── UI Builder ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Title bar
        title_bar = ttk.Frame(self)
        title_bar.pack(fill=tk.X, padx=0, pady=0)
        title_bar.configure(style="Surface.TFrame")
        tk.Frame(title_bar, bg=C["accent"], width=4).pack(side=tk.LEFT, fill=tk.Y)
        self._lbl_title_main = ttk.Label(title_bar, text="ALE  OmniAccess Stellar",
                  style="Title.TLabel", background=C["surface"], padding=(16, 10))
        self._lbl_title_main.pack(side=tk.LEFT)
        self._lbl_title_sub = ttk.Label(title_bar, text=f"AP Status Monitor   v{APP_VERSION}",
                  style="Dim.TLabel", background=C["surface"], padding=(0, 10))
        self._lbl_title_sub.pack(side=tk.LEFT)
        ttk.Button(title_bar, text="⚙  Settings", command=self._open_settings,
                   style="Ghost.TButton").pack(side=tk.RIGHT, padx=(4,12), pady=6)
        ttk.Button(title_bar, text="ℹ  About", command=self._show_about,
                   style="Ghost.TButton").pack(side=tk.RIGHT, padx=(0, 0), pady=6)

        # Theme switcher
        self._theme_var = tk.StringVar(value=CURRENT_THEME)
        theme_frame = ttk.Frame(title_bar, style="Surface.TFrame")
        theme_frame.pack(side=tk.RIGHT, padx=(0, 4), pady=6)
        for theme_name, label in [("dark", "Dark"), ("light", "Light")]:
            ttk.Radiobutton(
                theme_frame, text=label, value=theme_name,
                variable=self._theme_var, command=self._apply_theme,
            ).pack(side=tk.LEFT, padx=2)

        # ── Control bar
        ctrl = ttk.Frame(self, style="Surface2.TFrame")
        ctrl.pack(fill=tk.X, padx=0, pady=(1, 0))

        ttk.Label(ctrl, text="IP Range:", style="Surf2.TLabel", padding=(12, 8)).pack(side=tk.LEFT)
        self._e_start = ttk.Entry(ctrl, width=15)
        self._e_start.insert(0, "192.168.85.1")
        self._e_start.pack(side=tk.LEFT, padx=(0, 4), pady=6)
        ttk.Label(ctrl, text="→", style="Surf2.TLabel").pack(side=tk.LEFT)
        self._e_end = ttk.Entry(ctrl, width=15)
        self._e_end.insert(0, "192.168.85.10")
        self._e_end.pack(side=tk.LEFT, padx=(4, 12), pady=6)

        ttk.Label(ctrl, text="  |  IP List:", style="Surf2.TLabel").pack(side=tk.LEFT)
        self._e_list = ttk.Entry(ctrl, width=30)
        self._e_list.pack(side=tk.LEFT, padx=(0, 12), pady=6)

        sep = tk.Frame(ctrl, bg=C["border"], width=1)
        sep.pack(side=tk.LEFT, fill=tk.Y, pady=4, padx=4)

        ttk.Label(ctrl, text="  Refresh:", style="Surf2.TLabel").pack(side=tk.LEFT)
        self._interval_var = tk.IntVar(value=60)
        spin = ttk.Spinbox(ctrl, from_=10, to=600, increment=10,
                           textvariable=self._interval_var, width=5)
        spin.pack(side=tk.LEFT, padx=(4, 2), pady=6)
        ttk.Label(ctrl, text="s", style="Surf2.TLabel").pack(side=tk.LEFT, padx=(0, 8))

        self._btn_scan    = ttk.Button(ctrl, text="▶  Scan",    command=self._start_scan)
        self._btn_refresh = ttk.Button(ctrl, text="↻  Refresh", command=self._refresh_selected,
                                       style="Ghost.TButton")
        self._btn_stop    = ttk.Button(ctrl, text="■  Stop",    command=self._stop_auto,
                                       style="Ghost.TButton", state=tk.DISABLED)
        self._btn_scan.pack(side=tk.LEFT, padx=(0, 4), pady=6)
        self._btn_refresh.pack(side=tk.LEFT, padx=(0, 4), pady=6)
        self._btn_stop.pack(side=tk.LEFT, padx=(0, 12), pady=6)

        # Auto-refresh countdown
        self._countdown_var = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self._countdown_var, style="Surf2.TLabel",
                  foreground=C["text_dim"]).pack(side=tk.LEFT)

        # ── Main area
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=0, pady=(1, 0))

        # ── Left: AP list
        left = ttk.Frame(main, style="Surface.TFrame", width=260)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=0, pady=0)
        left.pack_propagate(False)

        ttk.Label(left, text="ACCESS POINTS", style="Surf.TLabel",
                  foreground=C["text_dim"], font=("Segoe UI", 8, "bold"),
                  padding=(12, 10, 12, 6)).pack(fill=tk.X)

        # Summary bar — pack BEFORE listbox so it stays at bottom without gap
        self._summary_var = tk.StringVar(value="No APs scanned")
        ttk.Label(left, textvariable=self._summary_var, style="Surf.TLabel",
                  foreground=C["text_dim"], font=("Segoe UI", 8),
                  padding=(6, 3)).pack(fill=tk.X, side=tk.BOTTOM)

        ap_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL)
        ap_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._ap_listbox = tk.Listbox(
            left,
            yscrollcommand=ap_scroll.set,
            bg=C["surface"], fg=C["text"], selectbackground=C["selected"],
            selectforeground=C["text_bright"], activestyle="none",
            borderwidth=0, highlightthickness=0, relief="flat",
            font=("Segoe UI", 9), cursor="hand2",
        )
        ap_scroll.config(command=self._ap_listbox.yview)
        self._ap_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._ap_listbox.bind("<<ListboxSelect>>", self._on_ap_select)

        # Draggable divider
        divider = tk.Frame(main, bg=C["border"], width=4, cursor="sb_h_double_arrow")
        divider.pack(side=tk.LEFT, fill=tk.Y)

        def _on_drag(event):
            new_w = max(160, min(500, divider.winfo_x() + event.x))
            left.configure(width=new_w)
        divider.bind("<B1-Motion>", _on_drag)

        # ── Right: detail area
        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # AP header in detail area
        self._detail_header = ttk.Frame(right, style="Surface2.TFrame")
        self._detail_header.pack(fill=tk.X)
        self._lbl_ap_ip     = ttk.Label(self._detail_header, text="—",
                                         style="Title.TLabel", background=C["surface2"],
                                         padding=(16, 10, 8, 10))
        self._lbl_ap_ip.pack(side=tk.LEFT)
        self._lbl_ap_model  = ttk.Label(self._detail_header, text="",
                                         style="Dim.TLabel", background=C["surface2"],
                                         padding=(0, 10))
        self._lbl_ap_model.pack(side=tk.LEFT)
        self._lbl_ap_location = ttk.Label(self._detail_header, text="",
                                           style="Dim.TLabel", background=C["surface2"],
                                           foreground=C["accent"], padding=(0, 10))
        self._lbl_ap_location.pack(side=tk.LEFT)
        self._lbl_ap_status = ttk.Label(self._detail_header, text="",
                                         background=C["surface2"],
                                         padding=(12, 10), font=FONT_BOLD)
        self._lbl_ap_status.pack(side=tk.RIGHT)

        tk.Frame(right, bg=C["border"], height=1).pack(fill=tk.X)

        # Notebook (tabs)
        self._nb = ttk.Notebook(right)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._tab_system   = self._make_tab("System")
        self._tab_client_list = self._make_tab("Client List")
        self._tab_wireless = self._make_tab("Wireless")
        self._tab_network  = self._make_tab("Network")
        self._tab_mesh     = self._make_tab("MESH")
        self._tab_topo     = self._make_tab("Topology")

        self._nb.add(self._tab_system,      text="  System  ")
        self._nb.add(self._tab_client_list, text="  Client List  ")
        self._nb.add(self._tab_wireless,    text="  Wireless  ")
        self._nb.add(self._tab_network,     text="  Network  ")
        self._nb.add(self._tab_mesh,        text="  MESH  ")
        self._nb.add(self._tab_topo,        text="  Topology  ")

        self._build_tab_system()
        self._build_tab_client_list()
        self._build_tab_wireless()
        self._build_tab_network()
        self._build_tab_mesh()
        self._build_tab_topo()

        # Sync theme radio to loaded setting
        if hasattr(self, "_theme_var"):
            self._theme_var.set(CURRENT_THEME)

        # Populate fields from saved settings
        self._e_start.delete(0, tk.END)
        self._e_start.insert(0, self._settings.get("ip_start", ""))
        self._e_end.delete(0, tk.END)
        self._e_end.insert(0, self._settings.get("ip_end", ""))
        self._e_list.delete(0, tk.END)
        self._e_list.insert(0, self._settings.get("ip_list", ""))
        self._interval_var.set(self._settings.get("interval", 60))

        # ── Status bar
        status_bar = ttk.Frame(self, style="Surface.TFrame")
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(status_bar, bg=C["accent2"], width=4).pack(side=tk.LEFT, fill=tk.Y)
        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(status_bar, textvariable=self._status_var,
                  style="Surf.TLabel", foreground=C["text_dim"],
                  font=FONT_SMALL, padding=(10, 5)).pack(side=tk.LEFT)
        self._progress = ttk.Progressbar(status_bar, mode="determinate", length=150)
        self._progress.pack(side=tk.RIGHT, padx=10, pady=5)

    def _make_tab(self, name):
        f = ttk.Frame(self._nb)
        f.configure(style="TFrame")
        return f

    # ── TreeView context menu (copy) ──────────────────────────────────────

    def _attach_tree_context_menu(self, tree):
        """
        Bind a right-click context menu to a Treeview.
        Offers: copy clicked cell | copy full row | copy all rows as TSV.
        """
        menu = tk.Menu(tree, tearoff=0)
        ctx  = {"row": None, "col_idx": None}

        def _identify_cell(event):
            iid    = tree.identify_row(event.y)
            col_id = tree.identify_column(event.x)
            if not iid or not col_id:
                return None, None
            try:
                col_idx = int(col_id.lstrip("#")) - 1
            except ValueError:
                return iid, None
            return iid, col_idx

        def _copy_cell():
            iid, col_idx = ctx["row"], ctx["col_idx"]
            if iid is None or col_idx is None:
                return
            vals = tree.item(iid, "values")
            if col_idx < len(vals):
                self._clipboard(str(vals[col_idx]))

        def _copy_row():
            iid = ctx["row"]
            if not iid:
                return
            cols = tree["columns"]
            vals = tree.item(iid, "values")
            hdrs = [tree.heading(c)["text"] for c in cols]
            text = "\t".join(
                f"{h}: {v}" for h, v in zip(hdrs, vals)
                if str(v) not in ("—", "", "None")
            )
            self._clipboard(text)

        def _copy_all():
            cols  = tree["columns"]
            hdrs  = [tree.heading(c)["text"] for c in cols]
            lines = ["\t".join(hdrs)]
            for iid in tree.get_children():
                vals = tree.item(iid, "values")
                lines.append("\t".join(str(v) for v in vals))
            self._clipboard("\n".join(lines))

        def _on_right_click(event):
            iid, col_idx   = _identify_cell(event)
            ctx["row"]     = iid
            ctx["col_idx"] = col_idx
            if iid:
                tree.selection_set(iid)
            # Dynamic cell preview in menu label
            if iid and col_idx is not None:
                vals     = tree.item(iid, "values")
                cell_val = str(vals[col_idx]) if col_idx < len(vals) else ""
                preview  = (cell_val[:28] + "…") if len(cell_val) > 28 else cell_val
                menu.entryconfig(0, label=f"Zelle kopieren: „{preview}“")
            else:
                menu.entryconfig(0, label="Zelle kopieren")
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        menu.add_command(label="Zelle kopieren",          command=_copy_cell)
        menu.add_command(label="Zeile kopieren",          command=_copy_row)
        menu.add_separator()
        menu.add_command(label="Alle kopieren (TSV)", command=_copy_all)
        tree.bind("<Button-3>", _on_right_click)

    def _clipboard(self, text: str):
        """Write text to the system clipboard."""
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()

    # ── Tab: System ───────────────────────────────────────────────────────────

    def _build_tab_system(self):
        p = self._tab_system
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=1)

        # Left column: identity
        lf = ttk.LabelFrame(p, text="Identity", padding=12)
        lf.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        self._sys_fields = {}
        for row, (label, key) in enumerate([
            ("AP Name",      "ap_name"),
            ("Location",     "location"),
            ("Model",        "model"),
            ("Serial",       "serial"),
            ("MAC Address",  "mac"),
            ("Firmware",     "firmware"),
            ("Country",      "country"),
            ("Deploy Mode",  "mode"),
            ("Cluster Role", "cluster_role"),
            ("Cluster ID",   "cluster_id"),
            ("Date / Time",  "date"),
        ]):
            ttk.Label(lf, text=label, style="Dim.TLabel").grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 16))
            var = tk.StringVar(value="—")
            ttk.Label(lf, textvariable=var, style="TLabel").grid(
                row=row, column=1, sticky="w", pady=3)
            self._sys_fields[key] = var

        # Right column: resources
        rf = ttk.LabelFrame(p, text="Resources", padding=12)
        rf.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)

        ttk.Label(rf, text="Uptime", style="Dim.TLabel").grid(row=0, column=0, sticky="w", pady=3, padx=(0,16))
        self._uptime_var = tk.StringVar(value="—")
        ttk.Label(rf, textvariable=self._uptime_var).grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(rf, text="Load (1/5/15 min)", style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self._load_var = tk.StringVar(value="—")
        ttk.Label(rf, textvariable=self._load_var).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(rf, text="Memory", style="Dim.TLabel").grid(row=2, column=0, sticky="w", pady=(12,3))
        self._mem_lbl = ttk.Label(rf, text="—")
        self._mem_lbl.grid(row=2, column=1, sticky="w", pady=(12,3))

        self._mem_bar = ttk.Progressbar(rf, mode="determinate", length=180)
        self._mem_bar.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(rf, text="Used / Total", style="Dim.TLabel").grid(row=4, column=0, sticky="w", pady=3)
        self._mem_detail = tk.StringVar(value="—")
        ttk.Label(rf, textvariable=self._mem_detail).grid(row=4, column=1, sticky="w", pady=3)

        ttk.Label(rf, text="Clients connected", style="Dim.TLabel").grid(row=5, column=0, sticky="w", pady=(12,3))
        self._client_count_var = tk.StringVar(value="—")
        ttk.Label(rf, textvariable=self._client_count_var,
                  foreground=C["ok"], font=FONT_BOLD).grid(row=5, column=1, sticky="w", pady=(12,3))

    # ── Tab: Clients ──────────────────────────────────────────────────────────

    def _build_tab_client_list(self):
        """Single unified client list — identity + radio + health in one table."""
        p = self._tab_client_list

        cols = (
            "hostname", "mac", "ip", "ssid",
            "band", "snr", "txrate", "rxrate", "mode",
            "auth", "assoc",
            "rx_mb", "tx_mb",
            "score", "health",
        )
        hdrs = (
            "Hostname", "MAC", "IP", "SSID",
            "Band", "SNR", "TX Rate", "RX Rate", "Mode",
            "Auth", "Assoc Time",
            "RX (MB)", "TX (MB)",
            "Score", "Health",
        )
        widths = (
            175, 145, 115, 135,
            65,  70,  75,  75,  165,
            80,  90,
            75,  75,
            65,  105,
        )

        frame = ttk.Frame(p)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        self._client_count_lbl = ttk.Label(frame, text="0 clients", style="Dim.TLabel")
        self._client_count_lbl.pack(anchor="w", pady=(0, 6))

        sb_y = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        sb_x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL)
        self._client_tree = ttk.Treeview(
            frame, columns=cols, show="headings",
            yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.config(command=self._client_tree.yview)
        sb_x.config(command=self._client_tree.xview)

        for col, hdr, w in zip(cols, hdrs, widths):
            self._client_tree.heading(col, text=hdr)
            self._client_tree.column(col, width=w, minwidth=40, anchor="w")

        # Health cell tags — color only the "health" column text, not the whole row.
        # Achieved via tag on foreground only (no background override).
        self._client_tree.tag_configure("h_healthy",  foreground="#4cdb8a")
        self._client_tree.tag_configure("h_warning",  foreground="#f0c040")
        self._client_tree.tag_configure("h_critical", foreground="#e05050")

        sb_y.pack(side=tk.RIGHT, fill=tk.Y)
        self._client_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        sb_x.pack(fill=tk.X)

        self._client_tree.bind("<Double-Button-1>", self._on_client_list_dblclick)
        self._attach_tree_context_menu(self._client_tree)


    # ── Tab: Wireless ─────────────────────────────────────────────────────────

    def _build_tab_wireless(self):
        p = self._tab_wireless
        cols   = ("iface", "essid", "std", "channel", "bitrate", "txpower", "quality", "signal", "noise")
        hdrs   = ("Interface", "SSID", "Standard", "Ch", "Bit Rate", "TX Power", "Quality", "Signal", "Noise")
        widths = (100, 160, 100, 45, 110, 95, 75, 90, 90)

        frame = ttk.Frame(p)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        sb_y = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        sb_x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL)
        self._wireless_tree = ttk.Treeview(frame, columns=cols, show="headings",
                                            yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.config(command=self._wireless_tree.yview)
        sb_x.config(command=self._wireless_tree.xview)

        for col, hdr, w in zip(cols, hdrs, widths):
            self._wireless_tree.heading(col, text=hdr)
            self._wireless_tree.column(col, width=w, minwidth=50)

        sb_y.pack(side=tk.RIGHT, fill=tk.Y)
        self._wireless_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        sb_x.pack(fill=tk.X)

    # ── Tab: Network ──────────────────────────────────────────────────────────

    def _build_tab_network(self):
        p = self._tab_network
        p.columnconfigure(0, weight=1)

        # Interface info
        iface_f = ttk.LabelFrame(p, text="LAN Interface  (br-wan)", padding=12)
        iface_f.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        iface_f.columnconfigure(1, weight=1)
        iface_f.columnconfigure(3, weight=1)

        self._net_fields = {}
        fields_left  = [("IP Address", "ip"), ("Subnet Mask", "mask"), ("MAC", "mac")]
        fields_right = [("RX Traffic", "rx"), ("TX Traffic", "tx"), ("DNS Server", "dns")]

        for row, (lbl, key) in enumerate(fields_left):
            ttk.Label(iface_f, text=lbl, style="Dim.TLabel").grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 16))
            var = tk.StringVar(value="—")
            ttk.Label(iface_f, textvariable=var).grid(row=row, column=1, sticky="w", pady=4)
            self._net_fields[key] = var

        for row, (lbl, key) in enumerate(fields_right):
            ttk.Label(iface_f, text=lbl, style="Dim.TLabel").grid(
                row=row, column=2, sticky="w", pady=4, padx=(32, 16))
            var = tk.StringVar(value="—")
            ttk.Label(iface_f, textvariable=var).grid(row=row, column=3, sticky="w", pady=4)
            self._net_fields[key] = var

        # Routes
        route_f = ttk.LabelFrame(p, text="Routing Table", padding=12)
        route_f.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        p.rowconfigure(1, weight=1)

        cols = ("dest", "gateway", "mask", "iface")
        hdrs = ("Destination", "Gateway", "Mask", "Interface")
        widths = (150, 150, 150, 100)

        sb = ttk.Scrollbar(route_f, orient=tk.VERTICAL)
        self._route_tree = ttk.Treeview(route_f, columns=cols, show="headings",
                                         height=6, yscrollcommand=sb.set)
        sb.config(command=self._route_tree.yview)
        for col, hdr, w in zip(cols, hdrs, widths):
            self._route_tree.heading(col, text=hdr)
            self._route_tree.column(col, width=w)
        self._route_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── AP list helpers ───────────────────────────────────────────────────────

    STATUS_ICONS = {"ok": "●", "error": "●", "scanning": "○", "pending": "○"}
    STATUS_COLORS = {"ok": C["ok"], "error": C["err"], "scanning": C["warn"], "pending": C["text_dim"]}

    def _sorted_ips(self):
        """Return IPs sorted by current list_sort setting."""
        def key(ip):
            name = (self._ap_data.get(ip) or {}).get("ap_name", "") or                    ((self._ap_data.get(ip) or {}).get("system") or {}).get("ap_name", "")
            if self._list_sort == "name" and name:
                return name.lower()
            try:
                return ".".join(f"{int(x):03d}" for x in ip.split("."))
            except Exception:
                return ip
        return sorted(self._ap_data.keys(), key=key)

    def _refresh_ap_list(self):
        sel_ip  = self._selected
        ips     = self._sorted_ips()
        show    = self._list_show

        self._ap_listbox.delete(0, tk.END)
        for ip in ips:
            d      = self._ap_data[ip]
            status = d.get("status", "pending")
            icon   = self.STATUS_ICONS.get(status, "○")
            count  = d.get("client_count", "")
            count_s = f" {count}👤" if isinstance(count, int) else ""
            name   = d.get("ap_name", "") or (d.get("system") or {}).get("ap_name", "")

            if show == "name":
                label = f"  {icon}  {name or ip}{count_s}"
            elif show == "ip":
                label = f"  {icon}  {ip}{count_s}"
            else:  # both — name first
                label = f"  {icon}  {name}  {ip}{count_s}" if name else f"  {icon}  {ip}{count_s}"

            self._ap_listbox.insert(tk.END, label)
            self._ap_listbox.itemconfig(tk.END, fg=self.STATUS_COLORS.get(status, C["text_dim"]))

        # Re-select
        if sel_ip and sel_ip in self._ap_data:
            try:
                self._ap_listbox.selection_set(ips.index(sel_ip))
            except ValueError:
                pass

        ok    = sum(1 for d in self._ap_data.values() if d.get("status") == "ok")
        err   = sum(1 for d in self._ap_data.values() if d.get("status") == "error")
        total = len(self._ap_data)
        self._summary_var.set(f"{total} APs   ✓{ok}  ✗{err}")

    def _on_ap_select(self, _event=None):
        sel = self._ap_listbox.curselection()
        if not sel:
            return
        ips = self._sorted_ips()
        if sel[0] < len(ips):
            self._selected = ips[sel[0]]
            self._render_detail(self._selected)

    # ── Detail rendering ──────────────────────────────────────────────────────

    def _render_detail(self, ip):
        d = self._ap_data.get(ip, {})
        status = d.get("status", "pending")

        # Header
        self._lbl_ap_ip.config(text=ip)
        sys      = d.get("system", {})
        model    = sys.get("model", "")
        ap_name  = d.get("ap_name", "") or sys.get("ap_name", "")
        location = d.get("location", "") or sys.get("location", "")
        model_str = "  " + "  |  ".join(filter(None, [ap_name, model]))
        self._lbl_ap_model.config(text=model_str)
        self._lbl_ap_location.config(
            text=f"  📍 {location}" if location and location != "—" else ""
        )
        if status == "ok":
            self._lbl_ap_status.config(text="● Online", foreground=C["ok"])
        elif status == "error":
            err = d.get("error", "")
            self._lbl_ap_status.config(text=f"● Offline  {err[:60]}", foreground=C["err"])
        else:
            self._lbl_ap_status.config(text="○ Scanning…", foreground=C["warn"])

        if status != "ok":
            self._clear_detail()
            return

        # ── System tab
        for key, var in self._sys_fields.items():
            var.set(sys.get(key, "—") or "—")
        self._uptime_var.set(sys.get("uptime", "—"))
        self._load_var.set(
            f"{sys.get('load1','?')}  /  {sys.get('load5','?')}  /  {sys.get('load15','?')}")

        mem_pct = sys.get("mem_pct", 0)
        mem_used  = sys.get("mem_used", 0)
        mem_total = sys.get("mem_total", 0)
        self._mem_bar["value"] = mem_pct
        color = C["err"] if mem_pct > 80 else C["warn"] if mem_pct > 60 else C["ok"]
        self._mem_lbl.config(text=f"{mem_pct} %", foreground=color)
        used_kb  = f"{mem_used} kB"
        total_kb = f"{mem_total} kB"
        self._mem_detail.set(f"{used_kb}  /  {total_kb}")

        cc = d.get("client_count", 0)
        self._client_count_var.set(str(cc))

        # ── Client List tab (unified)
        for row in self._client_tree.get_children():
            self._client_tree.delete(row)
        clients = d.get("clients", [])
        self._client_list_clients = clients
        n = len(clients)
        self._client_count_lbl.config(
            text=f"{n} client{'s' if n != 1 else ''} connected")

        for c in clients:
            snr_val   = c.get("snr") or c.get("snr_raw")
            snr_str   = f"{snr_val} dB" if snr_val is not None else "—"
            health    = c.get("health_status") or "—"
            score_v   = c.get("health_score", 0) or 0
            score_str = f"{score_v}/100" if score_v else "—"
            band_str  = c.get("band") or c.get("freq") or "—"
            mode_str  = c.get("mode") or "—"

            # Tag drives only foreground color on the health cell text
            if score_v >= 80:
                tag = "h_healthy"
            elif score_v >= 50:
                tag = "h_warning"
            else:
                tag = "h_critical"

            iid = self._client_tree.insert("", tk.END, values=(
                c.get("hostname") or c.get("mac", "—"),
                c.get("mac", "—"),
                c.get("ip",  "—"),
                c.get("ssid","—"),
                band_str,
                snr_str,
                c.get("txrate","—"),
                c.get("rxrate","—"),
                mode_str,
                c.get("auth","—"),
                fmt_assoctime(c.get("assoctime")),
                fmt_mb(c.get("rx","—")),
                fmt_mb(c.get("tx","—")),
                score_str,
                health,
            ), tags=(tag,))

        # ── Wireless tab
        for row in self._wireless_tree.get_children():
            self._wireless_tree.delete(row)
        for iface in d.get("wireless", []):
            # TX power: prefer live value from iwlist, fall back to iwconfig value
            txpow = iface.get("txpower_cur") or iface.get("txpower", "—")
            self._wireless_tree.insert("", tk.END, values=(
                iface.get("iface","—"), iface.get("essid","—"), iface.get("std","—"),
                iface.get("channel","—"), iface.get("bitrate","—"), txpow,
                iface.get("quality","—"), iface.get("signal","—"), iface.get("noise","—"),
            ))

        # ── Network tab
        net = d.get("network", {})
        self._net_fields["ip"].set(net.get("ip", "—"))
        self._net_fields["mask"].set(net.get("mask", "—"))
        self._net_fields["mac"].set(net.get("mac", "—"))
        self._net_fields["rx"].set(fmt_bytes(net.get("rx_bytes")))
        self._net_fields["tx"].set(fmt_bytes(net.get("tx_bytes")))
        self._net_fields["dns"].set(net.get("dns", "—"))

        for row in self._route_tree.get_children():
            self._route_tree.delete(row)
        for r in net.get("routes", []):
            self._route_tree.insert("", tk.END, values=(
                r.get("dest","—"), r.get("gateway","—"),
                r.get("mask","—"), r.get("iface","—"),
            ))

        # ── MESH tab
        mesh = d.get("mesh", {})
        self._mesh_fields["mesh_ap_name"].set(d.get("ap_name", "—") or "—")
        self._mesh_fields["mesh_location"].set(d.get("location", "—") or "—")
        self._mesh_fields["mesh_role"].set(sys.get("cluster_role", "—") or "—")
        self._mesh_fields["mesh_cluster_id"].set(sys.get("cluster_id", "—") or "—")

        for row in self._neighbor_tree.get_children():
            self._neighbor_tree.delete(row)
        for nb in mesh.get("neighbors", []):
            rssi_2g_raw = nb.get("rssi_2g", "—")
            rssi_5g_raw = nb.get("rssi_5g", "—")
            nb_ip       = nb.get("ip", "")
            # Look up location from already-scanned AP data
            nb_loc = "—"
            if nb_ip and nb_ip in self._ap_data:
                nb_d   = self._ap_data[nb_ip]
                nb_loc = nb_d.get("location", "") or                          (nb_d.get("system") or {}).get("location", "") or "—"
            self._neighbor_tree.insert("", tk.END, values=(
                nb.get("name","—"), nb_ip or "—", nb_loc,
                nb.get("state","—"),
                fmt_rssi(rssi_2g_raw),
                fmt_rssi(rssi_5g_raw),
                nb.get("version","—"),
            ))
            item = self._neighbor_tree.get_children()[-1]
            # Color by best available RSSI
            best = rssi_5g_raw if rssi_5g_raw != "—" else rssi_2g_raw
            self._neighbor_tree.item(item, tags=(rssi_tag(best),))

        self._neighbor_tree.tag_configure("rssi_ok",   foreground=C["ok"])
        self._neighbor_tree.tag_configure("rssi_warn", foreground=C["warn"])
        self._neighbor_tree.tag_configure("rssi_bad",  foreground=C["err"])

        # ── Topology canvas
        ap_name = d.get("ap_name", "") or sys.get("ap_name", "This AP")
        self._topo_data = (ap_name, ip, mesh.get("neighbors", []))
        self._redraw_topology()

    def _clear_detail(self):
        for var in self._sys_fields.values():
            var.set("—")
        self._uptime_var.set("—")
        self._load_var.set("—")
        self._mem_bar["value"] = 0
        self._mem_lbl.config(text="—")
        self._mem_detail.set("—")
        self._client_count_var.set("—")
        for tree in (self._client_tree, self._wireless_tree, self._route_tree):
            for row in tree.get_children():
                tree.delete(row)
        for var in self._net_fields.values():
            var.set("—")
        for row in self._neighbor_tree.get_children():
            self._neighbor_tree.delete(row)

    # ── Tab: Client Troubleshooting ───────────────────────────────────────────


    def _on_client_list_dblclick(self, event):
        sel = self._client_tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not hasattr(self, "_client_list_clients"):
            return
        idx = self._client_tree.index(iid)
        if idx < len(self._client_list_clients):
            self._show_client_details(self._client_list_clients[idx])

    def _show_client_details(self, client):
        """Popup with full radio detail and health score breakdown."""
        win = tk.Toplevel(self)
        win.title("Client Detail")
        win.configure(bg=C["bg"])
        win.resizable(False, False)
        win.grab_set()

        # ── Helper: simple label row ──────────────────────────────────────────
        def info_row(parent, lbl, val, color=None):
            """Label + read-only Entry: selectable, Ctrl+C and right-click to copy."""
            f = ttk.Frame(parent)
            f.pack(fill=tk.X, padx=0, pady=2)
            ttk.Label(f, text=lbl, style="Dim.TLabel",
                      width=17, anchor="w").pack(side=tk.LEFT)
            text_val = str(val or "—")
            var = tk.StringVar(value=text_val)
            e = tk.Entry(
                f, textvariable=var,
                state="readonly",
                readonlybackground=C["bg"],
                fg=color if color else C["text"],
                relief=tk.FLAT, bd=0,
                highlightthickness=0,
                font=("Segoe UI", 10),
                width=max(10, len(text_val) + 2),
            )
            e.pack(side=tk.LEFT)
            ctx_m = tk.Menu(e, tearoff=0)
            ctx_m.add_command(label="Kopieren",
                              command=lambda v=var: self._clipboard(v.get()))
            e.bind("<Button-3>",
                   lambda ev, m=ctx_m: m.tk_popup(ev.x_root, ev.y_root))

        # ── Title ─────────────────────────────────────────────────────────────
        hostname = client.get("hostname") or client.get("mac", "—")
        ttk.Label(win, text=hostname, style="Title.TLabel",
                  padding=(20, 14, 20, 4)).pack(anchor="w")

        # ── Two-column layout ──────────────────────────────────────────────────────────────────
        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, padx=16, pady=(0, 8))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        # Left: Identity — all sta_list fields
        id_f = ttk.LabelFrame(body, text="Identity", padding=10)
        id_f.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=4)
        for lbl, val in [
            ("Hostname",   client.get("hostname") or "—"),
            ("MAC",        client.get("mac",  "—")),
            ("IP",         client.get("ip",   "—")),
            ("SSID",       client.get("ssid", "—")),
            ("Freq",       client.get("freq", "—")),
            ("Auth",       client.get("auth", "—")),
            ("Role",       client.get("role", "—")),
            ("Online",     fmt_online(client.get("online", "—"))),
            ("Assoc Time", fmt_assoctime(client.get("assoctime"))),
            ("RX",         fmt_mb(client.get("rx", "—"))),
            ("TX",         fmt_mb(client.get("tx", "—"))),
        ]:
            info_row(id_f, lbl, val)

        # Right: Radio — wlanconfig data
        radio_f = ttk.LabelFrame(body, text="Radio", padding=10)
        radio_f.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=4)

        snr_val   = client.get("snr") or client.get("snr_raw")
        snr_str   = f"{snr_val} dB" if snr_val is not None else "—"
        snr_color = (C["ok"]   if snr_val is not None and int(snr_val) > 30
                     else C["warn"] if snr_val is not None and int(snr_val) > 15
                     else C["err"])
        for lbl, val, col in [
            ("SNR",     snr_str,                       snr_color),
            ("Band",    client.get("band",    "—"),   None),
            ("TX Rate", client.get("txrate",  "—"),   None),
            ("RX Rate", client.get("rxrate",  "—"),   None),
            ("Mode",    client.get("mode",    "—"),   None),
        ]:
            info_row(radio_f, lbl, val, col)

                # ── Health score breakdown ────────────────────────────────────────────
        health   = client.get("health_status", "—")
        score    = client.get("health_score",  0)
        brkdown  = client.get("health_breakdown", [])

        hcolor = (C["ok"]  if "Healthy" in str(health)
             else C["warn"] if "Warning" in str(health)
             else C["err"])

        score_f = ttk.LabelFrame(body, text="Health Score Breakdown", padding=10)
        score_f.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 4))
        score_f.columnconfigure(3, weight=1)

        # Overall score header
        hdr = ttk.Frame(score_f)
        hdr.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(hdr, text=health, foreground=hcolor, font=FONT_BOLD).pack(side=tk.LEFT)
        ttk.Label(hdr, text=f"   Total: {score} / 100",
                  style="Dim.TLabel").pack(side=tk.LEFT, padx=(12, 0))

        # Factor rows: icon | label | bar | pts/max | note
        BAR_W = 140
        for item in brkdown:
            ok_flag = item["ok"]
            icon  = "✔" if ok_flag else "✘"
            icol  = C["ok"] if ok_flag else C["err"]
            pct   = item["points"] / item["max"] if item["max"] else 0
            bar_c = C["ok"] if pct >= 0.7 else C["warn"] if pct >= 0.4 else C["err"]

            row_f = ttk.Frame(score_f)
            row_f.pack(fill=tk.X, pady=3)

            ttk.Label(row_f, text=icon, foreground=icol,
                      width=2, font=FONT_BOLD).pack(side=tk.LEFT)
            ttk.Label(row_f, text=item["label"],
                      width=9, anchor="w").pack(side=tk.LEFT, padx=(4, 8))

            # Mini canvas bar
            bar_bg = tk.Canvas(row_f, width=BAR_W, height=14,
                               bg=C["surface2"], highlightthickness=0)
            bar_bg.pack(side=tk.LEFT)
            fill_w = int(BAR_W * pct)
            if fill_w > 0:
                bar_bg.create_rectangle(0, 0, fill_w, 14, fill=bar_c, outline="")
            bar_bg.create_text(BAR_W // 2, 7,
                               text=f"{item['points']}/{item['max']}",
                               fill=C["text_bright"], font=("Segoe UI", 8))

            ttk.Label(row_f, text=f"  {item['note']}",
                      style="Dim.TLabel", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(8, 0))

        # ── Close button ──────────────────────────────────────────────────────
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(4, 14))

        win.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - win.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    # ── Tab: MESH ─────────────────────────────────────────────────────────────

    def _build_tab_mesh(self):
        p = self._tab_mesh
        p.columnconfigure(0, weight=1)

        self._mesh_fields  = {}
        self._txpower_tree = None

        # ── This AP info
        info_f = ttk.LabelFrame(p, text="This AP", padding=12)
        info_f.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        info_f.columnconfigure(1, weight=1)
        info_f.columnconfigure(3, weight=1)

        for col_offset, fields in enumerate([
            [("AP Name", "mesh_ap_name"), ("Location", "mesh_location")],
            [("Cluster Role", "mesh_role"),  ("Cluster ID", "mesh_cluster_id")],
        ]):
            for row, (lbl, key) in enumerate(fields):
                ttk.Label(info_f, text=lbl, style="Dim.TLabel").grid(
                    row=row, column=col_offset*2, sticky="w", pady=3,
                    padx=(0 if col_offset==0 else 24, 12))
                var = tk.StringVar(value="—")
                ttk.Label(info_f, textvariable=var).grid(
                    row=row, column=col_offset*2+1, sticky="w", pady=3)
                self._mesh_fields[key] = var

        # ── Neighbor table
        neighbor_f = ttk.LabelFrame(p, text="Neighbor APs  (adme show)", padding=12)
        neighbor_f.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        p.rowconfigure(1, weight=1)

        cols   = ("name",  "ip",  "location", "state",  "rssi_2g",   "rssi_5g", "version")
        hdrs   = ("AP Name","IP", "Location", "State",  "RSSI 2.4G", "RSSI 5G", "Version")
        widths = (110, 120, 130, 90, 135, 135, 90)

        sb_n = ttk.Scrollbar(neighbor_f, orient=tk.VERTICAL)
        self._neighbor_tree = ttk.Treeview(
            neighbor_f, columns=cols, show="headings",
            yscrollcommand=sb_n.set)
        sb_n.config(command=self._neighbor_tree.yview)
        for col, hdr, w in zip(cols, hdrs, widths):
            self._neighbor_tree.heading(col, text=hdr)
            self._neighbor_tree.column(col, width=w)
        self._neighbor_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_n.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_tab_topo(self):
        p = self._tab_topo
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        # ── Toolbar: band selector
        ctrl = ttk.Frame(p)
        ctrl.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))

        ttk.Label(ctrl, text="Band:", style="Dim.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        self._topo_band = tk.StringVar(value="5g")
        for val, lbl in [("5g", "5 GHz"), ("2g", "2.4 GHz")]:
            ttk.Radiobutton(ctrl, text=lbl, value=val, variable=self._topo_band,
                            command=self._redraw_topology).pack(side=tk.LEFT, padx=(0, 8))

        # Legend
        for clr, lbl, dsh in [
            (C["ok"],       "Perfect  (≥ −67 dBm)", False),
            (C["warn"],     "OK  (−75 to −68 dBm)", False),
            (C["err"],      "Bad  (≤ −76 dBm)",     False),
            (C["text_dim"], "Offline",                        True),
        ]:
            dot = tk.Canvas(ctrl, width=22, height=8, bg=C["bg"],
                            highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=(14, 2))
            dot.create_line(0, 4, 22, 4, fill=clr, width=2,
                            dash=(4, 3) if dsh else ())
            ttk.Label(ctrl, text=lbl, style="Dim.TLabel",
                      font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 8))

        # ── Canvas
        self._topo_canvas = tk.Canvas(
            p, bg=C["surface"], highlightthickness=0, bd=0)
        self._topo_canvas.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._topo_canvas.bind("<Configure>", lambda e: self._redraw_topology())

        # Floating tooltip
        self._topo_tip = tk.Label(
            self._topo_canvas, text="", bg=C["surface2"], fg=C["text"],
            font=("Segoe UI", 8), padx=6, pady=3)

        # Topology state
        self._topo_data  = None
        self._topo_nodes = {}

    # ── Topology helpers ──────────────────────────────────────────────────────

    def _rssi_link_color(self, rssi_val):
        """Return line color for a given raw RSSI value (or None = offline)."""
        try:
            r = int(rssi_val)
            if r >= 29: return C["ok"]
            if r >= 21: return C["warn"]
            return C["err"]
        except (TypeError, ValueError):
            return C["text_dim"]

    def _rssi_link_width(self, rssi_val):
        try:
            r = int(rssi_val)
            if r >= 29: return 2.5
            if r >= 21: return 1.8
            return 1.2
        except (TypeError, ValueError):
            return 1.0

    def _force_layout(self, n_neighbors, W, H):
        """
        Simple radial + force-directed layout.
        Returns list of (cx, cy) for [self_ap, nb0, nb1, …].
        Self AP is always in the center.
        """
        import math, random
        cx, cy = W / 2, H / 2
        if n_neighbors == 0:
            return [(cx, cy)]

        # Start: self in center, neighbors evenly on a circle
        r0 = min(W, H) * 0.35
        positions = [(cx, cy)]
        for i in range(n_neighbors):
            angle = 2 * math.pi * i / n_neighbors - math.pi / 2
            positions.append((
                cx + r0 * math.cos(angle),
                cy + r0 * math.sin(angle),
            ))

        # Light repulsion between neighbors (20 iterations)
        margin = 36
        for _ in range(20):
            for i in range(1, len(positions)):
                fx, fy = 0.0, 0.0
                for j in range(len(positions)):
                    if i == j:
                        continue
                    dx = positions[i][0] - positions[j][0]
                    dy = positions[i][1] - positions[j][1]
                    d  = max(math.hypot(dx, dy), 1)
                    if d < 80:
                        fx += dx / d * (80 - d) * 0.3
                        fy += dy / d * (80 - d) * 0.3
                # Spring back toward initial radial pos
                angle = 2 * math.pi * (i - 1) / n_neighbors - math.pi / 2
                tx = cx + r0 * math.cos(angle)
                ty = cy + r0 * math.sin(angle)
                fx += (tx - positions[i][0]) * 0.15
                fy += (ty - positions[i][1]) * 0.15
                nx = max(margin, min(W - margin, positions[i][0] + fx))
                ny = max(margin, min(H - margin, positions[i][1] + fy))
                positions[i] = (nx, ny)

        return positions

    def _pick_rssi(self, nb):
        """Return the RSSI value for the currently selected band."""
        band = getattr(self, "_topo_band", None)
        val  = band.get() if band else "5g"
        if val == "2g":
            return nb.get("rssi_2g", "—")
        # 5g (default)
        return nb.get("rssi_5g", "—") if nb.get("rssi_5g", "—") != "—"                else nb.get("rssi_2g", "—")

    def _redraw_topology(self):
        """Draw the mesh topology on self._topo_canvas from self._topo_data."""
        c   = self._topo_canvas
        c.delete("all")
        self._topo_nodes = {}

        if not self._topo_data:
            c.create_text(
                c.winfo_width() / 2 or 200,
                c.winfo_height() / 2 or 100,
                text="Kein AP ausgewählt oder keine Mesh-Daten verfügbar",
                fill=C["text_dim"], font=("Segoe UI", 9))
            return

        self_name, self_ip, neighbors = self._topo_data
        W = c.winfo_width()  or 400
        H = c.winfo_height() or 220

        # Compute positions
        positions = self._force_layout(len(neighbors), W, H)
        self_pos  = positions[0]

        # --- Draw edges first (below nodes) ---
        for i, nb in enumerate(neighbors):
            nb_pos  = positions[i + 1]
            best_rssi = self._pick_rssi(nb)
            offline = nb.get("state", "") not in ("active", "static active")
            color   = C["text_dim"] if offline else self._rssi_link_color(best_rssi)
            width   = 1.0          if offline else self._rssi_link_width(best_rssi)
            dash    = (5, 3)       if offline else ()

            line_id = c.create_line(
                self_pos[0], self_pos[1],
                nb_pos[0],  nb_pos[1],
                fill=color, width=width, dash=dash,
                tags=("link", f"link_{i}"))

            # dBm label at midpoint
            if not offline and best_rssi != "—":
                try:
                    dbm = int(best_rssi) - 96
                    mx  = (self_pos[0] + nb_pos[0]) / 2
                    my  = (self_pos[1] + nb_pos[1]) / 2
                    c.create_text(mx, my - 8, text=f"{dbm} dBm",
                                  fill=color, font=("Segoe UI", 7),
                                  tags=("linklabel",))
                except (ValueError, TypeError):
                    pass

        # --- Draw nodes ---
        R = 22  # node radius

        def draw_node(x, y, label, color, is_self=False):
            ring = C["accent"] if is_self else color
            c.create_oval(x-R, y-R, x+R, y+R,
                          fill=color, outline=ring,
                          width=3 if is_self else 1.5,
                          tags=("node",))
            # Short label inside circle (up to 5 chars)
            short = label[:5] if len(label) > 5 else label
            c.create_text(x, y, text=short, fill=C["text_bright"],
                          font=("Segoe UI", 7, "bold"), tags=("node",))
            # Full label below circle
            c.create_text(x, y + R + 9, text=label, fill=C["text"],
                          font=("Segoe UI", 8), tags=("node",))

        # Self AP (center)
        draw_node(self_pos[0], self_pos[1], self_name or "This AP",
                  C["accent"], is_self=True)
        self._topo_nodes[self_name or "This AP"] = self_pos

        # Neighbor nodes
        for i, nb in enumerate(neighbors):
            nb_pos  = positions[i + 1]
            name    = nb.get("name", f"AP-{i+1}")
            offline = nb.get("state", "") not in ("active", "static active")
            best_rssi = self._pick_rssi(nb)
            if offline:
                node_color = C["text_dim"]
            else:
                node_color = self._rssi_link_color(best_rssi)

            draw_node(nb_pos[0], nb_pos[1], name, node_color)
            self._topo_nodes[name] = nb_pos

            # Bind hover tooltip to each neighbor node area
            oid = c.find_closest(nb_pos[0], nb_pos[1])
            tag = f"nb_{i}"
            # Tag all items near this node
            for item in c.find_overlapping(
                    nb_pos[0]-R-2, nb_pos[1]-R-2,
                    nb_pos[0]+R+2, nb_pos[1]+R+2):
                c.addtag_withtag(tag, item)

            def _enter(event, nb=nb, color=node_color):
                best = nb.get("rssi_5g","—") if nb.get("rssi_5g","—") != "—"                        else nb.get("rssi_2g","—")
                try:
                    dbm_str = f"{int(best)-96} dBm" if best != "—" else "—"
                except Exception:
                    dbm_str = "—"
                loc = nb.get("location", "—")
                lines = [nb.get("name","—"), nb.get("ip","—")]
                if loc and loc not in ("—", "", "---"):
                    lines.append(f"📍 {loc}")
                lines += [
                    f"2.4G: {fmt_rssi(nb.get('rssi_2g','—'))}",
                    f"5G:   {fmt_rssi(nb.get('rssi_5g','—'))}",
                    nb.get("state","—"),
                ]
                self._topo_tip.config(text="\n".join(lines))
                self._topo_tip.place(
                    x=min(event.x + 12, W - 140),
                    y=min(event.y + 12, H - 80))

            def _leave(event):
                self._topo_tip.place_forget()

            c.tag_bind(tag, "<Enter>", _enter)
            c.tag_bind(tag, "<Leave>", _leave)

    def _parse_ips(self):
        ips = []
        # Range
        start = self._e_start.get().strip()
        end   = self._e_end.get().strip()
        if start and end:
            try:
                sp = list(map(int, start.split(".")))
                ep = list(map(int, end.split(".")))
                if len(sp) == 4 and len(ep) == 4:
                    prefix = ".".join(str(x) for x in sp[:3])
                    ips += [f"{prefix}.{i}" for i in range(sp[3], ep[3] + 1)]
            except ValueError:
                messagebox.showerror("Error", "Invalid IP range format")
                return []
        # List
        raw_list = self._e_list.get().strip()
        if raw_list:
            ips += [ip.strip() for ip in raw_list.replace(" ", "").split(",") if ip.strip()]
        # Deduplicate, preserve order
        seen = set()
        result = []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                result.append(ip)
        return result

    def _start_scan(self):
        ips = self._parse_ips()
        if not ips:
            messagebox.showwarning("Warning", "Please enter at least one IP address.")
            return

        self._ap_data = {ip: {"status": "scanning"} for ip in ips}
        self._refresh_ap_list()
        self._clear_detail()
        self._lbl_ap_ip.config(text="—")
        self._lbl_ap_model.config(text="")
        self._lbl_ap_status.config(text="")
        self._selected = None

        self._btn_scan.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._progress["maximum"] = len(ips)
        self._progress["value"]   = 0
        self._status("Scanning %d APs…" % len(ips))
        self._scanning = True

        def run():
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futs = {pool.submit(fetch_ap_data, ip, True): ip for ip in ips}
                for fut in as_completed(futs):
                    result = fut.result()
                    self._q.put(("ap_result", result))

            self._q.put(("scan_done", len(ips)))

        threading.Thread(target=run, daemon=True).start()
        self._schedule_auto_refresh()

    def _refresh_selected(self):
        if not self._selected:
            return
        ip = self._selected
        self._ap_data[ip] = {"status": "scanning"}
        self._refresh_ap_list()
        self._status(f"Refreshing {ip}…")

        def run():
            result = fetch_ap_data(ip, force_static=True)
            self._q.put(("ap_result", result))
            self._q.put(("scan_done", 1))

        threading.Thread(target=run, daemon=True).start()

    def _stop_auto(self):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
            self._refresh_id = None
        self._scanning = False
        self._btn_stop.config(state=tk.DISABLED)
        self._countdown_var.set("")
        self._status("Auto-refresh stopped")

    def _schedule_auto_refresh(self):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
        interval = self._interval_var.get()
        self._auto_refresh_at = time.time() + interval
        self._update_countdown()

    def _update_countdown(self):
        remaining = int(self._auto_refresh_at - time.time())
        if remaining <= 0:
            self._countdown_var.set("")
            self._do_auto_refresh()
        else:
            self._countdown_var.set(f"  next refresh in {remaining}s")
            self._refresh_id = self.after(1000, self._update_countdown)

    def _do_auto_refresh(self):
        ips = list(self._ap_data.keys())
        if not ips:
            return
        for ip in ips:
            self._ap_data[ip]["status"] = "scanning"
        self._refresh_ap_list()
        self._status("Auto-refreshing %d APs…" % len(ips))

        def run():
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futs = {pool.submit(fetch_ap_data, ip, False): ip for ip in ips}
                for fut in as_completed(futs):
                    self._q.put(("ap_result", fut.result()))
            self._q.put(("scan_done", len(ips)))

        threading.Thread(target=run, daemon=True).start()
        self._schedule_auto_refresh()

    # ── Queue polling ─────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg_type, payload = self._q.get_nowait()
                if msg_type == "ap_result":
                    ip = payload["ip"]
                    self._ap_data[ip] = payload
                    self._progress["value"] = self._progress.cget("value") + 1
                    self._refresh_ap_list()
                    if ip == self._selected:
                        self._render_detail(ip)
                elif msg_type == "scan_done":
                    ok  = sum(1 for d in self._ap_data.values() if d.get("status") == "ok")
                    err = sum(1 for d in self._ap_data.values() if d.get("status") == "error")
                    self._status(f"Scan complete — {ok} online, {err} offline")
                    self._btn_scan.config(state=tk.NORMAL)
                    self._progress["value"] = 0
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _apply_theme(self):
        global CURRENT_THEME, C
        CURRENT_THEME = self._theme_var.get()
        C = get_theme(CURRENT_THEME)
        self._build_styles()
        self._repaint_widgets(self)
        # Manually repaint labels with explicit background that _repaint_widgets skips
        for lbl in (self._lbl_title_main, self._lbl_title_sub):
            lbl.configure(background=C["surface"])
        for lbl in (self._lbl_ap_ip, self._lbl_ap_model,
                    self._lbl_ap_location, self._lbl_ap_status):
            lbl.configure(background=C["surface2"])
        self._lbl_ap_location.configure(foreground=C["accent"])
        self._settings["theme"] = CURRENT_THEME
        save_settings(self._settings)

    def _repaint_widgets(self, widget):
        """Recursively update background/foreground of plain tk widgets."""
        wclass = widget.winfo_class()
        try:
            if wclass in ("Frame", "Tk"):
                widget.configure(bg=C["bg"])
            elif wclass == "Listbox":
                widget.configure(
                    bg=C["tree_bg"], fg=C["text"],
                    selectbackground=C["selected"],
                    selectforeground=C["text_bright"],
                )
            elif wclass == "Text":
                widget.configure(
                    bg=C["field_bg"], fg=C["text"],
                    insertbackground=C["text"],
                )
            elif wclass == "Label":
                cur_bg = widget.cget("bg")
                # Only repaint labels that have a theme-ish background
                if cur_bg not in ("SystemButtonFace", ""):
                    widget.configure(bg=C["bg"])
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._repaint_widgets(child)

    def _show_about(self):
        """About / Changelog popup."""
        win = tk.Toplevel(self)
        win.title("About")
        win.configure(bg=C["bg"])
        win.resizable(False, False)
        win.grab_set()

        # Header
        hdr = ttk.Frame(win, style="Surface.TFrame")
        hdr.pack(fill=tk.X)
        tk.Frame(hdr, bg=C["accent"], width=4).pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(hdr, text="ALE  OmniAccess Stellar",
                  style="Title.TLabel", background=C["surface"],
                  padding=(16, 12)).pack(side=tk.LEFT)
        ttk.Label(hdr, text=f"AP Status Monitor  \u2002v{APP_VERSION}",
                  style="Dim.TLabel", background=C["surface"],
                  padding=(0, 12)).pack(side=tk.LEFT)

        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, padx=20, pady=12)

        # Changelog entries: (version, date, [changes])
        CHANGELOG = [
            ("0.1.0", "2025-06-02", [
                "Initiale Version",
                "AP-Scan per SSH (IP-Range, Liste, Auto-Refresh)",
                "Tabs: System, Clients, Wireless, Network, MESH, Topology",
                "Client Troubleshooting Tab mit SNR-basiertem Health Score",
                "wlanconfig Interface-Erkennung dynamisch (über iwconfig + /sys/class/net)",
                "Debug SSH-Logging (Settings \u2192 Debug)",
                "Multi-Profil INI-System",
                "Dark / Light Theme",
            ]),
        ]

        ttk.Label(body, text="Changelog", style="Title.TLabel",
                  font=FONT_BOLD).pack(anchor="w", pady=(0, 8))

        # Scrollable changelog frame
        outer = ttk.Frame(body)
        outer.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(outer, orient=tk.VERTICAL)
        canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0,
                           yscrollcommand=sb.set, width=480, height=260)
        sb.config(command=canvas.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        canvas_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_resize(e):
            canvas.itemconfig(canvas_win, width=e.width)
        canvas.bind("<Configure>", _on_resize)
        inner.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))

        for ver, date, changes in CHANGELOG:
            vrow = ttk.Frame(inner)
            vrow.pack(fill=tk.X, pady=(8, 2))
            ttk.Label(vrow, text=f"v{ver}",
                      foreground=C["accent"], font=FONT_BOLD).pack(side=tk.LEFT)
            ttk.Label(vrow, text=f"  —  {date}",
                      style="Dim.TLabel", font=("Segoe UI", 9)).pack(side=tk.LEFT)
            for ch in changes:
                ttk.Label(inner, text=f"  •  {ch}",
                          wraplength=440, justify=tk.LEFT,
                          font=("Segoe UI", 9)).pack(anchor="w", padx=(8, 0))
            ttk.Separator(inner, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(8, 0))

        ttk.Button(win, text="Schlie\u00dfen", command=win.destroy).pack(pady=(4, 14))

        win.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - win.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    def _open_settings(self):
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, d):
        self._settings = d
        self._apply_settings(d)
        save_settings(d)
        # Update scan fields too
        self._e_start.delete(0, tk.END)
        self._e_start.insert(0, d["ip_start"])
        self._e_end.delete(0, tk.END)
        self._e_end.insert(0, d["ip_end"])
        self._e_list.delete(0, tk.END)
        self._e_list.insert(0, d["ip_list"])
        self._interval_var.set(d["interval"])
        self._refresh_ap_list()
        self._status("Settings saved.")

    def _status(self, msg):
        self._status_var.set(msg)


# ─── Profile chooser ─────────────────────────────────────────────────────────

class ProfileChooser(tk.Tk):
    """
    Shown at startup. Lets the user pick an existing profile or create a new one.
    Sets SETTINGS_FILE globally, then launches APMonitor.
    """
    def __init__(self):
        super().__init__()
        global CURRENT_THEME, C
        self.title("AP Status Monitor — Profil wählen")
        self.resizable(False, False)
        self.configure(bg=C["bg"])
        self._chosen = None
        self._build()
        # Center on screen
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w,  h  = self.winfo_width(),  self.winfo_height()
        self.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

    def _build(self):
        pad = {"padx": 16, "pady": 8}

        ttk.Label(self, text="ALE  OmniAccess Stellar", style="Title.TLabel",
                  padding=(20, 16, 20, 4)).pack()
        ttk.Label(self, text="Kundenprofil wählen oder neu erstellen",
                  style="Dim.TLabel", padding=(20, 0, 20, 12)).pack()

        # Profile list + scrollbar
        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, padx=20, pady=(0, 8))

        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        self._lb = tk.Listbox(
            frame, height=8, width=32,
            bg=C["tree_bg"], fg=C["text"],
            selectbackground=C["selected"], selectforeground=C["text_bright"],
            activestyle="none", borderwidth=0, highlightthickness=0,
            font=("Segoe UI", 10), yscrollcommand=sb.set)
        sb.config(command=self._lb.yview)
        self._lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._lb.bind("<Double-Button-1>", lambda e: self._select())

        self._refresh_list(select_first=True)

        # New profile row
        new_f = ttk.Frame(self)
        new_f.pack(fill=tk.X, padx=20, pady=(0, 4))
        ttk.Label(new_f, text="Neues Profil:", style="Dim.TLabel").pack(side=tk.LEFT)
        self._new_var = tk.StringVar()
        ttk.Entry(new_f, textvariable=self._new_var, width=18).pack(
            side=tk.LEFT, padx=(8, 6))
        ttk.Button(new_f, text="Erstellen", command=self._create,
                   style="Ghost.TButton").pack(side=tk.LEFT)

        # Delete button
        del_f = ttk.Frame(self)
        del_f.pack(fill=tk.X, padx=20, pady=(0, 8))
        ttk.Button(del_f, text="Profil löschen", command=self._delete,
                   style="Ghost.TButton").pack(side=tk.LEFT)

        # Open / Cancel
        btn_row = ttk.Frame(self)
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 16))
        ttk.Button(btn_row, text="Öffnen", command=self._select).pack(
            side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="Abbrechen", command=self.destroy,
                   style="Ghost.TButton").pack(side=tk.RIGHT)

    def _refresh_list(self, select_first=False, select_name=None):
        self._lb.delete(0, tk.END)
        profiles = list_profiles()
        if not profiles:
            profiles = ["default"]
        for p in profiles:
            self._lb.insert(tk.END, f"  {p}")
        if select_name and select_name in profiles:
            idx = profiles.index(select_name)
            self._lb.selection_set(idx)
            self._lb.see(idx)
        elif select_first and profiles:
            self._lb.selection_set(0)

    def _current_name(self):
        sel = self._lb.curselection()
        if not sel:
            return None
        return self._lb.get(sel[0]).strip()

    def _select(self):
        global SETTINGS_FILE
        name = self._current_name()
        if not name:
            return
        SETTINGS_FILE = profile_path(name)
        self._chosen = name
        self.destroy()

    def _create(self):
        name = self._new_var.get().strip()
        if not name:
            messagebox.showwarning("Fehler", "Bitte Profilnamen eingeben.", parent=self)
            return
        # Sanitise: only alphanum, dash, underscore
        import re as _re
        name = _re.sub(r"[^\w\-]", "_", name)
        path = profile_path(name)
        if not os.path.exists(path):
            # Create with defaults
            save_settings({
                "username": "support", "password": "aos2016",
                "timeout": 10, "workers": 10,
                "ip_start": "", "ip_end": "", "ip_list": "",
                "interval": 60, "theme": "dark",
                "dns_server": "", "list_show": "both", "list_sort": "ip",
            }, path=path)
        self._new_var.set("")
        self._refresh_list(select_name=name)

    def _delete(self):
        name = self._current_name()
        if not name or name == "default":
            messagebox.showinfo("Hinweis",
                "Das Standard-Profil kann nicht gelöscht werden.", parent=self)
            return
        if not messagebox.askyesno(
                "Profil löschen",
                f"Profil '{name}' wirklich löschen?", parent=self):
            return
        path = profile_path(name)
        try:
            os.remove(path)
        except OSError:
            pass
        self._refresh_list(select_first=True)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Build minimal style before ProfileChooser so ttk styles work
    _pre = tk.Tk()
    _pre.withdraw()
    _s = ttk.Style(_pre)
    _s.theme_use("clam")
    _s.configure(".", background=C["bg"], foreground=C["text"], font=("Segoe UI", 10))
    _s.configure("TFrame",      background=C["bg"])
    _s.configure("TLabel",      background=C["bg"], foreground=C["text"])
    _s.configure("Dim.TLabel",  background=C["bg"], foreground=C["text_dim"])
    _s.configure("Title.TLabel",background=C["bg"], foreground=C["text_bright"],
                 font=("Segoe UI", 13, "bold"))
    _s.configure("TButton",     background=C["accent"], foreground="#ffffff",
                 borderwidth=0, padding=(12, 6), font=("Segoe UI", 10, "bold"),
                 relief="flat")
    _s.configure("Ghost.TButton", background=C["surface"], foreground=C["text"],
                 borderwidth=1, padding=(10, 5), font=("Segoe UI", 10), relief="flat")
    _s.configure("TEntry",      fieldbackground=C["field_bg"], foreground=C["text"],
                 bordercolor=C["border"], padding=5)
    _s.configure("TScrollbar",  background=C["surface2"], troughcolor=C["bg"],
                 bordercolor=C["bg"], arrowcolor=C["text_dim"], width=8)
    _pre.destroy()

    chooser = ProfileChooser()
    chooser.mainloop()

    if chooser._chosen is None:
        # User closed dialog without selecting
        raise SystemExit(0)

    app = APMonitor()
    app.mainloop()
