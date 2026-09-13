"""Shared test configuration.

Real datasets live outside the repository. Set ETALUMA_SAMPLES to a folder
containing the Etaluma experiment folders; the default is
``../../private/Sample set`` relative to this tests folder. Tests that need
real data must use the ``samples`` fixture and skip when it is None.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
V04 = HERE.parent
SOURCE = V04 / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

DEFAULT_SAMPLES = V04.parent / "private" / "Sample set"


def samples_dir() -> Path | None:
    p = Path(os.environ.get("ETALUMA_SAMPLES", str(DEFAULT_SAMPLES)))
    return p if p.is_dir() else None


@pytest.fixture(scope="session")
def samples() -> Path | None:
    return samples_dir()


def require_samples(samples: Path | None, *names: str) -> Path:
    """Skip the calling test unless the sample root and every named experiment exist."""
    if samples is None:
        pytest.skip("real Etaluma samples not available (set ETALUMA_SAMPLES)")
    for n in names:
        if not (samples / n).is_dir():
            pytest.skip(f"sample {n} not available")
    return samples


@pytest.fixture(scope="session")
def synthetic_dataset(tmp_path_factory):
    """A small synthetic Etaluma-shaped experiment (see tests/fixtures/make_synthetic_dataset.py)."""
    from tests.fixtures.make_synthetic_dataset import make_dataset  # noqa: WPS433

    root = tmp_path_factory.mktemp("synthetic")
    return make_dataset(root / "20260101_120000_synthetic")


@pytest.fixture(autouse=True, scope="session")
def _isolated_user_data(tmp_path_factory):
    """Keep settings, histogram cache and logs of every test out of the real user folder."""
    if not os.environ.get("ETALUMA_DATA_DIR"):
        os.environ["ETALUMA_DATA_DIR"] = str(tmp_path_factory.mktemp("userdata"))
    yield
