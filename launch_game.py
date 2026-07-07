import sys
import os
import ctypes
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_mode import game_mode_on


def popup_error(msg: str):
    ctypes.windll.user32.MessageBoxW(0, msg, "Launch Game", 0x10)


def launch_game(steam_app_id: str | None = None, exe_path: str | None = None, name: str | None = None):
    """Activate game mode, then launch a game via Steam app ID or a direct executable/shortcut path."""
    if game_mode_on() is False:
        return

    label = name or steam_app_id or exe_path
    target = f"steam://rungameid/{steam_app_id}" if steam_app_id else exe_path
    try:
        os.startfile(target)
    except Exception as e:
        popup_error(f"Failed to launch {label}:\n{e}")


def main():
    parser = argparse.ArgumentParser(
        description="Activate game mode, then launch a game.",
        epilog="""
examples:
  python launch_game.py --steam 1297900 --name "Gothic 1 Remake"
  python launch_game.py --exe "C:\\Games\\MyGame\\game.exe" --name "My Game"
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--steam", dest="steam_app_id", help="Steam app ID of the game to launch")
    target.add_argument("--exe", dest="exe_path", help="Path to a game executable or shortcut (.exe/.lnk/.url)")
    parser.add_argument("--name", help="Friendly game name, used in messages", default=None)
    args = parser.parse_args()
    launch_game(steam_app_id=args.steam_app_id, exe_path=args.exe_path, name=args.name)


if __name__ == "__main__":
    main()
