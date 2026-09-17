"""Cross-process locks shared by the CLI and the local web controller (macOS/Linux)."""
import fcntl
import json
import os
from pathlib import Path


def lock_path(state_file, source):
    return Path(str(state_file) + f".{source}.lock")


class MonitorLock:
    def __init__(self, state_file, source):
        path = lock_path(state_file, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise ValueError(f"{source} is already running for this state file") from None
        self.file.seek(0)
        self.file.truncate()
        json.dump({"pid": os.getpid()}, self.file)
        self.file.flush()

    def close(self):
        self.file.close()


def running(state_file, source):
    path = lock_path(state_file, source)
    if not path.exists():
        return False
    with path.open("r") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
