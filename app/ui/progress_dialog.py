from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QVBoxLayout,
)

from .. import db
from ..pipeline import tasks
from .status_meta import PROJECT_STAGE_LABEL, status_meta

# Rough step weights for the overall percentage: cutting is quick, voice
# generation is usually the slowest part of a run.
_STEP_ORDER = ["cut", "extracting_audio", "transcribing", "transcribed",
               "tts_queued", "tts_processing", "tts_done"]


def _segment_progress_fraction(segment):
    status = segment.get("status")
    try:
        return (_STEP_ORDER.index(status) + 1) / len(_STEP_ORDER)
    except ValueError:
        return 1.0 if status in ("failed", "tts_failed") else 0.0


class ProgressOverviewDialog(QDialog):
    """A single place to watch the whole "Bắt đầu" run: overall % across
    every segment in every clip, a live list of what each one is doing, and
    the project's current pipeline stage — instead of having to click
    around the timeline to piece that together.

    Non-modal and has only a Close button (no Cancel — the run is a single
    synchronous task on the worker thread, see handle_start_project); closing
    this dialog does not stop the run, it keeps going in the background and
    the project view keeps reflecting it either way.
    """

    def __init__(self, config, worker, project_id, parent=None):
        super().__init__(parent)
        self.config = config
        self.worker = worker
        self.project_id = project_id

        self.setWindowTitle("Tiến độ xử lý")
        self.resize(820, 640)
        self.setModal(False)
        # A plain QDialog has no maximize/zoom button on macOS by default —
        # add it (plus minimize) so this can be made full-size to watch.
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Đang xử lý project")
        title.setStyleSheet("font-size: 20px; font-weight: 600; color: #17a673;")
        layout.addWidget(title)

        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet("font-size: 15px;")
        layout.addWidget(self.stage_label)

        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setFixedHeight(22)
        self.overall_bar.setFormat("%p%")
        layout.addWidget(self.overall_bar)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.list_widget, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #a3b0a9; font-size: 14px;")
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.close)
        layout.addWidget(buttons)

        worker.job_changed.connect(self._on_signal)
        worker.segment_changed.connect(lambda job_id, _index: self._on_signal(job_id))

        # Also poll on a light timer: handle_start_project runs synchronously
        # and emits signals as it goes, but polling here too means the
        # dialog stays correct even if a burst of signals got coalesced
        # elsewhere in the UI.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)

        self.refresh()

    def _on_signal(self, job_id):
        clip_ids = {c["id"] for c in db.list_clips(self.project_id)}
        if job_id == self.project_id or job_id in clip_ids:
            self.refresh()

    def refresh(self):
        job = db.get_job(self.project_id)
        if not job:
            return
        segments = tasks.project_segments_in_order(self.project_id)

        self.stage_label.setText(PROJECT_STAGE_LABEL.get(job.get("stage"), job.get("stage") or ""))

        if segments:
            fraction = sum(_segment_progress_fraction(s) for s in segments) / len(segments)
        else:
            fraction = 0.0
        if job.get("finalVideoStatus") == "done":
            fraction = 1.0
        self.overall_bar.setValue(round(fraction * 100))

        self.list_widget.clear()
        for segment in segments:
            meta = status_meta(segment.get("status"))
            clip_label = segment.get("clipName") or segment.get("clipId") or ""
            text = f"{clip_label} — đoạn {segment['index'] + 1}: {meta['label']}"
            if segment.get("ttsProgress"):
                text += f" ({segment['ttsProgress']})"
            if segment.get("error"):
                text += f" — {segment['error']}"
            self.list_widget.addItem(QListWidgetItem(text))

        done = sum(1 for s in segments if s.get("status") == "tts_done")
        failed = sum(1 for s in segments if s.get("status") in ("failed", "tts_failed"))
        final_status = job.get("finalVideoStatus")
        final_note = {
            "processing": " — đang ghép video...", "done": " — video hoàn chỉnh đã xong!",
            "failed": " — ghép video thất bại",
        }.get(final_status, "")
        self.summary_label.setText(
            f"{done}/{len(segments)} đoạn đã có giọng nói"
            + (f", {failed} lỗi" if failed else "")
            + final_note
        )

        if final_status in ("done", "failed"):
            self._timer.stop()
