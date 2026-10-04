from dataclasses import dataclass

from ..i18n import tr

WEAK, MEDIUM, STRONG = 0, 1, 2
_LABELS = {WEAK: tr("약함"), MEDIUM: tr("보통"), STRONG: tr("강함")}

_COMMON = {
    "password", "password1", "password12", "password123", "password1234",
    "passw0rd", "p@ssw0rd", "p@ssword", "qwerty", "qwerty123", "qwerty1234",
    "qwertyuiop", "qwertyuiop12", "asdfghjkl", "asdfqwer", "asdfqwer1234",
    "1q2w3e4r", "1q2w3e4r!", "1q2w3e4r5t", "1q2w3e4r5t6y", "zxcvbnm",
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "123456789012", "111111111111", "000000000000", "abc123", "abcd1234",
    "abcdefghijkl", "iloveyou", "iloveyou1234", "admin", "admin1234",
    "letmein", "welcome", "welcome1234", "monkey", "dragon", "sunshine",
    "princess", "football", "baseball", "superman", "trustno1",
}


@dataclass(frozen=True)
class Strength:
    level: int
    label: str


def evaluate(password: str) -> Strength:
    if (
        len(password) < 12
        or password.lower() in _COMMON
        or len(set(password)) == 1
    ):
        level = WEAK
    elif len(password) < 16:
        level = MEDIUM
    else:
        level = STRONG
    return Strength(level, _LABELS[level])
