import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_mode import restore_display_layout

if __name__ == "__main__":
    restore_display_layout()
