import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def pytest_configure(config):
    config.addinivalue_line("markers", "live: tests that hit live websites")
    config.addinivalue_line("markers", "nightly: nightly regression tests")
