from __future__ import annotations

import csv
import logging
import shutil
from datetime import date
from pathlib import Path

import pytest

from claims_intake.extract import FakeExtractor
from claims_intake.pipeline import Config, Pipeline, discover, read_file
from claims_intake.synth import TODAY, generate
from claims_intake.validate import load_roster


@pytest.fixture(scope="session")
def dataset(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("data")
    generate(root)
    return root


@pytest.fixture
def data(dataset, tmp_path) -> Path:
    """A private copy, so a test can add or change files."""
    dst = tmp_path / "data"
    shutil.copytree(dataset, dst)
    return dst


@pytest.fixture
def roster(dataset):
    return load_roster((dataset / "eligibility.csv").read_text())


def phi_values(data_root: Path) -> list[str]:
    out = []
    for r in csv.DictReader((data_root / "eligibility.csv").open()):
        y, m, d = r["dob"].split("-")
        out += [r["member_id"], r["name"], r["name"].split()[1], r["dob"], f"{m}/{d}/{y}"]
    return out


def run_pipeline(
    data: Path, out: Path, today: date = TODAY, baseline: dict | None = None, logger=None
) -> tuple[dict, Pipeline]:
    cfg = Config(
        out_dir=out, roster=load_roster((data / "eligibility.csv").read_text()), today=today, baseline=baseline
    )
    ex = FakeExtractor(data / "truth")
    p = Pipeline(cfg, "run-test", ex.name, logger or logging.getLogger("claims_intake.test"))
    for path in discover(data / "inbox"):
        for attempt in range(3):  # the flow's task retries, in miniature
            try:
                p.apply(read_file(path, ex))
                break
            except ConnectionError:
                if attempt == 2:
                    raise
    return p.finish(), p
