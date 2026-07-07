"""
Starts Steam minimized to the tray (no main window / login popups),
meant to run once at Windows login via a Startup-folder shortcut.

Running Steam silently at login means it's already running by the time
a game is launched later via launch_game.py, so no Steam window ever
flashes up mid-game-mode.
"""

import sys
import winreg
import subprocess
import ctypes
from pathlib import Path


def _popup_error(msg: str):
    ctypes.windll.user32.MessageBoxW(0, msg, "Steam Silent Start", 0x10)


def _find_steam_exe() -> Path | None:
    """Look up steam.exe via the registry keys Steam itself maintains."""
    candidates = (
        (winreg.HKEY_CURRENT_USER,  r"Software\Valve\Steam",              "SteamExe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",  "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam",              "InstallPath"),
    )
    for hive, key_path, value_name in candidates:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        exe = Path(value) if value_name == "SteamExe" else Path(value) / "steam.exe"
        if exe.exists():
            return exe
    return None


def main():
    steam_exe = _find_steam_exe()
    if steam_exe is None:
        _popup_error("Could not find steam.exe via the registry.\n\nIs Steam installed?")
        sys.exit(1)
    subprocess.Popen([str(steam_exe), "-silent"])


if __name__ == "__main__":
    main()
