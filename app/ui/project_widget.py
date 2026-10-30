import json
import os
import shutil
import uuid

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import db
from ..paths import job_output_dir, job_upload_dir, music_dir
from ..pipeline import tasks
from ..services import ffmpeg_service, local_tts_service, srt_service, translation_service
from .progress_dialog import ProgressOverviewDialog
from .status_meta import PROJECT_STAGE_LABEL, derive_overall_status
from .video_preview_widget import VideoPreviewWidget
from .voice_picker_dialog import VoicePickerDialog
from .widgets import StatusBadge, StepBreadcrumbWidget

STEPS = ["Nhập video", "Chỉnh lời & giọng", "Xem trước & xuất"]

READY_FOR_TTS_STATUSES = {"transcribed", "tts_queued", "tts_processing", "tts_done", "tts_failed"}
MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".m4v", ".mov"}
VIDEO_FILTER = "Video (*.mp4 *.mov *.m4v *.mkv *.avi *.webm)"


class SegmentCardWidget(QFrame):
    """One segment's row: status, transcript (editable), voice/TTS controls.
    Belongs to a clip (clip_id), not directly to the project.
    """

    def __init__(self, project_widget, clip_id, index, parent=None):
        super().__init__(parent)
        self.project_widget = project_widget
        self.clip_id = clip_id
        self.index = index
        self.setObjectName("segmentCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.title_label = QLabel(f"Đoạn {index + 1}")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 14px;")
        self.badge = StatusBadge()
        self.lang_label = QLabel("")
        self.progress_label = QLabel("")
        header.addWidget(self.title_label)
        header.addWidget(self.badge)
        header.addWidget(self.lang_label)
        header.addWidget(self.progress_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setFixedHeight(90)
        layout.addWidget(self.text_edit)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Lưu transcript")
        self.save_button.clicked.connect(self._save_text)
        self.retry_button = QPushButton("Nhận diện lại")
        self.retry_button.clicked.connect(self._retry)
        self.voice_button = QPushButton("Chọn giọng && tạo")
        self.voice_button.setProperty("accent", True)
        self.voice_button.clicked.connect(self._pick_voice_and_generate)
        self.listen_button = QPushButton("Nghe thử")
        self.listen_button.clicked.connect(self._listen)
        for b in (self.save_button, self.retry_button, self.voice_button, self.listen_button):
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e5484d;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        self._segment = None

    def update_data(self, segment):
        self._segment = segment
        self.setProperty("status", segment.get("status"))
        self.style().unpolish(self)
        self.style().polish(self)
        self.badge.set_status(segment.get("status"))
        self.lang_label.setText(f"Ngôn ngữ gốc: {segment.get('language') or '?'}" if segment.get("language") else "")
        progress = segment.get("ttsProgress")
        self.progress_label.setText(f"Đang tạo giọng: {progress}" if progress else "")

        if not self.text_edit.hasFocus():
            text = segment.get("editedText") or segment.get("transcriptText") or ""
            if self.text_edit.toPlainText() != text:
                self.text_edit.setPlainText(text)

        status = segment.get("status")
        self.retry_button.setEnabled(status in ("failed", "transcribed", "tts_done", "tts_failed"))
        self.voice_button.setEnabled(status in READY_FOR_TTS_STATUSES and status not in ("tts_queued", "tts_processing"))
        self.listen_button.setEnabled(bool(segment.get("ttsAudioPath")) and os.path.exists(segment.get("ttsAudioPath") or ""))
        self.error_label.setText(segment.get("error") or "")

    def _save_text(self):
        new_text = self.text_edit.toPlainText()
        db.update_segment(self.clip_id, self.index, editedText=new_text, transcriptText=new_text)
        QMessageBox.information(self, "Đã lưu", "Đã lưu transcript cho đoạn này")

    def _retry(self):
        self.project_widget.retry_segment(self.clip_id, self.index)

    def _pick_voice_and_generate(self):
        self.project_widget.pick_voice_and_generate_for_segment(self.clip_id, self.index)

    def _listen(self):
        path = self._segment.get("ttsAudioPath") if self._segment else None
        if path and os.path.exists(path):
            self.project_widget.play_audio(path, label=f"Nghe thử — Đoạn {self.index + 1}")


class ClipHeaderWidget(QFrame):
    """One clip's row in the timeline: name, status, preview/reorder/remove."""

    def __init__(self, project_widget, clip, parent=None):
        super().__init__(parent)
        self.project_widget = project_widget
        self.clip_id = clip["id"]
        self.setStyleSheet("background-color: #1f1f1f; border-radius: 6px;")

        layout = QHBoxLayout(self)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: 600;")
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(0)
        self.badge = StatusBadge()
        layout.addWidget(QLabel("Clip"))
        layout.addWidget(self.title_label, 1)
        layout.addWidget(self.badge)

        preview_button = QPushButton("Xem")
        preview_button.clicked.connect(lambda: project_widget.preview_clip(self.clip_id))
        up_button = QPushButton("Lên")
        up_button.clicked.connect(lambda: project_widget.move_clip(self.clip_id, -1))
        down_button = QPushButton("Xuống")
        down_button.clicked.connect(lambda: project_widget.move_clip(self.clip_id, 1))
        remove_button = QPushButton("Xóa")
        remove_button.setProperty("danger", True)
        remove_button.clicked.connect(lambda: project_widget.remove_clip(self.clip_id))
        for b in (preview_button, up_button, down_button, remove_button):
            layout.addWidget(b)

        self.update_data(clip)

    def update_data(self, clip):
        self.title_label.setText(clip.get("originalFilename") or clip["id"])
        self.badge.set_status(clip.get("stage"), kind="job")


class ProjectWidget(QWidget):
    """CapCut-ish project view: real video preview, a clip timeline (a
    project can hold several video files, cut/dubbed/merged in order),
    multilingual subtitle/SRT controls (Vietnamese, English, Bilingual, etc.),
    and a single "Bắt đầu" button that runs cut → transcribe → voice → merge.
    """

    def __init__(self, config, worker, project_id, notify=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.worker = worker
        self.project_id = project_id
        self.notify = notify or (lambda message, ms=4000: None)
        self.clip_headers = {}
        self.cards = {}
        self._updating_controls = False

        layout = QVBoxLayout(self)

        self.breadcrumb = StepBreadcrumbWidget(STEPS)
        layout.addWidget(self.breadcrumb)

        header = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.job_badge = StatusBadge(kind="job")
        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet("color: #17a673;")
        header.addWidget(self.title_label)
        header.addWidget(self.job_badge)
        header.addWidget(self.stage_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.job_error_label = QLabel("")
        self.job_error_label.setStyleSheet("color: #e5484d;")
        self.job_error_label.setWordWrap(True)
        layout.addWidget(self.job_error_label)

        self.preview = VideoPreviewWidget()
        layout.addWidget(self.preview)

        # Toolbar 1: Video input, Voice selection, Auto Start
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        add_clip_button = QPushButton("+ Thêm video")
        add_clip_button.setProperty("accent", True)
        add_clip_button.clicked.connect(self.add_clips)
        self.voice_label = QLabel("Giọng: chưa chọn")
        self.voice_label.setStyleSheet("color: #a0a0a0;")
        choose_voice_button = QPushButton("Chọn giọng (Việt / Anh / Đa ngôn ngữ)")
        choose_voice_button.clicked.connect(self.choose_default_voice)
        self.start_button = QPushButton("Bắt đầu (tự động)")
        self.start_button.setProperty("accent", True)
        self.start_button.clicked.connect(self.start_project)
        progress_button = QPushButton("Xem tiến độ")
        progress_button.clicked.connect(self.show_progress_dialog)
        toolbar.addWidget(add_clip_button)
        toolbar.addSpacing(10)
        toolbar.addWidget(self.voice_label)
        toolbar.addWidget(choose_voice_button)
        toolbar.addSpacing(10)
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(progress_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        # Toolbar 2: Multilingual Subtitles & SRT settings
        sub_toolbar = QHBoxLayout()
        sub_toolbar.setSpacing(8)
        sub_toolbar.addWidget(QLabel("Ngôn ngữ Sub & Voice:"))
        self.target_lang_combo = QComboBox()
        for label, code in translation_service.TARGET_LANGUAGES:
            self.target_lang_combo.addItem(label, code)
        self.target_lang_combo.currentIndexChanged.connect(self._on_language_settings_changed)
        sub_toolbar.addWidget(self.target_lang_combo)

        sub_toolbar.addWidget(QLabel("Chế độ SRT/Sub:"))
        self.sub_mode_combo = QComboBox()
        for label, mode_code in translation_service.SUBTITLE_MODES:
            self.sub_mode_combo.addItem(label, mode_code)
        self.sub_mode_combo.currentIndexChanged.connect(self._on_language_settings_changed)
        sub_toolbar.addWidget(self.sub_mode_combo)

        self.burn_sub_checkbox = QCheckBox("Ghi phụ đề (Sub) vào video")
        self.burn_sub_checkbox.toggled.connect(self._on_burn_sub_toggled)
        sub_toolbar.addWidget(self.burn_sub_checkbox)

        self.retranslate_button = QPushButton("Dịch / Cập nhật Sub")
        self.retranslate_button.clicked.connect(self.retranslate_project_subtitles)
        sub_toolbar.addWidget(self.retranslate_button)
        sub_toolbar.addStretch(1)
        layout.addLayout(sub_toolbar)

        self._progress_dialog = None

        # Toolbar 3: Background music, Merge, Download Multilingual SRT
        toolbar2 = QHBoxLayout()
        toolbar2.setSpacing(8)
        self.music_label = QLabel("Nhạc nền: chưa có")
        self.music_label.setStyleSheet("color: #a0a0a0;")
        music_button = QPushButton("Chọn nhạc nền")
        music_button.clicked.connect(self.choose_music)
        remove_music_button = QPushButton("Xóa nhạc nền")
        remove_music_button.setProperty("danger", True)
        remove_music_button.clicked.connect(self.remove_music)
        self.remove_music_button = remove_music_button
        self.merge_button = QPushButton("Ghép lại video hoàn chỉnh")
        self.merge_button.setProperty("accent", True)
        self.merge_button.clicked.connect(self.merge_project)
        self.srt_button = QPushButton("Tải SRT đa ngôn ngữ...")
        self.srt_button.clicked.connect(self.download_full_srt)
        toolbar2.addWidget(self.music_label)
        toolbar2.addWidget(music_button)
        toolbar2.addWidget(remove_music_button)
        toolbar2.addSpacing(12)
        toolbar2.addWidget(self.merge_button)
        toolbar2.addWidget(self.srt_button)
        toolbar2.addStretch(1)
        layout.addLayout(toolbar2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline_container = QWidget()
        self.timeline_layout = QVBoxLayout(self.timeline_container)
        self.timeline_layout.addStretch(1)
        scroll.setWidget(self.timeline_container)
        layout.addWidget(scroll, stretch=1)

        final_row = QHBoxLayout()
        self.final_status_label = QLabel("")
        self.final_status_label.setStyleSheet("font-weight: 600; color: #17a673;")
        self.open_final_button = QPushButton("Xem video hoàn chỉnh")
        self.open_final_button.setProperty("accent", True)
        self.open_final_button.clicked.connect(self.open_final_video)
        self.save_final_button = QPushButton("Lưu video ra...")
        self.save_final_button.clicked.connect(self.save_final_video)
        final_row.addWidget(self.final_status_label)
        final_row.addWidget(self.open_final_button)
        final_row.addWidget(self.save_final_button)
        final_row.addStretch(1)
        layout.addLayout(final_row)

        self.refresh()

    # -- data refresh -----------------------------------------------------

    def refresh(self):
        job = db.get_job(self.project_id)
        if not job:
            return
        clips = db.list_clips(self.project_id)
        all_segments = tasks.project_segments_in_order(self.project_id)

        self._updating_controls = True
        try:
            target_lang = job.get("targetLanguage") or getattr(self.config, "target_language", "vi") or "vi"
            lang_idx = self.target_lang_combo.findData(target_lang)
            if lang_idx >= 0:
                self.target_lang_combo.setCurrentIndex(lang_idx)

            sub_mode = job.get("subMode") or getattr(self.config, "sub_mode", "target") or "target"
            mode_idx = self.sub_mode_combo.findData(sub_mode)
            if mode_idx >= 0:
                self.sub_mode_combo.setCurrentIndex(mode_idx)

            burn_sub = bool(job.get("burnSubtitles"))
            self.burn_sub_checkbox.setChecked(burn_sub)
        finally:
            self._updating_controls = False

        self.title_label.setText(job.get("originalFilename") or "Project")
        self.job_badge.set_status(derive_overall_status(all_segments), kind="job")
        self.stage_label.setText(PROJECT_STAGE_LABEL.get(job.get("stage"), ""))
        self.job_error_label.setText(job.get("error") or "")

        voice_name = job.get("defaultVoiceName")
        self.voice_label.setText(f"Giọng: {voice_name}" if voice_name else "Giọng: chưa chọn")

        music_name = job.get("backgroundMusicName")
        self.music_label.setText(f"Nhạc nền: {music_name}" if music_name else "Nhạc nền: chưa có")
        self.remove_music_button.setEnabled(bool(job.get("backgroundMusicPath")))

        all_tts_done = bool(all_segments) and all(s.get("status") == "tts_done" for s in all_segments)
        self.merge_button.setEnabled(all_tts_done and job.get("finalVideoStatus") != "processing")

        final_status = job.get("finalVideoStatus")
        has_final = bool(job.get("finalVideoPath")) and os.path.exists(job.get("finalVideoPath") or "")
        self.final_status_label.setText(
            {"processing": "Đang ghép video...", "done": "Video hoàn chỉnh đã sẵn sàng",
             "failed": "Ghép video thất bại"}.get(final_status, "")
        )
        self.open_final_button.setEnabled(has_final)
        self.save_final_button.setEnabled(has_final)

        if has_final:
            active_step = 2
        elif clips:
            active_step = 1
        else:
            active_step = 0
        self.breadcrumb.set_active_step(active_step)

        self._rebuild_timeline(clips)

    def _on_language_settings_changed(self):
        if self._updating_controls:
            return
        target_lang = self.target_lang_combo.currentData() or "vi"
        sub_mode = self.sub_mode_combo.currentData() or "target"
        db.update_job(self.project_id, targetLanguage=target_lang, subMode=sub_mode)
        self.config.target_language = target_lang
        self.config.sub_mode = sub_mode

    def _on_burn_sub_toggled(self, checked):
        if self._updating_controls:
            return
        db.update_job(self.project_id, burnSubtitles=1 if checked else 0)
        self.config.burn_subtitles = bool(checked)

    def retranslate_project_subtitles(self):
        segments = tasks.project_segments_in_order(self.project_id)
        if not segments or not any(s.get("transcriptCues") for s in segments):
            QMessageBox.information(
                self, "Chưa có phụ đề", "Hãy thêm video và đợi nhận diện xong trước khi dịch/cập nhật phụ đề."
            )
            return
        target_lang = self.target_lang_combo.currentData() or "vi"
        sub_mode = self.sub_mode_combo.currentData() or "target"
        db.update_job(self.project_id, targetLanguage=target_lang, subMode=sub_mode)
        self.worker.submit(
            tasks.handle_retranslate_project, self.config, self.project_id, target_lang, sub_mode
        )
        lang_name = translation_service.language_label(target_lang)
        self.notify(f"Đang dịch & cập nhật phụ đề sang {lang_name} ({self.sub_mode_combo.currentText()})...")

    def _rebuild_timeline(self, clips):
        seen_clip_ids = set()
        seen_card_keys = set()

        for clip in clips:
            clip_id = clip["id"]
            seen_clip_ids.add(clip_id)
            header = self.clip_headers.get(clip_id)
            if header is None:
                header = ClipHeaderWidget(self, clip)
                self.clip_headers[clip_id] = header
                self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, header)
            header.update_data(clip)

            for segment in db.get_all_segments(clip_id):
                key = (clip_id, segment["index"])
                seen_card_keys.add(key)
                card = self.cards.get(key)
                if card is None:
                    card = SegmentCardWidget(self, clip_id, segment["index"])
                    self.cards[key] = card
                    header_position = self.timeline_layout.indexOf(header)
                    self.timeline_layout.insertWidget(header_position + 1, card)
                card.update_data(segment)

        for clip_id in list(self.clip_headers):
            if clip_id not in seen_clip_ids:
                self.clip_headers.pop(clip_id).deleteLater()
        for key in list(self.cards):
            if key not in seen_card_keys:
                self.cards.pop(key).deleteLater()

    # -- clips -----------------------------------------------------

    def add_clips(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Chọn 1 hoặc nhiều video", "", VIDEO_FILTER)
        if not paths:
            return
        for src_path in paths:
            clip_id = str(uuid.uuid4())
            dest_dir = job_upload_dir(self.config, clip_id)
            os.makedirs(dest_dir, exist_ok=True)
            ext = os.path.splitext(src_path)[1]
            dest_path = os.path.join(dest_dir, f"original{ext}")
            shutil.copyfile(src_path, dest_path)

            order_index = db.next_clip_order_index(self.project_id)
            db.create_clip(
                id=clip_id, jobId=self.project_id, orderIndex=order_index,
                originalFilename=os.path.basename(src_path), filePath=dest_path, stage="queued",
            )
            self.worker.submit(tasks.handle_cut_clip, self.config, clip_id, dest_path)
        self.refresh()
        self.notify(f"Đã thêm {len(paths)} video — đang cắt đoạn và nhận diện giọng nói...")

    def remove_clip(self, clip_id):
        clip = db.get_clip(clip_id)
        if not clip:
            return
        confirm = QMessageBox.question(
            self, "Xóa clip", f"Xóa clip \"{clip.get('originalFilename')}\" khỏi project?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        db.delete_clip(clip_id)
        shutil.rmtree(job_upload_dir(self.config, clip_id), ignore_errors=True)
        shutil.rmtree(job_output_dir(self.config, clip_id), ignore_errors=True)
        self.refresh()
        self.notify(f"Đã xóa clip \"{clip.get('originalFilename')}\"")

    def move_clip(self, clip_id, direction):
        clips = db.list_clips(self.project_id)
        position = next((i for i, c in enumerate(clips) if c["id"] == clip_id), None)
        if position is None:
            return
        swap_with = position + direction
        if not (0 <= swap_with < len(clips)):
            return
        a, b = clips[position], clips[swap_with]
        db.update_clip(a["id"], orderIndex=b["orderIndex"])
        db.update_clip(b["id"], orderIndex=a["orderIndex"])
        self.refresh()

    def preview_clip(self, clip_id):
        clip = db.get_clip(clip_id)
        if clip and os.path.exists(clip.get("filePath") or ""):
            self.preview.load(clip["filePath"], label=f"Xem: {clip.get('originalFilename')}")
            self.preview.set_cues(self._clip_cues_in_clip_timeline(clip_id))
            self.preview.play()

    def play_audio(self, path, label=None):
        """Plays a standalone audio file (TTS result, etc.) through the same
        in-app player used for video preview, instead of relying on the OS
        having some app associated with .wav — one playback path, always
        works the same way.
        """
        self.preview.load(path, label=label)
        self.preview.clear_cues()
        self.preview.play()

    def _clip_cues_in_clip_timeline(self, clip_id):
        """Every segment's SRT cues, offset by the segment's startSec so the
        timestamps line up with the ORIGINAL (uncut) clip file being
        previewed — formatted according to the selected `subMode`.
        """
        sub_mode = self.sub_mode_combo.currentData() or "target"
        cues = []
        for segment in db.get_all_segments(clip_id):
            raw = segment.get("transcriptCues")
            if not raw:
                continue
            offset = segment.get("startSec") or 0
            try:
                for cue in json.loads(raw):
                    cues.append({
                        "start": offset + cue["start"],
                        "end": offset + cue["end"],
                        "text": srt_service.format_cue_text(cue, mode=sub_mode),
                    })
            except (ValueError, TypeError, KeyError):
                continue
        return cues

    # -- voice / start -----------------------------------------------------

    def choose_default_voice(self):
        preferred_lang = self.target_lang_combo.currentData()
        dialog = VoicePickerDialog(self.config, self, preferred_lang=preferred_lang)
        if dialog.exec() != dialog.DialogCode.Accepted or not dialog.selected_voice:
            return None
        voice = dialog.selected_voice
        updates = {"defaultVoiceId": voice["voice_id"], "defaultVoiceName": voice["name"]}
        # Automatically align targetLanguage if user picked an English/Multilingual voice
        voice_lang = voice.get("lang")
        if local_tts_service.is_multilingual_voice(voice["voice_id"]) and voice_lang:
            updates["targetLanguage"] = voice_lang
        db.update_job(self.project_id, **updates)
        self.refresh()
        self.notify(f"Đã chọn giọng: {voice['name']}")
        return voice

    def start_project(self):
        if not db.list_clips(self.project_id):
            QMessageBox.warning(self, "Chưa có video", "Thêm ít nhất 1 video trước khi Bắt đầu")
            return
        job = db.get_job(self.project_id)
        voice_id = job.get("defaultVoiceId")
        if not voice_id:
            voice = self.choose_default_voice()
            if not voice:
                return
            voice_id = voice["voice_id"]

        target_lang = self.target_lang_combo.currentData() or job.get("targetLanguage") or "vi"
        sub_mode = self.sub_mode_combo.currentData() or job.get("subMode") or "target"
        db.update_job(self.project_id, targetLanguage=target_lang, subMode=sub_mode)

        self.worker.submit(tasks.handle_start_project, self.config, self.project_id, voice_id, target_lang)
        self.refresh()
        self.notify("Đã bắt đầu xử lý tự động: cắt video → nhận diện & dịch → tạo giọng nói → ghép video")
        self._progress_dialog = ProgressOverviewDialog(self.config, self.worker, self.project_id, self)
        self._progress_dialog.show()

    def show_progress_dialog(self):
        if self._progress_dialog is None:
            self._progress_dialog = ProgressOverviewDialog(self.config, self.worker, self.project_id, self)
        self._progress_dialog.refresh()
        self._progress_dialog.show()
        self._progress_dialog.raise_()
        self._progress_dialog.activateWindow()

    # -- per-segment actions -----------------------------------------------------

    def retry_segment(self, clip_id, index):
        target_lang = self.target_lang_combo.currentData() or "vi"
        sub_mode = self.sub_mode_combo.currentData() or "target"
        db.update_segment(
            clip_id, index, status="cut", error="", audioPath="", srtPath="",
            transcriptText="", editedText=None, language="",
        )
        self.refresh()
        self.worker.submit(
            tasks.handle_transcribe_segment, self.config, clip_id, index, target_lang, sub_mode
        )
        self.notify(f"Đang nhận diện lại đoạn {index + 1}...")

    def _all_segments_ready_for_tts(self):
        segments = tasks.project_segments_in_order(self.project_id)
        not_ready = next((s for s in segments if s.get("status") not in READY_FOR_TTS_STATUSES), None)
        if not_ready:
            QMessageBox.warning(
                self, "Chưa sẵn sàng",
                "Đợi dịch và nhận diện xong tất cả các đoạn rồi mới tạo giọng nói",
            )
            return None
        return segments

    def pick_voice_and_generate_for_segment(self, clip_id, index):
        if self._all_segments_ready_for_tts() is None:
            return
        segment = db.get_segment(clip_id, index)
        if segment.get("status") in ("tts_queued", "tts_processing"):
            QMessageBox.information(self, "Đang xử lý", "Đoạn này đang tạo giọng nói, vui lòng đợi")
            return

        preferred_lang = self.target_lang_combo.currentData() or "vi"
        dialog = VoicePickerDialog(self.config, self, preferred_lang=preferred_lang)
        if dialog.exec() != dialog.DialogCode.Accepted or not dialog.selected_voice:
            return
        voice = dialog.selected_voice
        target_lang = voice.get("lang") if local_tts_service.is_multilingual_voice(voice["voice_id"]) else preferred_lang
        db.update_segment(clip_id, index, status="tts_queued", ttsVoiceId=voice["voice_id"], error="")
        self.refresh()
        self.worker.submit(tasks.handle_tts_segment, self.config, clip_id, index, voice["voice_id"], target_lang)
        self.notify(f"Đã bắt đầu tạo giọng nói \"{voice['name']}\" cho đoạn {index + 1}")

    # -- music -----------------------------------------------------

    def choose_music(self):
        job = db.get_job(self.project_id)
        if job.get("finalVideoStatus") == "processing":
            QMessageBox.warning(self, "Đang ghép video", "Video đang ghép, hãy đợi hoàn tất rồi tải nhạc")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file nhạc nền", "",
            "Audio/Video (*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.mp4 *.m4v *.mov)",
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in MUSIC_EXTENSIONS:
            QMessageBox.warning(self, "File không hợp lệ", "File nhạc phải là MP3, WAV, M4A, AAC, OGG, FLAC hoặc video MP4/MOV có tiếng")
            return
        try:
            if not ffmpeg_service.has_audio_stream(path, self.config.ffprobe_path):
                QMessageBox.warning(self, "Không có âm thanh", "File đã chọn không có luồng âm thanh")
                return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Lỗi kiểm tra file", str(exc))
            return

        dest_dir = music_dir(self.config, self.project_id)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, f"background-{uuid.uuid4()}{ext}")
        shutil.copyfile(path, dest_path)

        previous_path = job.get("backgroundMusicPath")
        db.update_job(self.project_id, backgroundMusicPath=dest_path, backgroundMusicName=os.path.basename(path))
        if previous_path and previous_path != dest_path and os.path.exists(previous_path):
            try:
                os.unlink(previous_path)
            except OSError:
                pass
        self.refresh()
        self.notify(f"Đã chọn nhạc nền: {os.path.basename(path)}")

    def remove_music(self):
        job = db.get_job(self.project_id)
        if job.get("finalVideoStatus") == "processing":
            QMessageBox.warning(self, "Đang ghép video", "Video đang ghép, hãy đợi hoàn tất rồi xóa nhạc")
            return
        previous_path = job.get("backgroundMusicPath")
        db.update_job(self.project_id, backgroundMusicPath=None, backgroundMusicName=None)
        if previous_path and os.path.exists(previous_path):
            try:
                os.unlink(previous_path)
            except OSError:
                pass
        self.refresh()
        self.notify("Đã xóa nhạc nền")

    # -- merge / final video -----------------------------------------------------

    def merge_project(self):
        job = db.get_job(self.project_id)
        segments = tasks.project_segments_in_order(self.project_id)
        not_ready = next((s for s in segments if s.get("status") != "tts_done"), None)
        if not_ready:
            clip_label = not_ready.get("clipName") or not_ready["clipId"]
            QMessageBox.warning(self, "Chưa sẵn sàng", f"{clip_label} — đoạn {not_ready['index'] + 1} chưa có giọng nói")
            return
        music_path = job.get("backgroundMusicPath")
        if music_path and not os.path.exists(music_path):
            QMessageBox.warning(self, "Thiếu file nhạc", "File nhạc đã lưu không còn trên máy, vui lòng chọn lại")
            return

        db.update_job(self.project_id, finalVideoStatus="processing")
        self.refresh()
        self.worker.submit(tasks.handle_merge_project, self.config, self.project_id, music_path)
        self.notify("Đã bắt đầu ghép video hoàn chỉnh...")

    def open_final_video(self):
        job = db.get_job(self.project_id)
        path = job.get("finalVideoPath")
        if path and os.path.exists(path):
            self.preview.load(path, label="Video hoàn chỉnh")
            self.preview.play()

    def save_final_video(self):
        job = db.get_job(self.project_id)
        path = job.get("finalVideoPath")
        if not (path and os.path.exists(path)):
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Lưu video", os.path.basename(path), "MP4 (*.mp4)")
        if dest:
            shutil.copyfile(path, dest)
            QMessageBox.information(self, "Đã lưu", f"Đã lưu video vào {dest}")

    def download_full_srt(self):
        segments = tasks.project_segments_in_order(self.project_id)
        if not segments or any(not s.get("srtPath") or not os.path.exists(s["srtPath"]) for s in segments):
            QMessageBox.warning(self, "Chưa sẵn sàng", "Transcript của một số đoạn vẫn đang xử lý")
            return

        current_target = self.target_lang_combo.currentData() or "vi"
        current_mode = self.sub_mode_combo.currentData() or "target"

        export_options = [
            f"Theo cài đặt hiện tại ({translation_service.language_label(current_target)} — {self.sub_mode_combo.currentText()})",
            "Song ngữ (Ngôn ngữ đích + Ngôn ngữ gốc)",
            "Chỉ Ngôn ngữ gốc (Original)",
            "Dịch & Xuất SRT Tiếng Việt (vi)",
            "Dịch & Xuất SRT Tiếng Anh (en)",
            "Dịch & Xuất SRT Tiếng Trung (zh)",
            "Dịch & Xuất SRT Tiếng Nhật (ja)",
            "Dịch & Xuất SRT Tiếng Hàn (ko)",
            "Dịch & Xuất SRT Tiếng Nga (ru)",
        ]
        choice, ok = QInputDialog.getItem(
            self, "Chọn định dạng / ngôn ngữ SRT",
            "Chọn chế độ xuất file phụ đề (.srt):",
            export_options, 0, False,
        )
        if not ok or not choice:
            return

        chosen_idx = export_options.index(choice)
        if chosen_idx == 0:
            mode, target_lang = current_mode, current_target
        elif chosen_idx == 1:
            mode, target_lang = "bilingual", current_target
        elif chosen_idx == 2:
            mode, target_lang = "original", "original"
        else:
            lang_map = {3: "vi", 4: "en", 5: "zh", 6: "ja", 7: "ko", 8: "ru"}
            mode, target_lang = "target", lang_map.get(chosen_idx, current_target)

        try:
            combined = tasks.build_project_srt(
                self.config, self.project_id, mode=mode, target_language=target_lang
            )
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi dịch/xuất SRT", str(exc))
            return

        suffix = f"{target_lang}-{mode}" if mode == "bilingual" else target_lang
        default_name = f"{self.project_id}.{suffix}.srt"
        dest, _ = QFileDialog.getSaveFileName(self, "Lưu SRT", default_name, "SubRip (*.srt)")
        if dest:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(combined)
            self.notify(f"Đã lưu SRT ({suffix}) vào {dest}")
