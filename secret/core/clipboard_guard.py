from typing import Callable


class ClipboardGuard:
    def __init__(
        self,
        get_text: Callable[[], str | None],
        set_text: Callable[[str], None],
        clear: Callable[[], None],
        seconds: int = 30,
    ):
        self._get_text = get_text
        self._set_text = set_text
        self._clear = clear
        self.seconds = seconds
        self.remaining = 0
        self._last: str | None = None

    def copy(self, text: str) -> None:
        self._set_text(text)
        self._last = text
        self.remaining = self.seconds

    def tick(self) -> int:
        if self.remaining <= 0:
            return 0
        self.remaining -= 1
        if self.remaining == 0:
            self.expire()
        return self.remaining

    def expire(self) -> None:
        last, self._last, self.remaining = self._last, None, 0
        if last is None:
            return
        try:
            if self._get_text() == last:
                self._clear()
        except Exception:
            pass
