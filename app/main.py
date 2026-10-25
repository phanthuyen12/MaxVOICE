import os
import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from . import db
from .config import load_config
from .pipeline.worker import PipelineWorker
from .services import local_tts_service
from .ui.main_window import MainWindow
from .ui.theme import STYLESHEET

LOGO_PATH = os.path.join(os.path.dirname(__file__), "resources", "logo.png")


def _prewarm_vieneu(worker, config):
    """Loads the VieNeu model on the worker thread as soon as the app opens,
    so by the time the user opens the voice picker it's already warm instead
    of making them wait ~10s with no feedback the first time.
    """
    if config.tts_backend != "vieneu":
        return
    local_tts_service.list_preset_voices()


def main():
    config = load_config()
    db.connect(config.db_path)
    local_tts_service.init(config)

    app = QApplication(sys.argv)
    app.setApplicationName("MaxVoice")
    app.setStyleSheet(STYLESHEET)
    if os.path.exists(LOGO_PATH):
        app.setWindowIcon(QIcon(LOGO_PATH))

    worker = PipelineWorker()
    worker.start()
    worker.submit(_prewarm_vieneu, config)

    window = MainWindow(config, worker)
    window.show()

    exit_code = app.exec()
    worker.stop()
    worker.wait(5000)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
