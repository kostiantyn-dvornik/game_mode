import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_mode import (
    _popup_error, _tv_wake, _tv_connect, _tv_set_input, _tv_set_volume,
    _set_display_extend_all, _arrange_monitors_ltr, save_display_layout,
    MONITOR_NAMES, MONITOR_ORDER_LTR, TV_ON_INPUT, TV_ON_VOLUME,
)

if __name__ == "__main__":
    _tv_wake()
    time.sleep(3)
    tv = _tv_connect()
    if tv is None:
        _popup_error("TV is not reachable.\n\nMake sure the TV is powered on and connected to the network.")
        sys.exit(1)
    _tv_set_input(tv, TV_ON_INPUT)
    _tv_set_volume(tv, TV_ON_VOLUME)
    tv.disconnect()
    save_display_layout()
    _set_display_extend_all()
    _arrange_monitors_ltr(*(MONITOR_NAMES[k] for k in MONITOR_ORDER_LTR))
