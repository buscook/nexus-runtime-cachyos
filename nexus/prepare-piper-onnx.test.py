#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Focused fixtures for the pinned Piper ONNX Runtime prefetch helper."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

HELPER_PATH = Path(__file__).with_name("prepare-piper-onnx.py")
SPEC = importlib.util.spec_from_file_location("prepare_piper_onnx", HELPER_PATH)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

HEADERS = [
    "build/native/include/cpu_provider_factory.h",
    "build/native/include/onnxruntime_c_api.h",
    "build/native/include/onnxruntime_cxx_api.h",
    "build/native/include/onnxruntime_cxx_inline.h",
    "build/native/include/onnxruntime_run_options_config_keys.h",
    "build/native/include/onnxruntime_session_options_config_keys.h",
    "build/native/include/provider_options.h",
]
LIBS = {
    "linux-x64": b"fixture-onnx-x86_64\n" * 64,
    "linux-arm64": b"fixture-onnx-aarch64\n" * 64,
}
CMAKE = (
    b"cmake_minimum_required(VERSION 3.13)\n"
    b"ExternalProject_Add(\n"
    b"    piper_phonemize_external\n"
    b"    PREFIX \"p\"\n"
    b"    URL \"u\"\n"
    b"    CMAKE_ARGS -DCMAKE_INSTALL_PREFIX:PATH=${PIPER_PHONEMIZE_DIR}\n"
    b"  )\n"
)


def package_entries():
    entries = {header: b"# " + header.encode() + b"\n" for header in HEADERS}
    for runtime, data in LIBS.items():
        entries[f"runtimes/{runtime}/native/libonnxruntime.so"] = data
    entries["LICENSE.txt"] = b"fixture license\n"
    entries["ThirdPartyNotices.txt"] = b"fixture notices\n"
    entries["build/native/include/ignored.txt"] = b"not a header\n"
    return entries


def write_package(path, entries, modes=None, duplicates=()):
    modes = modes or {}
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = modes.get(name, 0) << 16
            archive.writestr(info, data)
        for name, data in duplicates:
            archive.writestr(name, data)


def fixture_lock(path, cmake):
    package_sha = helper.sha256_file(path)
    patched = helper.derive_piper_cmake(cmake)
    return {
        "schema": 1,
        "package": {
            "id": "Microsoft.ML.OnnxRuntime",
            "version": "1.14.1",
            "url": "https://api.nuget.org/v3-flatcontainer/microsoft.ml.onnxruntime/1.14.1/"
                   "microsoft.ml.onnxruntime.1.14.1.nupkg",
            "filename": "microsoft.ml.onnxruntime.1.14.1.nupkg",
            "size": path.stat().st_size,
            "sha256": package_sha,
            "sha512": "fixture",
            "source_commit": "c" * 40,
            "catalog": {
                "url": "https://api.nuget.org/v3/catalog0/data/2023.02.27.18.52.05/"
                       "microsoft.ml.onnxruntime.1.14.1.json",
                "sha256": "d" * 64,
            },
        },
        "piper": {
            "commit": "9" * 40,
            "cmake_sha256": helper.sha256_bytes(cmake),
            "cmake_patched_sha256": helper.sha256_bytes(patched),
        },
        "architectures": {
            "x86_64": {"runtime": "linux-x64", "library": "libonnxruntime.so.1.14.1",
                       "sha256": helper.sha256_bytes(LIBS["linux-x64"])},
            "aarch64": {"runtime": "linux-arm64", "library": "libonnxruntime.so.1.14.1",
                        "sha256": helper.sha256_bytes(LIBS["linux-arm64"])},
        },
        "headers": HEADERS,
        "notices": ["LICENSE.txt", "ThirdPartyNotices.txt"],
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nexus-piper-onnx-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "src"
        self.source.mkdir()
        (self.source / "CMakeLists.txt").write_bytes(CMAKE)
        self.package = self.root / "fixture.nupkg"
        write_package(self.package, package_entries())
        self.lock = fixture_lock(self.package, CMAKE)

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, arch="x86_64", **arguments):
        return helper.prepare(self.source, self.root / "output", arch, self.lock, **arguments)

    def assert_no_output(self):
        self.assertFalse((self.root / "output").exists())


class PackageValidation(Fixture):
    def test_prepares_allowlisted_headers_library_notices_and_proof(self):
        result = self.prepare(archive=self.package)
        self.assertTrue(result["changed"])
        output = self.root / "output"
        self.assertEqual(helper.sha256_file(output / "lib" / "libonnxruntime.so.1.14.1"),
                         self.lock["architectures"]["x86_64"]["sha256"])
        self.assertEqual(os.readlink(output / "lib" / "libonnxruntime.so"), "libonnxruntime.so.1.14.1")
        self.assertEqual((output / "LICENSE.txt").read_bytes(), b"fixture license\n")
        self.assertEqual((output / "ThirdPartyNotices.txt").read_bytes(), b"fixture notices\n")
        for header in HEADERS:
            self.assertEqual((output / "include" / Path(header).name).read_bytes(), b"# " + header.encode() + b"\n")
        self.assertFalse((output / "build").exists())
        self.assertFalse((output / "ignored.txt").exists())
        proof = json.loads((output / ".nexus-onnxruntime.json").read_text())
        self.assertEqual(proof["arch"], "x86_64")
        self.assertEqual(proof["package"]["sha256"], self.lock["package"]["sha256"])
        self.assertNotIn("ignored", " ".join(proof["files"]))

    def test_second_prepare_is_offline_idempotent(self):
        self.prepare(archive=self.package)
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in (self.root / "output").rglob("*") if path.is_file()}
        result = self.prepare(offline=True)
        self.assertFalse(result["changed"])
        self.assertFalse(result["patched"])
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in (self.root / "output").rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_seeded_cache_is_used_offline(self):
        cache = self.root / "nexus-onnxruntime-cache"
        cache.mkdir()
        shutil.copyfile(self.package, cache / self.lock["package"]["filename"])
        self.prepare(offline=True)
        self.assertTrue((self.root / "output" / "lib" / "libonnxruntime.so.1.14.1").is_file())

    def test_arm64_uses_the_arm64_library(self):
        self.prepare(arch="aarch64", archive=self.package)
        library = self.root / "output" / "lib" / "libonnxruntime.so.1.14.1"
        self.assertEqual(helper.sha256_file(library), self.lock["architectures"]["aarch64"]["sha256"])
        self.assertNotEqual(helper.sha256_file(library), self.lock["architectures"]["x86_64"]["sha256"])

    def test_rejects_wrong_size(self):
        broken = self.root / "truncated.nupkg"
        broken.write_bytes(self.package.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            self.prepare(archive=broken)
        self.assert_no_output()

    def test_rejects_wrong_hash(self):
        broken = self.root / "modified.nupkg"
        data = bytearray(self.package.read_bytes())
        data[len(data) // 2] ^= 0xFF
        broken.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.prepare(archive=broken)
        self.assert_no_output()

    def test_rejects_missing_or_empty_headers(self):
        for header in (HEADERS[0], HEADERS[3]):
            entries = package_entries()
            if header == HEADERS[0]:
                del entries[header]
            else:
                entries[header] = b""
            package = self.root / f"bad-{Path(header).name}.nupkg"
            write_package(package, entries)
            lock = fixture_lock(package, CMAKE)
            with self.assertRaisesRegex(ValueError, "Missing or empty ONNX Runtime header|Missing ONNX Runtime header"):
                helper.prepare(self.source, self.root / "output", "x86_64", lock, archive=package)
            self.assert_no_output()
            (self.source / "CMakeLists.txt").write_bytes(CMAKE)

    def test_rejects_archive_symlink_directory_and_unsafe_entries(self):
        link = self.root / "linked.nupkg"
        link.symlink_to(self.package)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.prepare(archive=link)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.prepare(archive=self.root)

        entries = package_entries()
        lib = "runtimes/linux-x64/native/libonnxruntime.so"
        for name, mode in (("symlink", 0o120777), ("directory", 0o040755)):
            package = self.root / f"{name}.nupkg"
            write_package(package, entries, modes={lib: mode})
            lock = fixture_lock(package, CMAKE)
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                helper.prepare(self.source, self.root / "output", "x86_64", lock, archive=package)
            self.assert_no_output()
            (self.source / "CMakeLists.txt").write_bytes(CMAKE)

        for name in ("../escape", "/absolute"):
            entries = package_entries()
            entries[name] = b"unsafe"
            package = self.root / (name.replace("/", "_").replace(".", "") + ".nupkg")
            write_package(package, entries)
            lock = fixture_lock(package, CMAKE)
            with self.assertRaisesRegex(ValueError, "Unsafe ONNX Runtime package entry"):
                helper.prepare(self.source, self.root / "output", "x86_64", lock, archive=package)
            self.assert_no_output()
            (self.source / "CMakeLists.txt").write_bytes(CMAKE)

        entries = package_entries()
        package = self.root / "duplicate.nupkg"
        with mock.patch("warnings.warn"):
            write_package(package, entries, duplicates=[(HEADERS[1], b"duplicate")])
        lock = fixture_lock(package, CMAKE)
        with self.assertRaisesRegex(ValueError, "duplicate entries"):
            helper.prepare(self.source, self.root / "output", "x86_64", lock, archive=package)
        self.assert_no_output()

    def test_rejects_bad_cache_without_downloading(self):
        cache = self.root / "nexus-onnxruntime-cache"
        cache.mkdir()
        (cache / self.lock["package"]["filename"]).write_bytes(b"short")
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            self.prepare(offline=True)
        self.assert_no_output()

    def test_offline_without_a_cache_fails(self):
        with self.assertRaisesRegex(ValueError, "--offline"):
            self.prepare(offline=True)
        self.assert_no_output()


class PiperSource(Fixture):
    def test_patches_only_the_derived_cmake_once(self):
        patched = helper.derive_piper_cmake(CMAKE)
        self.assertEqual(patched.count(b"-DONNXRUNTIME_DIR:PATH=${ONNXRUNTIME_DIR}"), 1)
        self.assertTrue(helper.patch_piper_source(self.source, self.lock))
        self.assertEqual((self.source / "CMakeLists.txt").read_bytes(), patched)
        self.assertFalse(helper.patch_piper_source(self.source, self.lock))
        self.assertEqual((self.source / "CMakeLists.txt").read_bytes(), patched)

    def test_rejects_unpinned_and_symlinked_cmake(self):
        (self.source / "CMakeLists.txt").write_bytes(CMAKE + b"# changed\n")
        with self.assertRaisesRegex(ValueError, "does not match the pinned source"):
            self.prepare(archive=self.package)
        self.assert_no_output()

        (self.source / "CMakeLists.txt").unlink()
        target = self.root / "outside-cmake"
        target.write_bytes(CMAKE)
        (self.source / "CMakeLists.txt").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.prepare(archive=self.package)
        self.assert_no_output()


class Downloads(Fixture):
    class Response:
        def __init__(self, data, length=None):
            self.data = io.BytesIO(data)
            self.headers = {} if length is None else {"Content-Length": str(length)}

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return False

        def read(self, size=-1):
            return self.data.read(size)

    def download(self, response):
        destination = self.root / "cache" / self.lock["package"]["filename"]
        with mock.patch.object(helper.urllib.request, "urlopen", return_value=response):
            helper.download_package(self.lock["package"]["url"], destination, self.lock)
        return destination

    def test_rejects_bad_status_zero_truncated_oversized_and_wrong_hash(self):
        size = self.lock["package"]["size"]
        destination = self.root / "cache" / self.lock["package"]["filename"]
        with mock.patch.object(helper.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("HTTP 404 Not Found")):
            with self.assertRaises(urllib.error.URLError):
                helper.download_package(self.lock["package"]["url"], destination, self.lock)
        for response in (self.Response(b"", 0), self.Response(b"x" * (size - 1), size),
                         self.Response(b"x" * (size + 1), size + 1)):
            with self.assertRaisesRegex(ValueError, "size mismatch|exceeded"):
                self.download(response)
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.download(self.Response(b"x" * size, size))
        self.assertFalse((self.root / "cache" / self.lock["package"]["filename"]).exists())
        self.assertFalse([path for path in (self.root / "cache").glob(".*") if path.is_file()])


if __name__ == "__main__":
    unittest.main()
