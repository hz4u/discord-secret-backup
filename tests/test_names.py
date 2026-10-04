import pytest

from secret.core.names import validate_name


@pytest.mark.parametrize("name", ["여행", "사진 2025", "a.b.c", "제주도_바다.jpg"])
def test_valid(name):
    assert validate_name(name) is None


@pytest.mark.parametrize(
    "name, fragment",
    [
        ("", "비어"),
        ("   ", "비어"),
        ("a/b", "쓸 수 없는 문자"),
        ('a"b', "쓸 수 없는 문자"),
        ("a?", "쓸 수 없는 문자"),
        ("끝에점.", "점이나 공백"),
        ("끝에공백 ", "점이나 공백"),
        ("CON", "예약된 이름"),
        ("com1.txt", "예약된 이름"),
        ("..", "쓸 수 없는"),
        ("x" * 256, "너무 깁니다"),
    ],
)
def test_invalid(name, fragment):
    assert fragment in validate_name(name)
