#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prefetch the pinned ONNX Runtime package for the derived Piper source.

The upstream Piper CMake asks piper-phonemize to download ONNX Runtime from
GitHub.  That asset is no longer available, so this helper installs the byte
identical runtime from the official Microsoft NuGet package and makes the
derived Piper CMake point piper-phonemize at it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile

LOCK_NAME = "onnxruntime.lock.json"
PIPER_ARG = "    CMAKE_ARGS -DONNXRUNTIME_DIR:PATH=${ONNXRUNTIME_DIR}\n"
PIPER_ANCHOR = "    CMAKE_ARGS -DCMAKE_INSTALL_PREFIX:PATH=${PIPER_PHONEMIZE_DIR}\n"
SUPPORTED_ARCHES = ("x86_64", "aarch64")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path=None):
    path = Path(path) if path else Path(__file__).with_name(LOCK_NAME)
    lock = json.loads(path.read_text(encoding="utf-8"))
    validate_lock(lock)
    return lock


def _hex(value, length=64):
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def validate_lock(lock):
    if lock.get("schema") != 1:
        raise ValueError("Unsupported ONNX Runtime lock schema")
    package = lock["package"]
    package_id, version = package["id"], package["version"]
    filename = f"{package_id.lower()}.{version}.nupkg"
    expected = f"https://api.nuget.org/v3-flatcontainer/{package_id.lower()}/{version}/{filename}"
    if package["url"] != expected:
        raise ValueError("ONNX Runtime package URL is not the pinned official NuGet URL")
    if package["filename"] != filename or not isinstance(package["size"], int) or package["size"] <= 0:
        raise ValueError("Invalid ONNX Runtime package size or filename")
    for name in ("sha256", "sha512"):
        if not isinstance(package[name], str) or not package[name]:
            raise ValueError(f"Missing ONNX Runtime package {name}")
    if not _hex(package["sha256"]):
        raise ValueError("Invalid ONNX Runtime package SHA256")
    catalog = package["catalog"]
    if not catalog["url"].startswith("https://api.nuget.org/v3/catalog0/data/") or not _hex(catalog["sha256"]):
        raise ValueError("Invalid ONNX Runtime catalog pin")
    if not _hex(package.get("source_commit", ""), 40):
        raise ValueError("Invalid ONNX Runtime source commit")
    piper = lock["piper"]
    if not _hex(piper["commit"], 40) or not _hex(piper["cmake_sha256"]) or not _hex(piper["cmake_patched_sha256"]):
        raise ValueError("Invalid Piper source pin")
    headers = lock["headers"]
    if not headers or len(set(headers)) != len(headers):
        raise ValueError("Invalid ONNX Runtime header allowlist")
    for header in headers:
        parts = Path(header).parts
        if parts[:3] != ("build", "native", "include") or not header.endswith(".h") or ".." in parts:
            raise ValueError(f"Unsafe ONNX Runtime header allowlist entry: {header}")
    notices = lock["notices"]
    if sorted(notices) != ["LICENSE.txt", "ThirdPartyNotices.txt"]:
        raise ValueError("Invalid ONNX Runtime notices allowlist")
    architectures = lock["architectures"]
    for arch in SUPPORTED_ARCHES:
        entry = architectures[arch]
        if entry["runtime"] != {"x86_64": "linux-x64", "aarch64": "linux-arm64"}[arch]:
            raise ValueError(f"Wrong ONNX Runtime runtime mapping for {arch}")
        if entry["library"] != f"libonnxruntime.so.{version}" or not _hex(entry["sha256"]):
            raise ValueError(f"Invalid ONNX Runtime library pin for {arch}")


def derive_piper_cmake(data):
    if data.count(PIPER_ANCHOR.encode()) != 1:
        raise ValueError("Pinned Piper CMake does not contain exactly one piper-phonemize install argument")
    derived = data.replace(PIPER_ANCHOR.encode(), PIPER_ANCHOR.encode() + PIPER_ARG.encode())
    if derived.count(PIPER_ARG.encode()) != 1:
        raise ValueError("Refusing to duplicate the ONNX Runtime CMake argument")
    return derived


def patch_piper_source(source, lock):
    source = Path(source)
    cmake = source / "CMakeLists.txt"
    try:
        stat_result = cmake.lstat()
    except FileNotFoundError:
        raise ValueError(f"Missing derived Piper CMake source: {cmake}") from None
    if not stat.S_ISREG(stat_result.st_mode):
        raise ValueError(f"Piper CMake source must be a regular file: {cmake}")
    original = cmake.read_bytes()
    digest = sha256_bytes(original)
    if digest == lock["piper"]["cmake_patched_sha256"]:
        if original.count(PIPER_ARG.encode()) != 1:
            raise ValueError("Derived Piper CMake is missing the ONNX Runtime argument")
        return False
    if digest != lock["piper"]["cmake_sha256"]:
        raise ValueError("Derived Piper CMake does not match the pinned source")
    derived = derive_piper_cmake(original)
    if sha256_bytes(derived) != lock["piper"]["cmake_patched_sha256"]:
        raise ValueError("Derived Piper CMake does not match the pinned derivative")
    descriptor, temporary = tempfile.mkstemp(dir=cmake.parent, prefix=".CMakeLists.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(derived)
        os.chmod(temporary, stat.S_IMODE(stat_result.st_mode))
        os.replace(temporary, cmake)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _regular(path, what):
    try:
        result = path.lstat()
    except FileNotFoundError:
        raise ValueError(f"Missing {what}: {path}") from None
    if not stat.S_ISREG(result.st_mode):
        raise ValueError(f"{what} must be a regular file: {path}")
    return result


def validate_package(path, lock):
    path = Path(path)
    result = _regular(path, "ONNX Runtime package")
    package = lock["package"]
    if result.st_size != package["size"]:
        raise ValueError(f"ONNX Runtime package size mismatch: {result.st_size} != {package['size']}")
    if sha256_file(path) != package["sha256"]:
        raise ValueError("ONNX Runtime package SHA256 mismatch")


def archive_payloads(path, lock, arch):
    runtime = lock["architectures"][arch]
    headers = {header: Path(header).name for header in lock["headers"]}
    library_source = f"runtimes/{runtime['runtime']}/native/libonnxruntime.so"
    allowed = set(headers) | {library_source, *lock["notices"]}
    payloads = {}
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("ONNX Runtime package contains duplicate entries")
            for info in archive.infolist():
                name = info.filename
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts:
                    raise ValueError(f"Unsafe ONNX Runtime package entry: {name}")
                if name not in allowed:
                    continue
                mode = info.external_attr >> 16
                if mode & 0o170000 and not stat.S_ISREG(mode):
                    raise ValueError(f"ONNX Runtime package entry is not a regular file: {name}")
                payloads[name] = archive.read(info)
    except zipfile.BadZipFile:
        raise ValueError("ONNX Runtime package is not a valid ZIP archive") from None
    for header in headers:
        if header not in payloads or not payloads[header]:
            raise ValueError(f"Missing or empty ONNX Runtime header: {header}")
    if library_source not in payloads:
        raise ValueError(f"Missing ONNX Runtime library: {library_source}")
    if sha256_bytes(payloads[library_source]) != runtime["sha256"]:
        raise ValueError("ONNX Runtime library SHA256 mismatch")
    for notice in lock["notices"]:
        if not payloads.get(notice):
            raise ValueError(f"Missing ONNX Runtime notice: {notice}")
    return payloads


def _write_file(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(mode)


def write_output(output, payloads, lock, arch):
    runtime = lock["architectures"][arch]
    output.mkdir(mode=0o755, exist_ok=True)
    output.chmod(0o755)
    files = {}
    for header, basename in {header: Path(header).name for header in lock["headers"]}.items():
        destination = output / "include" / basename
        _write_file(destination, payloads[header])
        files[f"include/{basename}"] = sha256_bytes(payloads[header])
    library = output / "lib" / runtime["library"]
    _write_file(library, payloads[f"runtimes/{runtime['runtime']}/native/libonnxruntime.so"], 0o755)
    files[f"lib/{runtime['library']}"] = runtime["sha256"]
    (output / "lib" / "libonnxruntime.so").symlink_to(runtime["library"])
    for notice in lock["notices"]:
        _write_file(output / notice, payloads[notice])
        files[notice] = sha256_bytes(payloads[notice])
    proof = {
        "schema": 1,
        "package": {name: lock["package"][name] for name in ("url", "size", "sha256")},
        "arch": arch,
        "library": runtime["library"],
        "files": files,
        "links": {"lib/libonnxruntime.so": runtime["library"]},
    }
    _write_file(output / ".nexus-onnxruntime.json",
                json.dumps(proof, sort_keys=True, indent=2).encode() + b"\n")
    return proof


def output_current(output, lock, arch):
    output = Path(output)
    try:
        if not stat.S_ISDIR(output.lstat().st_mode):
            return False
        proof_path = output / ".nexus-onnxruntime.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return False
    if proof.get("schema") != 1 or proof.get("arch") != arch or proof.get("library") != lock["architectures"][arch]["library"]:
        return False
    package = lock["package"]
    if any(proof.get("package", {}).get(name) != package[name] for name in ("url", "size", "sha256")):
        return False
    expected = {f"include/{Path(header).name}" for header in lock["headers"]}
    expected |= {f"lib/{lock['architectures'][arch]['library']}", *lock["notices"]}
    if set(proof.get("files", {})) != expected:
        return False
    for relative, digest in proof["files"].items():
        path = output / relative
        if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
            return False
    link = output / "lib" / "libonnxruntime.so"
    if not link.is_symlink() or os.readlink(link) != lock["architectures"][arch]["library"]:
        return False
    return True


def download_package(url, destination, lock):
    package = lock["package"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix="." + destination.name + ".")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "nexus-runtime-piper-onnx/1"})
        total = 0
        with os.fdopen(descriptor, "wb") as stream:
            with urllib.request.urlopen(request, timeout=60) as response:
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != package["size"]:
                    raise ValueError(f"ONNX Runtime download size mismatch: {length} != {package['size']}")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > package["size"]:
                        raise ValueError("ONNX Runtime download exceeded the pinned size")
                    stream.write(chunk)
        if total != package["size"]:
            raise ValueError(f"ONNX Runtime download size mismatch: {total} != {package['size']}")
        if sha256_file(Path(temporary)) != package["sha256"]:
            raise ValueError("ONNX Runtime download SHA256 mismatch")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def acquire_archive(output, lock, archive=None, offline=False):
    package = lock["package"]
    if archive is not None:
        validate_package(archive, lock)
        return Path(archive)
    cache = Path(output).parent / "nexus-onnxruntime-cache" / package["filename"]
    if cache.exists() or cache.is_symlink():
        validate_package(cache, lock)
        return cache
    if offline:
        raise ValueError(f"Pinned ONNX Runtime package is not cached and --offline was given: {cache}")
    download_package(package["url"], cache, lock)
    return cache


def prepare(source, output, arch, lock, archive=None, offline=False):
    validate_lock(lock)
    if arch not in SUPPORTED_ARCHES:
        raise ValueError(f"Unsupported ONNX Runtime architecture: {arch}")
    patched = patch_piper_source(source, lock)
    output = Path(output)
    if output_current(output, lock, arch):
        return {"changed": False, "patched": patched, "output": output}
    package = acquire_archive(output, lock, archive, offline)
    payloads = archive_payloads(package, lock, arch)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=output.parent, prefix=f".{output.name}."))
    try:
        write_output(staging, payloads, lock, arch)
        if output.exists() or output.is_symlink():
            if output.is_symlink() or not output.is_dir():
                raise ValueError(f"Refusing unsafe ONNX Runtime output path: {output}")
            shutil.rmtree(output)
        os.replace(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"changed": True, "patched": patched, "output": output, "package": package}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="derived Piper source directory")
    parser.add_argument("--output", required=True, type=Path, help="ONNX Runtime SDK directory")
    parser.add_argument("--arch", required=True, choices=SUPPORTED_ARCHES)
    parser.add_argument("--archive", type=Path, help="pre-downloaded pinned NuGet package")
    parser.add_argument("--offline", action="store_true", help="never download; require the pinned cache")
    arguments = parser.parse_args(argv)
    lock = load_lock()
    try:
        result = prepare(arguments.source, arguments.output, arguments.arch, lock,
                         archive=arguments.archive, offline=arguments.offline)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"Piper ONNX Runtime preparation failed: {error}", file=sys.stderr)
        return 1
    action = "prepared" if result["changed"] else "already current"
    print(f"Piper ONNX Runtime {lock['package']['version']} {action}: {result['output']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
