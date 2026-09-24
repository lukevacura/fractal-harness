"""Hidden acceptance tests for the ledger benchmark tasks.

Black-box: they run the CLI the task text specifies (`python -m ledger.cli ...`) in the
repo under test (the current working directory), so they judge any implementation, planned
or not, without depending on internal function names.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

DATA = """date,amount,currency,category,description
2026-01-03,-120.00,USD,food,groceries
2026-01-09,-10.00,EUR,food,cafe in paris
2026-01-15,-900.00,USD,rent,
2026-01-31,3000.00,USD,salary,
2026-02-02,-50.00,USD,food,
2026-02-10,-22.00,EUR,travel,train
"""


@pytest.fixture
def repo() -> Path:
    return Path(os.getcwd())


@pytest.fixture
def ledger(repo, tmp_path):
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "ledger.cli", *args], cwd=repo, capture_output=True,
                              text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(repo)})
    return run


@pytest.fixture
def data_csv(tmp_path) -> Path:
    p = tmp_path / "data.csv"
    p.write_text(DATA)
    return p
