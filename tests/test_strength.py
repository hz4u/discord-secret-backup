import pytest

from secret.core.strength import MEDIUM, STRONG, WEAK, evaluate


@pytest.mark.parametrize(
    "password, level",
    [
        ("", WEAK),
        ("a1b2c3d4e5f", WEAK),
        ("a1b2c3d4e5f6", MEDIUM),
        ("a1b2c3d4e5f6g7h", MEDIUM),
        ("a1b2c3d4e5f6g7h8", STRONG),
        ("a" * 20, WEAK),
        ("password1234", WEAK),
        ("PASSWORD1234", WEAK),
        ("우리집강아지이름은뽀삐입니다요", MEDIUM),
        ("우리집강아지이름은뽀삐랑초코입니다", STRONG),
    ],
)
def test_evaluate_levels(password, level):
    assert evaluate(password).level == level


def test_labels():
    assert evaluate("").label == "약함"
    assert evaluate("a1b2c3d4e5f6").label == "보통"
    assert evaluate("a1b2c3d4e5f6g7h8").label == "강함"
