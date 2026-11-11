#!/usr/bin/env python3
"""
Monitor brightness + power + audio + game mode controller.
Requires: pip install screen-brightness-control pycaw
"""

import sys
import json
import struct
import time
import argparse
import ctypes
import winreg
import msvcrt
from functools import wraps
from ctypes import wintypes
from pathlib import Path
from app_logging import LOG_FILE, set_console_streams

try:
    import screen_brightness_control as sbc
except ImportError:
    print("Missing dependency. Install it with:\n  pip install screen-brightness-control")
    sys.exit(1)

try:
    import comtypes
    import comtypes.client
    from comtypes import GUID, HRESULT, IUnknown, COMMETHOD
except ImportError:
    print("Missing dependency. Install it with:\n  pip install comtypes")
    sys.exit(1)

try:
    from hisense_tv.client import HisenseTV
except ImportError:
    print("Missing dependency. Install it with:\n  pip install myhisense-tv")
    sys.exit(1)

import subprocess as _subprocess

_dxva2 = ctypes.windll.dxva2
_user32 = ctypes.windll.user32

import os as _os
_state_dir = Path(_os.environ.get('APPDATA', Path.home())) / 'BrightnessController'
_state_dir.mkdir(parents=True, exist_ok=True)
STATE_FILE          = _state_dir / "game_mode_state.json"
DISPLAY_LAYOUT_FILE = _state_dir / "display_layout.json"


# ===========================================================================
# USER CONFIGURATION — edit these to adapt the script to your own hardware
# ===========================================================================

# Monitor name substrings, matched against Windows' friendly device name for
# each monitor (see the names printed by `game_mode.py list`). "desktop" and
# "tv" are required; add/remove any other roles (e.g. "second") to match
# however many monitors you actually have — there's no fixed limit in this
# code (_set_display_extend_all sizes itself off len(MONITOR_NAMES)); the
# only real ceiling is how many displays Windows/your GPU can drive at once.
MONITOR_NAMES = {
    "desktop": "HA240Y",   # primary desktop monitor — cloned onto the TV in game mode
    "second":  "LG FHD",   # secondary desktop monitor
    "tv":      "HISENSE",  # TV acting as a monitor
}
# Left-to-right order used when arranging all configured monitors as an
# extended desktop (see MONITOR_NAMES above) — must list the same roles.
MONITOR_ORDER_LTR = ("tv", "second", "desktop")  # TV, LG, Acer

# Default audio output devices (substring match against `audio list`).
GAME_MODE_CONFIG = {
    "audio_on":  "HISENSE",                       # audio device while game mode is ON
    "audio_off": "High Definition Audio Device",  # audio device restored when game mode is OFF
    "tv_input":  "hdmi1",                         # TV input to activate in game mode
}

# TV network info, for the Hisense VIDAA local control API + Wake-on-LAN.
# These are placeholders — real values are machine-specific and shouldn't
# be committed to a public repo. Override them in local_config.py
# (gitignored, see below) instead of editing these defaults.
TV_IP   = "192.168.1.100"
TV_MAC  = "aa:bb:cc:dd:ee:ff"
TV_PORT = 36669  # Hisense's local MQTT control port

# Client certificate/key for Hisense's local MQTT control API (mutual TLS).
# These are the long-known, publicly shared credentials used throughout the
# smart-home community (e.g. Home Assistant's Hisense TV integrations) for
# controlling a Hisense/VIDAA TV you own over your own LAN — not a secret
# tied to this TV specifically.
HISENSE_CERT_FILE = Path(__file__).parent / "hisense_certs" / "rcm_certchain_pem.cer"
HISENSE_KEY_FILE  = Path(__file__).parent / "hisense_certs" / "rcm_pem_privkey.pkcs8"

GAME_MODE_TV_VOLUME = 22  # TV volume level while game mode is active (0-100)

# Input/volume used by turn_on_tv.py when just turning the TV on normally
# (as opposed to GAME_MODE_CONFIG["tv_input"] / GAME_MODE_TV_VOLUME above).
# Valid inputs: "tv", "av", "hdmi1", "hdmi2", "hdmi3", "hdmi4".
TV_ON_INPUT  = "hdmi1"
TV_ON_VOLUME = 18

# Bluetooth device name substring for the gamepad, used only to look up its
# real battery level via GATT (see ble_battery_helper.py).
CONTROLLER_BLE_NAME = "Xbox"

# Optional machine-specific overrides (gitignored). Create local_config.py
# next to this file with lines like `TV_IP = "..."` / `TV_MAC = "..."` to
# override any of the placeholders above without committing them.
try:
    from local_config import *  # noqa: F401,F403
except ImportError:
    pass

# ===========================================================================


def _popup_error(message: str):
    """Show a Windows error popup dialog."""
    ctypes.windll.user32.MessageBoxW(0, message, "Game Mode Error", 0x10)  # MB_ICONERROR


def _popup_warning(message: str):
    """Show a Windows warning popup dialog."""
    ctypes.windll.user32.MessageBoxW(0, message, "Game Mode Warning", 0x30)  # MB_ICONWARNING


# ---------------------------------------------------------------------------
# Controller check
# ---------------------------------------------------------------------------

def _find_connected_controller():
    """Return (xinput_dll, port) for the first actively connected gamepad, or None.

    XInputGetState returns 0 only when the controller is ON and responding.
    Paired-but-off Bluetooth controllers return ERROR_DEVICE_NOT_CONNECTED (1167),
    so this correctly distinguishes powered-on from merely paired devices.
    """
    buf = (ctypes.c_byte * 16)()
    for lib in ('xinput1_4', 'xinput1_3', 'xinput9_1_0'):
        try:
            xi = getattr(ctypes.windll, lib)
            for port in range(4):
                if xi.XInputGetState(port, buf) == 0:
                    return xi, port
            return None  # XInput loaded — no controller on any port = not connected
        except OSError:
            continue
    return None


_LOW_BATTERY_THRESHOLD_PCT = 40
_BLE_BATTERY_HELPER = Path(__file__).parent / "ble_battery_helper.py"


def _controller_battery_warning() -> str | None:
    """Return a warning message if the controller's real battery level is low, else None.

    Reads the level via ble_battery_helper.py, run as a subprocess: winrt's
    async Bluetooth GATT calls hang indefinitely in-process once comtypes
    (used elsewhere in this module for audio device switching) has
    initialized a COM apartment on the same thread — the two don't share
    an apartment model. Running the read in a separate process sidesteps
    that entirely.
    """
    try:
        result = _subprocess.run(
            [sys.executable, str(_BLE_BATTERY_HELPER), CONTROLLER_BLE_NAME],
            capture_output=True, text=True, timeout=10,
            creationflags=_subprocess.CREATE_NO_WINDOW)
    except (OSError, _subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    if not output.isdigit():
        return None
    pct = int(output)
    if pct <= _LOW_BATTERY_THRESHOLD_PCT:
        return f"Controller battery is low ({pct}%) — consider charging it before playing."
    return None


# ---------------------------------------------------------------------------
# Display topology
# ---------------------------------------------------------------------------

import ctypes.wintypes as _wt

class _LUID(ctypes.Structure):
    _fields_ = [('LowPart', _wt.DWORD), ('HighPart', _wt.LONG)]

class _DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [('Numerator', _wt.UINT), ('Denominator', _wt.UINT)]

class _DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [('adapterId', _LUID), ('id', _wt.UINT), ('modeInfoIdx', _wt.UINT), ('statusFlags', _wt.UINT)]

class _DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ('adapterId', _LUID), ('id', _wt.UINT), ('modeInfoIdx', _wt.UINT),
        ('outputTechnology', _wt.UINT), ('rotation', _wt.UINT), ('scaling', _wt.UINT),
        ('refreshRate', _DISPLAYCONFIG_RATIONAL), ('scanLineOrdering', _wt.UINT),
        ('targetAvailable', _wt.BOOL), ('statusFlags', _wt.UINT),
    ]

class _DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [('sourceInfo', _DISPLAYCONFIG_PATH_SOURCE_INFO), ('targetInfo', _DISPLAYCONFIG_PATH_TARGET_INFO), ('flags', _wt.UINT)]

class _DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [('_data', ctypes.c_byte * 64)]

class _DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [('type', _wt.UINT), ('size', _wt.UINT), ('adapterId', _LUID), ('id', _wt.UINT)]

class _DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ('header', _DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ('flags', _wt.UINT), ('outputTechnology', _wt.UINT),
        ('edidManufactureId', _wt.USHORT), ('edidProductCodeId', _wt.USHORT),
        ('connectorInstance', _wt.UINT),
        ('monitorFriendlyDeviceName', ctypes.c_wchar * 64),
        ('monitorDevicePath', ctypes.c_wchar * 128),
    ]

_QDC_ALL_PATHS                  = 0x00000001
_QDC_ONLY_ACTIVE_PATHS         = 0x00000002
_SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x00000020
_SDC_APPLY                      = 0x00000080
_SDC_SAVE_TO_DATABASE           = 0x00000200
_SDC_ALLOW_CHANGES              = 0x00000400


def _query_display_config():
    numPaths = ctypes.c_uint32()
    numModes = ctypes.c_uint32()
    _user32.GetDisplayConfigBufferSizes(_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(numPaths), ctypes.byref(numModes))
    paths = (_DISPLAYCONFIG_PATH_INFO * numPaths.value)()
    modes = (_DISPLAYCONFIG_MODE_INFO * numModes.value)()
    _user32.QueryDisplayConfig(_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(numPaths), paths, ctypes.byref(numModes), modes, None)
    return paths, modes, numPaths.value, numModes.value


def save_display_layout():
    """Snapshot the current display config (topology + positions) to disk."""
    paths, modes, nP, nM = _query_display_config()
    DISPLAY_LAYOUT_FILE.write_text(json.dumps({
        "nPaths":      nP,
        "nModes":      nM,
        "paths_bytes": list(bytes(paths)),
        "modes_bytes": list(bytes(modes)),
    }))
    print("Display layout saved.")


def restore_display_layout() -> bool:
    """Restore the display config previously saved by save_display_layout."""
    if not DISPLAY_LAYOUT_FILE.exists():
        _popup_error("No saved display layout found.\n\nRun 'Turn On TV' first to save a snapshot.")
        return False
    state = json.loads(DISPLAY_LAYOUT_FILE.read_text())
    nP = state["nPaths"]
    nM = state["nModes"]
    saved_paths = (_DISPLAYCONFIG_PATH_INFO * nP).from_buffer_copy(bytes(state["paths_bytes"]))
    saved_modes = (_DISPLAYCONFIG_MODE_INFO * nM).from_buffer_copy(bytes(state["modes_bytes"]))
    ret = _user32.SetDisplayConfig(nP, saved_paths, nM, saved_modes,
        _SDC_USE_SUPPLIED_DISPLAY_CONFIG | _SDC_APPLY |
        _SDC_SAVE_TO_DATABASE | _SDC_ALLOW_CHANGES)
    if ret == 0:
        print("Display layout restored.")
        return True
    print(f"Display layout restore failed: {ret} (0x{ret & 0xFFFFFFFF:08X})")
    _popup_error(f"Failed to restore display layout (error {ret}).")
    return False


def _set_display_extend_all() -> bool:
    """Extend all monitors configured in MONITOR_NAMES as separate screens,
    even when some of them (typically the desktop monitor and TV) are
    currently cloned.

    QDC_ALL_PATHS reveals inactive paths for a monitor that's currently
    cloned onto another one's source. Supplying each monitor's own distinct
    source with modeInfoIdx=0xFFFFFFFF lets Windows choose native modes via
    SDC_ALLOW_CHANGES.
    """
    nP = ctypes.c_uint32()
    nM = ctypes.c_uint32()
    _user32.GetDisplayConfigBufferSizes(_QDC_ALL_PATHS, ctypes.byref(nP), ctypes.byref(nM))
    all_paths = (_DISPLAYCONFIG_PATH_INFO * nP.value)()
    all_modes = (_DISPLAYCONFIG_MODE_INFO * nM.value)()
    _user32.QueryDisplayConfig(_QDC_ALL_PATHS, ctypes.byref(nP), all_paths,
                               ctypes.byref(nM), all_modes, None)

    # Build target-key → name map (call DisplayConfigGetDeviceInfo once per target)
    name_by_target: dict = {}
    for p in all_paths:
        key = (p.targetInfo.adapterId.LowPart, p.targetInfo.id)
        if key not in name_by_target:
            n = _get_monitor_name(p)
            if n:
                name_by_target[key] = n

    # Process "desktop" and "tv" first (in that order), since they're the
    # pair that may currently share a cloned source; any other configured
    # monitors follow, in whatever order they're defined in MONITOR_NAMES.
    roles = [k for k in ("desktop", "tv") if k in MONITOR_NAMES]
    roles += [k for k in MONITOR_NAMES if k not in ("desktop", "tv")]

    # For each monitor pick a path with a unique source; prefer the active path
    # (keeps current source assignment) then inactive ones with a free source ID.
    chosen = []
    used_sources: set = set()
    for role in roles:
        want = MONITOR_NAMES[role]
        candidates = [
            p for p in all_paths
            if want in name_by_target.get(
                (p.targetInfo.adapterId.LowPart, p.targetInfo.id), '')
        ]
        candidates.sort(key=lambda p: 0 if p.flags & 1 else 1)
        for p in candidates:
            src_key = (p.sourceInfo.adapterId.LowPart, p.sourceInfo.id)
            if src_key not in used_sources:
                new_p = _DISPLAYCONFIG_PATH_INFO()
                ctypes.memmove(ctypes.byref(new_p), ctypes.byref(p), ctypes.sizeof(p))
                new_p.flags = 1
                new_p.sourceInfo.modeInfoIdx = 0xFFFFFFFF
                new_p.targetInfo.modeInfoIdx = 0xFFFFFFFF
                chosen.append(new_p)
                used_sources.add(src_key)
                break

    if len(chosen) != len(MONITOR_NAMES):
        print(f"_set_display_extend_all: found only {len(chosen)}/{len(MONITOR_NAMES)} monitors")
        return False

    new_paths = (_DISPLAYCONFIG_PATH_INFO * len(chosen))()
    for i, p in enumerate(chosen):
        new_paths[i] = p

    ret = _user32.SetDisplayConfig(
        len(chosen), new_paths, 0, None,
        _SDC_USE_SUPPLIED_DISPLAY_CONFIG | _SDC_APPLY |
        _SDC_SAVE_TO_DATABASE | _SDC_ALLOW_CHANGES)
    if ret != 0:
        print(f"_set_display_extend_all failed: {ret} (0x{ret & 0xFFFFFFFF:08X})")
    return ret == 0


def _arrange_monitors_ltr(*monitor_names: str, primary: str | None = None) -> bool:
    """Arrange monitors left-to-right, placing the primary display at (0, 0).

    Reads each monitor's source-mode width from the current display config,
    then re-issues SetDisplayConfig with corrected positionX values.

    DISPLAYCONFIG_MODE_INFO layout (64 bytes):
      offset 0  : infoType (UINT32) — 1 = source mode
      offset 4  : id
      offset 8  : adapterId (LUID)
      offset 16 : width  (UINT32)   ─┐
      offset 20 : height (UINT32)    │ source-mode union
      offset 24 : pixelFormat        │
      offset 28 : positionX (INT32)  │
      offset 32 : positionY (INT32) ─┘
    """
    paths, modes, nP, nM = _query_display_config()

    name_to_path: dict = {}
    for p in paths:
        name = _get_monitor_name(p)
        for want in monitor_names:
            if want in name:
                name_to_path[want] = p

    if len(name_to_path) != len(monitor_names):
        print(f"_arrange_monitors_ltr: found {list(name_to_path)}, expected {list(monitor_names)}")
        return False

    mode_bufs = [bytearray(bytes(m._data)) for m in modes]
    primary = primary or MONITOR_NAMES["desktop"]
    positions = {}
    x_offset = 0
    for want in monitor_names:
        p = name_to_path[want]
        src_idx = p.sourceInfo.modeInfoIdx
        if src_idx >= nM:
            return False
        buf = mode_bufs[src_idx]
        if struct.unpack_from('<I', buf, 0)[0] != 1:  # must be a source mode (type=1, target=2)
            return False
        width = struct.unpack_from('<I', buf, 16)[0]
        positions[want] = (src_idx, x_offset)
        x_offset += width

    if primary not in positions:
        print(f"Primary monitor '{primary}' not found in display order.")
        return False
    primary_x = positions[primary][1]
    for src_idx, x in positions.values():
        # Windows makes the display at the desktop origin the main display.
        struct.pack_into('<i', mode_bufs[src_idx], 28, x - primary_x)
        struct.pack_into('<i', mode_bufs[src_idx], 32, 0)

    all_bytes = b''.join(bytes(buf) for buf in mode_bufs)
    new_modes = (_DISPLAYCONFIG_MODE_INFO * nM).from_buffer_copy(all_bytes)

    ret = _user32.SetDisplayConfig(nP, paths, nM, new_modes,
        _SDC_USE_SUPPLIED_DISPLAY_CONFIG | _SDC_APPLY | _SDC_SAVE_TO_DATABASE)
    if ret != 0:
        print(f"_arrange_monitors_ltr failed: {ret} (0x{ret & 0xFFFFFFFF:08X})")
    return ret == 0


def _get_monitor_name(path) -> str:
    info = _DISPLAYCONFIG_TARGET_DEVICE_NAME()
    info.header.type = 2  # DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
    info.header.size = ctypes.sizeof(info)
    info.header.adapterId = path.targetInfo.adapterId
    info.header.id = path.targetInfo.id
    _user32.DisplayConfigGetDeviceInfo(ctypes.byref(info.header))
    return info.monitorFriendlyDeviceName


def _set_display_topology(clone: bool) -> bool:
    import subprocess
    if clone:
        # Step 1: Clone all displays via DisplaySwitch to get a consistent base.
        # SetDisplayConfig SDC_TOPOLOGY_CLONE returns error 87 when the current
        # config has stale mode entries; DisplaySwitch.exe handles this correctly.
        r1 = subprocess.run(['DisplaySwitch.exe', '/clone'], capture_output=True)
        if r1.returncode != 0:
            print(f"Clone-all (DisplaySwitch /clone) failed: {r1.returncode}")
            return False
        time.sleep(1)  # let Windows settle before re-querying

        # DisplaySwitch can clone only a subset. Explicitly give every target
        # the desktop monitor's source and let Windows choose compatible modes.
        active, _, _, _ = _query_display_config()
        selected = []
        for want in MONITOR_NAMES.values():
            path = next((p for p in active if want in _get_monitor_name(p)), None)
            if path is None:
                print(f"Cannot clone: monitor {want} is not active.", flush=True)
                return False
            selected.append(path)
        desktop = selected[list(MONITOR_NAMES).index("desktop")]
        adapter = (desktop.sourceInfo.adapterId.LowPart, desktop.sourceInfo.adapterId.HighPart)
        if any((p.sourceInfo.adapterId.LowPart, p.sourceInfo.adapterId.HighPart) != adapter
               for p in selected):
            print("Cannot clone monitors across different display adapters.", flush=True)
            return False
        paths = (_DISPLAYCONFIG_PATH_INFO * len(selected))()
        for i, path in enumerate(selected):
            paths[i] = path
            paths[i].sourceInfo.id = desktop.sourceInfo.id
            paths[i].sourceInfo.modeInfoIdx = 0xFFFFFFFF
            paths[i].targetInfo.modeInfoIdx = 0xFFFFFFFF
            paths[i].flags = 1
        ret = _user32.SetDisplayConfig(
            len(paths), paths, 0, None,
            _SDC_USE_SUPPLIED_DISPLAY_CONFIG | _SDC_APPLY |
            _SDC_SAVE_TO_DATABASE | _SDC_ALLOW_CHANGES)
        if ret != 0:
            print(f"Explicit three-screen clone failed: Windows error {ret}.", flush=True)
            return False
        time.sleep(2)

        # Confirm every configured monitor is active on the same source.
        active, _, _, _ = _query_display_config()
        names = [_get_monitor_name(p) for p in active]
        sources = {(p.sourceInfo.adapterId.LowPart, p.sourceInfo.adapterId.HighPart,
                    p.sourceInfo.id) for p in active}
        verified = (len(active) == len(MONITOR_NAMES) and len(sources) == 1
                    and all(any(want in name for name in names)
                            for want in MONITOR_NAMES.values()))
        print("Verified: Acer, LG and TV duplicate the same desktop." if verified
              else f"Duplication verification failed. Active monitors: {names}", flush=True)
        return verified
    else:
        # Restore full extend via DisplaySwitch (restores all attached monitors)
        return subprocess.run(['DisplaySwitch.exe', '/extend'], capture_output=True).returncode == 0


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
        ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD),
    ]

_MONITOR_DEFAULTTOPRIMARY   = 1
_MONITOR_DEFAULTTONEAREST   = 2
_GW_OWNER                   = 4
_GWL_EXSTYLE                = -20
_WS_EX_TOOLWINDOW           = 0x00000080
_SWP_NOACTIVATE             = 0x0010
_SW_RESTORE                 = 9


def _bring_windows_to_main_screen(include_minimized: bool = False):
    """Move visible top-level windows off any other monitor onto the primary one.

    Windows already on the primary monitor, owned (dialogs, tooltips,
    etc.), or without a titlebar are left alone. Size is kept, just
    clamped to fit; moved windows are cascaded so they don't all land in
    the same spot.

    Minimized windows are skipped by default. With include_minimized=True
    they're explicitly un-minimized (restored) and moved too, regardless
    of which monitor they were on, since there's no visible position to
    judge "already on the main screen" from while minimized.
    """
    primary_hmon = _user32.MonitorFromPoint(wintypes.POINT(0, 0), _MONITOR_DEFAULTTOPRIMARY)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(primary_hmon, ctypes.byref(info))
    work = info.rcWork
    max_w = work.right - work.left
    max_h = work.bottom - work.top

    hwnds = []
    cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @cb_type
    def _enum(hwnd, lparam):
        hwnds.append(hwnd)
        return True

    _user32.EnumWindows(_enum, 0)

    moved = 0
    for hwnd in hwnds:
        minimized = _user32.IsIconic(hwnd)
        if not _user32.IsWindowVisible(hwnd):
            continue
        if minimized and not include_minimized:
            continue
        if _user32.GetWindowTextLengthW(hwnd) == 0:
            continue
        if _user32.GetWindow(hwnd, _GW_OWNER) != 0:
            continue
        if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
            continue
        if not minimized and _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST) == primary_hmon:
            continue

        if minimized:
            _user32.ShowWindow(hwnd, _SW_RESTORE)

        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        width = min(rect.right - rect.left, max_w)
        height = min(rect.bottom - rect.top, max_h)
        step = (moved % 10) * 30
        # hwndInsertAfter=0 (HWND_TOP) brings it to the top of the z-order so it's
        # actually visible, not just correctly positioned but buried underneath
        # other windows; SWP_NOACTIVATE still avoids stealing keyboard focus.
        _user32.SetWindowPos(hwnd, 0, work.left + step, work.top + step, width, height,
                              _SWP_NOACTIVATE)
        moved += 1

    if moved:
        print(f"Moved {moved} window(s) to the main screen.")
    else:
        print("All windows already on the main screen.")


# ---------------------------------------------------------------------------
# Monitor helpers
# ---------------------------------------------------------------------------

class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("handle", wintypes.HANDLE), ("description", ctypes.c_wchar * 128)]


def _enum_physical_monitors(require_caps=True):
    """Enumerate physical monitor handles.
    require_caps=True  — only returns monitors that respond to GetMonitorCapabilities (default).
    require_caps=False — returns all handles including powered-off monitors (needed for power-on).
    """
    hmons = []
    cb_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    @cb_type
    def _cb(hmon, hdc, rect, data):
        hmons.append(hmon)
        return True

    _user32.EnumDisplayMonitors(None, None, _cb, 0)

    handles = []
    for hmon in hmons:
        count = ctypes.c_ulong()
        _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count))
        phys = (_PHYSICAL_MONITOR * count.value)()
        _dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, phys)
        for p in phys:
            if not require_caps:
                handles.append(p.handle)
            else:
                caps = ctypes.c_ulong()
                color = ctypes.c_ulong()
                if _dxva2.GetMonitorCapabilities(p.handle, ctypes.byref(caps), ctypes.byref(color)):
                    handles.append(p.handle)
                else:
                    _dxva2.DestroyPhysicalMonitor(p.handle)
    return handles


def _set_power(display, state):
    handles = _enum_physical_monitors()
    if not handles:
        print("No DDC/CI capable monitors found.")
        sys.exit(1)

    targets = [(i, h) for i, h in enumerate(handles) if display is None or i == display]
    if not targets:
        print(f"Monitor {display} not found.")
        for h in handles:
            _dxva2.DestroyPhysicalMonitor(h)
        sys.exit(1)

    label = "on" if state == 1 else "off"
    monitor_names = sbc.list_monitors()
    for i, handle in targets:
        ok = _dxva2.SetVCPFeature(handle, 0xD6, state)
        name = monitor_names[i] if i < len(monitor_names) else f"Monitor {i}"
        print(f"Turned {label}: {name}" if ok else f"Failed to turn {label}: {name}")

    for h in handles:
        _dxva2.DestroyPhysicalMonitor(h)


def _desktop_monitor_ddc_brightness(op: str, value: int = 0) -> int | None:
    """Identify Acer by its DDC model, never by enumeration order."""
    handles = _enum_physical_monitors(require_caps=False)
    try:
        for handle in handles:
            length = wintypes.DWORD()
            if not _dxva2.GetCapabilitiesStringLength(handle, ctypes.byref(length)):
                continue
            if not 0 < length.value <= 65536:
                continue
            caps = ctypes.create_string_buffer(length.value)
            if not _dxva2.CapabilitiesRequestAndCapabilitiesReply(handle, caps, length.value):
                continue
            model = caps.value.decode('ascii', errors='replace').lower()
            if f"model({MONITOR_NAMES['desktop']}".lower() not in model:
                continue
            current, maximum, kind = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            if op != 'get':
                if not _dxva2.SetVCPFeature(handle, 0x10, value):
                    print(f"Acer brightness command failed: {value}%.", flush=True)
                    return None
            if not _dxva2.GetVCPFeatureAndVCPFeatureReply(
                    handle, 0x10, ctypes.byref(kind), ctypes.byref(current), ctypes.byref(maximum)):
                print('Could not verify Acer brightness.', flush=True)
                return None
            print(f"Acer brightness readback: {current.value}/{maximum.value}.", flush=True)
            if op != 'get' and current.value != value:
                return None
            return current.value
        print('Acer not found by DDC model.', flush=True)
        return None
    finally:
        for handle in handles:
            _dxva2.DestroyPhysicalMonitor(handle)


def _wake_lg_by_display_path() -> bool:
    """Find an isolated LG display through Windows even when DDC reads fail."""
    class SourceName(ctypes.Structure):
        _fields_ = [('header', _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                    ('name', ctypes.c_wchar * 32)]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD),
                    ('device', ctypes.c_wchar * 32)]

    paths, _, _, _ = _query_display_config()
    target = next((p for p in paths if MONITOR_NAMES.get('second', 'LG FHD')
                   in _get_monitor_name(p)), None)
    if target is None:
        return False
    def source_key(p):
        return (p.sourceInfo.adapterId.LowPart, p.sourceInfo.adapterId.HighPart, p.sourceInfo.id)
    if sum(source_key(p) == source_key(target) for p in paths) != 1:
        print("LG wake mapping requires its restored separate desktop.", flush=True)
        return False
    source = SourceName()
    source.header.type = 1
    source.header.size = ctypes.sizeof(source)
    source.header.adapterId = target.sourceInfo.adapterId
    source.header.id = target.sourceInfo.id
    if _user32.DisplayConfigGetDeviceInfo(ctypes.byref(source.header)) != 0:
        return False
    result = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    @callback_type
    def visit(hmon, dc, rect, data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)) or info.device != source.name:
            return True
        count = wintypes.DWORD()
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
            return True
        physical = (_PHYSICAL_MONITOR * count.value)()
        if _dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count, physical):
            for monitor in physical:
                try:
                    result.append(bool(_dxva2.SetVCPFeature(monitor.handle, 0xD6, 1)))
                finally:
                    _dxva2.DestroyPhysicalMonitor(monitor.handle)
        return True
    _user32.EnumDisplayMonitors(None, None, visit, 0)
    ok = bool(result) and all(result)
    print(f"LG wake via Windows display mapping: {'sent' if ok else 'failed'}.", flush=True)
    return ok


def _set_lg_sleep(sleep: bool) -> bool:
    """Identify LG by its own DDC model, including in a cloned group."""
    name = MONITOR_NAMES.get("second", "LG FHD")
    handles = _enum_physical_monitors(require_caps=False)
    try:
        for handle in handles:
            length = wintypes.DWORD()
            if not _dxva2.GetCapabilitiesStringLength(handle, ctypes.byref(length)):
                continue
            if not 0 < length.value <= 65536:
                continue
            caps = ctypes.create_string_buffer(length.value)
            if not _dxva2.CapabilitiesRequestAndCapabilitiesReply(handle, caps, length.value):
                continue
            if f"model({name}".lower() not in caps.value.decode("ascii", errors="replace").lower():
                continue
            # D6=2 is standby, rather than hard power-off, so DDC can wake it.
            ok = bool(_dxva2.SetVCPFeature(handle, 0xD6, 2 if sleep else 1))
            print(f"LG {'standby' if sleep else 'wake'} command {'sent' if ok else 'failed'}.", flush=True)
            return ok
        if not sleep:
            return _wake_lg_by_display_path()
        print("Could not identify LG for its power command.", flush=True)
        return False
    finally:
        for handle in handles:
            _dxva2.DestroyPhysicalMonitor(handle)


def _set_power_by_name(monitor_name: str, state: int):
    """Turn a monitor on/off by matching its name (case-insensitive substring).
    Power-on uses require_caps=False so powered-off monitors are still reachable.
    """
    powering_on = (state == 1)
    handles = _enum_physical_monitors(require_caps=not powering_on)
    monitor_names = sbc.list_monitors()
    label = "on" if powering_on else "off"
    found = False
    for i, handle in enumerate(handles):
        name = monitor_names[i] if i < len(monitor_names) else ''
        if monitor_name.lower() in name.lower():
            ok = _dxva2.SetVCPFeature(handle, 0xD6, state)
            print(f"Turned {label}: {name}" if ok else f"Failed to turn {label}: {name}")
            found = True
    for h in handles:
        _dxva2.DestroyPhysicalMonitor(h)
    if not found:
        print(f"Monitor '{monitor_name}' not found for power control.")


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

class _IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Activate',
            (['in'], ctypes.POINTER(GUID)),
            (['in'], ctypes.c_ulong),
            (['in'], ctypes.c_void_p),
            (['out'], ctypes.POINTER(ctypes.POINTER(IUnknown)))),
        COMMETHOD([], HRESULT, 'OpenPropertyStore'),
        COMMETHOD([], HRESULT, 'GetId', (['out'], ctypes.POINTER(ctypes.c_wchar_p))),
        COMMETHOD([], HRESULT, 'GetState'),
    ]


class _IAudioEndpointVolume(IUnknown):
    _iid_ = GUID('{5CDF2C82-841E-4546-9722-0CF74078229A}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'RegisterControlChangeNotify', (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'UnregisterControlChangeNotify', (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'GetChannelCount', (['out'], ctypes.POINTER(ctypes.c_uint))),
        COMMETHOD([], HRESULT, 'SetMasterVolumeLevel',
            (['in'], ctypes.c_float), (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'SetMasterVolumeLevelScalar',
            (['in'], ctypes.c_float), (['in'], ctypes.c_void_p)),
        COMMETHOD([], HRESULT, 'GetMasterVolumeLevel', (['out'], ctypes.POINTER(ctypes.c_float))),
        COMMETHOD([], HRESULT, 'GetMasterVolumeLevelScalar', (['out'], ctypes.POINTER(ctypes.c_float))),
    ]

_CLSCTX_ALL = 23  # INPROC_SERVER | INPROC_HANDLER | LOCAL_SERVER | REMOTE_SERVER


class _IMMDeviceCollection(IUnknown):
    _iid_ = GUID('{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetCount', (['out'], ctypes.POINTER(ctypes.c_uint))),
        COMMETHOD([], HRESULT, 'Item',
            (['in'], ctypes.c_uint),
            (['out'], ctypes.POINTER(ctypes.POINTER(_IMMDevice)))),
    ]


class _IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnumAudioEndpoints',
            (['in'], ctypes.c_uint), (['in'], ctypes.c_uint),
            (['out'], ctypes.POINTER(ctypes.POINTER(_IMMDeviceCollection)))),
        COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint',
            (['in'], ctypes.c_uint), (['in'], ctypes.c_uint),
            (['out'], ctypes.POINTER(ctypes.POINTER(_IMMDevice)))),
    ]


_CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
_CLSID_PolicyConfig = GUID('{294935CE-F637-4E7C-A41B-AB255460B862}')


def _audio_device_name(guid: str) -> str:
    key_path = rf'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\{guid}\Properties'
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
        endpoint, _ = winreg.QueryValueEx(key, '{a45c254e-df1c-4efd-8020-67d146a850e0},2')
        try:
            device, _ = winreg.QueryValueEx(key, '{b3f8fa53-0004-438e-9003-51a46e139bfc},6')
        except Exception:
            device = ''
        winreg.CloseKey(key)
        return f"{endpoint} ({device})" if device else endpoint
    except Exception:
        return guid


def _get_audio_devices() -> list[tuple[str, str]]:
    """Return list of (endpoint_id, friendly_name) for all active render devices."""
    enumerator = comtypes.client.CreateObject(_CLSID_MMDeviceEnumerator, interface=_IMMDeviceEnumerator)
    collection = enumerator.EnumAudioEndpoints(0, 1)  # eRender, DEVICE_STATE_ACTIVE
    count = collection.GetCount()
    devices = []
    for i in range(count):
        dev = collection.Item(i)
        endpoint_id = dev.GetId()
        guid = endpoint_id.split('}.')[1]
        name = _audio_device_name(guid)
        devices.append((endpoint_id, name))
    return devices


def _get_default_audio_id() -> str:
    enumerator = comtypes.client.CreateObject(_CLSID_MMDeviceEnumerator, interface=_IMMDeviceEnumerator)
    dev = enumerator.GetDefaultAudioEndpoint(0, 0)  # eRender, eConsole
    return dev.GetId()


def _default_endpoint_volume() -> _IAudioEndpointVolume:
    """Return the IAudioEndpointVolume interface for the current default render device."""
    enumerator = comtypes.client.CreateObject(_CLSID_MMDeviceEnumerator, interface=_IMMDeviceEnumerator)
    dev = enumerator.GetDefaultAudioEndpoint(0, 0)  # eRender, eConsole
    return dev.Activate(_IAudioEndpointVolume._iid_, _CLSCTX_ALL, None).QueryInterface(_IAudioEndpointVolume)


def _get_master_volume() -> float | None:
    """Return the current default device's master volume (0.0-1.0), or None on failure."""
    try:
        return _default_endpoint_volume().GetMasterVolumeLevelScalar()
    except Exception:
        return None


def _set_master_volume(level: float) -> bool:
    """Set the current default device's master volume (0.0-1.0)."""
    try:
        _default_endpoint_volume().SetMasterVolumeLevelScalar(level, None)
        return True
    except Exception:
        return False


def _set_default_audio(endpoint_id: str) -> bool:
    FuncType = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint)
    # Try multiple vtable offsets — different device types respond to different ones
    for vtable_idx in [5, 12]:
        try:
            obj = comtypes.client.CreateObject(_CLSID_PolicyConfig)
            this = ctypes.cast(obj, ctypes.c_void_p).value
            vtable_ptr = ctypes.cast(this, ctypes.POINTER(ctypes.c_void_p))[0]
            fn = ctypes.cast(ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))[vtable_idx], FuncType)
            fn(this, endpoint_id, 0)
        except Exception:
            pass
    return _get_default_audio_id() == endpoint_id


def _find_audio_by_name(name: str) -> str | None:
    """Find endpoint_id by matching friendly name (case-insensitive substring)."""
    name_lower = name.lower()
    for endpoint_id, friendly in _get_audio_devices():
        if name_lower in friendly.lower():
            return endpoint_id
    return None


# ---------------------------------------------------------------------------
# Monitor brightness commands
# ---------------------------------------------------------------------------

def list_monitors():
    monitors = sbc.list_monitors()
    if not monitors:
        print("No monitors found.")
        return
    print(f"Found {len(monitors)} monitor(s):")
    for i, name in enumerate(monitors):
        try:
            brightness = sbc.get_brightness(display=i)
            level = brightness[0] if isinstance(brightness, list) else brightness
            print(f"  [{i}] {name} — brightness: {level}%")
        except Exception:
            print(f"  [{i}] {name} — brightness unreadable")


def get_brightness(display=None):
    try:
        result = sbc.get_brightness(display=display) if display is not None else sbc.get_brightness()
        levels = result if isinstance(result, list) else [result]
        monitors = sbc.list_monitors()
        for i, level in enumerate(levels):
            name = monitors[i] if i < len(monitors) else f"Monitor {i}"
            print(f"  {name}: {level}%")
    except Exception as e:
        print(f"Error reading brightness: {e}")
        sys.exit(1)


def set_brightness(level: int, display=None):
    if not 0 <= level <= 100:
        print("Brightness must be between 0 and 100.")
        sys.exit(1)
    try:
        if display is not None:
            sbc.set_brightness(level, display=display)
            monitors = sbc.list_monitors()
            name = monitors[display] if display < len(monitors) else f"Monitor {display}"
            print(f"Set {name} brightness to {level}%")
        else:
            sbc.set_brightness(level)
            print(f"Set all monitors to {level}%")
    except Exception as e:
        print(f"Error setting brightness: {e}")
        sys.exit(1)


def fade_brightness(target: int, display=None, steps=20, delay=0.03):
    try:
        current_list = sbc.get_brightness(display=display) if display is not None else sbc.get_brightness()
        current = current_list[0] if isinstance(current_list, list) else current_list
    except Exception as e:
        print(f"Error reading brightness: {e}")
        sys.exit(1)
    step_size = (target - current) / steps
    print(f"Fading {current}% → {target}%")
    for i in range(1, steps + 1):
        value = max(0, min(100, round(current + step_size * i)))
        try:
            sbc.set_brightness(value, display=display) if display is not None else sbc.set_brightness(value)
        except Exception:
            pass
        time.sleep(delay)
    print("Done.")


# ---------------------------------------------------------------------------
# Audio commands
# ---------------------------------------------------------------------------

def list_audio():
    devices = _get_audio_devices()
    default_id = _get_default_audio_id()
    print(f"Found {len(devices)} audio output device(s):")
    for i, (endpoint_id, name) in enumerate(devices):
        marker = " [default]" if endpoint_id == default_id else ""
        print(f"  [{i}] {name}{marker}")


def set_audio(name: str):
    endpoint_id = _find_audio_by_name(name)
    if endpoint_id is None:
        print(f"No audio device matching '{name}' found. Run 'audio list' to see available devices.")
        sys.exit(1)
    _, friendly = next((d for d in _get_audio_devices() if d[0] == endpoint_id), (None, name))
    if _set_default_audio(endpoint_id):
        print(f"Audio switched to: {friendly}")
    else:
        print(f"Failed to switch audio to: {friendly}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# TV control via Hisense's local VIDAA MQTT API
# ---------------------------------------------------------------------------

def _tv_wake():
    """Wake the TV from standby via Wake-on-LAN magic packet."""
    mac = bytes.fromhex(TV_MAC.replace(":", "").replace("-", ""))
    if len(mac) != 6:
        raise ValueError("TV_MAC must contain six bytes")
    result = _subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "& {" + Path(__file__).with_name("wake_tv.ps1").read_text(
             encoding="utf-8") + "} -MacAddress '" + mac.hex() + "'"],
        capture_output=True, text=True, timeout=15,
        creationflags=_subprocess.CREATE_NO_WINDOW)
    if result.returncode == 0:
        print(result.stdout.strip(), flush=True)
        print("Waiting for TV to confirm power-on...", flush=True)
        return
    print(f"Adapter-specific wake failed: {result.stderr.strip()}", flush=True)
    import socket as _socket
    mac = TV_MAC.replace(":", "")
    magic = bytes.fromhex("FF" * 6 + mac * 16)
    with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        s.sendto(magic, ("255.255.255.255", 9))
    print("Wake packet sent; TV power-on is not confirmed yet.", flush=True)

def _tv_discover() -> list[str]:
    """Find the configured TV MAC in Windows' LAN neighbor table."""
    import ipaddress
    import re
    try:
        mac = TV_MAC.replace(':', '').replace('-', '')
        if not re.fullmatch(r'[0-9a-fA-F]{12}', mac):
            raise ValueError('TV_MAC must contain 12 hexadecimal digits')
        result = _subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "& {" + Path(__file__).with_name("tv_discovery.ps1").read_text(
                 encoding="utf-8") + "} -MacAddress '" +
             mac + "'"],
            capture_output=True, text=True, timeout=15,
            creationflags=_subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            print(f"TV discovery failed: {result.stderr.strip()}")
            return []
        return [str(ipaddress.IPv4Address(line.strip()))
                for line in result.stdout.splitlines() if line.strip()]
    except (OSError, ValueError, _subprocess.TimeoutExpired) as exc:
        print(f"TV discovery failed: {exc}")
        return []


def _tv_connect_at(address: str) -> HisenseTV | None:
    """Try the control API, avoiding a long MQTT wait on a closed port."""
    import socket
    from threading import Event
    try:
        with socket.create_connection((address, TV_PORT), timeout=1):
            pass
    except OSError:
        return None
    pairing_required = Event()
    tv = HisenseTV(
        address, port=TV_PORT, verify_ssl=False,
        certfile=str(HISENSE_CERT_FILE), keyfile=str(HISENSE_KEY_FILE),
        enable_persistence=True,
        on_auth_required=pairing_required.set,
    )
    tv._game_mode_pairing_required = pairing_required
    try:
        if tv.connect():
            return tv
    except Exception as exc:
        print(f"TV connection to {address} failed: {exc}")
    try:
        tv.disconnect()
    except Exception:
        pass
    return None


def _tv_connect() -> HisenseTV | None:
    """Connect to the TV's local control API. Returns a connected client, or None if unreachable."""
    global TV_IP
    tv = _tv_connect_at(TV_IP)
    if tv is not None:
        return tv
    for address in _tv_discover():
        if address == TV_IP:
            continue
        tv = _tv_connect_at(address)
        if tv is not None:
            print(f"Discovered TV at {address} (previous address: {TV_IP}).")
            TV_IP = address
            return tv
    return None

def _tv_connect_retry(timeout: float = 25.0, interval: float = 2.0) -> HisenseTV | None:
    """Keep trying _tv_connect() until it succeeds or timeout elapses.

    Wake-on-LAN doesn't guarantee the TV's network stack is up right away —
    boot time varies — so a single attempt after a fixed delay is fragile.
    """
    deadline = time.monotonic() + timeout
    waits = 0
    while True:
        tv = _tv_connect()
        if tv is not None:
            try:
                if _tv_confirm_awake(tv):
                    return tv
            except Exception as exc:
                print(f"TV power check failed: {exc}", flush=True)
            tv.disconnect()
            print("TV did not confirm it is awake; stopping startup.", flush=True)
            return None
        if time.monotonic() >= deadline:
            return None
        print("TV not ready yet; retrying...", flush=True)
        time.sleep(interval)
        waits += 1
        if waits % 2 == 0 and time.monotonic() < deadline:
            print("TV still unavailable after two waits; resending wake packet...", flush=True)
            _tv_wake()


def _tv_pair(tv) -> bool:
    """Handle PIN entry on the main thread, never in an MQTT callback."""
    _show_startup_console()
    print("TV pairing required. Enter the 4-digit code shown on the TV.", flush=True)
    while True:
        try:
            pin = input("TV pairing code (C to return to retry/skip/cancel): ").strip()
        except (EOFError, OSError, KeyboardInterrupt):
            return False
        if pin.lower() in ("c", "cancel"):
            return False
        if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
            print("Enter exactly four digits, or C to cancel pairing.", flush=True)
            continue
        if tv.authenticate(pin) or tv.is_authenticated():
            print("TV pairing successful; continuing initialization.", flush=True)
            return True
        print("TV did not accept pairing. Check the current code on the TV and try again.", flush=True)


def _tv_confirm_awake(tv, timeout: float = 30.0) -> bool:
    """Confirm a TV-reported awake state and handle explicit pairing requests."""
    from collections import deque
    updates = deque()
    previous_callback = tv.on_state_change

    def on_state(state):
        updates.append(state)
        if previous_callback:
            previous_callback(state)

    tv.on_state_change = on_state
    deadline = time.monotonic() + timeout
    wake_sent = False
    waits = 0
    try:
        while time.monotonic() < deadline:
            reported = tv.get_state(timeout=2.0)
            print(f"TV state query: {reported!r}", flush=True)
            pairing_event = getattr(tv, "_game_mode_pairing_required", None)
            auth_requested = (pairing_event is not None and pairing_event.is_set())
            # Legacy TVs can accept control without issuing an auth token.
            # Only an explicit TV request is evidence that a PIN is needed.
            if auth_requested or tv.needs_authentication() or (
                    isinstance(reported, dict) and
                    reported.get("statetype") in ("authentication", "authenticationcode")):
                if not _tv_pair(tv):
                    return False
                if pairing_event is not None:
                    pairing_event.clear()
                updates.clear()
                # Time spent entering a PIN must not consume the wake timeout.
                deadline = time.monotonic() + timeout
                continue
            # The library can return the standby state received during connect
            # without emitting another callback. Use it to trigger wake.
            if isinstance(reported, dict) and reported.get("statetype") == "fake_sleep_0" and not wake_sent:
                print("TV reports standby; sending API power-on command...", flush=True)
                if not tv.send_key("KEY_POWER"):
                    print("API power-on command failed to send.", flush=True)
                    return False
                wake_sent = True
            while updates:
                state = updates.popleft()
                print(f"TV state broadcast: {state!r}", flush=True)
                kind = state.get("statetype")
                if kind in ("authentication", "authenticationcode"):
                    if not _tv_pair(tv):
                        return False
                    if pairing_event is not None:
                        pairing_event.clear()
                    updates.clear()
                    deadline = time.monotonic() + timeout
                    reported = None
                    break
                elif kind == "fake_sleep_0":
                    if not wake_sent:
                        print("TV is in standby; sending power-on command...", flush=True)
                        if not tv.send_key("KEY_POWER"):
                            return False
                        wake_sent = True
                elif kind in ("sourceswitch", "fake_sleep_1"):
                    print(f"TV reports awake ({kind}).", flush=True)
                    return True
            # get_state() also returns broadcasts received during connect.
            # An unchanged awake state is valid; it need not change again.
            if isinstance(reported, dict) and reported.get("statetype") in ("sourceswitch", "fake_sleep_1"):
                print(f"TV reports awake ({reported['statetype']}).", flush=True)
                return True
            print("Waiting for TV to confirm power-on...", flush=True)
            time.sleep(1)
            waits += 1
            if waits % 2 == 0 and time.monotonic() < deadline:
                print("Power-on still unconfirmed after two waits; resending wake packet...", flush=True)
                _tv_wake()
        return False
    finally:
        tv.on_state_change = previous_callback

def _tv_set_input(tv: HisenseTV, input_name: str) -> bool:
    """Switch TV to a named input ('tv', 'av', 'hdmi1'-'hdmi4'). Returns True on success."""
    if tv.set_source(input_name):
        print(f"TV input set to {input_name.upper()}")
        return True
    print(f"Failed to set TV input to {input_name}")
    return False

def _tv_set_volume(tv: HisenseTV, target: int):
    """Set the TV's volume directly (0-100)."""
    if tv.set_volume(target):
        print(f"TV volume set to {target}")
    else:
        print(f"Warning: failed to set TV volume to {target}")

def _tv_standby(tv: HisenseTV):
    """Put the TV into standby, if it's currently on."""
    if tv.power_off():
        print("TV standby sent.")
    else:
        print("Warning: failed to send TV standby.")


def _startup_step(label, action):
    """Return (continue_startup, result), offering retry or skip on failure."""
    while True:
        try:
            result = action()
            if result:
                return True, result
            detail = "The step did not complete successfully."
        except Exception as exc:
            detail = str(exc)
        print(f"{label} failed: {detail}", flush=True)
        while True:
            try:
                choice = input("[R]etry / [S]kip / [C]ancel: ").strip().lower()
            except (EOFError, OSError, KeyboardInterrupt):
                choice = "c"
            if choice in ("r", "retry", "s", "skip", "c", "cancel"):
                break
            print("Enter R to retry, S to skip, or C to cancel.", flush=True)
        if choice in ("r", "retry"):
            continue
        if choice in ("s", "skip"):
            print(f"Skipped: {label}.", flush=True)
            return True, None
        print("Game mode initialization cancelled.", flush=True)
        return False, None


def _startup_tv():
    _tv_wake()
    return _tv_connect_retry()


def _show_startup_console(title="Game Mode - Starting"):
    """Give pythonw shortcut launches a console for live progress."""
    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow():
        return
    if kernel32.AllocConsole():
        kernel32.SetConsoleTitleW(title)
        set_console_streams(
            open("CONOUT$", "w", encoding="utf-8", buffering=1),
            open("CONOUT$", "w", encoding="utf-8", buffering=1))
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
        print(f"Log file: {LOG_FILE}", flush=True)


def _serialize_game_mode(operation):
    """Protect saved state from overlapping shortcut invocations."""
    @wraps(operation)
    def run(*args, **kwargs):
        if operation.__name__ == "game_mode_on":
            _show_startup_console()
        elif operation.__name__ == "game_mode_off":
            _show_startup_console("Game Mode - Restoring")
        with (_state_dir / "game_mode.lock").open("a+b") as lock:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("A Game Mode operation is already running. Wait for it to finish.")
                return False
            try:
                return operation(*args, **kwargs)
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return run


@_serialize_game_mode
def game_mode_on():
    if STATE_FILE.exists():
        print("Keeping the original saved session; checking TV power...", flush=True)
        proceed, tv = _startup_step("Waking and connecting to TV", _startup_tv)
        if not proceed:
            return False
        if tv is not None:
            tv.disconnect()
        print("Reapplying gaming display layout...", flush=True)
        proceed, cloned = _startup_step(
            "Duplicating displays", lambda: _set_display_topology(clone=True))
        if not proceed:
            return False
        if not cloned:
            return True
        if _desktop_monitor_ddc_brightness('set', 0) is None:
            _popup_warning("Could not dim Acer. Check the app log for brightness readback.")
        if not _set_lg_sleep(True):
            _popup_warning("Displays are duplicated, but LG did not accept the standby command.")
        return True

    print("Activating game mode...")

    print("[1/7] Checking controller connection...", flush=True)
    proceed, controller = _startup_step("Checking controller connection", _find_connected_controller)
    if not proceed:
        return False

    print("[2/7] Reading controller battery (may take up to 10 seconds)...", flush=True)
    battery_warning = _controller_battery_warning()
    if battery_warning:
        _popup_warning(battery_warning)
        print(f"Warning: {battery_warning}")

    # Wake TV and verify it's reachable before doing anything else
    print("[3/7] Waking TV and waiting for a connection...", flush=True)
    proceed, tv = _startup_step("Waking and connecting to TV", _startup_tv)
    if not proceed:
        return False

    # Save complete display config so restore can reconstruct the exact topology
    print("[4/7] Saving display layout, Acer brightness and Windows volume...", flush=True)
    pre_paths, pre_modes, pre_nP, pre_nM = _query_display_config()
    paths_bytes = list(bytes(pre_paths))
    modes_bytes = list(bytes(pre_modes))

    # Save desktop monitor brightness via DDC/CI (sbc can't reach it when cloned with the TV)
    desktop_brightness = _desktop_monitor_ddc_brightness('get')

    # Save Windows master volume so it can be restored after game mode ends
    windows_volume = _get_master_volume()

    STATE_FILE.write_text(json.dumps({
        "display_nPaths":       pre_nP,
        "display_nModes":       pre_nM,
        "display_paths_bytes":  paths_bytes,
        "display_modes_bytes":  modes_bytes,
        "desktop_brightness":   desktop_brightness,
        "windows_volume":       windows_volume,
    }))

    # Dim the desktop monitor before cloning; restore its saved level on exit.
    print("[5/7] Dimming Acer and duplicating Acer, LG and TV...", flush=True)
    if desktop_brightness is not None:
        if _desktop_monitor_ddc_brightness('set', 0) is not None:
            print("Desktop monitor brightness set to 0.")
        else:
            print("Warning: could not set desktop monitor brightness to 0.")

    # Duplicate the same desktop across all connected displays.
    proceed, cloned = _startup_step(
        "Duplicating displays", lambda: _set_display_topology(clone=True))
    if not proceed:
        if tv is not None:
            tv.disconnect()
        return False
    if cloned:
        print("Acer, LG and TV duplicate the same screen.")
    if cloned and not _set_lg_sleep(True):
        _popup_warning("Displays are duplicated, but LG did not accept the standby command.")

    # Switch audio for game mode
    print("[6/7] Switching audio to the TV...", flush=True)
    audio_target = GAME_MODE_CONFIG["audio_on"]
    endpoint_id = _find_audio_by_name(audio_target)
    if endpoint_id is None:
        _popup_error(
            f"Audio device '{audio_target}' not found.\n\n"
            "Make sure the device is plugged in and powered on.\n"
            "Run 'audio list' to see available devices."
        )
        print(f"Error: audio device '{audio_target}' not found.")
    else:
        devices = _get_audio_devices()
        _, friendly = next((d for d in devices if d[0] == endpoint_id), (None, audio_target))
        if _set_default_audio(endpoint_id):
            print(f"Audio switched to: {friendly}")
            if _set_master_volume(1.0):
                print("Windows volume maximized.")
            else:
                print("Warning: could not maximize Windows volume.")
        else:
            _popup_error(f"Failed to switch audio to '{friendly}'.\n\nThe device may not be responding.")
            print(f"Error: failed to switch audio to '{friendly}'.")

    # Set TV input + volume for gaming (already confirmed connected above)
    print("[7/7] Setting TV input and volume...", flush=True)
    if tv is not None:
        _tv_set_input(tv, GAME_MODE_CONFIG["tv_input"])
        _tv_set_volume(tv, GAME_MODE_TV_VOLUME)
        tv.disconnect()
    else:
        print("Skipped TV input and volume: TV connection was skipped.", flush=True)

    print("Game mode ON. Ready to play.", flush=True)


@_serialize_game_mode
def game_mode_off():
    print("Deactivating game mode...")

    print("[1/6] Loading saved settings...", flush=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    restore_succeeded = True

    # Restore audio to configured speakers device
    print("[2/6] Restoring desktop audio and Windows volume...", flush=True)
    audio_off_target = GAME_MODE_CONFIG["audio_off"]
    speakers_id = _find_audio_by_name(audio_off_target)
    if speakers_id is None:
        _popup_error(f"Audio device '{audio_off_target}' not found.")
        print(f"Error: audio device '{audio_off_target}' not found.")
        restore_succeeded = False
    elif _set_default_audio(speakers_id):
        print(f"Audio restored to: {audio_off_target}")
        if _set_master_volume(0.5):
            print("Windows volume restored to 50%.")
        else:
            print("Warning: could not restore Windows volume.")
            restore_succeeded = False
    else:
        _popup_error(f"Failed to restore audio to '{audio_off_target}'.")
        print(f"Error: failed to restore audio to '{audio_off_target}'.")
        restore_succeeded = False

    # Just put the TV into standby — no input/volume switching
    print("[3/6] Connecting to TV and requesting standby...", flush=True)
    tv = _tv_connect()
    if tv:
        _tv_standby(tv)
        tv.disconnect()

    # Restore exact display config saved before game mode
    print("[4/6] Restoring saved display layout...", flush=True)
    display_restored = False
    if "display_paths_bytes" in state:
        nP = state["display_nPaths"]
        nM = state["display_nModes"]
        saved_paths = (_DISPLAYCONFIG_PATH_INFO * nP).from_buffer_copy(
            bytes(state["display_paths_bytes"]))
        saved_modes = (_DISPLAYCONFIG_MODE_INFO * nM).from_buffer_copy(
            bytes(state["display_modes_bytes"]))
        ret = _user32.SetDisplayConfig(nP, saved_paths, nM, saved_modes,
            _SDC_USE_SUPPLIED_DISPLAY_CONFIG | _SDC_APPLY | _SDC_SAVE_TO_DATABASE | _SDC_ALLOW_CHANGES)
        if ret == 0:
            display_restored = True
            print("Display config restored.")
        else:
            print(f"Exact restore failed ({ret}), falling back to extend...")

    if not display_restored:
        if not _set_display_topology(clone=False):
            _popup_error("Failed to restore display to extend mode.")
            print("Warning: could not restore display topology.")
            restore_succeeded = False
        else:
            print("Display restored to extend mode.")

    print("Waking LG after display layout restoration...", flush=True)
    for delay in (1, 2, 3):
        time.sleep(delay)
        if _set_lg_sleep(False):
            break
    else:
        restore_succeeded = False

    # Restore desktop monitor brightness — DDC/CI needs a few seconds to settle after a topology change
    print("[5/6] Restoring Acer brightness...", flush=True)
    prev_brightness = state.get("desktop_brightness")
    if prev_brightness is None:
        prev_brightness = 100
        print("No saved Acer brightness; restoring to 100%.", flush=True)
    if prev_brightness is not None:
        for wait in (2, 3, 5):
            print(f"Waiting {wait} seconds for monitor brightness control...", flush=True)
            time.sleep(wait)
            if _desktop_monitor_ddc_brightness('set', prev_brightness) is not None:
                print(f"Desktop monitor brightness restored to {prev_brightness}%.")
                break
        else:
            print(f"Warning: could not restore desktop monitor brightness to {prev_brightness}%.")
            restore_succeeded = False

    # Bring any windows left on another monitor back to the main screen
    print("[6/6] Moving windows back to the main screen...", flush=True)
    _bring_windows_to_main_screen()

    if restore_succeeded:
        STATE_FILE.unlink(missing_ok=True)
        print("Game mode OFF. Restore complete.", flush=True)
    else:
        print("Restore incomplete. Saved state kept; run Restore Mode again to retry.")
    return restore_succeeded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Monitor brightness, power, audio, and game mode controller.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python brightness.py list                    # list monitors
  python brightness.py set 75                  # set all monitors to 75%
  python brightness.py set 75 -d 0             # set monitor 0 to 75%
  python brightness.py fade 30                 # fade all monitors to 30%
  python brightness.py off -d 0               # turn off monitor 0
  python brightness.py on -d 0                # turn on monitor 0
  python brightness.py audio list              # list audio devices
  python brightness.py audio set "TV"          # switch audio to a device by name
  python brightness.py game-mode on            # activate game mode
  python brightness.py game-mode off           # deactivate game mode
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List monitors and brightness")

    p_get = subparsers.add_parser("get", help="Get current brightness")
    p_get.add_argument("-d", "--display", type=int, default=None)

    p_set = subparsers.add_parser("set", help="Set brightness (0-100)")
    p_set.add_argument("level", type=int)
    p_set.add_argument("-d", "--display", type=int, default=None)

    p_fade = subparsers.add_parser("fade", help="Smoothly fade to target brightness")
    p_fade.add_argument("level", type=int)
    p_fade.add_argument("-d", "--display", type=int, default=None)
    p_fade.add_argument("--steps", type=int, default=20)
    p_fade.add_argument("--delay", type=float, default=0.03)

    p_off = subparsers.add_parser("off", help="Turn off monitor(s)")
    p_off.add_argument("-d", "--display", type=int, default=None)

    p_on = subparsers.add_parser("on", help="Turn on monitor(s)")
    p_on.add_argument("-d", "--display", type=int, default=None)

    p_audio = subparsers.add_parser("audio", help="Audio device commands")
    audio_sub = p_audio.add_subparsers(dest="audio_cmd", required=True)
    audio_sub.add_parser("list", help="List audio output devices")
    p_audio_set = audio_sub.add_parser("set", help="Set default audio device")
    p_audio_set.add_argument("name", help="Device name (partial match)")

    p_gm = subparsers.add_parser("game-mode", help="Game mode (monitor off + audio switch)")
    gm_sub = p_gm.add_subparsers(dest="gm_cmd", required=True)
    gm_sub.add_parser("on", help="Activate game mode")
    gm_sub.add_parser("off", help="Deactivate game mode")

    args = parser.parse_args()

    if args.command == "list":
        list_monitors()
    elif args.command == "get":
        get_brightness(display=args.display)
    elif args.command == "set":
        set_brightness(args.level, display=args.display)
    elif args.command == "fade":
        fade_brightness(args.level, display=args.display, steps=args.steps, delay=args.delay)
    elif args.command == "off":
        _set_power(args.display, state=4)
    elif args.command == "on":
        _set_power(args.display, state=1)
    elif args.command == "audio":
        if args.audio_cmd == "list":
            list_audio()
        elif args.audio_cmd == "set":
            set_audio(args.name)
    elif args.command == "game-mode":
        if args.gm_cmd == "on":
            game_mode_on()
        elif args.gm_cmd == "off":
            game_mode_off()


if __name__ == "__main__":
    main()
