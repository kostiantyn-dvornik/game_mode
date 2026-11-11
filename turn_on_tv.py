import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_mode import (
    _popup_error, _tv_wake, _tv_connect_retry, _tv_set_input, _tv_set_volume,
    _set_display_extend_all, _arrange_monitors_ltr, save_display_layout,
    MONITOR_NAMES, MONITOR_ORDER_LTR, TV_ON_INPUT, TV_ON_VOLUME,
)

if __name__ == "__main__":
    _tv_wake()
    tv = _tv_connect_retry()
    if tv is None:
        _popup_error("TV did not confirm power-on.\n\nCheck the TV connection and network standby settings.")
        sys.exit(1)
    _tv_set_input(tv, TV_ON_INPUT)
    _tv_set_volume(tv, TV_ON_VOLUME)
    tv.disconnect()
    save_display_layout()
    _set_display_extend_all()
    if not _arrange_monitors_ltr(
            *(MONITOR_NAMES[k] for k in MONITOR_ORDER_LTR),
            primary=MONITOR_NAMES["desktop"]):
        _popup_error("Could not arrange TV, LG and Acer with Acer as the main display.")
        sys.exit(1)
