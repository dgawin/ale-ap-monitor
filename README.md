# ALE OmniAccess Stellar – AP Status Monitor

A professional Windows desktop tool for monitoring and troubleshooting ALE OmniAccess Stellar access points via SSH.

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Python](https://img.shields.io/badge/python-3.8%2B-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Multi-AP Scan** — IP range or custom list, parallel SSH workers
- **Auto-Refresh** — configurable interval with two-tier data strategy
  - Static data (name, model, firmware) cached for 10 min
  - Dynamic data (clients, MESH, traffic) fetched every cycle
- **Tabs per AP**
  - System — model, firmware, uptime, memory
  - Clients — associated stations with hostname resolution
  - Wireless — interface details, channel, TX power
  - Network — WAN IP, routes, DNS
  - MESH — ADME neighbor table
  - Topology — visual AP map
  - **Client Troubleshooting** — SNR-based health score with breakdown
- **SSH Debug Logging** — captures every command + response to a log file
- **Multi-Profile INI system** — separate config per site
- **Dark / Light theme**
- **PyInstaller EXE build** — runs standalone, INI files stored next to EXE

## Requirements

```
pip install paramiko
```

Optional for DNS resolution:
```
pip install dnspython
```

## Build EXE

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=ap_monitor.ico ap_status_monitor.py
```

EXE and INI files will be created in the same directory.

## Usage

1. Enter IP range (e.g. `192.168.1.1` → `192.168.1.50`) or a comma-separated IP list
2. Set SSH credentials under **Settings**
3. Click **Scan**
4. Select an AP in the left panel to view details

## SSH Commands Used

| Category | Commands |
|---|---|
| System | `showsysinfo`, `uptime`, `free`, `date`, `getmode` |
| Clients | `ssudo sta_list`, `wlanconfig <iface> list` |
| Wireless | `iwconfig`, `iwlist txpower`, `iwlist <iface> channel` |
| Network | `ifconfig br-wan`, `route -n`, `cat /etc/resolv.conf` |
| MESH | `adme show` |
| Cluster | `cluster_mgt -x show=self` |

## Tested Devices

| Model | Firmware |
|---|---|
| OAW-AP1201 | AWOS 5.0.4 |
| OAW-AP1221 | AWOS 5.0.4 |
| OAW-AP1431 | AWOS 5.0.4 |

## Changelog

### v0.1.0 (2025-06-02)
- Initial release
- AP scan via SSH (IP range, list, auto-refresh)
- Tabs: System, Clients, Wireless, Network, MESH, Topology
- Client Troubleshooting tab with SNR-based health score
- Dynamic wlanconfig interface discovery (via iwconfig + /sys/class/net)
- SSH debug logging (Settings → Debug)
- Multi-profile INI system
- Dark / Light theme

## Disclaimer

This project is an independent, community-developed tool and is **not affiliated with, endorsed by, or associated with Alcatel-Lucent Enterprise (ALE)** in any way.

The names **ALE**, **Alcatel-Lucent Enterprise**, **OmniAccess**, and **Stellar** are trademarks or registered trademarks of Alcatel-Lucent Enterprise. All other product and company names mentioned are the property of their respective owners.

These names are used solely to describe compatibility with ALE OmniAccess Stellar hardware. No trademark rights are claimed.
