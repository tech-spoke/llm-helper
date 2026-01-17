"""Sample test file for backend verification testing."""


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def test_add():
    """Test add function."""
    assert add(1, 2) == 3
    assert add(0, 0) == 0
    assert add(-1, 1) == 0


def test_add_negative():
    """Test add with negative numbers."""
    assert add(-5, -3) == -8
    assert add(-10, 5) == -5


if __name__ == "__main__":
    test_add()
    test_add_negative()
    print("All tests passed!")
