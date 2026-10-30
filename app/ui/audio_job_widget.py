import os

from PyQt6.QtWidgets import (
    QComboBox,
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
from ..pipeline import tasks
from ..services import translation_service
from .widgets import StatusBadge


class AudioJobWidget(QWidget):
    """Detail view for the standalone 'Voice to Text' flow: one file, one
    transcript, with multilingual translation & bilingual SRT export.
    """

    def __init__(self, config, worker, job_id, notify=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.worker = worker
        self.job_id = job_id
        self.notify = notify or (lambda message, ms=4000: None)
        self._updating_controls = False

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

        sub_toolbar = QHBoxLayout()
        sub_toolbar.setSpacing(8)
        sub_toolbar.addWidget(QLabel("Ngôn ngữ đích:"))
        self.target_lang_combo = QComboBox()
        for label, code in translation_service.TARGET_LANGUAGES:
            self.target_lang_combo.addItem(label, code)
        self.target_lang_combo.currentIndexChanged.connect(self._on_settings_changed)
        sub_toolbar.addWidget(self.target_lang_combo)

        sub_toolbar.addWidget(QLabel("Chế độ SRT/Sub:"))
        self.sub_mode_combo = QComboBox()
        for label, mode_code in translation_service.SUBTITLE_MODES:
            self.sub_mode_combo.addItem(label, mode_code)
        self.sub_mode_combo.currentIndexChanged.connect(self._on_settings_changed)
        sub_toolbar.addWidget(self.sub_mode_combo)

        self.retranslate_button = QPushButton("Dịch / Cập nhật Sub")
        self.retranslate_button.setProperty("accent", True)
        self.retranslate_button.clicked.connect(self.retranslate_subtitles)
        sub_toolbar.addWidget(self.retranslate_button)
        sub_toolbar.addStretch(1)
        layout.addLayout(sub_toolbar)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e5484d;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(False)
        layout.addWidget(self.text_edit, stretch=1)

        hint_label = QLabel("Bạn có thể chọn ngôn ngữ đích (Việt / Anh / Song ngữ...) hoặc sửa transcript bên dưới trực tiếp trước khi tải SRT.")
        hint_label.setStyleSheet("color: #a0a0a0;")
        layout.addWidget(hint_label)

        actions = QHBoxLayout()
        self.save_text_button = QPushButton("Lưu chỉnh sửa")
        self.save_text_button.clicked.connect(self._save_text)
        self.srt_button = QPushButton("Tải SRT đa ngôn ngữ...")
        self.srt_button.setProperty("accent", True)
        self.srt_button.clicked.connect(self.download_srt)
        actions.addWidget(self.save_text_button)
        actions.addWidget(self.srt_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.refresh()

    def _on_settings_changed(self):
        if self._updating_controls:
            return
        target_lang = self.target_lang_combo.currentData() or "vi"
        sub_mode = self.sub_mode_combo.currentData() or "target"
        db.update_job(self.job_id, targetLanguage=target_lang, subMode=sub_mode)

    def retranslate_subtitles(self):
        segment = db.get_segment(self.job_id, 0)
        if not segment or not segment.get("transcriptCues"):
            QMessageBox.information(self, "Chưa sẵn sàng", "Hãy đợi nhận diện giọng nói xong trước khi dịch.")
            return
        target_lang = self.target_lang_combo.currentData() or "vi"
        sub_mode = self.sub_mode_combo.currentData() or "target"
        db.update_job(self.job_id, targetLanguage=target_lang, subMode=sub_mode)
        self.worker.submit(
            tasks.handle_retranslate_project, self.config, self.job_id, target_lang, sub_mode
        )
        self.notify(f"Đang dịch phụ đề sang {translation_service.language_label(target_lang)}...")

    def _save_text(self):
        text = self.text_edit.toPlainText()
        db.update_segment(self.job_id, 0, editedText=text, transcriptText=text)
        self.notify("Đã lưu nội dung transcript")

    def refresh(self):
        job = db.get_job(self.job_id)
        if not job:
            return
        segment = db.get_segment(self.job_id, 0)

        self._updating_controls = True
        try:
            target_lang = job.get("targetLanguage") or getattr(self.config, "target_language", "vi") or "vi"
            idx = self.target_lang_combo.findData(target_lang)
            if idx >= 0:
                self.target_lang_combo.setCurrentIndex(idx)
            sub_mode = job.get("subMode") or getattr(self.config, "sub_mode", "target") or "target"
            m_idx = self.sub_mode_combo.findData(sub_mode)
            if m_idx >= 0:
                self.sub_mode_combo.setCurrentIndex(m_idx)
        finally:
            self._updating_controls = False

        self.title_label.setText(job.get("originalFilename") or self.job_id)
        status = segment.get("status") if segment else job.get("stage")
        self.badge.set_status(status)
        self.lang_label.setText(f"Ngôn ngữ gốc: {segment.get('language')}" if segment and segment.get("language") else "")
        self.error_label.setText((segment or {}).get("error") or "")

        if not self.text_edit.hasFocus() and segment:
            text = segment.get("editedText") or segment.get("transcriptText") or ""
            if self.text_edit.toPlainText() != text:
                self.text_edit.setPlainText(text)

        self.srt_button.setEnabled(bool(segment and segment.get("srtPath") and os.path.exists(segment["srtPath"])))

    def download_srt(self):
        segment = db.get_segment(self.job_id, 0)
        if not segment or not segment.get("srtPath") or not os.path.exists(segment["srtPath"]):
            QMessageBox.warning(self, "Chưa sẵn sàng", "Transcript vẫn đang xử lý")
            return
        target_lang = self.target_lang_combo.currentData() or "vi"
        sub_mode = self.sub_mode_combo.currentData() or "target"
        try:
            srt_content = tasks.build_project_srt(
                self.config, self.job_id, mode=sub_mode, target_language=target_lang
            )
        except Exception:
            with open(segment["srtPath"], "r", encoding="utf-8") as src:
                srt_content = src.read()

        suffix = f"{target_lang}-{sub_mode}" if sub_mode == "bilingual" else target_lang
        dest, _ = QFileDialog.getSaveFileName(self, "Lưu SRT", f"{self.job_id}.{suffix}.srt", "SubRip (*.srt)")
        if dest:
            with open(dest, "w", encoding="utf-8") as out:
                out.write(srt_content)
            self.notify(f"Đã lưu SRT vào {dest}")
