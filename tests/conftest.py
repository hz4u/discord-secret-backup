import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from secret.core import crypto_core  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "real_kdf: use production scrypt parameters")


@pytest.fixture(autouse=True)
def fast_kdf(request, monkeypatch):
    if "real_kdf" not in request.keywords:
        monkeypatch.setattr(crypto_core, "KDF_N", 2**10)


@pytest.fixture(autouse=True)
def usb_drive(monkeypatch):
    from secret.core import drive

    monkeypatch.setattr("secret.ui.session.detect_drive", lambda root: drive.USB)
