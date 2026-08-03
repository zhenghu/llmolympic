"""Verify that built distributions carry the declared MIT license metadata and text."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from llmolympic import __version__

LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")


def _single(paths: list[Path], description: str) -> Path:
    if len(paths) != 1:
        raise AssertionError(f"expected one {description}, found {len(paths)}")
    return paths[0]


def _verify_metadata(metadata: Message) -> None:
    metadata_version = tuple(int(part) for part in metadata["Metadata-Version"].split("."))
    if metadata_version < (2, 4):
        raise AssertionError("license metadata requires Metadata-Version 2.4 or newer")
    if metadata["Version"] != __version__:
        raise AssertionError("distribution metadata version does not match llmolympic.__version__")
    if metadata["License-Expression"] != "MIT":
        raise AssertionError("distribution License-Expression is not MIT")
    if sorted(metadata.get_all("License-File", [])) != sorted(LEGAL_FILES):
        raise AssertionError("distribution License-File metadata is incomplete")


def verify_distributions(dist_dir: Path) -> None:
    expected_legal_files = {name: Path(name).read_bytes() for name in LEGAL_FILES}
    wheel = _single(list(dist_dir.glob("*.whl")), "wheel")
    sdist = _single(list(dist_dir.glob("*.tar.gz")), "sdist")
    if not wheel.match(f"llmolympic-{__version__}-*.whl"):
        raise AssertionError("wheel filename does not match llmolympic.__version__")
    if sdist.name != f"llmolympic-{__version__}.tar.gz":
        raise AssertionError("sdist filename does not match llmolympic.__version__")

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = _single(
            [Path(name) for name in names if name.endswith(".dist-info/METADATA")],
            "wheel METADATA file",
        )
        metadata = BytesParser(policy=default).parsebytes(archive.read(str(metadata_name)))
        _verify_metadata(metadata)
        for filename, expected in expected_legal_files.items():
            legal_name = _single(
                [Path(name) for name in names if name.endswith(f".dist-info/licenses/{filename}")],
                f"wheel {filename} file",
            )
            if archive.read(str(legal_name)) != expected:
                raise AssertionError(f"wheel {filename} does not match the repository file")

    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        metadata_member = _single(
            [Path(member.name) for member in members if member.name.endswith("/PKG-INFO")],
            "sdist PKG-INFO file",
        )
        metadata_file = archive.extractfile(str(metadata_member))
        if metadata_file is None:
            raise AssertionError("sdist PKG-INFO could not be read")
        _verify_metadata(BytesParser(policy=default).parsebytes(metadata_file.read()))
        for filename, expected in expected_legal_files.items():
            legal_member = _single(
                [
                    Path(member.name)
                    for member in members
                    if len(Path(member.name).parts) == 2 and member.name.endswith(f"/{filename}")
                ],
                f"sdist root {filename} file",
            )
            legal_file = archive.extractfile(str(legal_member))
            if legal_file is None or legal_file.read() != expected:
                raise AssertionError(f"sdist {filename} does not match the repository file")

    print(f"MIT license metadata and payload verified for llmolympic {__version__}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_distribution_license.py DIST_DIR")
    verify_distributions(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
