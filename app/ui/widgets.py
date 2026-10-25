from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .status_meta import JOB_TYPE_LABEL, derive_overall_status, status_meta

JOB_TYPE_TAG = {"video": "VIDEO", "audio": "AUDIO"}

AVATAR_PALETTE = ["#ffc107", "#ff8a65", "#64b5f6", "#81c784", "#ba68c8", "#4db6ac", "#f06292"]


def avatar_color_for(name):
    return AVATAR_PALETTE[sum(ord(c) for c in (name or "?")) % len(AVATAR_PALETTE)]


class AvatarLabel(QLabel):
    """A small circular initial-letter avatar, like the voice-card avatars
    in the reference design (colored circle + first letter of the name).
    """

    def __init__(self, name, size=36, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        initial = (name or "?").strip()[:1].upper() or "?"
        color = avatar_color_for(name)
        self.setText(initial)
        self.setStyleSheet(
            f"background-color: {color}; color: #1a1400; border-radius: {size // 2}px;"
            f"font-weight: 700; font-size: {int(size * 0.42)}px;"
        )


class StepBreadcrumbWidget(QWidget):
    """The "① Nhập video — ② Chỉnh lời & giọng — ③ Xem trước & xuất" progress
    strip from the reference design. Purely a visual progress indicator —
    clicking a step doesn't navigate (the page below stays one continuous
    view), it just shows the user where the project currently stands.
    """

    def __init__(self, steps, parent=None):
        super().__init__(parent)
        self._labels = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for i, text in enumerate(steps):
            dot = QLabel(str(i + 1))
            dot.setFixedSize(22, 22)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label = QLabel(text)
            label.setStyleSheet("color: #a0a0a0; font-size: 12px;")
            self._labels.append((dot, label))
            layout.addWidget(dot)
            layout.addWidget(label)
            if i < len(steps) - 1:
                sep = QLabel("────")
                sep.setStyleSheet("color: #333333;")
                layout.addWidget(sep)
        layout.addStretch(1)
        self.set_active_step(0)

    def set_active_step(self, active_index):
        for i, (dot, label) in enumerate(self._labels):
            if i < active_index:
                dot.setStyleSheet("background-color: #198754; color: white; border-radius: 11px; font-weight: 700; font-size: 11px;")
                label.setStyleSheet("color: #198754; font-size: 12px; font-weight: 600;")
            elif i == active_index:
                dot.setStyleSheet("background-color: #17a673; color: #1a1400; border-radius: 11px; font-weight: 700; font-size: 11px;")
                label.setStyleSheet("color: #17a673; font-size: 12px; font-weight: 600;")
            else:
                dot.setStyleSheet("background-color: #2a2a2a; color: #a0a0a0; border-radius: 11px; font-weight: 700; font-size: 11px;")
                label.setStyleSheet("color: #a0a0a0; font-size: 12px;")


class StatusBadge(QLabel):
    def __init__(self, status=None, kind="segment", parent=None):
        super().__init__(parent)
        self.setMinimumWidth(140)
        self._last = None
        self.set_status(status, kind)

    def set_status(self, status, kind="segment"):
        key = (status, kind)
        if key == self._last:
            return  # skip redundant setStyleSheet/layout work — called on every poll tick
        self._last = key
        meta = status_meta(status, kind)
        self.setText(meta["label"])
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"background-color: {meta['color']}; color: white; border-radius: 10px;"
            "padding: 3px 10px; font-weight: 600; font-size: 12px;"
        )


def _job_list_status(job, segments):
    if job["type"] == "video":
        return derive_overall_status(segments), "job"
    status = segments[0]["status"] if segments else job.get("stage", "")
    return status, "segment"


class JobListItemWidget(QWidget):
    """One colorful row in the job list: type icon, filename, status pill.

    Reusable via update_data() so the job list can refresh in place instead
    of tearing down and rebuilding every row on every poll tick.
    """

    def __init__(self, job, segments, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self.icon_label = QLabel()
        self.icon_label.setFixedWidth(46)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet(
            "font-size: 10px; font-weight: 700; color: #a3b0a9;"
            "border: 1px solid #2b3832; border-radius: 4px; padding: 2px 4px;"
        )
        layout.addWidget(self.icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: 600;")
        self.title_label.setWordWrap(True)
        self.subtitle_label = QLabel()
        self.subtitle_label.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.subtitle_label)
        layout.addLayout(text_col, 1)

        self.badge = StatusBadge()
        self.badge.setMinimumWidth(0)
        layout.addWidget(self.badge)

        self._job_id = None
        self.update_data(job, segments)

    def update_data(self, job, segments):
        if job["id"] != self._job_id:
            self._job_id = job["id"]
            self.icon_label.setText(JOB_TYPE_TAG.get(job["type"], "JOB"))
            self.title_label.setText(job.get("originalFilename") or job["id"])
            self.subtitle_label.setText(JOB_TYPE_LABEL.get(job["type"], job["type"]))
        status, kind = _job_list_status(job, segments)
        self.badge.set_status(status, kind)
