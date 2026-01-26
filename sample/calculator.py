"""Simple calculator module for testing ctags cache."""


class Calculator:
    """A simple calculator class."""

    def __init__(self):
        self.result = 0

    def add(self, a, b):
        """Add two numbers."""
        self.result = a + b
        return self.result

    def subtract(self, a, b):
        """Subtract b from a."""
        self.result = a - b
        return self.result

    def multiply(self, a, b):
        """Multiply two numbers."""
        self.result = a * b
        return self.result

    def divide(self, a, b):
        """Divide a by b."""
        if b == 0:
            raise ValueError("Cannot divide by zero")
        self.result = a / b
        return self.result

    def power(self, a, b):
        """Return a raised to the power of b."""
        self.result = a ** b
        return self.result


def calculate_sum(numbers):
    """Calculate sum of a list of numbers."""
    return sum(numbers)


def calculate_average(numbers):
    """Calculate average of a list of numbers."""
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)


def main():
    """Main function for testing."""
    calc = Calculator()
    print(f"5 + 3 = {calc.add(5, 3)}")
    print(f"10 - 4 = {calc.subtract(10, 4)}")
    print(f"3 * 7 = {calc.multiply(3, 7)}")
    print(f"10 / 2 = {calc.divide(10, 2)}")
    print(f"Sum of [1,2,3,4,5] = {calculate_sum([1, 2, 3, 4, 5])}")
    print(f"Average of [1,2,3,4,5] = {calculate_average([1, 2, 3, 4, 5])}")


if __name__ == "__main__":
    main()

# Test: キャッシュ更新確認用のコメント

# キャッシュ更新テスト: ファイル変更を検知するか？
# 追加テスト: 並列編集のテスト (2026-01-23)
# Parallel Execution Test: 2026-01-23 22:21 JST
