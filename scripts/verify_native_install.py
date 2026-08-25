"""Verify a complete native install through trusted local HTTPS."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.server
import ipaddress
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from csaf.setup.assets import AssetLimits, SetupError, extract_verified_archive
from csaf.setup.types import InstallState

PLATFORMS = ("linux-arm64", "linux-x64", "macos-arm64", "macos-x64", "windows-arm64", "windows-x64")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_DIAGNOSTICS = frozenset({"setup-install"})


class NativeVerificationError(RuntimeError):
    """The local release cannot prove a complete native installation."""

    def __init__(self, message: str, *, diagnostic: str | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic if diagnostic in _SAFE_DIAGNOSTICS else None


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as incoming:
        while chunk := incoming.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _link_or_reparse(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse)


def _regular(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and not _link_or_reparse(details)


def _safe_existing_path(path: Path, message: str) -> Path:
    """Reject links/reparse points in a supplied path before resolving it."""
    supplied = Path(os.path.abspath(os.fspath(path)))
    for candidate in (supplied, *supplied.parents):
        try:
            details = candidate.lstat()
        except OSError as exc:
            raise NativeVerificationError(message) from exc
        if _link_or_reparse(details):
            raise NativeVerificationError(message)
    try:
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise NativeVerificationError(message) from exc


def _verified_asset(path: Path, *, sha256: str, size: int, root: Path | None = None) -> Path:
    """Return one regular asset only after exact size and SHA-256 verification."""
    path = _safe_existing_path(Path(path), "local release asset verification failed")
    if root is not None:
        safe_root = _safe_existing_path(Path(root), "local release asset verification failed")
        try:
            path.relative_to(safe_root)
        except ValueError as exc:
            raise NativeVerificationError("local release asset verification failed") from exc
    if (
        not _regular(path)
        or type(size) is not int
        or size <= 0
        or not isinstance(sha256, str)
        or not _HASH.fullmatch(sha256)
        or path.stat().st_size != size
        or _hash(path) != sha256
    ):
        raise NativeVerificationError("local release asset verification failed")
    return path


def _extract_uv(archive: Path, destination: Path, platform: str, source_url: str) -> Path:
    limits = AssetLimits(
        max_archive_bytes=64 * 1024 * 1024,
        max_members=8,
        max_member_bytes=64 * 1024 * 1024,
        max_total_bytes=96 * 1024 * 1024,
    )
    try:
        extracted = extract_verified_archive(archive, destination, limits=limits)
    except SetupError as exc:
        raise NativeVerificationError("uv archive verification failed") from exc
    if platform.startswith("windows-"):
        expected = {"uv.exe", "uvw.exe", "uvx.exe"}
        executable = destination / "uv.exe"
    else:
        archive_name = Path(urlsplit(source_url).path).name
        if not archive_name.endswith(".tar.gz"):
            raise NativeVerificationError("uv archive verification failed")
        prefix = archive_name.removesuffix(".tar.gz")
        expected = {f"{prefix}/uv", f"{prefix}/uvx"}
        executable = destination / prefix / "uv"
    names = {path.relative_to(destination).as_posix() for path in extracted}
    if names != expected or not _regular(executable) or executable.stat().st_size <= 0:
        raise NativeVerificationError("uv archive verification failed")
    if not platform.startswith("windows-"):
        executable.chmod(0o700)
    return executable


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeVerificationError("local release metadata verification failed") from exc
    if not isinstance(value, dict):
        raise NativeVerificationError("local release metadata verification failed")
    return value


def _asset_filename(record: dict[str, Any]) -> str:
    if set(record) != {"url", "sha256", "size"}:
        raise NativeVerificationError("release manifest asset verification failed")
    name = Path(urlsplit(str(record["url"])).path).name
    if not name or name in {".", ".."}:
        raise NativeVerificationError("release manifest asset verification failed")
    return name


def _verify_sums(release_dir: Path) -> None:
    sums = release_dir / "SHA256SUMS"
    if not _regular(sums):
        raise NativeVerificationError("release checksums verification failed")
    expected: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not re.fullmatch(r"[0-9a-f]{64}  [A-Za-z0-9][A-Za-z0-9_.-]*", line):
            raise NativeVerificationError("release checksums verification failed")
        digest, name = line.split("  ", 1)
        if name in expected:
            raise NativeVerificationError("release checksums verification failed")
        expected[name] = digest
    actual = {path.name for path in release_dir.iterdir() if _regular(path) and path != sums}
    if set(expected) != actual:
        raise NativeVerificationError("release checksums verification failed")
    for name, digest in expected.items():
        if _hash(release_dir / name) != digest:
            raise NativeVerificationError("release checksums verification failed")


def _certificate(directory: Path) -> tuple[Path, Path]:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise NativeVerificationError(
            "cryptography is required for trusted TLS verification"
        ) from exc
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CSAF local verifier CA")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "ca.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _AssetHandler(http.server.BaseHTTPRequestHandler):
    assets: dict[str, Path] = {}

    def _send(self, *, body: bool) -> None:
        name = self.path.removeprefix("/")
        if "/" in name or "?" in name or name not in self.assets:
            self.send_error(404)
            return
        path = self.assets[name]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        if body:
            with path.open("rb") as incoming:
                shutil.copyfileobj(incoming, self.wfile, length=1024 * 1024)

    def do_GET(self) -> None:
        self._send(body=True)

    def do_HEAD(self) -> None:
        self._send(body=False)

    def log_message(self, *_args: object) -> None:
        return


def _write_egress_guard(directory: Path) -> Path:
    """Create a startup policy that permits only loopback network connections."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    directory.chmod(0o700)
    policy = directory / "sitecustomize.py"
    policy.write_text(
        """import errno
import ipaddress
import os
import socket

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_create_connection = socket.create_connection


def _allowed(address):
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if isinstance(host, str) and host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guarded_connect(sock, address):
    if not _allowed(address):
        raise PermissionError(errno.EACCES, "CSAF external network egress blocked")
    return _original_connect(sock, address)


def _guarded_connect_ex(sock, address):
    if not _allowed(address):
        raise PermissionError(errno.EACCES, "CSAF external network egress blocked")
    return _original_connect_ex(sock, address)


def _guarded_create_connection(address, *args, **kwargs):
    if not _allowed(address):
        raise PermissionError(errno.EACCES, "CSAF external network egress blocked")
    return _original_create_connection(address, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.create_connection = _guarded_create_connection
os.environ["CSAF_EGRESS_GUARD_ACTIVE"] = "1"
_proof = os.environ.get("CSAF_EGRESS_GUARD_PROOF")
if _proof:
    try:
        _descriptor = os.open(_proof, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(_descriptor, "w", encoding="utf-8") as _output:
            _output.write("CSAF_EGRESS_GUARD_ACTIVE\\n")
""",
        encoding="utf-8",
        newline="\n",
    )
    policy.chmod(0o600)
    return policy


def _activated_environment(launcher: Path, env: dict[str, str]) -> dict[str, str]:
    """Isolate activated commands to the installed runtime and verifier guard."""
    runtime = launcher.parent
    site_packages = runtime / "site-packages"
    try:
        details = site_packages.lstat()
        site_packages.resolve(strict=True).relative_to(runtime.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise NativeVerificationError("activated runtime import path verification failed") from exc
    if _link_or_reparse(details) or not stat.S_ISDIR(details.st_mode):
        raise NativeVerificationError("activated runtime import path verification failed")

    inherited = env.get("PYTHONPATH", "")
    guard_text = inherited.split(os.pathsep, 1)[0]
    if not guard_text:
        raise NativeVerificationError("activated egress guard verification failed")
    guard = _safe_existing_path(Path(guard_text), "activated egress guard verification failed")
    policy = guard / "sitecustomize.py"
    try:
        guard_details = guard.lstat()
        children = {child.name for child in guard.iterdir()}
    except OSError as exc:
        raise NativeVerificationError("activated egress guard verification failed") from exc
    if (
        not stat.S_ISDIR(guard_details.st_mode)
        or children != {"sitecustomize.py"}
        or not _regular(policy)
        or (os.name == "posix" and stat.S_IMODE(guard_details.st_mode) != 0o700)
    ):
        raise NativeVerificationError("activated egress guard verification failed")

    activated = env.copy()
    activated["PYTHONPATH"] = str(site_packages) + os.pathsep + str(guard)
    activated["PYTHONNOUSERSITE"] = "1"
    return activated


def _prepare_data_root(data: Path) -> Path:
    """Create the verifier-owned installer root with private POSIX permissions."""
    data.mkdir(mode=0o700)
    data.chmod(0o700)
    bin_directory = data / "bin"
    bin_directory.mkdir(mode=0o700)
    bin_directory.chmod(0o700)
    return bin_directory


def _run(
    command: list[str], *, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if result.returncode not in (0, 2):
        raise NativeVerificationError("native verification command failed")
    return result


def _activated_launcher(data: Path, version: str, platform: str) -> Path:
    """Resolve the exact activated launcher from strict installed state."""
    data = Path(data).absolute()
    current = _json(data / "current.json")
    try:
        state = InstallState.model_validate(_json(data / "state.json"))
    except Exception as exc:
        raise NativeVerificationError("activated runtime state verification failed") from exc
    expected = data / "versions" / version
    if (
        set(current) != {"schema_version", "active_version", "runtime_path"}
        or current.get("schema_version") != 1
        or current.get("active_version") != version
        or current.get("runtime_path") != str(expected)
        or state.active_version is None
        or str(state.active_version) != version
        or state.runtime_paths.get(state.active_version) != expected
    ):
        raise NativeVerificationError("activated runtime state verification failed")
    for path in (data, data / "versions", expected):
        try:
            details = path.lstat()
        except OSError as exc:
            raise NativeVerificationError("activated runtime path verification failed") from exc
        if path.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise NativeVerificationError("activated runtime path verification failed")
    try:
        expected.resolve(strict=True).relative_to(data.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise NativeVerificationError("activated runtime path verification failed") from exc
    launcher = expected / ("csaf.exe" if platform.startswith("windows-") else "csaf")
    if not _regular(launcher):
        raise NativeVerificationError("activated runtime launcher verification failed")
    if not platform.startswith("windows-"):
        mode = stat.S_IMODE(launcher.stat().st_mode)
        if not mode & stat.S_IXUSR or mode & 0o077:
            raise NativeVerificationError("activated runtime launcher verification failed")
    return launcher


def _verify_activated_runtime(
    *, data: Path, version: str, platform: str, env: dict[str, str], proof: Path
) -> dict[str, Any]:
    launcher = _activated_launcher(data, version, platform)
    activated_env = _activated_environment(launcher, env)
    try:
        proof.unlink(missing_ok=False)
    except OSError as exc:
        raise NativeVerificationError(
            "activated launcher egress proof verification failed"
        ) from exc
    import_probe = _run(
        [
            sys.executable,
            "-c",
            "import pathlib,sys,csaf; "
            "pathlib.Path(csaf.__file__).resolve(strict=True).relative_to("
            "pathlib.Path(sys.argv[1]).resolve(strict=True)); "
            "print('CSAF_RUNTIME_IMPORT_OK')",
            str(launcher.parent / "site-packages"),
        ],
        env=activated_env,
        timeout=30,
    )
    if import_probe.returncode != 0 or import_probe.stdout.strip() != "CSAF_RUNTIME_IMPORT_OK":
        raise NativeVerificationError("activated runtime import verification failed")
    help_result = _run([str(launcher), "--help"], env=activated_env, timeout=30)
    if help_result.returncode != 0:
        raise NativeVerificationError("activated launcher help smoke failed")
    try:
        if proof.read_text(encoding="utf-8") != "CSAF_EGRESS_GUARD_ACTIVE\n":
            raise NativeVerificationError("activated launcher did not load egress policy")
    except OSError as exc:
        raise NativeVerificationError("activated launcher did not load egress policy") from exc
    doctor = _run(
        [str(launcher), "--database", ":memory:", "setup", "doctor", "--json"],
        env=activated_env,
        timeout=180,
    )
    try:
        report = json.loads(doctor.stdout)
    except json.JSONDecodeError as exc:
        raise NativeVerificationError("native doctor JSON verification failed") from exc
    if doctor.returncode != 0 or report.get("status") != "ready":
        raise NativeVerificationError("native doctor did not report ready")
    return report


def verify_native_install(
    *,
    release_dir: Path,
    dependencies_path: Path,
    platform: str,
    uv_path: Path,
    officecli_path: Path,
    csaf_executable: Path,
) -> dict[str, Any]:
    """Run install and READY doctor using only verified local HTTPS assets."""
    release_dir = _safe_existing_path(Path(release_dir), "release directory verification failed")
    if not release_dir.is_dir() or platform not in PLATFORMS:
        raise NativeVerificationError("release directory verification failed")
    _verify_sums(release_dir)
    manifest = _json(release_dir / "csaf-release-manifest.json")
    dependencies = _json(Path(dependencies_path))
    if set(manifest) != {
        "schema_version",
        "version",
        "runtime",
        "codex_skill",
        "claude_plugin",
        "officecli",
    }:
        raise NativeVerificationError("release manifest verification failed")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version):
        raise NativeVerificationError("release manifest verification failed")
    if set(manifest["runtime"]) != set(PLATFORMS) or set(manifest["officecli"]["assets"]) != set(
        PLATFORMS
    ):
        raise NativeVerificationError("release manifest verification failed")
    runtime_record = manifest["runtime"][platform]
    codex_record = manifest["codex_skill"]
    claude_record = manifest["claude_plugin"]
    office_record = manifest["officecli"]["assets"][platform]
    uv_record = dependencies["uv"]["assets"][platform]
    runtime = _verified_asset(
        release_dir / _asset_filename(runtime_record),
        sha256=runtime_record["sha256"],
        size=runtime_record["size"],
        root=release_dir,
    )
    codex = _verified_asset(
        release_dir / _asset_filename(codex_record),
        sha256=codex_record["sha256"],
        size=codex_record["size"],
        root=release_dir,
    )
    claude = _verified_asset(
        release_dir / _asset_filename(claude_record),
        sha256=claude_record["sha256"],
        size=claude_record["size"],
        root=release_dir,
    )
    uv_archive = _verified_asset(uv_path, sha256=uv_record["sha256"], size=uv_record["size"])
    office = _verified_asset(
        officecli_path, sha256=office_record["sha256"], size=office_record["size"]
    )
    csaf = _safe_existing_path(Path(csaf_executable), "installed csaf launcher verification failed")
    if not _regular(csaf):
        raise NativeVerificationError("installed csaf launcher verification failed")
    with tempfile.TemporaryDirectory(prefix="csaf-native-ready-") as temporary:
        root = Path(temporary)
        data = root / "data"
        codex_home = root / "codex"
        served = root / "served"
        bin_directory = _prepare_data_root(data)
        codex_home.mkdir()
        served.mkdir()
        private_uv = bin_directory / ("uv.exe" if platform.startswith("windows-") else "uv")
        extracted_uv = _extract_uv(
            uv_archive, root / "uv-extracted", platform, str(uv_record["url"])
        )
        shutil.copy2(extracted_uv, private_uv)
        if not platform.startswith("windows-"):
            private_uv.chmod(0o700)
        approved = {
            "runtime.zip": runtime,
            "codex.zip": codex,
            "claude.zip": claude,
            "officecli.bin": office,
        }
        local_assets: dict[str, Path] = {}
        for name, source in approved.items():
            target = served / name
            shutil.copy2(source, target)
            local_assets[name] = target
        cert_path, key_path = _certificate(root)
        handler = type("BoundedAssetHandler", (_AssetHandler,), {"assets": local_assets})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"https://localhost:{server.server_port}"

            def record(name: str, path: Path) -> dict[str, object]:
                return {"url": f"{base}/{name}", "sha256": _hash(path), "size": path.stat().st_size}

            local_manifest = {
                "schema_version": 1,
                "version": version,
                "runtime": {
                    item: record("runtime.zip", local_assets["runtime.zip"]) for item in PLATFORMS
                },
                "codex_skill": record("codex.zip", local_assets["codex.zip"]),
                "claude_plugin": record("claude.zip", local_assets["claude.zip"]),
                "officecli": {
                    "version": manifest["officecli"]["version"],
                    "minimum_version": manifest["officecli"]["minimum_version"],
                    "assets": {
                        item: record("officecli.bin", local_assets["officecli.bin"])
                        for item in PLATFORMS
                    },
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_bytes(
                (json.dumps(local_manifest, indent=2, sort_keys=True) + "\n").encode()
            )
            guard = _write_egress_guard(root / "egress-policy")
            env = os.environ.copy()
            inherited_pythonpath = env.get("PYTHONPATH")
            env.update(
                {
                    "CSAF_DATA_ROOT": str(data),
                    "CODEX_HOME": str(codex_home),
                    "SSL_CERT_FILE": str(cert_path),
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9",
                    "NO_PROXY": "localhost,127.0.0.1",
                    "OFFICECLI_SKIP_UPDATE": "1",
                    "CSAF_INSTALLER_NETWORK_FORBIDDEN": "1",
                    "CSAF_EGRESS_GUARD_PROOF": str(root / "console-egress-proof"),
                    "PYTHONPATH": str(guard.parent)
                    + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
                }
            )
            probe = _run(
                [
                    sys.executable,
                    "-c",
                    "import os,socket\n"
                    "assert os.environ['CSAF_EGRESS_GUARD_ACTIVE'] == '1'\n"
                    "try:\n"
                    "    socket.socket().connect(('203.0.113.1', 443))\n"
                    "except PermissionError:\n"
                    "    print('CSAF_EGRESS_BLOCKED')\n"
                    "else:\n"
                    "    raise SystemExit('external socket unexpectedly allowed')\n",
                ],
                env=env,
                timeout=15,
            )
            if probe.returncode != 0 or probe.stdout.strip() != "CSAF_EGRESS_BLOCKED":
                raise NativeVerificationError("native egress policy verification failed")
            proof = root / "console-egress-proof"
            proof.unlink(missing_ok=False)
            install = _run(
                [
                    str(csaf),
                    "--database",
                    ":memory:",
                    "setup",
                    "install",
                    "--manifest",
                    str(manifest_path),
                    "--yes",
                    "--codex-only",
                ],
                env=env,
                timeout=300,
            )
            if install.returncode != 0:
                raise NativeVerificationError(
                    "native setup install did not become ready", diagnostic="setup-install"
                )
            if proof.read_text(encoding="utf-8") != "CSAF_EGRESS_GUARD_ACTIVE\n":
                raise NativeVerificationError("installed console did not load egress policy")
            installed_office = (
                data
                / "officecli"
                / manifest["officecli"]["version"]
                / ("officecli.exe" if platform.startswith("windows-") else "officecli")
            )
            if (
                not _regular(installed_office)
                or _hash(installed_office) != office_record["sha256"]
                or not (codex_home / "skills/csaf/SKILL.md").is_file()
            ):
                raise NativeVerificationError("native installed state verification failed")
            return _verify_activated_runtime(
                data=data,
                version=version,
                platform=platform,
                env=env,
                proof=proof,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--officecli", type=Path, required=True)
    parser.add_argument("--csaf", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = verify_native_install(
            release_dir=args.release_dir,
            dependencies_path=args.dependencies,
            platform=args.platform,
            uv_path=args.uv,
            officecli_path=args.officecli,
            csaf_executable=args.csaf,
        )
    except NativeVerificationError as exc:
        diagnostic = f" [{exc.diagnostic}]" if exc.diagnostic else ""
        print(f"native install verification failed{diagnostic}", file=sys.stderr)
        return 2
    except Exception:
        print("native install verification failed", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
