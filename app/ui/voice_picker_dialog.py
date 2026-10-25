import os
import re

from PyQt6.QtCore import QSize, QUrl, Qt, QThread, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..services import local_tts_service, resona_service
from .widgets import AvatarLabel

DEMO_TEXT = "Xin chào, đây là giọng nói mẫu."


class VoiceRowWidget(QWidget):
    """One voice card: avatar circle, name + subtitle, inline play button —
    matches the "Giọng đọc" cards in the reference design.
    """

    def __init__(self, name, subtitle, on_play, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(10)
        layout.addWidget(AvatarLabel(name))
        col = QVBoxLayout()
        col.setSpacing(1)
        title = QLabel(name)
        title.setStyleSheet("font-weight: 600;")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        col.addWidget(title)
        col.addWidget(subtitle_label)
        layout.addLayout(col, 1)
        play_button = QPushButton("Nghe")
        play_button.setMinimumWidth(60)
        play_button.clicked.connect(on_play)
        layout.addWidget(play_button)


class _FetchVoicesThread(QThread):
    done = pyqtSignal(dict, str)

    def __init__(self, config, params):
        super().__init__()
        self.config = config
        self.params = params

    def run(self):
        try:
            data = resona_service.list_voices(
                self.config.resona_api_base,
                self.config.resona_api_key,
                self.params,
                timeout=self.config.resona_request_timeout_s,
            )
            self.done.emit(data, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit({}, str(exc))


class _ListVieneuVoicesThread(QThread):
    done = pyqtSignal(list, dict, str)  # presets, cloned, error

    def __init__(self, config):
        super().__init__()
        self.config = config

    def run(self):
        try:
            # First call in the process loads the ONNX model (can take a
            # few seconds) — run it here, off the GUI thread, so opening
            # this dialog never freezes the window.
            presets = local_tts_service.list_preset_voices()
            cloned = local_tts_service.load_cloned_voices(self.config)
            self.done.emit(presets, cloned, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit([], {}, str(exc))


class _CloneVoiceThread(QThread):
    done = pyqtSignal(str, str)  # name, error

    def __init__(self, config, name, ref_audio_path):
        super().__init__()
        self.config = config
        self.name = name
        self.ref_audio_path = ref_audio_path

    def run(self):
        try:
            local_tts_service.clone_voice(self.config, self.name, self.ref_audio_path)
            self.done.emit(self.name, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit("", str(exc))


class _SynthesizeDemoThread(QThread):
    done = pyqtSignal(str, str)  # wav_path, error

    def __init__(self, config, voice_name, dest_path):
        super().__init__()
        self.config = config
        self.voice_name = voice_name
        self.dest_path = dest_path

    def run(self):
        try:
            audio = local_tts_service.synthesize(DEMO_TEXT, voice=self.voice_name)
            local_tts_service.save_wav(audio, self.dest_path)
            self.done.emit(self.dest_path, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit("", str(exc))


class VoicePickerDialog(QDialog):
    """Lets the user pick a voice. Branches on config.tts_backend:
    - "vieneu": 25 local preset voices + any voice cloned from a short
      reference audio sample (no network, no per-character cost).
    - "resona": search Resona's hosted voice catalog over the API.
    Either way, on accept `self.selected_voice` is {"voice_id": ..., "name": ...}.
    """

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.selected_voice = None
        self._thread = None

        self.setWindowTitle("Chọn giọng nói")
        self.resize(520, 480)

        self._demo_player = QMediaPlayer()
        self._demo_audio_output = QAudioOutput()
        self._demo_player.setAudioOutput(self._demo_audio_output)
        self._demo_thread = None

        self.layout_root = QVBoxLayout(self)
        if config.tts_backend == "vieneu":
            self._build_vieneu_ui()
        else:
            self._build_resona_ui()

    # -- VieNeu (local, free) -----------------------------------------------------

    def _build_vieneu_ui(self):
        title = QLabel("Chọn giọng nói (VieNeu — chạy local, miễn phí)")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #17a673;")
        self.layout_root.addWidget(title)

        self.loading_bar = QProgressBar()
        self.loading_bar.setRange(0, 0)  # indeterminate — clearly "still working", not frozen
        self.loading_bar.setTextVisible(False)
        self.loading_bar.setFixedHeight(4)
        self.layout_root.addWidget(self.loading_bar)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.accept)
        self.layout_root.addWidget(self.list_widget)

        clone_row = QHBoxLayout()
        clone_button = QPushButton("Thêm giọng của bạn (nhân bản từ file mẫu 3-8s)...")
        clone_button.clicked.connect(self._clone_new_voice)
        remove_button = QPushButton("Xóa giọng đã nhân bản")
        remove_button.setProperty("danger", True)
        remove_button.clicked.connect(self._remove_selected_cloned_voice)
        clone_row.addWidget(clone_button)
        clone_row.addWidget(remove_button)
        self.layout_root.addLayout(clone_row)

        self.status_label = QLabel("")
        self.layout_root.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("accent", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.layout_root.addWidget(buttons)

        self._reload_vieneu_voices()

    def _reload_vieneu_voices(self, select_name=None):
        self.list_widget.clear()
        self.loading_bar.setVisible(True)
        self.status_label.setText("Đang tải model giọng nói — lần đầu có thể mất khoảng 10-20 giây, các lần sau sẽ nhanh hơn nhiều...")
        self._pending_select_name = select_name
        self._voices_thread = _ListVieneuVoicesThread(self.config)
        self._voices_thread.done.connect(self._on_vieneu_voices_loaded)
        self._voices_thread.start()

    def _on_vieneu_voices_loaded(self, presets, cloned, error):
        select_name = self._pending_select_name
        self.list_widget.clear()
        self.loading_bar.setVisible(False)
        if error:
            self.status_label.setText(f"Lỗi tải danh sách giọng: {error}")
            return

        def add_row(name, subtitle, is_cloned):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, is_cloned)
            item.setSizeHint(QSize(0, 52))
            self.list_widget.addItem(item)
            row = VoiceRowWidget(name, subtitle, on_play=lambda: self._play_vieneu_demo(name))
            self.list_widget.setItemWidget(item, row)
            if select_name == name:
                self.list_widget.setCurrentItem(item)

        for name in cloned:
            add_row(name, "Giọng đã nhân bản", True)

        for label, name in presets:
            if name in cloned:
                continue  # already listed above as a cloned voice
            subtitle = label.split(" — ", 1)[1] if " — " in label else "Giọng có sẵn"
            add_row(name, subtitle, False)

        self.status_label.setText(f"{len(cloned)} giọng đã nhân bản, {len(presets)} giọng có sẵn")

    def _clone_new_voice(self):
        ref_audio_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file audio mẫu (3-8 giây, giọng rõ, ít tạp âm)", "",
            "Audio (*.wav *.mp3 *.m4a *.flac *.ogg)",
        )
        if not ref_audio_path:
            return
        name, ok = QInputDialog.getText(self, "Tên giọng mới", "Đặt tên cho giọng này:")
        if not ok or not name.strip():
            return
        name = name.strip()

        self.status_label.setText(f"Đang nhân bản giọng \"{name}\"...")
        self._thread = _CloneVoiceThread(self.config, name, ref_audio_path)
        self._thread.done.connect(self._on_cloned)
        self._thread.start()

    def _on_cloned(self, name, error):
        if error:
            self.status_label.setText(f"Lỗi nhân bản giọng: {error}")
            return
        self._reload_vieneu_voices(select_name=name)

    def _play_vieneu_demo(self, voice_name):
        safe_name = re.sub(r"[^\w\- ]", "_", voice_name)
        cache_dir = os.path.join(self.config.storage_dir, "voice_demo_cache")
        os.makedirs(cache_dir, exist_ok=True)
        dest_path = os.path.join(cache_dir, f"{safe_name}.wav")

        if os.path.exists(dest_path):
            self._play_file(dest_path)
            return

        self.status_label.setText(f"Đang tạo demo cho giọng \"{voice_name}\"...")
        self._demo_thread = _SynthesizeDemoThread(self.config, voice_name, dest_path)
        self._demo_thread.done.connect(self._on_demo_synthesized)
        self._demo_thread.start()

    def _on_demo_synthesized(self, wav_path, error):
        if error:
            self.status_label.setText(f"Lỗi tạo demo: {error}")
            return
        self.status_label.setText("")
        self._play_file(wav_path)

    def _play_file(self, path):
        self._demo_player.setSource(QUrl.fromLocalFile(path))
        self._demo_player.play()

    def _play_url(self, url):
        self._demo_player.setSource(QUrl(url))
        self._demo_player.play()

    def _remove_selected_cloned_voice(self):
        item = self.list_widget.currentItem()
        if not item or not item.data(Qt.ItemDataRole.UserRole + 1):
            QMessageBox.information(self, "Chưa chọn", "Chọn một giọng đã nhân bản để xóa (không xóa được giọng có sẵn)")
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        confirm = QMessageBox.question(
            self, "Xóa giọng", f"Xóa giọng \"{name}\" đã nhân bản?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        local_tts_service.remove_cloned_voice(self.config, name)
        self._reload_vieneu_voices()

    # -- Resona (cloud API) -----------------------------------------------------

    def _build_resona_ui(self):
        title = QLabel("Chọn giọng nói (Resona)")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #17a673;")
        self.layout_root.addWidget(title)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Tìm theo tên, giọng hoặc thẻ...")
        self.search_edit.returnPressed.connect(self.fetch_voices)
        self.gender_combo = QComboBox()
        self.gender_combo.addItem("Tất cả giới tính", "")
        self.gender_combo.addItem("Nữ", "female")
        self.gender_combo.addItem("Nam", "male")
        self.gender_combo.addItem("Trung tính", "neutral")
        self.category_combo = QComboBox()
        self.category_combo.addItem("Tất cả loại", "")
        self.category_combo.addItem("Công khai", "public")
        self.category_combo.addItem("Giọng của tôi", "cloned")
        search_button = QPushButton("Tìm")
        search_button.setProperty("accent", True)
        search_button.clicked.connect(self.fetch_voices)
        filters.addWidget(self.search_edit)
        filters.addWidget(self.gender_combo)
        filters.addWidget(self.category_combo)
        filters.addWidget(search_button)
        self.layout_root.addLayout(filters)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.accept)
        self.layout_root.addWidget(self.list_widget)

        demo_row = QHBoxLayout()
        demo_button = QPushButton("Nghe demo giọng đang chọn")
        demo_button.clicked.connect(self._play_resona_demo)
        demo_row.addWidget(demo_button)
        demo_row.addStretch(1)
        self.layout_root.addLayout(demo_row)

        self.status_label = QLabel("")
        self.layout_root.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("accent", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.layout_root.addWidget(buttons)

        self.fetch_voices()

    def fetch_voices(self):
        if not self.config.resona_api_key:
            self.status_label.setText("Chưa cấu hình RESONA_API_KEY trong Cài đặt")
            return

        params = {"page_size": 50}
        if self.search_edit.text().strip():
            params["search"] = self.search_edit.text().strip()
        if self.gender_combo.currentData():
            params["gender"] = self.gender_combo.currentData()
        if self.category_combo.currentData():
            params["category"] = self.category_combo.currentData()

        self.status_label.setText("Đang tải danh sách giọng nói...")
        self.list_widget.clear()
        self._thread = _FetchVoicesThread(self.config, params)
        self._thread.done.connect(self._on_fetched)
        self._thread.start()

    def _on_fetched(self, data, error):
        if error:
            self.status_label.setText(f"Lỗi: {error}")
            return
        voices = data.get("voices", [])
        self.status_label.setText(f"{len(voices)} giọng nói" if voices else "Không tìm thấy giọng nói nào")
        for voice in voices:
            label = f"{voice.get('name')} — {voice.get('gender', '?')}, {voice.get('accent', '?')}"
            if voice.get("tags"):
                label += f" ({', '.join(voice['tags'])})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, voice.get("voice_id"))
            item.setData(Qt.ItemDataRole.UserRole + 2, voice.get("name"))
            item.setData(Qt.ItemDataRole.UserRole + 3, voice.get("preview_url"))
            self.list_widget.addItem(item)

    def _play_resona_demo(self):
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.information(self, "Chưa chọn", "Chọn một giọng để nghe demo")
            return
        preview_url = item.data(Qt.ItemDataRole.UserRole + 3)
        if not preview_url:
            QMessageBox.information(self, "Không có demo", "Giọng này không có file demo từ Resona")
            return
        self._play_url(preview_url)

    # -- shared accept -----------------------------------------------------

    def accept(self):
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.warning(self, "Chưa chọn giọng", "Hãy chọn một giọng nói trong danh sách")
            return
        voice_id = item.data(Qt.ItemDataRole.UserRole)
        name = item.data(Qt.ItemDataRole.UserRole + 2) or voice_id
        self.selected_voice = {"voice_id": voice_id, "name": name}
        super().accept()
