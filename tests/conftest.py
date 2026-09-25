"""Общее для тестов."""

import pytest


@pytest.fixture(scope="session")
def qt_app():
    """Одно приложение Qt на все тесты (без экрана на Linux — offscreen)."""
    import os
    import sys
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
