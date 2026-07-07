import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_mode import game_mode_off

if __name__ == "__main__":
    game_mode_off()
