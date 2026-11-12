from __future__ import annotations

import shutil
import sys
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressState:
    session_index: int
    session_total: int
    session: str
    phase: str
    fraction: float
    message: str
    rows: int | None = None


class IngestionProgress:
    """TTY progress bar with readable, rate-limited output when redirected."""

    def __init__(self, stream=None, *, enabled=True, log_interval=30.0):
        self.stream = stream or sys.stdout
        self.enabled = enabled
        self.log_interval = max(1.0, float(log_interval))
        self.started = time.monotonic()
        self._state: ProgressState | None = None
        self._last_phase = None
        self._last_log = 0.0
        self._last_width = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.tty = bool(enabled and getattr(self.stream, "isatty", lambda: False)())
        self._thread = None
        if self.tty:
            self._thread = threading.Thread(target=self._refresh, daemon=True)
            self._thread.start()

    def __call__(self, message):
        self.log(str(message))

    def update(self, *, session_index=0, session_total=0, session="-", phase,
               fraction=0.0, message="", rows=None):
        state = ProgressState(
            int(session_index), int(session_total), str(session), str(phase).upper(),
            max(0.0, min(1.0, float(fraction))), str(message),
            int(rows) if rows is not None else None,
        )
        with self._lock:
            self._state = state
            now = time.monotonic()
            phase_key = (state.session_index, state.phase)
            if self.tty:
                if phase_key != self._last_phase:
                    if self._last_width:
                        self._write("\r" + " " * self._last_width + "\r", ending="")
                        self._last_width = 0
                    self._write(self._line(state, bar=False), ending="\n")
                    self._last_phase = phase_key
                self._render_locked()
            elif self.enabled and (phase_key != self._last_phase or now - self._last_log >= self.log_interval):
                self._write(self._line(state, bar=False), ending="\n")
                self._last_phase, self._last_log = phase_key, now

    def log(self, message):
        with self._lock:
            if self.tty and self._last_width:
                self._write("\r" + " " * self._last_width + "\r", ending="")
                self._last_width = 0
            self._write(message, ending="\n")
            if self.tty and self._state is not None:
                self._render_locked()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            if self.tty and self._last_width:
                self._write("\r" + " " * self._last_width + "\r", ending="")
                self._last_width = 0

    def _refresh(self):
        while not self._stop.wait(1.0):
            with self._lock:
                if self._state is not None:
                    self._render_locked()

    def _render_locked(self):
        width = max(40, shutil.get_terminal_size((120, 20)).columns - 1)
        bar_width = 12 if width < 100 else 24
        line = self._line(self._state, bar=True, bar_width=bar_width)
        line = line[:width]
        padding = max(0, self._last_width - len(line))
        self._write("\r" + line + " " * padding, ending="")
        self._last_width = len(line)

    def _line(self, state, *, bar, bar_width=24):
        elapsed = self._duration(time.monotonic() - self.started)
        overall = (
            (max(0, state.session_index - 1) + state.fraction) / state.session_total
            if state.session_total else state.fraction
        )
        percent = 100.0 * max(0.0, min(1.0, overall))
        prefix = f"[{state.session_index}/{state.session_total}]" if state.session_total else "[preflight]"
        pieces = [prefix]
        if bar:
            completed = int(bar_width * percent / 100)
            pieces.append(f"[{'#' * completed}{'-' * (bar_width - completed)}] {percent:5.1f}%")
        pieces.extend((state.session, state.phase))
        pieces.append(f"elapsed={elapsed}")
        if state.message:
            pieces.append(state.message)
        if state.rows is not None:
            pieces.append(f"rows={state.rows:,}")
        return " | ".join(pieces)

    def _write(self, value, *, ending):
        try:
            self.stream.write(value, ending=ending)
        except TypeError:
            self.stream.write(value + ending)
        flush = getattr(self.stream, "flush", None)
        if flush:
            flush()

    @staticmethod
    def _duration(seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
