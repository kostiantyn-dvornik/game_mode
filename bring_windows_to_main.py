import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_mode import _bring_windows_to_main_screen

if __name__ == "__main__":
    _bring_windows_to_main_screen(include_minimized=True)
