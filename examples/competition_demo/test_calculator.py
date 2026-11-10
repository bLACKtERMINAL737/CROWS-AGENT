from calculator import add


def test_add_positive_numbers():
    assert add(2, 3) == 5


def test_add_with_negative_number():
    assert add(-4, 1) == -3
