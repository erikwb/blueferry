"""Python child processes test the checkout, not an installed BlueFerry."""
import subprocess
import sys

import blueferry


def test_child_python_imports_the_checkout_under_test():
    command = [sys.executable, "-c", "import blueferry; print(blueferry.__file__)"]
    child = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
    assert child.stdout.strip() == blueferry.__file__
