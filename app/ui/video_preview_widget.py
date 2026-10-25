from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)


def _format_ms(ms):
    total_seconds = max(0, ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


class VideoPreviewWidget(QWidget):
    """Real in-app playback (QMediaPlayer + QVideoWidget) — the "canvas" the
    user can watch a clip or the final dubbed video run, instead of only
    opening it in an external player.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.now_playing_label = QLabel("Chưa chọn video")
        self.now_playing_label.setStyleSheet("color: #a0a0a0; font-size: 12px;")
        layout.addWidget(self.now_playing_label)

        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(280)
        self.video_widget.setStyleSheet("background-color: black;")

        # Caption bubble burned over the video, like the reference design —
        # a QStackedLayout in StackAll mode draws every widget it holds on
        # top of each other at the same geometry, which is the simplest way
        # to overlay a label on a QVideoWidget without a custom paint path.
        overlay_container = QWidget()
        stack = QStackedLayout(overlay_container)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        stack.addWidget(self.video_widget)

        caption_layer = QWidget()
        caption_layer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        caption_layout = QVBoxLayout(caption_layer)
        caption_layout.addStretch(1)
        caption_row = QHBoxLayout()
        self.caption_label = QLabel("")
        self.caption_label.setWordWrap(True)
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 170); color: white; padding: 6px 16px;"
            "border-radius: 8px; font-size: 13px;"
        )
        self.caption_label.setVisible(False)
        caption_row.addStretch(1)
        caption_row.addWidget(self.caption_label)
        caption_row.addStretch(1)
        caption_layout.addLayout(caption_row)
        caption_layout.addSpacing(18)
        stack.addWidget(caption_layer)

        layout.addWidget(overlay_container, stretch=1)
        self._cues = []

        controls = QHBoxLayout()
        self.play_button = QPushButton("Phát")
        self.play_button.setMinimumWidth(90)
        self.play_button.clicked.connect(self._toggle_play)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.sliderMoved.connect(self._seek)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        controls.addWidget(self.play_button)
        controls.addWidget(self.position_slider, stretch=1)
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)

    def load(self, file_path, label=None):
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(file_path))
        self.now_playing_label.setText(label or file_path)
        self.clear_cues()

    def play(self):
        self.player.play()

    def clear(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.now_playing_label.setText("Chưa chọn video")
        self.clear_cues()

    def set_cues(self, cues):
        """cues: [{"start": seconds, "end": seconds, "text": str}, ...],
        relative to this video's own timeline. Shown as a caption bubble
        over the preview while playback position falls inside a cue.
        """
        self._cues = sorted(cues, key=lambda c: c["start"])

    def clear_cues(self):
        self._cues = []
        self.caption_label.setVisible(False)

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_playback_state_changed(self, state):
        self.play_button.setText("Tạm dừng" if state == QMediaPlayer.PlaybackState.PlayingState else "Phát")

    def _on_position_changed(self, position_ms):
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position_ms)
        self.time_label.setText(f"{_format_ms(position_ms)} / {_format_ms(self.player.duration())}")
        self._update_caption(position_ms / 1000)

    def _update_caption(self, position_sec):
        if not self._cues:
            return
        cue = next((c for c in self._cues if c["start"] <= position_sec <= c["end"]), None)
        if cue:
            self.caption_label.setText(cue["text"])
            self.caption_label.setVisible(True)
        else:
            self.caption_label.setVisible(False)

    def _on_duration_changed(self, duration_ms):
        self.position_slider.setRange(0, duration_ms)

    def _seek(self, position_ms):
        self.player.setPosition(position_ms)
