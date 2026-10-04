import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_set_get_clear():
    from secret.core import win_clipboard

    win_clipboard.set_text("테스트 secret 123")
    assert win_clipboard.get_text() == "테스트 secret 123"
    win_clipboard.clear()
    assert win_clipboard.get_text() is None


def test_history_exclusion_formats_are_set():
    from secret.core import win_clipboard

    win_clipboard.set_text("x")
    try:
        for name in win_clipboard.PRIVACY_FORMATS:
            assert win_clipboard.has_format(name), name
    finally:
        win_clipboard.clear()
