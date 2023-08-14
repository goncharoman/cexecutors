import os
import threading
import time
from concurrent.futures import CancelledError, Future

import pytest
from hamcrest import assert_that, calling, instance_of, is_, raises

from cexecutors import interruptible
from cexecutors.interruptible import Interrupt, InterruptibleThreadPoolExecutor

not_supported = interruptible.CURRENT_IMPLEMENTATION not in interruptible.SUPPORTED_IMPLEMENTATIONS


def wait(secs: int):
    for _ in range(secs * 10):
        time.sleep(.1)


def is_pending(future: Future):
    return not future.done() and not future.running() and not future.cancelled()


def is_finished(future: Future):
    return future.done() and not future.running() and not future.cancelled()


def is_cancelled(future: Future):
    return future.done() and not future.running() and future.cancelled()


def is_running(future: Future):
    return not future.done() and future.running() and not future.cancelled()


@pytest.fixture
def executor():
    with InterruptibleThreadPoolExecutor(max_workers=5) as _executor:
        yield _executor


def test_interruptible_future_default_num_workers():
    with InterruptibleThreadPoolExecutor() as executor:
        assert_that(executor.max_workers, is_(
            min(32, (os.cpu_count() or 1) + 4)))


def test_interruptible_future_have_correct_basic_stetes():

    event = threading.Event()

    def dowork():
        event.wait()

    with InterruptibleThreadPoolExecutor(max_workers=1) as executor:
        # first submitted future has a RUNNING state
        first_future = executor.submit(dowork)
        assert is_running(first_future)

        # second submitted future has a PENDING state
        second_future = executor.submit(dowork)
        assert is_pending(second_future)

        # pending future can be canceled
        assert_that(second_future.cancel(), is_(True))
        assert is_cancelled(second_future)

        # canceling an already canceled future returns true (and does not change the state)
        assert_that(second_future.cancel(), is_(True))
        assert is_cancelled(second_future)

        # finished future has correct state (DONE)
        event.set()
        first_future.result()
        assert is_finished(first_future)

        # finished future cannot be cancelled
        assert_that(first_future.cancel(), is_(False))
        assert not is_cancelled(first_future)


def test_interruptible_future_can_execute_done_callbacks(executor: InterruptibleThreadPoolExecutor):

    callback_called = False

    def callback(future):
        nonlocal callback_called
        callback_called = True

    future = executor.submit(lambda: None)
    future.add_done_callback(callback)

    assert_that(future.result(), is_(None))
    assert_that(callback_called, is_(True))


def test_interruptible_executor_can_execute_fn(executor: InterruptibleThreadPoolExecutor):

    def dowork(*args, **kwargs):
        return args, kwargs

    future = executor.submit(dowork, 42, kw="world")

    assert_that(future.result(), is_(((42,), {"kw": "world"})))


def test_interruptible_executor_can_handle_execption(executor: InterruptibleThreadPoolExecutor):

    exc = ValueError(42)

    def dowork():
        raise exc

    future = executor.submit(dowork)

    assert_that(future.exception(), is_(exc))
    assert_that(future.result, raises(exc.__class__))


@pytest.mark.skipif(not_supported, reason="Interruption is not supported on this platform.")
def test_interruptible_executor_can_interrupt_running_task(executor: InterruptibleThreadPoolExecutor):

    event = threading.Event()
    caught_exception = None

    def dowork():
        try:
            event.set()
            wait(2)
        except BaseException as exc:
            nonlocal caught_exception
            caught_exception = exc
            raise exc

    future = executor.submit(dowork)

    event.wait()  # wait for func to run in executor worker

    assert_that(future.cancel(), is_(True))
    # use .result func to wait for worker executor to finish
    # because cancellation can't be done instantly
    assert_that(future.result, raises(CancelledError))
    assert is_cancelled(future)
    # check that execution interrupted by an Interrupt exception
    assert_that(caught_exception, instance_of(Interrupt))


def test_interruptible_executor_can_shutdowned(executor: InterruptibleThreadPoolExecutor):
    executor.shutdown()
    executor.shutdown()


def test_interruptible_executor_must_raise_exc_if_submit_after_shutdown(executor: InterruptibleThreadPoolExecutor):
    executor.shutdown()
    assert_that(calling(executor.submit).with_args(
        lambda: None), raises(RuntimeError))


def test_interruptible_executor_must_cancel_pendings_futures_on_shutdown():

    callback_called = False

    def dowork():
        wait(2)

    def callback(future):
        nonlocal callback_called
        callback_called = True

    with InterruptibleThreadPoolExecutor(max_workers=1) as executor:
        running_future = executor.submit(dowork)
        pending_future = executor.submit(dowork)
        pending_future.add_done_callback(callback)

        # shutdown executor without waiting
        executor.shutdown(wait=False, cancel_futures=True)

        assert_that(pending_future.cancelled(), is_(True))

        running_future.cancel()  # interrupt the first (running) future

        # cancelled future must also call a callback
        assert_that(callback_called, is_(True))


def test_raise_thread_exception_on_existent_thread():

    caught_exception = None
    event = threading.Event()

    def dowork():
        try:
            event.set()
            wait(2)
        except BaseException as exc:
            nonlocal caught_exception
            caught_exception = exc

    th = threading.Thread(target=dowork)
    th.start()
    event.wait()  # wait until dowork func is called

    interruptible.raise_thread_exception(th.ident, Interrupt)
    th.join()  # wait for the thread to stop

    assert_that(caught_exception, is_(Interrupt))


def test_raise_thread_exception_on_nonexistent_thread():
    assert_that(
        calling(interruptible.raise_thread_exception).with_args(
            tid=2 * 31 - 1, exctype=Interrupt),
        raises(ValueError, "invalid thread id")
    )


def test_raise_thread_exception_with_non_valid_exctype():
    assert_that(
        calling(interruptible.raise_thread_exception).with_args(
            tid=2 * 31 - 1, exctype=ValueError()),
        raises(TypeError)
    )


def test_raise_thread_exception_unsupported_platform(monkeypatch):
    monkeypatch.setattr(interruptible, "CURRENT_IMPLEMENTATION", "not supported")
    with pytest.warns(UserWarning, match=r"Unsupported Python implementation for raising exceptions on threads .*"):
        interruptible.raise_thread_exception(1, Interrupt)
