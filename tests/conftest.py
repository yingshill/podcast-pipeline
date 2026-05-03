"""Add project root to sys.path so tests can import pipeline modules directly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def pytest_addoption(parser):
    parser.addoption("--url", action="store", default=None, help="Podcast URL for integration tests")
