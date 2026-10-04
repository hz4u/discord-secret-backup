from secret.core.clipboard_guard import ClipboardGuard


class FakeClipboard:
    def __init__(self):
        self.value = None
        self.cleared = 0

    def get(self):
        return self.value

    def set(self, text):
        self.value = text

    def clear(self):
        self.value = ""
        self.cleared += 1


def make(seconds=3):
    cb = FakeClipboard()
    return cb, ClipboardGuard(cb.get, cb.set, cb.clear, seconds=seconds)


def test_clears_after_countdown():
    cb, guard = make()
    guard.copy("secret")
    assert cb.value == "secret"
    assert guard.remaining == 3
    assert [guard.tick(), guard.tick()] == [2, 1]
    assert cb.cleared == 0
    assert guard.tick() == 0
    assert cb.cleared == 1
    assert cb.value == ""


def test_does_not_clear_user_content():
    cb, guard = make()
    guard.copy("secret")
    cb.value = "user copied something else"
    for _ in range(3):
        guard.tick()
    assert cb.cleared == 0
    assert cb.value == "user copied something else"


def test_recopy_resets_timer():
    cb, guard = make()
    guard.copy("a")
    guard.tick()
    guard.tick()
    guard.copy("b")
    assert guard.remaining == 3
    guard.tick()
    assert cb.cleared == 0


def test_tick_when_inactive_is_noop():
    cb, guard = make()
    assert guard.tick() == 0
    assert cb.cleared == 0


def test_expire_immediately_on_close():
    cb, guard = make()
    guard.copy("secret")
    guard.expire()
    assert cb.cleared == 1
    assert guard.remaining == 0
    guard.expire()
    assert cb.cleared == 1


def test_clipboard_read_error_is_ignored():
    cb, guard = make(seconds=1)

    def broken():
        raise RuntimeError("clipboard locked")

    guard._get_text = broken
    guard.copy("secret")
    guard.tick()
    assert guard.remaining == 0
