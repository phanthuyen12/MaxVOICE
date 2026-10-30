from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..services import ffmpeg_service, translation_service


class _InstallModelThread(QThread):
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, source, target="vi"):
        super().__init__()
        self.source = source
        self.target = target

    def run(self):
        try:
            translation_service.install_translation_model(
                self.source, target=self.target, progress_cb=self.progress.emit
            )
            self.finished_ok.emit(f"Đã cài xong model dịch {self.source} → {self.target}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class SettingsDialog(QDialog):
    """Local app settings — replaces backend/.env (no Redis/Mongo/Node config needed)."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._install_thread = None

        self.setWindowTitle("Cài đặt")
        self.resize(520, 0)

        layout = QVBoxLayout(self)
        title = QLabel("Cài đặt")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #17a673;")
        layout.addWidget(title)
        form = QFormLayout()

        self.tts_backend_combo = QComboBox()
        self.tts_backend_combo.addItem("VieNeu & Đa ngôn ngữ — chạy local/miễn phí (khuyên dùng)", "vieneu")
        self.tts_backend_combo.addItem("Resona — API trả theo ký tự, cần API key", "resona")
        self.tts_backend_combo.setCurrentIndex(0 if config.tts_backend == "vieneu" else 1)
        self.tts_backend_combo.currentIndexChanged.connect(self._update_backend_fields_enabled)
        form.addRow("Engine tạo giọng nói:", self.tts_backend_combo)

        self.default_target_lang_combo = QComboBox()
        for label, code in translation_service.TARGET_LANGUAGES:
            self.default_target_lang_combo.addItem(label, code)
        idx = self.default_target_lang_combo.findData(getattr(config, "target_language", "vi"))
        if idx >= 0:
            self.default_target_lang_combo.setCurrentIndex(idx)
        form.addRow("Ngôn ngữ Sub/Voice mặc định:", self.default_target_lang_combo)

        self.default_sub_mode_combo = QComboBox()
        for label, code in translation_service.SUBTITLE_MODES:
            self.default_sub_mode_combo.addItem(label, code)
        m_idx = self.default_sub_mode_combo.findData(getattr(config, "sub_mode", "target"))
        if m_idx >= 0:
            self.default_sub_mode_combo.setCurrentIndex(m_idx)
        form.addRow("Chế độ SRT/Sub mặc định:", self.default_sub_mode_combo)

        self.api_key_edit = QLineEdit(config.resona_api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Resona API Key:", self.api_key_edit)

        self.api_base_edit = QLineEdit(config.resona_api_base)
        form.addRow("Resona API Base:", self.api_base_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 1200)
        self.timeout_spin.setValue(int(config.resona_request_timeout_s))
        self.timeout_spin.setSuffix(" giây")
        form.addRow("Resona request timeout:", self.timeout_spin)

        self.segment_seconds_spin = QSpinBox()
        self.segment_seconds_spin.setRange(10, 600)
        self.segment_seconds_spin.setValue(int(config.segment_seconds))
        self.segment_seconds_spin.setSuffix(" giây")
        form.addRow("Độ dài mỗi đoạn video:", self.segment_seconds_spin)

        self.whisper_model_combo = QComboBox()
        self.whisper_model_combo.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.whisper_model_combo.setCurrentText(config.whisper_model)
        form.addRow("Whisper model:", self.whisper_model_combo)

        self.whisper_device_combo = QComboBox()
        self.whisper_device_combo.addItems(["cpu", "cuda"])
        self.whisper_device_combo.setCurrentText(config.whisper_device)
        form.addRow("Whisper device:", self.whisper_device_combo)

        self.whisper_compute_combo = QComboBox()
        self.whisper_compute_combo.addItems(["int8", "float16", "float32"])
        self.whisper_compute_combo.setCurrentText(config.whisper_compute_type)
        form.addRow("Whisper compute type:", self.whisper_compute_combo)

        self.ffmpeg_edit = QLineEdit(config.ffmpeg_path)
        form.addRow("Đường dẫn ffmpeg:", self.ffmpeg_edit)

        self.ffprobe_edit = QLineEdit(config.ffprobe_path)
        form.addRow("Đường dẫn ffprobe:", self.ffprobe_edit)

        storage_row = QWidget()
        storage_layout = QHBoxLayout(storage_row)
        storage_layout.setContentsMargins(0, 0, 0, 0)
        self.storage_edit = QLineEdit(config.storage_dir)
        browse_button = QPushButton("Chọn...")
        browse_button.clicked.connect(self._browse_storage_dir)
        storage_layout.addWidget(self.storage_edit)
        storage_layout.addWidget(browse_button)
        form.addRow("Thư mục lưu dữ liệu:", storage_row)

        layout.addLayout(form)

        check_ffmpeg_button = QPushButton("Kiểm tra ffmpeg/ffprobe")
        check_ffmpeg_button.clicked.connect(self._check_ffmpeg)
        layout.addWidget(check_ffmpeg_button)

        translate_row = QHBoxLayout()
        self.translate_lang_combo = QComboBox()
        for label, code in translation_service.COMMON_SOURCE_LANGUAGES:
            self.translate_lang_combo.addItem(label, code)
        install_model_button = QPushButton("Cài model dịch Offline (Argos)")
        install_model_button.clicked.connect(self._install_translation_model)
        translate_row.addWidget(self.translate_lang_combo, 1)
        translate_row.addWidget(install_model_button)
        layout.addLayout(translate_row)
        hint_label = QLabel(
            "Mặc định phần mềm tự động dịch đa ngôn ngữ (Việt ↔ Anh, Trung, Nhật, Hàn, Pháp, Đức...). "
            "Bạn cũng có thể cài thêm gói Argos Translate để dịch hoàn toàn offline."
        )
        hint_label.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("accent", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_backend_fields_enabled()

    def _update_backend_fields_enabled(self):
        is_resona = self.tts_backend_combo.currentData() == "resona"
        for widget in (self.api_key_edit, self.api_base_edit, self.timeout_spin):
            widget.setEnabled(is_resona)

    def _browse_storage_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu dữ liệu", self.storage_edit.text())
        if directory:
            self.storage_edit.setText(directory)

    def _check_ffmpeg(self):
        ffmpeg_path = self.ffmpeg_edit.text().strip() or "ffmpeg"
        ffprobe_path = self.ffprobe_edit.text().strip() or "ffprobe"
        try:
            ffmpeg_service.check_binary(ffmpeg_path)
            ffmpeg_service.check_binary(ffprobe_path)
            self.status_label.setText("OK: tìm thấy ffmpeg và ffprobe")
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Lỗi: {exc}")

    def _install_translation_model(self):
        source = self.translate_lang_combo.currentData()
        if not source:
            code, ok = QInputDialog.getText(
                self, "Mã ngôn ngữ", "Nhập mã ISO-639-1 (ví dụ: ko, ja, zh, fr, de):"
            )
            if not ok or not code.strip():
                return
            source = code.strip().lower()

        target = self.default_target_lang_combo.currentData() or "vi"
        if target == "original" or target == source:
            target = "vi"
        self.status_label.setText(f"Đang cài model dịch {source} → {target}...")
        self._install_thread = _InstallModelThread(source, target=target)
        self._install_thread.progress.connect(self.status_label.setText)
        self._install_thread.finished_ok.connect(self.status_label.setText)
        self._install_thread.failed.connect(lambda msg: self.status_label.setText(f"Lỗi: {msg}"))
        self._install_thread.start()

    def apply_to_config(self):
        self.config.tts_backend = self.tts_backend_combo.currentData()
        self.config.target_language = self.default_target_lang_combo.currentData() or "vi"
        self.config.sub_mode = self.default_sub_mode_combo.currentData() or "target"
        self.config.resona_api_key = self.api_key_edit.text().strip()
        self.config.resona_api_base = self.api_base_edit.text().strip() or "https://resona.live"
        self.config.resona_request_timeout_s = self.timeout_spin.value()
        self.config.segment_seconds = self.segment_seconds_spin.value()
        self.config.whisper_model = self.whisper_model_combo.currentText()
        self.config.whisper_device = self.whisper_device_combo.currentText()
        self.config.whisper_compute_type = self.whisper_compute_combo.currentText()
        self.config.ffmpeg_path = self.ffmpeg_edit.text().strip() or "ffmpeg"
        self.config.ffprobe_path = self.ffprobe_edit.text().strip() or "ffprobe"
        self.config.storage_dir = self.storage_edit.text().strip() or self.config.storage_dir
