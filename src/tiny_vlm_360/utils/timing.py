"""Timing utilities."""

import time
from contextlib import contextmanager
from typing import Generator


@contextmanager
def Timer(name: str = "Operation") -> Generator[None, None, None]:
    """
    Context manager for timing operations.

    Usage:
        with Timer("Processing"):
            # do work
    """
    start = time.time()
    yield
    elapsed = time.time() - start
    print(f"{name} took {elapsed:.2f} seconds")

