from threading import Lock
from time import monotonic

from fastapi import HTTPException


class RateLimit:
    def __init__(self, attempts: int, interval: float):
        self.attempts = attempts
        self.interval = interval
        self.entries: dict[str, tuple[float, int]] = {}
        self.lock = Lock()

    def check(self, peer: str) -> None:
        now = monotonic()
        with self.lock:
            if len(self.entries) >= 1024:
                self.entries = {
                    peer: entry
                    for peer, entry in self.entries.items()
                    if entry[0] > now
                }
                if len(self.entries) >= 1024 and peer not in self.entries:
                    raise HTTPException(429, "Try again later")
            expires, count = self.entries.get(peer, (now, 0))
            if expires <= now:
                expires, count = now + self.interval, 0
            if count >= self.attempts:
                raise HTTPException(429, "Try again later")
            self.entries[peer] = (expires, count + 1)
