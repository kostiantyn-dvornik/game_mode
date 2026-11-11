"""Persistent per-process logs, also mirrored to any visible console."""
import atexit
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"game_mode_{datetime.now():%Y%m%d_%H%M%S_%f}_{os.getpid()}.log"
logger = logging.getLogger("game_mode_app")
logger.setLevel(logging.INFO)
logger.propagate = False
handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024,
                              backupCount=1, encoding="utf-8", delay=True)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


class LoggedStream:
    def __init__(self, console, level):
        self.console = console
        self.level = level
        self.pending = ""

    def write(self, text):
        if self.console is not None:
            self.console.write(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                logger.log(self.level, line.rstrip())
        return len(text)

    def flush(self):
        if self.console is not None:
            self.console.flush()
        if self.pending:
            logger.log(self.level, self.pending)
            self.pending = ""

    def isatty(self):
        return self.console is not None and self.console.isatty()

    @property
    def encoding(self):
        return getattr(self.console, "encoding", "utf-8")


sys.stdout = LoggedStream(sys.stdout, logging.INFO)
sys.stderr = LoggedStream(sys.stderr, logging.ERROR)


def set_console_streams(stdout, stderr):
    sys.stdout.console = stdout
    sys.stderr.console = stderr


def _exception_hook(exc_type, value, traceback):
    logger.critical("Unhandled exception", exc_info=(exc_type, value, traceback))
    if sys.stderr.console is not None:
        sys.__excepthook__(exc_type, value, traceback)


sys.excepthook = _exception_hook


def _finish():
    sys.stdout.flush()
    sys.stderr.flush()
    logger.info("Process finished")


atexit.register(_finish)
logger.info("Process started: %s", sys.argv)
logger.info("Log file: %s", LOG_FILE)
# Separate session files avoid rotation collisions between simultaneous shortcuts.
for old in sorted(LOG_DIR.glob("game_mode_*.log"), reverse=True)[20:]:
    try:
        old.unlink()
        Path(str(old) + ".1").unlink(missing_ok=True)
    except OSError:
        pass
