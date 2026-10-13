"""Coordinate speech CUDA capture without serializing ordinary inference.

All participating model work holds a shared lease. Capture can reserve the
process only when no other thread has a lease. A failed reservation is a
pre-execution decision to defer capture, never an inference retry. Callers
still synchronize pending CUDA work before beginning an admitted capture.
"""
from contextlib import contextmanager
from functools import wraps
import inspect
import threading


class CudaExecution:
    def __init__(self):
        self._condition = threading.Condition()
        self._workers = {}
        self._capturing = None

    @contextmanager
    def work(self):
        owner = threading.get_ident()
        with self._condition:
            self._condition.wait_for(lambda: self._capturing in (None, owner))
            self._workers[owner] = self._workers.get(owner, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                remaining = self._workers[owner] - 1
                if remaining:
                    self._workers[owner] = remaining
                else:
                    del self._workers[owner]
                self._condition.notify_all()

    @contextmanager
    def try_capture(self):
        owner = threading.get_ident()
        with self._condition:
            admitted = self._capturing is None and all(thread == owner for thread in self._workers)
            if admitted:
                self._capturing = owner
        try:
            yield admitted
        finally:
            if admitted:
                with self._condition:
                    self._capturing = None
                    self._condition.notify_all()


speech_cuda = CudaExecution()


def cuda_work(function):
    """Keep a lease for a call, or for the lifetime of a streaming iterator."""
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def stream(*args, **kwargs):
            with speech_cuda.work():
                yield from function(*args, **kwargs)
        return stream

    @wraps(function)
    def call(*args, **kwargs):
        with speech_cuda.work():
            return function(*args, **kwargs)
    return call
