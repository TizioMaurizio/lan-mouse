import os

# Desktop tests use Qt's isolated in-memory clipboard, never the user's desktop.
os.environ["QT_QPA_PLATFORM"] = "offscreen"


import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qt():
    application = QApplication.instance() or QApplication([])
    yield application
    # Qt's offscreen clipboard owns the last QMimeData. Release it while Python
    # is still alive, before Qt/Python tear down their QObject wrappers.
    application.clipboard().clear()
    application.processEvents()
