from __future__ import annotations

import ctypes
import inspect
import logging
import platform
import threading
import warnings
import weakref
from concurrent.futures import _base
from concurrent.futures.thread import ThreadPoolExecutor as _ThreadPoolExecutor
from typing import Any, Callable, Optional, Type, TypeVar, Union

from typing_extensions import ParamSpec

T = TypeVar("T")
P = ParamSpec("P")


CURRENT_IMPLEMENTATION = platform.python_implementation()
SUPPORTED_IMPLEMENTATIONS = {"CPython"}

logger = logging.getLogger(__name__)


def _refcb(ref: weakref.ReferenceType):

    def wrapper(*args, **kwargs) -> bool:
        try:
            ref()(*args, **kwargs)  # type: ignore
        except BaseException as exc:
            logger.warning("Exception caught: %s", str(exc), exc_info=exc)
            return False
        return True

    return wrapper


class Interrupt(BaseException):
    """Exception to be raised on the thread"""
    ...


def raise_thread_exception(tid: int, exctype: Type[BaseException]):
    """Raise exception in thread.

    Important: currently only works for СPython

    Arguments:
        tid -- Thread identifier (see threading.get_ident)
        exctype -- Exception type to raise
    """
    if CURRENT_IMPLEMENTATION == "CPython":
        _raise_thread_exception_cpython(tid, exctype)
    else:
        warnings.warn(
            "Unsupported Python implementation for raising exceptions on threads (supported only CPython)",
            stacklevel=2
        )


def _raise_thread_exception_cpython(tid: int, exctype: Type[BaseException]):
    # Makes it possible to throw exceptions in the context of a given thread
    # (uses PyThreadState_SetAsyncExc function (via ctypes), so a CPython implementation is needed).
    #
    # This solution based on recipe "Killable Threads" by Tomer Filiba.
    # More info:
    #   https://code.activestate.com/recipes/496960-thread2-killable-threads
    #   https://stackoverflow.com/questions/323972/is-there-any-way-to-kill-a-thread
    #
    # IMPORTANT:
    # Known issues are described here: https://github.com/intuited/terminable_thread#issues

    if not inspect.isclass(exctype) or not issubclass(exctype, BaseException):
        raise TypeError("exctype must be exception type (not instance)")
    count = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.py_object(exctype))
    if count == 0:
        raise ValueError("invalid thread id")
    elif count > 1:
        # if it returns a number greater than 1, the effect of the previous call should be canceled
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.c_long(0))
        logger.critical("Exception %s was set in multiple threads. Trying cancelling", exctype.__name__)
        raise SystemError("exception was set in multiple threads")


class Future(_base.Future):

    @property
    def _delegate(self) -> Union[_base.Future, None]:
        return getattr(self, "_dlg", None)

    @_delegate.setter
    def _delegate(self, future: _base.Future):
        if hasattr(self, "_dlg"):
            raise AttributeError("delegate already setted")
        with self._lock:
            self._dlg = future
        future.add_done_callback(self._delegate_resolve_cb)

    @_delegate.deleter
    def _delegate(self):
        self._dlg = None

    def __init__(self, delegate: _base.Future, interrupt_callback_ref: weakref.ReferenceType) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._delegate = delegate
        self._interrupt_cb = _refcb(interrupt_callback_ref)

    def _delegate_resolve_cb(self, delegate: _base.Future) -> None:
        assert self._delegate is delegate, "unexpected delegate object"

        del self._delegate

        if delegate.cancelled():
            super().cancel()
            return

        exc = delegate.exception()
        if exc is not None:
            if isinstance(exc, Interrupt):
                with self._lock:
                    super().cancel()
                    self.set_running_or_notify_cancel()
                return

            self.set_exception(exc)

        else:
            self.set_result(delegate.result())

    def set_result(self, result: Any) -> None:
        with self._lock:
            return super().set_result(result)

    def set_exception(self, exception: Optional[BaseException]) -> None:
        with self._lock:
            return super().set_exception(exception)

    def running(self) -> bool:
        with self._lock:
            if self.done():
                return False
            return self._delegate is not None and (self._delegate.running() or self._delegate.done())

    def cancel(self) -> bool:
        with self._lock:
            if self.cancelled():
                return True
            if self.done():
                return False
            if self._interrupt_delegate():
                return True
            if not self._cancel_delegate():
                return False
            cancelled = super().cancel()
            if cancelled:
                self.set_running_or_notify_cancel()
        return cancelled

    def _interrupt_delegate(self) -> bool:
        with self._lock:
            if self._delegate and self._delegate.running():
                return self._interrupt_cb()
        return False

    def _cancel_delegate(self) -> bool:
        with self._lock:
            if self._delegate:
                return self._delegate.cancel()
        return False

    def __repr__(self) -> str:
        return "<%s at %#x>" % (self.__class__.__name__, id(self))


class _InterruptibleWork:

    def __init__(self, fn: Callable, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self._tid: Union[None, int] = None
        self._lock = threading.RLock()

    def run(self) -> Any:
        with self._lock:
            if self._tid is not None:
                raise RuntimeError("work is already running in another thread")
            self._tid = threading.get_ident()
        try:
            return self.fn(*self.args, **self.kwargs)
        finally:
            with self._lock:
                self._tid = None

    def interrupt(self) -> None:
        with self._lock:
            if self._tid is None:
                raise RuntimeError("work not running")
            raise_thread_exception(self._tid, Interrupt)
            logger.debug("Interrupt work (%#x) running on thread %s", id(self), self._tid)


class InterruptibleThreadPoolExecutor(_base.Executor):

    @property
    def max_workers(self) -> int:
        _max_workers = getattr(self, "_max_workers", None)
        if _max_workers is None:
            _max_workers = self._max_workers = self._delegate._max_workers
        return _max_workers

    def __init__(self, max_workers: Optional[int] = None) -> None:
        self._delegate = _ThreadPoolExecutor(max_workers)
        self._shutdown = False
        self._shutdown_lock = threading.Lock()

        # unsupported platform warning
        if CURRENT_IMPLEMENTATION not in SUPPORTED_IMPLEMENTATIONS:
            warnings.warn(
                "Unsupported Python implementations for interruption running futures."
                "The behavior of futures will be the same as native futures.",
                stacklevel=2
            )

    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future:
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            w = _InterruptibleWork(fn, *args, **kwargs)
            f = self._delegate.submit(w.run)
            return Future(f, weakref.WeakMethod(w.interrupt))

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._shutdown_lock:
            self._shutdown = True
        self._delegate.shutdown(wait=wait, cancel_futures=cancel_futures)
