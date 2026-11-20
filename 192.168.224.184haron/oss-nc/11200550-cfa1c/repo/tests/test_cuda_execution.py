"""Capture exclusion and concurrent inference contracts, without CUDA."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from core.cuda_execution import CudaExecution, cuda_work
import core.cuda_execution as execution


def test_ordinary_work_is_concurrent():
    gate = CudaExecution()
    entered = threading.Barrier(2, timeout=2)

    def work():
        with gate.work():
            entered.wait()
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(work) for _ in range(2)]
        for future in futures:
            future.result(timeout=3)


def test_capture_defers_without_waiting_when_another_owner_is_active():
    gate = CudaExecution()
    entered, release = threading.Event(), threading.Event()

    def other_work():
        with gate.work():
            entered.set()
            assert release.wait(3)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(other_work)
        assert entered.wait(2)
        try:
            with gate.work(), gate.try_capture() as admitted:
                assert not admitted
        finally:
            release.set()
        future.result(timeout=3)
    with gate.try_capture() as admitted:
        assert admitted


def test_capture_blocks_new_work_until_capture_finishes():
    gate = CudaExecution()
    started, entered = threading.Event(), threading.Event()

    def work():
        started.set()
        with gate.work():
            entered.set()
    with ThreadPoolExecutor(1) as pool:
        with gate.try_capture() as admitted:
            assert admitted
            future = pool.submit(work)
            assert started.wait(2)
            assert not entered.wait(.05)
        future.result(timeout=2)
    assert entered.is_set()


def test_reentrant_work_can_upgrade_but_nested_capture_is_rejected():
    gate = CudaExecution()
    with gate.work(), gate.work(), gate.try_capture() as admitted:
        assert admitted
        with gate.work(), gate.try_capture() as nested:
            assert not nested
    with gate.try_capture() as admitted:
        assert admitted


def test_exception_releases_capture_and_work():
    gate = CudaExecution()
    with pytest.raises(RuntimeError, match="synthetic"):
        with gate.work(), gate.try_capture() as admitted:
            assert admitted
            raise RuntimeError("synthetic")
    with ThreadPoolExecutor(1) as pool:
        def capture():
            with gate.try_capture() as admitted:
                return admitted
        assert pool.submit(capture).result(timeout=2)


def test_stream_lease_lasts_through_iteration_and_is_released_on_close(monkeypatch):
    gate = CudaExecution()
    monkeypatch.setattr(execution, "speech_cuda", gate)

    @cuda_work
    def chunks():
        yield 1
        yield 2

    def capture():
        with gate.try_capture() as admitted:
            return admitted

    iterator = chunks()
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(capture).result(timeout=2)
        assert next(iterator) == 1
        assert not pool.submit(capture).result(timeout=2)
        iterator.close()
        assert pool.submit(capture).result(timeout=2)
