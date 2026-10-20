from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from scripts import build_production_release as release

COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
SUPERVISORY_IMAGE = f"ghcr.io/globel420/reactor-controller-supervisory@{DIGEST}"
GRAFANA_IMAGE = f"ghcr.io/globel420/reactor-controller-grafana@{DIGEST}"
EPOCH = 1_700_000_000
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


class ProductionReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "payload").mkdir()
        (self.root / "payload/runtime.txt").write_text("runtime\n", encoding="utf-8")
        (self.root / "payload/firmware.cpp").write_text("// firmware\n", encoding="utf-8")
        (self.root / "reactor.env.example").write_text(
            "REACTOR_RELEASE_VERSION=__RELEASE_VERSION__\n"
            "SUPERVISORY_IMAGE=__SUPERVISORY_IMAGE__\n"
            "GRAFANA_IMAGE=__GRAFANA_IMAGE__\n",
            encoding="utf-8",
        )
        (self.root / "reactor-bootstrap.sh").write_text(
            '#!/bin/sh\nreadonly RELEASE_VERSION="v2.1.0"\nexit 0\n',
            encoding="utf-8",
        )
        self.runtime_paths = (
            PurePosixPath("payload/runtime.txt"),
            PurePosixPath("reactor.env.example"),
        )
        self.firmware_paths = (PurePosixPath("payload/firmware.cpp"),)

    def build(self, output_name: str = "dist") -> tuple[Path, dict[str, object]]:
        output = self.root / output_name
        manifest = release.build_release(
            repo_root=self.root,
            output_dir=output,
            version="v2.1.0",
            source_commit=COMMIT,
            source_date_epoch=EPOCH,
            supervisory_image=SUPERVISORY_IMAGE,
            grafana_image=GRAFANA_IMAGE,
            include_firmware=True,
            bootstrap=Path("reactor-bootstrap.sh"),
            runtime_paths=self.runtime_paths,
            firmware_paths=self.firmware_paths,
        )
        return output, manifest

    def test_builds_flat_deterministic_archives_and_release_metadata(self) -> None:
        first, manifest = self.build("dist-one")
        second, second_manifest = self.build("dist-two")

        self.assertEqual(manifest, second_manifest)
        self.assertEqual(manifest["schema"], "reactor-release-v1")
        self.assertEqual(manifest["assets"]["runtime"]["archive_root"], ".")
        for filename in sorted(path.name for path in first.iterdir()):
            self.assertEqual(
                hashlib.sha256((first / filename).read_bytes()).hexdigest(),
                hashlib.sha256((second / filename).read_bytes()).hexdigest(),
                filename,
            )

        runtime_name = manifest["assets"]["runtime"]["name"]
        with tarfile.open(first / runtime_name, "r:gz") as archive:
            names = archive.getnames()
            self.assertIn("payload/runtime.txt", names)
            self.assertIn("reactor.env.example", names)
            self.assertIn("RELEASE.json", names)
            env_text = archive.extractfile("reactor.env.example").read().decode()
            self.assertIn("REACTOR_RELEASE_VERSION=v2.1.0", env_text)
            self.assertIn(SUPERVISORY_IMAGE, env_text)
            self.assertNotIn("__RELEASE_", env_text)

    def test_checksums_cover_every_release_file_except_checksum_file(self) -> None:
        output, _ = self.build()
        lines = (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        covered = {line.split("  ", 1)[1] for line in lines}
        expected = {path.name for path in output.iterdir()} - {"SHA256SUMS"}
        self.assertEqual(covered, expected)
        for line in lines:
            digest, filename = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((output / filename).read_bytes()).hexdigest())

    def test_prerelease_bootstrap_is_pinned_to_exact_tag(self) -> None:
        output = self.root / "dist-rc"
        release.build_release(
            repo_root=self.root,
            output_dir=output,
            version="v2.1.0-rc.1",
            source_commit=COMMIT,
            source_date_epoch=EPOCH,
            supervisory_image=SUPERVISORY_IMAGE,
            grafana_image=GRAFANA_IMAGE,
            include_firmware=False,
            bootstrap=Path("reactor-bootstrap.sh"),
            runtime_paths=self.runtime_paths,
        )

        bootstrap = (output / "reactor-bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn('readonly RELEASE_VERSION="v2.1.0-rc.1"', bootstrap)
        self.assertNotIn('readonly RELEASE_VERSION="v2.1.0"', bootstrap)

    def test_sbom_lists_only_archived_files(self) -> None:
        output, manifest = self.build()
        sbom_name = manifest["assets"]["runtime"]["sbom"]["name"]
        sbom = json.loads((output / sbom_name).read_text(encoding="utf-8"))
        names = {item["fileName"] for item in sbom["files"]}
        self.assertEqual(
            names,
            {"payload/runtime.txt", "reactor.env.example", "RELEASE.json"},
        )

    def test_rejects_forbidden_test_and_emulator_paths(self) -> None:
        for unsafe in ("tests/test_release.py", "emulators/sensor.py", ".git/config"):
            with self.subTest(unsafe=unsafe), self.assertRaises(
                release.ReleaseBuildError
            ):
                release._reject_forbidden(PurePosixPath(unsafe))

    def test_rejects_path_traversal_and_symlinks(self) -> None:
        with self.assertRaises(release.ReleaseBuildError):
            release._safe_relative_path("../secret")
        link = self.root / "payload/link"
        try:
            link.symlink_to(self.root / "payload/runtime.txt")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(release.ReleaseBuildError, "symlinks are forbidden"):
            release.collect_entries(self.root, (PurePosixPath("payload/link"),))

    def test_requires_digest_pinned_images(self) -> None:
        with self.assertRaisesRegex(release.ReleaseBuildError, "immutable sha256"):
            release.build_release(
                repo_root=self.root,
                output_dir=self.root / "dist",
                version="v2.1.0",
                source_commit=COMMIT,
                source_date_epoch=EPOCH,
                supervisory_image="ghcr.io/globel420/reactor-controller-supervisory:v2.1.0",
                grafana_image=GRAFANA_IMAGE,
                include_firmware=False,
                runtime_paths=self.runtime_paths,
            )

    def test_rejects_noncanonical_image_repository(self) -> None:
        with self.assertRaisesRegex(release.ReleaseBuildError, "canonical public GHCR"):
            release.build_release(
                repo_root=self.root,
                output_dir=self.root / "dist",
                version="v2.1.0",
                source_commit=COMMIT,
                source_date_epoch=EPOCH,
                supervisory_image=f"ghcr.io/example/reactor-supervisory@{DIGEST}",
                grafana_image=GRAFANA_IMAGE,
                include_firmware=False,
                runtime_paths=self.runtime_paths,
            )

    def test_rejects_nonempty_output_directory(self) -> None:
        output = self.root / "dist"
        output.mkdir()
        (output / "stale").write_text("stale", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseBuildError, "must be empty"):
            release.build_release(
                repo_root=self.root,
                output_dir=output,
                version="v2.1.0",
                source_commit=COMMIT,
                source_date_epoch=EPOCH,
                supervisory_image=SUPERVISORY_IMAGE,
                grafana_image=GRAFANA_IMAGE,
                include_firmware=False,
                runtime_paths=self.runtime_paths,
            )

    def test_rejects_bootstrap_outside_repository(self) -> None:
        outside = self.root.parent / "reactor-bootstrap.sh"
        outside.write_text("#!/bin/sh\n", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaisesRegex(release.ReleaseBuildError, "inside the repository"):
            release.build_release(
                repo_root=self.root,
                output_dir=self.root / "dist",
                version="v2.1.0",
                source_commit=COMMIT,
                source_date_epoch=EPOCH,
                supervisory_image=SUPERVISORY_IMAGE,
                grafana_image=GRAFANA_IMAGE,
                include_firmware=False,
                bootstrap=outside,
                runtime_paths=self.runtime_paths,
            )


class RepositoryPayloadContractTests(unittest.TestCase):
    def test_release_workflow_pulls_arm64_images_with_explicit_platform(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('docker pull --platform linux/arm64 "$reference"', workflow)
        self.assertNotIn('docker pull "$reference"', workflow)

    def test_tracked_allowlists_are_exact_regular_production_files(self) -> None:
        runtime_paths = release.read_allowlist(REPO_ROOT, release.RUNTIME_ALLOWLIST)
        firmware_paths = release.read_allowlist(REPO_ROOT, release.FIRMWARE_ALLOWLIST)

        self.assertIn(
            PurePosixPath("deploy/production/systemd/reactor-system-manager.service"),
            runtime_paths,
        )
        self.assertIn(PurePosixPath("compose.production.yml"), runtime_paths)
        self.assertIn(PurePosixPath("reactor.env.example"), runtime_paths)
        self.assertIn(PurePosixPath("firmware/platformio.ini"), firmware_paths)
        self.assertIn(PurePosixPath("pump_firmware/platformio.ini"), firmware_paths)

        release.collect_entries(
            REPO_ROOT,
            runtime_paths,
            substitutions={
                b"__RELEASE_VERSION__": b"v2.1.0",
                b"__SUPERVISORY_IMAGE__": SUPERVISORY_IMAGE.encode(),
                b"__GRAFANA_IMAGE__": GRAFANA_IMAGE.encode(),
            },
        )
        release.collect_entries(REPO_ROOT, firmware_paths)

    def test_production_payloads_exclude_nonproduction_trees(self) -> None:
        runtime_paths = release.read_allowlist(REPO_ROOT, release.RUNTIME_ALLOWLIST)
        firmware_paths = release.read_allowlist(REPO_ROOT, release.FIRMWARE_ALLOWLIST)
        combined = {str(path).lower() for path in (*runtime_paths, *firmware_paths)}

        for forbidden in (
            ".git",
            ".agents",
            "tests/",
            "test_",
            "emulator",
            "examples/",
            "documents/",
            "platformio_scanner.ini",
            "mcp",
        ):
            self.assertFalse(
                any(forbidden in path for path in combined),
                f"forbidden production payload entry contains {forbidden}",
            )

    def test_production_compose_has_no_source_build_or_emulator_fallback(self) -> None:
        compose = (REPO_ROOT / "compose.production.yml").read_text(encoding="utf-8")
        lowered = compose.lower()
        self.assertNotIn("build:", lowered)
        self.assertNotIn("emulator", lowered)
        self.assertNotIn("simulation", lowered)
        self.assertIn("${SUPERVISORY_IMAGE:", compose)
        self.assertIn("${GRAFANA_IMAGE:", compose)
        self.assertIn("profiles: [commissioning]", compose)
        self.assertIn("profiles: [hardware]", compose)
        env_template = (REPO_ROOT / "reactor.env.example").read_text(encoding="utf-8")
        self.assertNotIn("COMPOSE_PROFILES=", env_template)

    def test_runtime_and_firmware_builder_share_v2_config_hashes(self) -> None:
        firmware = load_module(
            "release_test_firmware_env", REPO_ROOT / "scripts/load_firmware_env.py"
        )
        runtime = load_module(
            "release_test_reactor_runtime",
            REPO_ROOT / "deploy/production/lib/reactor_runtime.py",
        )
        expected = {
            target: f"0x{firmware.config_fingerprint(firmware.DEFAULTS, target):08X}"
            for target in ("mega", "uno")
        }

        self.assertEqual(runtime.expected_firmware_hashes(firmware.DEFAULTS), expected)
        self.assertEqual(firmware.FINGERPRINT_SCHEMA, "reactor-firmware-config-v2")

        changed = dict(firmware.DEFAULTS)
        changed["MEGA_HEATER_ACTIVE_HIGH"] = "0"
        self.assertNotEqual(
            runtime.expected_firmware_hashes(changed)["mega"], expected["mega"]
        )
        changed = dict(firmware.DEFAULTS)
        changed["UNO_ENABLE_ACTIVE_LOW"] = "0"
        self.assertNotEqual(
            runtime.expected_firmware_hashes(changed)["uno"], expected["uno"]
        )


if __name__ == "__main__":
    unittest.main()
