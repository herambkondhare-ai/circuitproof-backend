"""
Timing and progress helpers (timing.py)

timed(label)       prints "[timing] <label>: 1.23s" when the block ends.
set_stage(name)    tells whoever is watching this thread (a generation job) what is happening now.
watching(callback) installs that watcher for the current thread.
"""

import threading
import time
from contextlib import contextmanager

_local = threading.local()


@contextmanager
def timed(label):
    started = time.perf_counter()
    try:
        yield
    finally:
        print(f"[timing] {label}: {time.perf_counter() - started:.2f}s", flush=True)


def set_stage(stage):
    callback = getattr(_local, "callback", None)
    if callback:
        callback(stage)


@contextmanager
def watching(callback):
    previous = getattr(_local, "callback", None)
    _local.callback = callback
    try:
        yield
    finally:
        _local.callback = previous
