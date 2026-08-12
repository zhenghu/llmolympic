"""Verify that built distributions carry the declared MIT license metadata and text."""

from __future__ import annotations

import runpy
import sys
import tarfile
import zipfile
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = runpy.run_path(PROJECT_ROOT / "llmolympic" / "__init__.py")["__version__"]
if not isinstance(VERSION, str):
    raise TypeError("llmolympic.__version__ must be a string")
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")
WEB_ASSET_PATHS = (
    "llmolympic/web/static/index.html",
    "llmolympic/web/static/REACT_LICENSE.txt",
    "llmolympic/web/static/assets/app.css",
    "llmolympic/web/static/assets/app.js",
    "llmolympic/web/static/assets/react.production.min.js",
    "llmolympic/web/static/assets/react-dom.production.min.js",
)
REACT_ASSET_DIGESTS = {
    "llmolympic/web/static/assets/react.production.min.js": (
        "d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd"
    ),
    "llmolympic/web/static/assets/react-dom.production.min.js": (
        "35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d"
    ),
}


def _single(paths: list[Path], description: str) -> Path:
    if len(paths) != 1:
        raise AssertionError(f"expected one {description}, found {len(paths)}")
    return paths[0]


def _verify_metadata(metadata: Message) -> None:
    metadata_version = tuple(int(part) for part in metadata["Metadata-Version"].split("."))
    if metadata_version < (2, 4):
        raise AssertionError("license metadata requires Metadata-Version 2.4 or newer")
    if metadata["Version"] != VERSION:
        raise AssertionError("distribution metadata version does not match llmolympic.__version__")
    if metadata["License-Expression"] != "MIT":
        raise AssertionError("distribution License-Expression is not MIT")
    if sorted(metadata.get_all("License-File", [])) != sorted(LEGAL_FILES):
        raise AssertionError("distribution License-File metadata is incomplete")


def verify_distributions(dist_dir: Path) -> None:
    expected_legal_files = {name: (PROJECT_ROOT / name).read_bytes() for name in LEGAL_FILES}
    expected_web_assets = {name: (PROJECT_ROOT / name).read_bytes() for name in WEB_ASSET_PATHS}
    for name, expected_digest in REACT_ASSET_DIGESTS.items():
        if sha256(expected_web_assets[name]).hexdigest() != expected_digest:
            raise AssertionError(f"bundled React asset digest changed: {name}")
    wheel = _single(list(dist_dir.glob("*.whl")), "wheel")
    sdist = _single(list(dist_dir.glob("*.tar.gz")), "sdist")
    if not wheel.match(f"llmolympic-{VERSION}-*.whl"):
        raise AssertionError("wheel filename does not match llmolympic.__version__")
    if sdist.name != f"llmolympic-{VERSION}.tar.gz":
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
        for filename, expected in expected_web_assets.items():
            if filename not in names or archive.read(filename) != expected:
                raise AssertionError(f"wheel Web asset is missing or changed: {filename}")

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
        for filename, expected in expected_web_assets.items():
            asset_member = _single(
                [
                    Path(member.name)
                    for member in members
                    if member.name.endswith(f"/{filename}")
                ],
                f"sdist {filename} file",
            )
            asset_file = archive.extractfile(str(asset_member))
            if asset_file is None or asset_file.read() != expected:
                raise AssertionError(f"sdist Web asset is missing or changed: {filename}")

    print(f"MIT license metadata, notices, and Web assets verified for llmolympic {VERSION}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_distribution_license.py DIST_DIR")
    verify_distributions(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
