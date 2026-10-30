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
        play_button.clicked.connect(lambda _checked=False: on_play())
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
        cloned = local_tts_service.load_cloned_voices(self.config)
        try:
            # First call in the process loads the ONNX model (can take a
            # few seconds) — run it here, off the GUI thread, so opening
            # this dialog never freezes the window.
            presets = local_tts_service.list_preset_voices()
            self.done.emit(presets, cloned, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit([], cloned, str(exc))


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

    def __init__(self, config, voice_id, dest_path):
        super().__init__()
        self.config = config
        self.voice_id = voice_id
        self.dest_path = dest_path

    def run(self):
        try:
            demo_text = local_tts_service.demo_text_for_voice(self.voice_id)
            local_tts_service.synthesize_to_file(
                demo_text, self.dest_path, voice=self.voice_id, ffmpeg=self.config.ffmpeg_path
            )
            self.done.emit(self.dest_path, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit("", str(exc))


class VoicePickerDialog(QDialog):
    """Lets the user pick a voice. Branches on config.tts_backend:
    - "vieneu": 25 local Vietnamese preset voices + English/Multilingual voices
      + any voice cloned from a short reference audio sample.
    - "resona": search Resona's hosted voice catalog over the API.
    Either way, on accept `self.selected_voice` is {"voice_id": ..., "name": ..., "lang": ...}.
    """

    def __init__(self, config, parent=None, preferred_lang=None):
        super().__init__(parent)
        self.config = config
        self.preferred_lang = preferred_lang
        self.selected_voice = None
        self._thread = None
        self._presets = []
        self._cloned = {}

        self.setWindowTitle("Chọn giọng nói (Tiếng Việt / Tiếng Anh / Đa ngôn ngữ)")
        self.resize(580, 540)

        self._demo_player = QMediaPlayer()
        self._demo_audio_output = QAudioOutput()
        self._demo_player.setAudioOutput(self._demo_audio_output)
        self._demo_thread = None

        self.layout_root = QVBoxLayout(self)
        if config.tts_backend == "vieneu":
            self._build_vieneu_ui()
        else:
            self._build_resona_ui()

    # -- VieNeu + Multilingual (local, free) ------------------------------------

    def _build_vieneu_ui(self):
        title = QLabel("Chọn giọng nói (Tiếng Việt VieNeu & Tiếng Anh / Đa ngôn ngữ)")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #17a673;")
        self.layout_root.addWidget(title)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Lọc ngôn ngữ:"))
        self.lang_filter_combo = QComboBox()
        self.lang_filter_combo.addItem("Tất cả giọng (Việt + Anh + Trung + Hàn + Nhật + Nga...)", "all")
        self.lang_filter_combo.addItem("🇻🇳 Tiếng Việt (VieNeu Offline + Việt Neural Nam/Nữ)", "vi")
        self.lang_filter_combo.addItem("🇺🇸🇬🇧 Tiếng Anh (English US / UK — 8 giọng Nam & Nữ)", "en")
        self.lang_filter_combo.addItem("🇨🇳🇹🇼 Tiếng Trung (Chinese — 6 giọng Nam & Nữ)", "zh")
        self.lang_filter_combo.addItem("🇰🇷 Tiếng Hàn (Korean — 3 giọng Nam & Nữ)", "ko")
        self.lang_filter_combo.addItem("🇯🇵 Tiếng Nhật (Japanese — Nam & Nữ)", "ja")
        self.lang_filter_combo.addItem("🇷🇺 Tiếng Nga (Russian — 12 giọng Nam & Nữ)", "ru")
        self.lang_filter_combo.addItem("🌐 Pháp / Đức / Tây Ban Nha / Thái (Nam & Nữ)", "other")
        if self.preferred_lang:
            idx = self.lang_filter_combo.findData(self.preferred_lang)
            if idx >= 0:
                self.lang_filter_combo.setCurrentIndex(idx)
            elif self.preferred_lang not in ("vi", "original"):
                self.lang_filter_combo.setCurrentIndex(self.lang_filter_combo.findData("other"))
        self.lang_filter_combo.currentIndexChanged.connect(self._populate_voice_list)
        filter_row.addWidget(self.lang_filter_combo, 1)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Tìm tên giọng...")
        self.search_input.textChanged.connect(self._populate_voice_list)
        filter_row.addWidget(self.search_input, 1)
        self.layout_root.addLayout(filter_row)

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
        self._pending_select_name = select_name
        # Immediately show English & Multilingual voices so user doesn't wait if they want English
        self._populate_voice_list()
        self.loading_bar.setVisible(True)
        self.status_label.setText(
            "Đang tải thêm 25 giọng Tiếng Việt VieNeu offline (các giọng Tiếng Anh & Neural đã sẵn sàng)..."
        )
        self._voices_thread = _ListVieneuVoicesThread(self.config)
        self._voices_thread.done.connect(self._on_vieneu_voices_loaded)
        self._voices_thread.start()

    def _on_vieneu_voices_loaded(self, presets, cloned, error):
        self.loading_bar.setVisible(False)
        self._presets = presets or []
        self._cloned = cloned or {}
        self._populate_voice_list(select_id=self._pending_select_name)
        if error:
            self.status_label.setText(
                f"Đang dùng bộ giọng Đa ngôn ngữ/Neural ({len(local_tts_service.list_multilingual_voices())} giọng). "
                f"Lưu ý VieNeu local: {error}"
            )
        else:
            self.status_label.setText(
                f"{len(self._cloned)} giọng nhân bản • {len(self._presets)} giọng Việt VieNeu • "
                f"{len(local_tts_service.list_multilingual_voices())} giọng Tiếng Anh & Đa ngôn ngữ"
            )

    def _populate_voice_list(self, select_id=None):
        if not isinstance(select_id, str):
            select_id = None
        self.list_widget.clear()
        lang_filter = self.lang_filter_combo.currentData() or "all"
        query = (self.search_input.text() or "").strip().lower()

        def add_row(voice_id, display_name, subtitle, is_cloned, lang_code):
            if lang_filter in ("vi", "en", "zh", "ko", "ja", "ru") and lang_code != lang_filter:
                return
            if lang_filter == "other" and lang_code in ("vi", "en", "zh", "ko", "ja", "ru"):
                return
            if query and query not in display_name.lower() and query not in subtitle.lower():
                return

            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, voice_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, is_cloned)
            item.setData(Qt.ItemDataRole.UserRole + 2, display_name)
            item.setData(Qt.ItemDataRole.UserRole + 4, lang_code)
            item.setSizeHint(QSize(0, 54))
            self.list_widget.addItem(item)
            row = VoiceRowWidget(
                display_name,
                subtitle,
                on_play=lambda _checked=False, vid=voice_id, nm=display_name: self._play_vieneu_demo(vid, nm),
            )
            self.list_widget.setItemWidget(item, row)
            if select_id and select_id in (voice_id, display_name):
                self.list_widget.setCurrentItem(item)

        # 1. Cloned voices
        for name in self._cloned:
            add_row(name, name, "🇻🇳 Giọng đã nhân bản (VieNeu Local)", True, "vi")

        # 2. Built-in English & Multilingual voices
        for v in local_tts_service.list_multilingual_voices():
            add_row(v["id"], v["name"], v["subtitle"], False, v["lang"])

        # 3. 25 VieNeu local Vietnamese preset voices
        for label, name in self._presets:
            if name in self._cloned:
                continue
            subtitle = label.split(" — ", 1)[1] if " — " in label else "Giọng Tiếng Việt có sẵn"
            add_row(name, name, f"🇻🇳 VieNeu Offline • {subtitle}", False, "vi")

        if self.list_widget.count() > 0 and self.list_widget.currentItem() is None:
            self.list_widget.setCurrentRow(0)

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

    def _play_vieneu_demo(self, voice_id, display_name=None):
        safe_name = re.sub(r"[^\w\- ]", "_", str(voice_id))
        cache_dir = os.path.join(self.config.storage_dir, "voice_demo_cache")
        os.makedirs(cache_dir, exist_ok=True)
        dest_path = os.path.join(cache_dir, f"v4_{safe_name}.wav")

        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 256:
            self._play_file(dest_path)
            return

        label = display_name or voice_id
        self.status_label.setText(f"Đang tạo demo cho giọng \"{label}\"...")
        self._demo_thread = _SynthesizeDemoThread(self.config, voice_id, dest_path)
        self._demo_thread.done.connect(self._on_demo_synthesized)
        self._demo_thread.start()

    def _on_demo_synthesized(self, wav_path, error):
        if error:
            self.status_label.setText(f"Lỗi tạo demo: {error}")
            return
        self.status_label.setText("Đang phát giọng mẫu...")
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
        lang = item.data(Qt.ItemDataRole.UserRole + 4) or local_tts_service.voice_language(voice_id)
        self.selected_voice = {"voice_id": voice_id, "name": name, "lang": lang}
        super().accept()
