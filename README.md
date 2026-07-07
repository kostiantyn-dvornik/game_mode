# Game Mode Controller

A set of Windows/Python scripts that automate switching between a "desktop"
multi-monitor setup and a "game mode" setup (single TV display + TV audio +
TV input switching). It's built around a 3-monitor + Android TV setup, but
every hardware-specific detail (monitor names, TV network info, audio
device names, etc.) lives in one config block in [game_mode.py](game_mode.py),
so it can be adapted to different hardware — see
[Hardware-specific configuration](#hardware-specific-configuration).

## What it does

- **`game_mode.py`** — the core module and CLI. It provides:
  - Monitor brightness control (get/set/fade) via `screen-brightness-control`
    and raw DDC/CI (VCP) calls.
  - Monitor power on/off via DDC/CI.
  - Default audio output switching (via undocumented `IPolicyConfig` COM
    interface).
  - Display topology management (clone/extend, monitor arrangement, save
    and restore exact display layouts) via the Windows `QueryDisplayConfig`
    / `SetDisplayConfig` API.
  - Android TV control over ADB (wake-on-LAN, standby, input switching,
    volume control).
  - `game-mode on` / `game-mode off`: the combined workflow — checks a
    gamepad is connected, warns (non-blocking) if its battery is low,
    wakes and connects to the TV, saves current display/audio/brightness
    state, clones the desktop monitor onto the TV, switches audio to the
    TV, and sets the TV to the gaming input/volume. `off` reverses all of
    it using the saved state.
- **`launch_game.py`** — generic launcher: activates game mode
  (`game_mode_on()`) and then launches any game, either a Steam title
  (`--steam <app id>`) or a local executable/shortcut (`--exe <path>`).
  Used directly by all "start gaming" Desktop shortcuts, including Gothic's.
- **`turn_on_tv.py`** — wakes the TV, sets its input/volume, saves the
  current display layout, then re-extends all three monitors and arranges
  them left-to-right (desktop monitor, second monitor, TV). Used to bring
  the TV into an extended (not cloned) desktop layout.
- **`restore_display.py`** — restores the display layout previously saved
  by `save_display_layout()` (used by `turn_on_tv.py`).
- **`restore_mode.py`** — calls `game_mode_off()` directly (equivalent to
  `game_mode.py game-mode off`).
- **`ble_battery_helper.py`** — reads the real battery percentage of a
  paired Bluetooth LE gamepad via its GATT Battery Service (0x180F). Run
  as a subprocess by `game_mode.py` (see Requirements below for why), not
  imported directly.
- **`steam_silent_start.py`** — starts Steam minimized to the tray
  (`steam.exe -silent`), with no main window or login popups. Meant to run
  once at Windows login via a Startup-folder shortcut, so Steam is already
  running by the time a game is launched and never flashes its window
  mid-game-mode.

State is persisted as JSON under
`%APPDATA%\BrightnessController\` (`game_mode_state.json` and
`display_layout.json`) so `game-mode off` / `restore_display.py` can put
things back exactly as they were.

## Requirements

- **Windows 10/11** — the scripts rely on Win32 APIs (`user32`, `dxva2`,
  `winreg`) and `ctypes.windll`, so this will not run on other platforms.
- **Python 3.10+** (uses `X | None` union type hints).
- Python packages — see [requirements.txt](requirements.txt):
  ```
  pip install -r requirements.txt
  ```
  - `screen-brightness-control`
  - `comtypes`
  - `winrt-Windows.Devices.Bluetooth`,
    `winrt-Windows.Devices.Bluetooth.GenericAttributeProfile`,
    `winrt-Windows.Devices.Enumeration`, `winrt-Windows.Storage.Streams`,
    `winrt-Windows.Foundation`, `winrt-Windows.Foundation.Collections` —
    used only by `ble_battery_helper.py` for the controller low-battery
    warning. If missing, `game_mode.py` still runs fine; that warning is
    just silently skipped.
- **ADB (Android Platform Tools)** for TV control, expected at the
  path installed by:
  ```
  winget install Google.PlatformTools
  ```
  (`game_mode.py` looks for `adb.exe` under
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\`.)
- An Xbox-compatible gamepad reachable via XInput (`xinput1_4` /
  `xinput1_3` / `xinput9_1_0`) — `game-mode on` refuses to run without one
  connected. The low-battery warning reads the controller's Bluetooth LE
  GATT Battery Service directly instead of XInput's or
  `Windows.Gaming.Input`'s own battery APIs — both were verified to
  return a fixed, never-changing value for a Bluetooth-connected Xbox
  controller regardless of actual charge, while the raw GATT read matches
  what Windows' own Bluetooth settings page shows. That read runs in a
  separate process (`ble_battery_helper.py`) because it hangs
  indefinitely if run in the same process as `comtypes` (used elsewhere
  in `game_mode.py`) — the two don't share a COM apartment model.
- An Android TV, set up for ADB + Wake-on-LAN as described below.

## TV setup (one-time)

The scripts control the TV entirely over ADB, and wake it from standby via
Wake-on-LAN, so the TV itself needs to be configured once before any of
this works:

1. **Enable Developer options**: on the TV, go to
   `Settings → Device Preferences → About`, then select **Build** /
   **Android TV OS build** repeatedly (~7 times) until "You are now a
   developer!" appears.
2. **Enable network ADB debugging**: `Settings → Device Preferences →
   Developer options`, turn on **USB debugging** and **Network debugging**
   (naming varies by firmware; look for anything mentioning ADB or network
   debugging).
3. **Enable networked standby / quick start**: `Settings → Device
   Preferences → Power` (or similar), enable **Quick start mode** /
   **Networked standby**. Without this, the TV's network interface powers
   off in standby and Wake-on-LAN magic packets can't reach it.
4. **Find the TV's IP and MAC address**: `Settings → Network & Internet →
   [your connection] → IP address / MAC address`. Give the TV a static IP,
   or a DHCP reservation on your router, so it doesn't change — then set
   `TV_IP` and `TV_MAC` in [game_mode.py](game_mode.py) to match.
5. **Authorize this PC for ADB**: with the TV on, run
   `adb connect <TV_IP>:5555` from a terminal once. The TV will show an
   "Allow USB/network debugging?" prompt — accept it and check "always
   allow from this computer". You only need to do this once per PC (the
   scripts reconnect automatically afterwards).
6. **Find your TV's HDMI input IDs**: run
   `adb shell dumpsys tv_input` and look for the input IDs corresponding
   to each HDMI port, then update `TV_INPUTS` in
   [game_mode.py](game_mode.py) if they differ from the defaults (they are
   MediaTek/TV-firmware specific).

## Hardware-specific configuration

This project is built for one particular desk setup, but every hardware
detail is collected in a single **"USER CONFIGURATION"** block near the top
of [game_mode.py](game_mode.py) (right after the state-file setup) — that's
the only place you need to edit to adapt it to different monitors, a
different TV, or different audio devices:

| Variable | Notes |
|---|---|
| `MONITOR_NAMES` | Dict of role → name substring, matched against Windows' friendly monitor names (see `game_mode.py list`). `desktop` and `tv` are required; add or remove other roles (e.g. `second`) to match however many monitors you have — a single desktop monitor + TV works fine with just those two. |
| `MONITOR_ORDER_LTR` | Left-to-right order of the roles above when arranging all configured monitors as an extended desktop (used by `turn_on_tv.py`) — must list the same roles as `MONITOR_NAMES`. |
| `GAME_MODE_CONFIG` | `audio_on` / `audio_off` friendly-name substrings, and `tv_input` for game mode. |
| `TV_IP`, `TV_MAC`, `TV_PORT` | The TV's network info, for ADB + Wake-on-LAN. |
| `TV_INPUTS` | HDMI input IDs from `adb shell dumpsys tv_input`; TV-model specific. |
| `GAME_MODE_TV_VOLUME` | TV volume while game mode is active. |
| `TV_ON_INPUT`, `TV_ON_VOLUME` | TV input/volume used by `turn_on_tv.py` (turning the TV on normally, not for gaming). |
| `ADB_PATH` | Path to `adb.exe`, if installed somewhere other than the default `winget` location. |
| `CONTROLLER_BLE_NAME` | Bluetooth name substring for the gamepad, used to find its GATT Battery Service for the low-battery warning. |

The game to launch is configured separately — see
[launch_game.py](launch_game.py) below, since it's not tied to your
hardware and can be set per-shortcut without touching any code.

## Desktop shortcuts

These scripts are invoked day-to-day via Desktop `.lnk` shortcuts rather
than a terminal:

| Shortcut | Runs |
|---|---|
| `Launch Gothic 1 Remake.lnk` | `launch_game.py --steam 1297900 --name "Gothic 1 Remake"` |
| `Turn On TV.lnk` | `turn_on_tv.py` |
| `Restore Display Layout.lnk` | `restore_display.py` |
| `Restore Mode.lnk` | `restore_mode.py` |

There's also a shortcut in the Windows **Startup** folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), not the
Desktop, so Steam starts silently on login:

| Shortcut | Runs |
|---|---|
| `Steam Silent Start.lnk` | `steam_silent_start.py` |

## Usage

```
python game_mode.py list                    # list monitors
python game_mode.py set 75                  # set all monitors to 75%
python game_mode.py set 75 -d 0             # set monitor 0 to 75%
python game_mode.py fade 30                 # fade all monitors to 30%
python game_mode.py off -d 0                # turn off monitor 0
python game_mode.py on -d 0                 # turn on monitor 0
python game_mode.py audio list              # list audio devices
python game_mode.py audio set "TV"          # switch default audio output
python game_mode.py game-mode on            # activate game mode
python game_mode.py game-mode off           # deactivate game mode
```

Standalone helper scripts:

```
python launch_game.py --steam 1297900 --name "Gothic 1 Remake"   # any Steam game
python launch_game.py --exe "C:\Games\MyGame\game.exe" --name "My Game"  # any non-Steam game
python turn_on_tv.py         # wake TV, extend all 3 monitors, arrange them
python restore_display.py    # restore display layout saved by turn_on_tv.py
python restore_mode.py       # equivalent to `game_mode.py game-mode off`
python steam_silent_start.py # start Steam minimized to tray, no popups
```

### Adding a shortcut for a new game

To get a one-click "start gaming" Desktop icon for a different game, create
a shortcut (right-click Desktop → New → Shortcut) with:

- **Target**: `pythonw.exe "E:\game_mode\launch_game.py" --steam <app id> --name "<Game Name>"`
  (use `--exe "<path to game .exe/.lnk/.url>"` instead of `--steam` for a
  non-Steam game)
- **Start in**: `E:\game_mode`

No code changes needed for a new game — every launcher shortcut, including
Gothic's, points at `launch_game.py` with different arguments.
