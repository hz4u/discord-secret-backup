import argparse
import os
import sys
from pathlib import Path


def frozen_root(exe: Path, system_drive: str) -> Path:
    if exe.drive.upper() == system_drive.upper():
        return exe.parent
    return Path(exe.anchor)


def resolve_root(argv: list[str]) -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root")
    args, _ = parser.parse_known_args(argv)
    if args.root:
        return Path(args.root).resolve()
    if os.environ.get("SECRET_ROOT"):
        return Path(os.environ["SECRET_ROOT"]).resolve()
    if getattr(sys, "frozen", False):
        return frozen_root(Path(sys.executable).resolve(), os.environ.get("SystemDrive", "C:"))
    root = Path(__file__).resolve().parent.parent / "dev_usb"
    root.mkdir(exist_ok=True)
    return root


def _install_qt_translation(app, lang: str) -> None:
    from PySide6.QtCore import QLibraryInfo, QTranslator

    translator = QTranslator(app)
    if translator.load(f"qtbase_{'zh_CN' if lang == 'zh' else lang}", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)


def main() -> int:
    from . import i18n
    from .core import prefs

    root = resolve_root(sys.argv[1:])
    i18n.set_language(prefs.load(root).get("language") or i18n.system_language())

    from PySide6.QtWidgets import QApplication

    from .ui import theme
    from .ui.main_window import MainWindow
    from .ui.session import Session

    app = QApplication(sys.argv)
    app.setApplicationName("Secret")
    _install_qt_translation(app, i18n.current_language())
    theme.install(app)
    app.setWindowIcon(theme.app_icon())
    window = MainWindow(Session(root))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
