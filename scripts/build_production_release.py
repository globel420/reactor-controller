#!/usr/bin/env python3
"""Build deterministic, allowlisted production release assets.

The Raspberry Pi installer consumes these archives instead of cloning the
repository. Every archived source path must be explicitly listed, regular, and
free of development/test/emulator path components.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

SCHEMA = "reactor-release-v1"
PAYLOAD_SCHEMA = "reactor-release-payload-v1"
SPDX_VERSION = "SPDX-2.3"
RUNTIME_ALLOWLIST = Path("release/production-runtime-files.txt")
FIRMWARE_ALLOWLIST = Path("release/production-firmware-files.txt")
ENV_TEMPLATE = PurePosixPath("reactor.env.example")

FORBIDDEN_COMPONENTS = frozenset(
    {
        ".agents",
        ".git",
        ".github",
        ".mypy_cache",
        ".platformio",
        ".pytest_cache",
        ".ruff_cache",
        ".vscode",
        "__pycache__",
        "docs",
        "documents",
        "emulator",
        "emulators",
        "examples",
        "mcp",
        "node_modules",
        "test",
        "tests",
    }
)
FORBIDDEN_FILENAMES = frozenset(
    {
        "conftest.py",
        "deploy_pi.py",
        "docker-compose.yml",
        "mcp_config.json",
        "platformio_scanner.ini",
        "start.sh",
        "stop.sh",
    }
)
FORBIDDEN_SUFFIXES = (".db", ".db-shm", ".db-wal", ".log", ".pyc", ".pyo", ".sqlite")
VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
IMAGE_RE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")
EXPECTED_IMAGE_PREFIXES = {
    "supervisory": "ghcr.io/globel420/reactor-controller-supervisory@sha256:",
    "grafana": "ghcr.io/globel420/reactor-controller-grafana@sha256:",
}


class ReleaseBuildError(RuntimeError):
    """Raised when a release input violates the production boundary."""


@dataclasses.dataclass(frozen=True)
class ArchiveEntry:
    path: PurePosixPath
    content: bytes
    executable: bool = False


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw: str) -> PurePosixPath:
    if "\\" in raw:
        raise ReleaseBuildError(f"allowlist path must use forward slashes: {raw!r}")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or path == PurePosixPath("."):
        raise ReleaseBuildError(f"allowlist path must be relative: {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseBuildError(f"unsafe allowlist path: {raw!r}")
    return path


def _reject_forbidden(path: PurePosixPath) -> None:
    lowered_parts = tuple(part.lower() for part in path.parts)
    if FORBIDDEN_COMPONENTS.intersection(lowered_parts):
        raise ReleaseBuildError(f"development/test path is forbidden: {path}")
    filename = lowered_parts[-1]
    if filename in FORBIDDEN_FILENAMES:
        raise ReleaseBuildError(f"development file is forbidden: {path}")
    if filename.startswith("test_") or filename.endswith("_test.py"):
        raise ReleaseBuildError(f"test file is forbidden: {path}")
    if filename.endswith(FORBIDDEN_SUFFIXES):
        raise ReleaseBuildError(f"runtime/generated file is forbidden: {path}")


def read_allowlist(repo_root: Path, relative_path: Path) -> tuple[PurePosixPath, ...]:
    manifest = repo_root / relative_path
    if not manifest.is_file():
        raise ReleaseBuildError(f"missing allowlist: {relative_path}")
    entries: list[PurePosixPath] = []
    seen: set[PurePosixPath] = set()
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        path = _safe_relative_path(value)
        _reject_forbidden(path)
        if path in seen:
            raise ReleaseBuildError(
                f"duplicate allowlist entry {path} in {relative_path}:{line_number}"
            )
        seen.add(path)
        entries.append(path)
    if not entries:
        raise ReleaseBuildError(f"allowlist is empty: {relative_path}")
    return tuple(entries)


def _is_executable_payload(path: PurePosixPath, mode: int) -> bool:
    return bool(mode & 0o111) or path.suffix == ".sh" or (
        len(path.parts) >= 3 and path.parts[:3] == ("deploy", "production", "bin")
    ) or (
        len(path.parts) >= 3 and path.parts[:3] == ("deploy", "production", "libexec")
    )


def collect_entries(
    repo_root: Path,
    paths: Iterable[PurePosixPath],
    *,
    substitutions: dict[bytes, bytes] | None = None,
) -> list[ArchiveEntry]:
    root = repo_root.resolve()
    entries: list[ArchiveEntry] = []
    destinations: set[PurePosixPath] = set()
    for path in paths:
        _reject_forbidden(path)
        source = repo_root.joinpath(*path.parts)
        try:
            metadata = source.lstat()
        except FileNotFoundError as exc:
            raise ReleaseBuildError(f"allowlisted file is missing: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ReleaseBuildError(f"symlinks are forbidden in release payloads: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseBuildError(f"allowlisted path is not a regular file: {path}")
        try:
            source.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ReleaseBuildError(f"allowlisted file escapes repository: {path}") from exc
        if path in destinations:
            raise ReleaseBuildError(f"duplicate archive destination: {path}")
        destinations.add(path)
        content = source.read_bytes()
        if substitutions and path == ENV_TEMPLATE:
            for token, replacement in substitutions.items():
                if token not in content:
                    raise ReleaseBuildError(
                        f"release environment template is missing {token.decode()}"
                    )
                content = content.replace(token, replacement)
            if b"__RELEASE_" in content or b"__SUPERVISORY_" in content or b"__GRAFANA_" in content:
                raise ReleaseBuildError("unresolved release placeholder in reactor.env.example")
        entries.append(
            ArchiveEntry(
                path=path,
                content=content,
                executable=_is_executable_payload(path, metadata.st_mode),
            )
        )
    return entries


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _release_timestamp(source_date_epoch: int) -> str:
    return dt.datetime.fromtimestamp(source_date_epoch, tz=dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _payload_metadata(
    *,
    version: str,
    source_commit: str,
    source_date_epoch: int,
    payload: str,
    images: dict[str, str],
) -> dict[str, object]:
    return {
        "schema": PAYLOAD_SCHEMA,
        "firmware_runtime_compatibility": "same-release-source-required-v1",
        "version": version,
        "source_commit": source_commit,
        "created_at": _release_timestamp(source_date_epoch),
        "payload": payload,
        "platform": {"architecture": "linux/arm64", "os": "Raspberry Pi OS Lite 64-bit Trixie"},
        "images": images,
    }


def write_archive(path: Path, entries: Sequence[ArchiveEntry], source_date_epoch: int) -> None:
    ordered = sorted(entries, key=lambda entry: str(entry.path))
    if len({entry.path for entry in ordered}) != len(ordered):
        raise ReleaseBuildError("archive contains duplicate destinations")
    directories: set[PurePosixPath] = set()
    for entry in ordered:
        parent = entry.path.parent
        while parent != PurePosixPath("."):
            directories.add(parent)
            parent = parent.parent

    temporary = path.with_suffix(path.suffix + ".tmp")
    with (
        temporary.open("wb") as raw_stream,
        gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_stream, mtime=source_date_epoch
        ) as gz_stream,
        tarfile.open(
            fileobj=gz_stream, mode="w", format=tarfile.GNU_FORMAT
        ) as archive,
    ):
        for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
            info = tarfile.TarInfo(str(directory) + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = source_date_epoch
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info)
        for entry in ordered:
            info = tarfile.TarInfo(str(entry.path))
            info.size = len(entry.content)
            info.mode = 0o755 if entry.executable else 0o644
            info.mtime = source_date_epoch
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(entry.content))
    os.replace(temporary, path)


def write_spdx(
    path: Path,
    *,
    name: str,
    version: str,
    source_commit: str,
    source_date_epoch: int,
    entries: Sequence[ArchiveEntry],
) -> None:
    files = []
    relationships = []
    for index, entry in enumerate(sorted(entries, key=lambda item: str(item.path)), 1):
        spdx_id = f"SPDXRef-File-{index}"
        files.append(
            {
                "SPDXID": spdx_id,
                "fileName": str(entry.path),
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": _sha256_bytes(entry.content)}
                ],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-Package",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": spdx_id,
            }
        )
    document = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentDescribes": ["SPDXRef-Package"],
        "documentNamespace": (
            "https://github.com/globel420/reactor-controller/releases/"
            f"{version}/sbom/{name}/{source_commit}"
        ),
        "creationInfo": {
            "created": _release_timestamp(source_date_epoch),
            "creators": ["Tool: reactor-production-release-builder"],
        },
        "packages": [
            {
                "name": name,
                "SPDXID": "SPDXRef-Package",
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": True,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        ],
        "files": files,
        "relationships": relationships,
    }
    path.write_bytes(_json_bytes(document))


def _asset_record(archive: Path, sbom: Path) -> dict[str, object]:
    return {
        "name": archive.name,
        "sha256": _sha256_file(archive),
        "size": archive.stat().st_size,
        "archive_root": ".",
        "sbom": {
            "name": sbom.name,
            "sha256": _sha256_file(sbom),
            "size": sbom.stat().st_size,
        },
    }


def _validate_inputs(version: str, source_commit: str, images: dict[str, str]) -> None:
    if not VERSION_RE.fullmatch(version):
        raise ReleaseBuildError("version must be a v-prefixed semantic version")
    if not COMMIT_RE.fullmatch(source_commit):
        raise ReleaseBuildError("source commit must be a full hexadecimal commit id")
    for name, reference in images.items():
        if not IMAGE_RE.fullmatch(reference):
            raise ReleaseBuildError(f"{name} image must use an immutable sha256 digest")
        if not reference.startswith(EXPECTED_IMAGE_PREFIXES[name]):
            raise ReleaseBuildError(
                f"{name} image must use the canonical public GHCR package"
            )


def build_release(
    *,
    repo_root: Path,
    output_dir: Path,
    version: str,
    source_commit: str,
    source_date_epoch: int,
    supervisory_image: str,
    grafana_image: str,
    include_firmware: bool,
    bootstrap: Path | None = None,
    runtime_paths: Sequence[PurePosixPath] | None = None,
    firmware_paths: Sequence[PurePosixPath] | None = None,
) -> dict[str, object]:
    images = {"supervisory": supervisory_image, "grafana": grafana_image}
    _validate_inputs(version, source_commit, images)
    if source_date_epoch <= 0:
        raise ReleaseBuildError("SOURCE_DATE_EPOCH must be a positive integer")
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ReleaseBuildError(f"output directory must be empty: {output_dir}")
    if runtime_paths is None:
        runtime_paths = read_allowlist(repo_root, RUNTIME_ALLOWLIST)
    if include_firmware and firmware_paths is None:
        firmware_paths = read_allowlist(repo_root, FIRMWARE_ALLOWLIST)

    substitutions = {
        b"__RELEASE_VERSION__": version.encode(),
        b"__SUPERVISORY_IMAGE__": supervisory_image.encode(),
        b"__GRAFANA_IMAGE__": grafana_image.encode(),
    }
    runtime_entries = collect_entries(repo_root, runtime_paths, substitutions=substitutions)
    runtime_entries.append(
        ArchiveEntry(
            PurePosixPath("RELEASE.json"),
            _json_bytes(
                _payload_metadata(
                    version=version,
                    source_commit=source_commit,
                    source_date_epoch=source_date_epoch,
                    payload="runtime",
                    images=images,
                )
            ),
        )
    )

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent))
    try:
        runtime_archive = stage / f"reactor-controller-production-{version}.tar.gz"
        runtime_sbom = stage / f"reactor-controller-production-{version}.spdx.json"
        write_archive(runtime_archive, runtime_entries, source_date_epoch)
        write_spdx(
            runtime_sbom,
            name="reactor-controller-production",
            version=version,
            source_commit=source_commit,
            source_date_epoch=source_date_epoch,
            entries=runtime_entries,
        )
        assets: dict[str, object] = {"runtime": _asset_record(runtime_archive, runtime_sbom)}

        if include_firmware:
            if firmware_paths is None:
                raise ReleaseBuildError("firmware allowlist was not resolved")
            firmware_entries = collect_entries(repo_root, firmware_paths)
            firmware_entries.append(
                ArchiveEntry(
                    PurePosixPath("RELEASE.json"),
                    _json_bytes(
                        _payload_metadata(
                            version=version,
                            source_commit=source_commit,
                            source_date_epoch=source_date_epoch,
                            payload="firmware",
                            images={},
                        )
                    ),
                )
            )
            firmware_archive = stage / f"reactor-controller-firmware-{version}.tar.gz"
            firmware_sbom = stage / f"reactor-controller-firmware-{version}.spdx.json"
            write_archive(firmware_archive, firmware_entries, source_date_epoch)
            write_spdx(
                firmware_sbom,
                name="reactor-controller-firmware",
                version=version,
                source_commit=source_commit,
                source_date_epoch=source_date_epoch,
                entries=firmware_entries,
            )
            assets["firmware"] = _asset_record(firmware_archive, firmware_sbom)

        if bootstrap is not None:
            source = bootstrap if bootstrap.is_absolute() else repo_root / bootstrap
            if source.name != "reactor-bootstrap.sh":
                raise ReleaseBuildError("bootstrap asset must be named reactor-bootstrap.sh")
            if source.is_symlink() or not source.is_file():
                raise ReleaseBuildError(f"bootstrap is missing or not a regular file: {bootstrap}")
            try:
                source.resolve(strict=True).relative_to(repo_root)
            except ValueError as exc:
                raise ReleaseBuildError("bootstrap asset must be inside the repository") from exc
            target = stage / "reactor-bootstrap.sh"
            shutil.copyfile(source, target)
            target.chmod(0o755)
            assets["bootstrap"] = {
                "name": target.name,
                "sha256": _sha256_file(target),
                "size": target.stat().st_size,
            }

        manifest: dict[str, object] = {
            "schema": SCHEMA,
            "version": version,
            "source_commit": source_commit,
            "created_at": _release_timestamp(source_date_epoch),
            "platform": {
                "architecture": "linux/arm64",
                "os": "Raspberry Pi OS Lite 64-bit Trixie",
            },
            "images": images,
            "assets": assets,
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_bytes(_json_bytes(manifest))

        checksum_paths = sorted(
            (path for path in stage.iterdir() if path.name != "SHA256SUMS"),
            key=lambda path: path.name,
        )
        checksum_text = "".join(
            f"{_sha256_file(path)}  {path.name}\n" for path in checksum_paths
        )
        (stage / "SHA256SUMS").write_text(checksum_text, encoding="utf-8")

        if output_dir.exists():
            output_dir.rmdir()
        os.replace(stage, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, default=None)
    parser.add_argument("--supervisory-image", required=True)
    parser.add_argument("--grafana-image", required=True)
    parser.add_argument("--include-firmware", action="store_true")
    parser.add_argument("--bootstrap", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_date_epoch = args.source_date_epoch
    if source_date_epoch is None:
        raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if raw_epoch is None:
            raise ReleaseBuildError("pass --source-date-epoch or set SOURCE_DATE_EPOCH")
        source_date_epoch = int(raw_epoch)
    manifest = build_release(
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        version=args.version,
        source_commit=args.source_commit,
        source_date_epoch=source_date_epoch,
        supervisory_image=args.supervisory_image,
        grafana_image=args.grafana_image,
        include_firmware=args.include_firmware,
        bootstrap=args.bootstrap,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseBuildError as exc:
        raise SystemExit(f"release build rejected: {exc}") from exc
