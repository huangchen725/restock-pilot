"""Small in-process queue; business status and results remain in SQLite."""
from concurrent.futures import ThreadPoolExecutor
import threading

_lock = threading.Lock()
_pool = None


def submit(work):
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="restock")
        return _pool.submit(work)


def shutdown():
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool:
        pool.shutdown(wait=True)
