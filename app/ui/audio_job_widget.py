import os

from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import db
from .widgets import StatusBadge


class AudioJobWidget(QWidget):
    """Detail view for the standalone 'Voice to Text' flow: one file, one
    transcript, no cutting/TTS/merge.
    """

    def __init__(self, config, worker, job_id, notify=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.worker = worker
        self.job_id = job_id
        self.notify = notify or (lambda message, ms=4000: None)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.badge = StatusBadge()
        self.lang_label = QLabel("")
        header.addWidget(self.title_label)
        header.addWidget(self.badge)
        header.addWidget(self.lang_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e5484d;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(False)
        layout.addWidget(self.text_edit, stretch=1)

        hint_label = QLabel("Bạn có thể sửa transcript bên dưới trực tiếp trước khi tải SRT.")
        hint_label.setStyleSheet("color: #a0a0a0;")
        layout.addWidget(hint_label)

        actions = QHBoxLayout()
        self.srt_button = QPushButton("Tải SRT")
        self.srt_button.setProperty("accent", True)
        self.srt_button.clicked.connect(self.download_srt)
        actions.addWidget(self.srt_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.refresh()

    def refresh(self):
        job = db.get_job(self.job_id)
        if not job:
            return
        segment = db.get_segment(self.job_id, 0)

        self.title_label.setText(job.get("originalFilename") or self.job_id)
        status = segment.get("status") if segment else job.get("stage")
        self.badge.set_status(status)
        self.lang_label.setText(f"Ngôn ngữ: {segment.get('language')}" if segment and segment.get("language") else "")
        self.error_label.setText((segment or {}).get("error") or "")

        if not self.text_edit.hasFocus() and segment:
            text = segment.get("transcriptText") or ""
            if self.text_edit.toPlainText() != text:
                self.text_edit.setPlainText(text)

        self.srt_button.setEnabled(bool(segment and segment.get("srtPath") and os.path.exists(segment["srtPath"])))

    def download_srt(self):
        segment = db.get_segment(self.job_id, 0)
        if not segment or not segment.get("srtPath") or not os.path.exists(segment["srtPath"]):
            QMessageBox.warning(self, "Chưa sẵn sàng", "Transcript vẫn đang xử lý")
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Lưu SRT", f"{self.job_id}.srt", "SubRip (*.srt)")
        if dest:
            with open(segment["srtPath"], "r", encoding="utf-8") as src, open(dest, "w", encoding="utf-8") as out:
                out.write(src.read())
            self.notify(f"Đã lưu SRT vào {dest}")
