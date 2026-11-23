"""Process-local ordering for consent withdrawal and synchronous analysis dispatch."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock, RLock
from uuid import UUID
from weakref import WeakValueDictionary

_registry_lock = Lock()
_customer_locks: WeakValueDictionary[UUID, RLock] = WeakValueDictionary()


@contextmanager
def coordinate_style_consent(customer_id: UUID) -> Iterator[None]:
    """Order a customer's committed withdrawal against final check plus dispatch.

    Keep a strong reference while waiting/working; unused customer locks can be
    reclaimed. This shares the local sample store's single-process boundary.
    The synchronous analyzer holds the guard until it returns, so withdrawal
    waits for an already-started analysis, bounded by the provider's timeout.
    """
    with _registry_lock:
        guard = _customer_locks.get(customer_id)
        if guard is None:
            guard = RLock()
            _customer_locks[customer_id] = guard
    with guard:
        yield
