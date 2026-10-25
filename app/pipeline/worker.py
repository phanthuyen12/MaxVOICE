import queue
import traceback

from PyQt6.QtCore import QObject, QThread, pyqtSignal

# Two worker threads, not more: enough to let two independent things run at
# once (e.g. cutting/transcribing one clip while another segment's voice is
# being generated, or two projects' "Bắt đầu" runs overlapping) without
# piling on CPU the way an unbounded pool would — the user specifically
# wants this lighter on the machine, not heavier.
DEFAULT_THREAD_COUNT = 2


class _WorkerThread(QThread):
    def __init__(self, task_queue, coordinator):
        super().__init__()
        self._queue = task_queue
        self._coordinator = coordinator

    def run(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            fn, args, kwargs = item
            try:
                fn(self._coordinator, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surface any task failure to the UI
                self._coordinator.log.emit(traceback.format_exc())
                self._coordinator.task_failed.emit(str(exc))


class PipelineWorker(QObject):
    """A small pool of background threads (default 2) pulling from one
    shared FIFO queue — replacing the Redis + BullMQ queue setup from the
    web backend. A desktop app only has one user, so there's no separate
    queue server; tasks just run in submission order, picked up by
    whichever thread is free.

    Each task is a callable: task(worker) -> None, where `worker` is this
    PipelineWorker (so tasks can both emit progress signals and submit
    follow-up tasks). Tasks report progress by writing to the db and
    emitting `job_changed` / `segment_changed`, which the UI listens to
    (via Qt's cross-thread signal/slot queuing) to refresh.

    Only two task KINDS are allowed to run truly concurrently here:
    ffmpeg cutting and faster-whisper transcription. TTS synthesis
    (local_tts_service / resona_service) and the final merge are left to
    self-serialize via locks inside those services, since neither the
    VieNeu model session nor heavy video re-encoding has a documented
    thread-safety guarantee worth risking a crash over.
    """

    job_changed = pyqtSignal(str)
    segment_changed = pyqtSignal(str, int)
    task_failed = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, thread_count=DEFAULT_THREAD_COUNT):
        super().__init__()
        self._queue = queue.Queue()
        self._threads = [_WorkerThread(self._queue, self) for _ in range(thread_count)]

    def submit(self, fn, *args, **kwargs):
        self._queue.put((fn, args, kwargs))

    def start(self):
        for thread in self._threads:
            thread.start()

    def stop(self):
        for _ in self._threads:
            self._queue.put(None)

    def wait(self, ms=5000):
        for thread in self._threads:
            thread.wait(ms)
