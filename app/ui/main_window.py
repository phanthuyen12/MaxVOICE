import os
import shutil
import uuid

from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QPixmap

from .. import db
from ..config import save_config
from ..paths import job_output_dir, job_upload_dir
from ..pipeline import tasks
from .audio_job_widget import AudioJobWidget
from .project_widget import ProjectWidget
from .settings_dialog import SettingsDialog
from .widgets import JobListItemWidget

AUDIO_VIDEO_FILTER = "Audio/Video (*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.mp4 *.m4v *.mov *.mkv *.avi *.webm)"


class MainWindow(QMainWindow):
    def __init__(self, config, worker):
        super().__init__()
        self.config = config
        self.worker = worker
        self.current_job_id = None
        self.detail_widget = None
        self._job_items = {}  # job_id -> (QListWidgetItem, JobListItemWidget)
        self._refresh_pending = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._apply_pending_refresh)

        self.setWindowTitle("MaxVoice")
        self.resize(1200, 800)

        central = QWidget()
        root_layout = QVBoxLayout(central)

        logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "logo.png")
        if os.path.exists(logo_path):
            logo_label = QLabel()
            logo_label.setPixmap(QPixmap(logo_path).scaledToHeight(36, Qt.TransformationMode.SmoothTransformation))
            logo_row = QHBoxLayout()
            logo_row.addWidget(logo_label)
            logo_row.addStretch(1)
            root_layout.addLayout(logo_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        new_video_button = QPushButton("Project mới (lồng tiếng)")
        new_video_button.setProperty("accent", True)
        new_video_button.clicked.connect(self.new_video_job)
        new_audio_button = QPushButton("Voice to Text mới")
        new_audio_button.setProperty("accent", True)
        new_audio_button.clicked.connect(self.new_audio_job)
        delete_button = QPushButton("Xóa job đã chọn")
        delete_button.setProperty("danger", True)
        delete_button.clicked.connect(self.delete_selected_job)
        settings_button = QPushButton("Cài đặt")
        settings_button.clicked.connect(self.open_settings)
        toolbar.addWidget(new_video_button)
        toolbar.addWidget(new_audio_button)
        toolbar.addWidget(delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(settings_button)
        root_layout.addLayout(toolbar)

        splitter = QSplitter()
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 8, 0, 0)
        history_label = QLabel("Lịch sử job")
        history_label.setStyleSheet("font-weight: 600; font-size: 14px; color: #17a673;")
        left_layout.addWidget(history_label)
        self.job_list = QListWidget()
        self.job_list.setSpacing(2)
        self.job_list.currentItemChanged.connect(self._on_job_selected)
        left_layout.addWidget(self.job_list)
        left_panel.setMaximumWidth(380)
        splitter.addWidget(left_panel)

        self.stack = QStackedWidget()
        self.placeholder = QLabel(
            "Chọn một job ở danh sách bên trái,\nhoặc bấm \"Project mới\" / \"Voice to Text mới\" ở trên để bắt đầu."
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("color: #a0a0a0; font-size: 15px;")
        self.stack.addWidget(self.placeholder)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(1, 1)
        root_layout.addWidget(splitter, stretch=1)

        self.setCentralWidget(central)
        self.statusBar().setStyleSheet("color: #a3b0a9;")
        self.notify("MaxVoice đã sẵn sàng")

        self.worker.job_changed.connect(self._on_job_or_segment_changed)
        self.worker.segment_changed.connect(lambda job_id, _index: self._on_job_or_segment_changed(job_id))
        self.worker.task_failed.connect(self._on_task_failed)

        self.refresh_job_list()

    # -- job list -----------------------------------------------------

    def refresh_job_list(self):
        jobs = db.list_jobs(100)
        job_ids = [job["id"] for job in jobs]
        current_order = [
            self.job_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.job_list.count())
        ]

        if job_ids == current_order:
            # Fast path: same jobs, same order — just refresh each row's
            # badge/text in place instead of tearing the whole list down.
            # This is what runs on every poll tick while a job is active, so
            # avoiding a full rebuild here is what keeps the UI responsive.
            for job in jobs:
                _, widget = self._job_items.get(job["id"], (None, None))
                if widget is not None:
                    widget.update_data(job, tasks.all_segments_for_job(job))
            return

        selected_id = self.current_job_id
        self.job_list.blockSignals(True)
        self.job_list.clear()
        self._job_items = {}
        for job in jobs:
            segments = tasks.all_segments_for_job(job)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, job["id"])
            item.setSizeHint(QSize(0, 64))
            self.job_list.addItem(item)
            widget = JobListItemWidget(job, segments)
            self.job_list.setItemWidget(item, widget)
            self._job_items[job["id"]] = (item, widget)
            if job["id"] == selected_id:
                self.job_list.setCurrentItem(item)
        self.job_list.blockSignals(False)

    def notify(self, message, ms=4000):
        """Shows a brief, non-blocking confirmation in the window's status
        bar — every action (add clip, start processing, generate voice,
        retry...) reports through here so clicking something always visibly
        acknowledges it happened, without an interrupting popup for routine
        actions. Errors still go through QMessageBox (see _on_task_failed).
        """
        self.statusBar().showMessage(message, ms)

    def _on_job_selected(self, current, _previous):
        if not current:
            return
        self.show_job(current.data(Qt.ItemDataRole.UserRole))

    def _on_job_or_segment_changed(self, job_id):
        # Pipeline tasks can emit these signals many times a second (e.g. one
        # per TTS cue) — coalesce bursts into one UI refresh every ~200ms
        # instead of rebuilding widgets on every single tick.
        self._refresh_pending = True
        if not self._refresh_timer.isActive():
            self._refresh_timer.start(200)

    def _apply_pending_refresh(self):
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        self.refresh_job_list()
        if self.detail_widget is not None:
            self.detail_widget.refresh()

    def _on_task_failed(self, message):
        self.refresh_job_list()
        QMessageBox.warning(self, "Lỗi xử lý", message)

    # -- detail view -----------------------------------------------------

    def show_job(self, job_id):
        job = db.get_job(job_id)
        if not job:
            return
        self.current_job_id = job_id

        if self.detail_widget is not None:
            self.stack.removeWidget(self.detail_widget)
            self.detail_widget.deleteLater()

        if job["type"] == "video":
            self.detail_widget = ProjectWidget(self.config, self.worker, job_id, notify=self.notify)
        else:
            self.detail_widget = AudioJobWidget(self.config, self.worker, job_id, notify=self.notify)
        self.stack.addWidget(self.detail_widget)
        self.stack.setCurrentWidget(self.detail_widget)

    # -- new jobs -----------------------------------------------------

    def _copy_into_uploads(self, job_id, src_path):
        dest_dir = job_upload_dir(self.config, job_id)
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(src_path)[1]
        dest_path = os.path.join(dest_dir, f"original{ext}")
        shutil.copyfile(src_path, dest_path)
        return dest_path

    def new_video_job(self):
        project_id = str(uuid.uuid4())
        db.create_job(
            id=project_id, type="video", originalFilename="Project mới", stage="queued",
        )
        self.show_job(project_id)
        self.refresh_job_list()
        self.notify("Đã tạo project mới — thêm video rồi chọn giọng để Bắt đầu")

    def new_audio_job(self):
        src_path, _ = QFileDialog.getOpenFileName(self, "Chọn audio/video", "", AUDIO_VIDEO_FILTER)
        if not src_path:
            return
        job_id = str(uuid.uuid4())
        dest_path = self._copy_into_uploads(job_id, src_path)
        db.create_job(
            id=job_id, type="audio", originalFilename=os.path.basename(src_path),
            filePath=dest_path, stage="ready", segmentCount=1,
        )
        db.create_segment(job_id, 0, kind="audio-standalone", sourcePath=dest_path, status="cut")
        self.worker.submit(tasks.handle_transcribe_segment, self.config, job_id, 0)
        self.show_job(job_id)
        self.refresh_job_list()
        self.notify(f"Đã bắt đầu nhận diện giọng nói cho \"{os.path.basename(src_path)}\"")

    def delete_selected_job(self):
        item = self.job_list.currentItem()
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        confirm = QMessageBox.question(
            self, "Xóa job",
            "Xóa job này và toàn bộ file liên quan? Không thể hoàn tác.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        clip_ids = [clip["id"] for clip in db.list_clips(job_id)]
        db.delete_job(job_id)
        for owner_id in [job_id, *clip_ids]:
            for root in (job_upload_dir(self.config, owner_id), job_output_dir(self.config, owner_id)):
                shutil.rmtree(root, ignore_errors=True)

        if job_id == self.current_job_id:
            if self.detail_widget is not None:
                self.stack.removeWidget(self.detail_widget)
                self.detail_widget.deleteLater()
                self.detail_widget = None
            self.current_job_id = None
            self.stack.setCurrentWidget(self.placeholder)
        self.refresh_job_list()
        self.notify("Đã xóa job")

    # -- settings -----------------------------------------------------

    def open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            old_storage_dir = self.config.storage_dir
            dialog.apply_to_config()
            save_config(self.config)
            if self.config.storage_dir != old_storage_dir:
                QMessageBox.information(
                    self, "Cần khởi động lại",
                    "Thư mục lưu dữ liệu đã đổi — khởi động lại ứng dụng để áp dụng.",
                )
