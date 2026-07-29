from __future__ import annotations

import json
from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
PACKAGE_JSON = Path(__file__).resolve().parents[3] / "package.json"


def test_worker_runtime_excludes_the_vulnerable_perl_runtime() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert content.startswith("FROM python:3.12.13-alpine3.24\n")
    assert "perl" not in content.lower()
    assert "USER 10001:10001" in content


def test_worker_runtime_excludes_the_python_package_installer() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG PIP_VERSION=26.1.2" in content
    assert 'pip==${PIP_VERSION}' in content
    assert "python -m pip uninstall --yes pip" in content
    assert "rm -rf /usr/local/lib/python3.12/ensurepip" in content
    assert 'importlib.util.find_spec("pip") is None' in content


def test_worker_prepares_private_writable_scratch_for_the_non_root_user() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "install -d -o 10001 -g 10001 -m 0700 /scratch" in content


def test_worker_build_emits_one_scannable_arm64_manifest() -> None:
    scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]
    command = scripts["worker:build"]
    assert "--platform linux/arm64" in command
    assert "--provenance=false" in command
    assert "--load" in command
