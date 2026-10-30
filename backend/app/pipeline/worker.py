import importlib
import json
import logging
import os
import queue
import threading
import time
import traceback

from ..config import AppConfig, load_config

logger = logging.getLogger("maxvoice.worker")

DEFAULT_THREAD_COUNT = 2
REDIS_QUEUE_KEY = "kingvoice:queue"
REDIS_ACTIVE_KEY = "kingvoice:active_count"
REDIS_EVENTS_KEY = "kingvoice:events"


class _Signal:
    """Lightweight thread-safe signal replacement for PyQt6.QtCore.pyqtSignal
    so `tasks.py` can call `.emit(...)` identically in a headless container.
    """

    def __init__(self, name="", on_emit=None):
        self.name = name
        self._callbacks = []
        self._on_emit = on_emit
        self._lock = threading.Lock()

    def connect(self, callback):
        with self._lock:
            self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        if self._on_emit is not None:
            try:
                self._on_emit(self.name, args, kwargs)
            except Exception:
                pass
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(*args, **kwargs)
            except Exception:
                logger.exception("Signal callback error in %s", self.name)


def _serialize_arg(val):
    if isinstance(val, AppConfig):
        return {"__kingvoice_config__": True}
    return val


def _deserialize_arg(val):
    if isinstance(val, dict) and val.get("__kingvoice_config__") is True:
        return load_config()
    return val


class _WorkerThread(threading.Thread):
    def __init__(self, coordinator, idx=0):
        super().__init__(name=f"KingVoiceWorker-{idx}", daemon=True)
        self._coordinator = coordinator

    def run(self):
        while not self._coordinator._stop_event.is_set():
            item = self._coordinator._pop_next_task(timeout_s=1.5)
            if item is None:
                continue
            fn, args, kwargs = item
            self._coordinator._mark_task_active(delta=1)
            try:
                fn(self._coordinator, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                logger.error("Task failed: %s\n%s", exc, tb)
                self._coordinator.log.emit(tb)
                self._coordinator.task_failed.emit(str(exc))
            finally:
                self._coordinator._mark_task_active(delta=-1)


class PipelineWorker:
    """Background worker pool backed by Redis (`REDIS_URL`) for durable task queuing,
    with automatic fallback to in-memory `queue.Queue` if Redis is unavailable.
    """

    def __init__(self, thread_count=DEFAULT_THREAD_COUNT, redis_url=None):
        self.job_changed = _Signal("job_changed", on_emit=self._record_event)
        self.segment_changed = _Signal("segment_changed", on_emit=self._record_event)
        self.task_failed = _Signal("task_failed", on_emit=self._record_event)
        self.log = _Signal("log")

        self._redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None
        self._redis_available = False
        self._mem_queue = queue.Queue()
        self._active_local = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self._thread_count = max(1, int(thread_count))
        self._threads = [
            _WorkerThread(self, idx=i) for i in range(self._thread_count)
        ]
        self._started = False

    def _connect_redis(self):
        try:
            import redis

            client = redis.Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=3.0,
            )
            client.ping()
            self._redis = client
            self._redis_available = True
            logger.info("Connected to Redis queue at %s", self._redis_url)
        except Exception as exc:
            self._redis = None
            self._redis_available = False
            logger.info(
                "Redis not reachable at %s (%s) — using fast in-memory task queue fallback.",
                self._redis_url,
                exc,
            )

    @property
    def redis_connected(self) -> bool:
        if not self._redis_available or self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            self._redis_available = False
            return False

    @property
    def queue_backend(self) -> str:
        return "redis" if self._redis_available else "memory"

    @property
    def queue_size(self) -> int:
        if self._redis_available and self._redis is not None:
            try:
                return int(self._redis.llen(REDIS_QUEUE_KEY) or 0) + self._mem_queue.qsize()
            except Exception:
                self._redis_available = False
        return self._mem_queue.qsize()

    @property
    def active_tasks(self) -> int:
        if self._redis_available and self._redis is not None:
            try:
                val = int(self._redis.get(REDIS_ACTIVE_KEY) or 0)
                return max(0, val)
            except Exception:
                pass
        with self._lock:
            return max(0, self._active_local)

    def _mark_task_active(self, delta: int):
        with self._lock:
            self._active_local = max(0, self._active_local + delta)
        if self._redis_available and self._redis is not None:
            try:
                new_val = self._redis.incrby(REDIS_ACTIVE_KEY, delta)
                if new_val < 0:
                    self._redis.set(REDIS_ACTIVE_KEY, 0)
            except Exception:
                pass

    def _record_event(self, signal_name, args, _kwargs):
        if not self._redis_available or self._redis is None:
            return
        try:
            payload = json.dumps(
                {
                    "signal": signal_name,
                    "job_id": str(args[0]) if args else None,
                    "ts": time.time(),
                }
            )
            pipe = self._redis.pipeline()
            pipe.lpush(REDIS_EVENTS_KEY, payload)
            pipe.ltrim(REDIS_EVENTS_KEY, 0, 99)
            pipe.execute()
        except Exception:
            pass

    def submit(self, fn, *args, **kwargs):
        if self._redis_available and self._redis is not None:
            try:
                payload = json.dumps(
                    {
                        "module": fn.__module__,
                        "fn": fn.__name__,
                        "args": [_serialize_arg(a) for a in args],
                        "kwargs": {k: _serialize_arg(v) for k, v in kwargs.items()},
                        "queued_at": time.time(),
                    },
                    ensure_ascii=False,
                )
                self._redis.rpush(REDIS_QUEUE_KEY, payload)
                return
            except Exception as exc:
                logger.warning("Redis push failed (%s), falling back to memory queue", exc)
                self._redis_available = False

        self._mem_queue.put((fn, args, kwargs))

    def _pop_next_task(self, timeout_s=1.5):
        # 1. Check in-memory fallback queue first (non-blocking)
        try:
            item = self._mem_queue.get_nowait()
            self._mem_queue.task_done()
            return item
        except queue.Empty:
            pass

        # 2. Pop from Redis queue if available
        if self._redis_available and self._redis is not None:
            try:
                res = self._redis.blpop(REDIS_QUEUE_KEY, timeout=max(1, int(timeout_s)))
                if not res:
                    return None
                _, raw_json = res
                data = json.loads(raw_json)
                mod = importlib.import_module(data.get("module") or "app.pipeline.tasks")
                fn = getattr(mod, data["fn"])
                args = tuple(_deserialize_arg(a) for a in data.get("args", []))
                kwargs = {k: _deserialize_arg(v) for k, v in (data.get("kwargs") or {}).items()}
                return (fn, args, kwargs)
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.debug("Redis blpop interrupted or unavailable: %s", exc)
                time.sleep(0.3)
                return None

        # 3. Otherwise wait on in-memory queue with timeout
        try:
            item = self._mem_queue.get(timeout=timeout_s)
            self._mem_queue.task_done()
            return item
        except queue.Empty:
            return None

    def start(self):
        if self._started:
            return
        self._connect_redis()
        if self._redis_available and self._redis is not None:
            try:
                self._redis.set(REDIS_ACTIVE_KEY, 0)
            except Exception:
                pass
        self._started = True
        for thread in self._threads:
            thread.start()

    def stop(self):
        if not self._started:
            return
        self._stop_event.set()
        for _ in self._threads:
            self._mem_queue.put(None)

    def wait(self, timeout_s=5.0):
        for thread in self._threads:
            thread.join(timeout=timeout_s)
