from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_alive: set = set()


class _Signals(QObject):
    ok = Signal(object)
    err = Signal(object)
    progress = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn, with_progress: bool):
        super().__init__()
        self.fn = fn
        self.with_progress = with_progress
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn(self.signals.progress.emit) if self.with_progress else self.fn()
        except BaseException as exc:  # noqa: BLE001
            self.signals.err.emit(exc)
        else:
            self.signals.ok.emit(result)


def run_async(fn, on_ok=None, on_err=None, on_progress=None) -> _Task:
    task = _Task(fn, on_progress is not None)
    _alive.add(task)

    def finish(handler):
        def inner(value):
            _alive.discard(task)
            if handler:
                handler(value)
        return inner

    task.signals.ok.connect(finish(on_ok))
    task.signals.err.connect(finish(on_err))
    if on_progress:
        task.signals.progress.connect(on_progress)
    QThreadPool.globalInstance().start(task)
    return task
