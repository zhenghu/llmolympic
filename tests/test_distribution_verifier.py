"""Direct wheel/sdist membership checks for release-critical runtime files."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import verify_distribution_license as verifier


def _metadata() -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        "Name: llmolympic\n"
        f"Version: {verifier.VERSION}\n"
        "License-Expression: MIT\n"
        "License-File: LICENSE\n"
        "License-File: THIRD_PARTY_NOTICES.md\n"
        "\n"
    ).encode()


def _tar_member(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    archive.addfile(member, io.BytesIO(payload))


def _write_distributions(
    dist_dir: Path,
    *,
    wheel_human_input: bool = True,
    sdist_human_input: bool = True,
) -> None:
    root = verifier.PROJECT_ROOT
    version = verifier.VERSION
    metadata = _metadata()
    wheel = dist_dir / f"llmolympic-{version}-py3-none-any.whl"
    dist_info = f"llmolympic-{version}.dist-info"

    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        for filename in verifier.LEGAL_FILES:
            archive.writestr(f"{dist_info}/licenses/{filename}", (root / filename).read_bytes())
        for filename in verifier.WEB_ASSET_PATHS:
            archive.writestr(filename, (root / filename).read_bytes())
        if wheel_human_input:
            for filename in verifier.RUNTIME_PACKAGE_PATHS:
                archive.writestr(filename, (root / filename).read_bytes())

    sdist = dist_dir / f"llmolympic-{version}.tar.gz"
    sdist_root = f"llmolympic-{version}"
    with tarfile.open(sdist, mode="w:gz") as archive:
        _tar_member(archive, f"{sdist_root}/PKG-INFO", metadata)
        for filename in verifier.LEGAL_FILES:
            _tar_member(archive, f"{sdist_root}/{filename}", (root / filename).read_bytes())
        for filename in verifier.WEB_ASSET_PATHS:
            _tar_member(archive, f"{sdist_root}/{filename}", (root / filename).read_bytes())
        for filename in verifier.WEB_BUILD_INPUT_PATHS:
            _tar_member(archive, f"{sdist_root}/{filename}", (root / filename).read_bytes())
        if sdist_human_input:
            for filename in verifier.RUNTIME_PACKAGE_PATHS:
                _tar_member(archive, f"{sdist_root}/{filename}", (root / filename).read_bytes())


def test_distribution_verifier_accepts_human_input_in_wheel_and_sdist(tmp_path: Path) -> None:
    _write_distributions(tmp_path)

    verifier.verify_distributions(tmp_path)


@pytest.mark.parametrize(
    ("wheel_human_input", "sdist_human_input", "message"),
    [
        (False, True, "wheel runtime package file is missing"),
        (True, False, "sdist runtime package file llmolympic/human_input.py"),
    ],
)
def test_distribution_verifier_rejects_missing_human_input(
    tmp_path: Path,
    wheel_human_input: bool,
    sdist_human_input: bool,
    message: str,
) -> None:
    _write_distributions(
        tmp_path,
        wheel_human_input=wheel_human_input,
        sdist_human_input=sdist_human_input,
    )

    with pytest.raises(AssertionError, match=message):
        verifier.verify_distributions(tmp_path)
