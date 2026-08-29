from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from evals.agent_gate_v1.host_tools import trusted_host_tool

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes


class ArtifactPathError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactSnapshot:
    path: Path
    content: bytes
    sha256: str
    device: int
    inode: int


@dataclass(frozen=True)
class ExternalArtifactDigest:
    """Streaming identity for a potentially large repository-external artifact."""

    path: Path
    sha256: str
    size: int
    device: int
    inode: int


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_SHARE_READ = 0x00000001
    _OPEN_EXISTING = 3
    _CREATE_NEW = 1
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_DISPOSITION_INFO_CLASS = 4

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def discover_repository_root(anchor: Path) -> Path:
    result = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(anchor.resolve()),
            "rev-parse",
            "--show-toplevel",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ArtifactPathError("cannot discover the repository root")
    root = Path(result.stdout.strip()).resolve(strict=True)
    expected = Path(__file__).resolve().parents[3]
    if root != expected:
        raise ArtifactPathError("Git root disagrees with the evaluator source location")
    return root


def require_canonical_data_root(data_root: Path, repository_root: Path) -> Path:
    expected = (
        repository_root / "backend" / "eval_data" / "trip_text_cards_v1"
    ).resolve(strict=True)
    resolved = data_root.resolve(strict=True)
    if resolved != expected:
        raise ArtifactPathError("G01 agent evaluation requires the canonical governed data root")
    return resolved


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _linked_worktree_roots(repository_root: Path) -> list[Path]:
    result = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(repository_root.resolve(strict=True)),
            "worktree",
            "list",
            "--porcelain",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ArtifactPathError("cannot enumerate repository worktrees")
    roots: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            roots.append(Path(line.removeprefix("worktree ")).resolve(strict=True))
        except OSError as exc:
            raise ArtifactPathError("repository worktree boundary is unstable") from exc
    if not roots:
        raise ArtifactPathError("repository has no discoverable worktree boundary")
    return roots


def _reject_git_managed_location(path: Path, repository_root: Path) -> None:
    """Reject artifacts in this repository, linked worktrees, or any Git tree."""

    absolute = _safe_absolute(path)
    probe = absolute if absolute.exists() else absolute.parent
    try:
        resolved = probe.resolve(strict=True)
    except OSError as exc:
        raise ArtifactPathError("artifact has no stable repository boundary") from exc
    if any(is_within(resolved, root) for root in _linked_worktree_roots(repository_root)):
        raise ArtifactPathError("raw artifacts must remain outside every linked worktree")
    git_probe = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(resolved if resolved.is_dir() else resolved.parent),
            "rev-parse",
            "--is-inside-work-tree",
            "--is-inside-git-dir",
            "--is-bare-repository",
            "--git-common-dir",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if git_probe.returncode == 0:
        raise ArtifactPathError(
            "raw artifacts must not be stored in a Git worktree or Git directory"
        )
    for ancestor in (resolved, *resolved.parents):
        try:
            marker_exists = (ancestor / ".git").exists()
        except OSError as exc:
            raise ArtifactPathError("cannot inspect external Git boundary") from exc
        if marker_exists:
            raise ArtifactPathError("raw artifacts must not be stored in any Git worktree")


def _safe_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _posix_final_path(descriptor: int) -> Path:
    """Resolve an open descriptor without re-walking caller-controlled ancestors."""

    for fd_root in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            value = os.readlink(fd_root / str(descriptor))
        except OSError:
            continue
        if value.endswith(" (deleted)"):
            raise ArtifactPathError("external artifact path was removed while open")
        resolved = Path(value)
        if not resolved.is_absolute():
            raise ArtifactPathError("external artifact descriptor path is not absolute")
        try:
            return resolved.resolve(strict=True)
        except OSError as exc:
            raise ArtifactPathError(
                "external artifact descriptor has no stable final path"
            ) from exc
    raise ArtifactPathError(
        "POSIX external artifact verification requires descriptor path readback"
    )


def _posix_open_parent(path: Path, repository_root: Path) -> tuple[int, str, Path]:
    """Walk every ancestor with openat/O_NOFOLLOW and keep the parent handle open."""

    if sys.platform == "win32":
        raise ArtifactPathError("POSIX path helper is unavailable on Windows")
    absolute = _safe_absolute(path)
    parts = absolute.parts
    if len(parts) < 2 or parts[0] != os.sep:
        raise ArtifactPathError("external artifact path must be absolute")
    name = parts[-1]
    if name in {"", ".", ".."}:
        raise ArtifactPathError("external artifact filename is invalid")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    descriptor = os.open(os.sep, directory_flags)
    try:
        for component in parts[1:-1]:
            if component in {"", ".", ".."}:
                raise ArtifactPathError("external artifact ancestor is invalid")
            flags = directory_flags
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        resolved_parent = _posix_final_path(descriptor)
        _reject_git_managed_location(resolved_parent, repository_root)
        return descriptor, name, resolved_parent / name
    except Exception:
        os.close(descriptor)
        raise


def _posix_open_existing(
    path: Path,
    repository_root: Path,
) -> tuple[int, Path]:
    parent_descriptor, name, _nominal = _posix_open_parent(path, repository_root)
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            raise ArtifactPathError("external artifact must be a regular file")
        if information.st_nlink != 1:
            raise ArtifactPathError("external artifact must not be a hard link")
        resolved = _posix_final_path(descriptor)
        _reject_git_managed_location(resolved, repository_root)
        return descriptor, resolved
    except Exception:
        os.close(descriptor)
        raise


def _posix_create_exclusive(
    path: Path,
    repository_root: Path,
) -> tuple[int, int, str, Path]:
    parent_descriptor, name, _nominal = _posix_open_parent(path, repository_root)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    except Exception:
        os.close(parent_descriptor)
        raise
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
            raise ArtifactPathError("external artifact output is not a private file")
        resolved = _posix_final_path(descriptor)
        _reject_git_managed_location(resolved, repository_root)
        return descriptor, parent_descriptor, name, resolved
    except Exception:
        try:
            os.unlink(name, dir_fd=parent_descriptor)
        finally:
            os.close(descriptor)
            os.close(parent_descriptor)
        raise


def _reject_reparse_components(path: Path) -> None:
    if sys.platform != "win32":
        return
    absolute = _safe_absolute(path)
    existing = absolute if absolute.exists() else absolute.parent
    while not existing.exists():
        if existing.parent == existing:
            raise ArtifactPathError("artifact path has no stable existing parent")
        existing = existing.parent
    current = existing
    while True:
        attributes = getattr(os.lstat(current), "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ArtifactPathError("artifact path contains a Windows reparse point")
        if current.parent == current:
            break
        current = current.parent


def _windows_error(message: str) -> ArtifactPathError:
    return ArtifactPathError(f"{message}: winerror={ctypes.get_last_error()}")


def _windows_final_path(handle: int) -> Path:
    size = _kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if size == 0:
        raise _windows_error("cannot resolve artifact handle")
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = _kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise _windows_error("cannot read artifact handle path")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _windows_file_information(handle: int) -> _ByHandleFileInformation:
    information = _ByHandleFileInformation()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise _windows_error("cannot inspect artifact handle")
    if information.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ArtifactPathError("external artifact must not be a reparse point")
    if information.nNumberOfLinks != 1:
        raise ArtifactPathError("external artifact must not be a hard link")
    return information


def _windows_mark_delete(handle: int) -> None:
    disposition = _FileDispositionInfo(True)
    if not _kernel32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise _windows_error("cannot delete failed external artifact")


def _windows_open(path: Path, *, access: int, share: int, creation: int, flags: int) -> int:
    handle = _kernel32.CreateFileW(
        str(_safe_absolute(path)),
        access,
        share,
        None,
        creation,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _windows_error("cannot open external artifact")
    return int(handle)


def _assert_single_link(resolved: Path) -> None:
    """Reject hard-linked evidence without walking the potentially huge worktree.

    A file that aliases any repository path necessarily has more than one hard
    link.  Rejecting every multi-link artifact is deliberately stricter and
    avoids an unbounded, race-prone scan of tracked, ignored and untracked
    files.  Symlinks and junctions are handled by ``Path.resolve`` before this
    check.
    """

    if resolved.stat().st_nlink != 1:
        raise ArtifactPathError("external artifact must not be a hard link")


def require_external_existing(path: Path, repository_root: Path) -> Path:
    _reject_reparse_components(path)
    resolved = path.resolve(strict=True)
    _reject_git_managed_location(resolved, repository_root)
    _assert_single_link(resolved)
    return resolved


def read_external_snapshot(path: Path, repository_root: Path) -> ArtifactSnapshot:
    if sys.platform == "win32":
        _reject_reparse_components(path)
        handle = _windows_open(
            path,
            access=_GENERIC_READ,
            share=_FILE_SHARE_READ,
            creation=_OPEN_EXISTING,
            flags=_FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            resolved = _windows_final_path(handle)
            _reject_git_managed_location(resolved, repository_root)
            information = _windows_file_information(handle)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = _INVALID_HANDLE_VALUE
            with os.fdopen(descriptor, "rb", buffering=0) as stream:
                before = os.fstat(stream.fileno())
                content = stream.read()
                after = os.fstat(stream.fileno())
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != len(content)
                or after.st_size != len(content)
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ArtifactPathError("external artifact changed while it was being read")
            inode = (int(information.nFileIndexHigh) << 32) | int(
                information.nFileIndexLow
            )
            return ArtifactSnapshot(
                path=resolved,
                content=content,
                sha256=sha256(content).hexdigest(),
                device=int(information.dwVolumeSerialNumber),
                inode=inode,
            )
        finally:
            if handle != _INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(handle)
    descriptor, resolved = _posix_open_existing(path, repository_root)
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        before = os.fstat(handle.fileno())
        content = handle.read()
        after = os.fstat(handle.fileno())
        final_path = _posix_final_path(handle.fileno())
        _reject_git_managed_location(final_path, repository_root)
    before_identity = (before.st_dev, before.st_ino)
    if (
        before_identity != (after.st_dev, after.st_ino)
        or final_path != resolved
        or before.st_size != len(content)
        or after.st_size != len(content)
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_nlink != 1
        or after.st_nlink != 1
    ):
        raise ArtifactPathError("external artifact changed while it was being read")
    return ArtifactSnapshot(
        path=resolved,
        content=content,
        sha256=sha256(content).hexdigest(),
        device=before.st_dev,
        inode=before.st_ino,
    )


def hash_external_file_snapshot(
    path: Path,
    repository_root: Path,
) -> ExternalArtifactDigest:
    """Hash a large external artifact without loading it into process memory."""

    if sys.platform == "win32":
        _reject_reparse_components(path)
        handle = _windows_open(
            path,
            access=_GENERIC_READ,
            share=_FILE_SHARE_READ,
            creation=_OPEN_EXISTING,
            flags=_FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            resolved = _windows_final_path(handle)
            _reject_git_managed_location(resolved, repository_root)
            information = _windows_file_information(handle)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = _INVALID_HANDLE_VALUE
            digest = sha256()
            size = 0
            with os.fdopen(descriptor, "rb", buffering=0) as stream:
                before = os.fstat(stream.fileno())
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                after = os.fstat(stream.fileno())
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != size
                or after.st_size != size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ArtifactPathError("external artifact changed while it was being hashed")
            inode = (int(information.nFileIndexHigh) << 32) | int(
                information.nFileIndexLow
            )
            return ExternalArtifactDigest(
                path=resolved,
                sha256=digest.hexdigest(),
                size=size,
                device=int(information.dwVolumeSerialNumber),
                inode=inode,
            )
        finally:
            if handle != _INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(handle)
    descriptor, resolved = _posix_open_existing(path, repository_root)
    digest = sha256()
    size = 0
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        before = os.fstat(handle.fileno())
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(handle.fileno())
        final_path = _posix_final_path(handle.fileno())
        _reject_git_managed_location(final_path, repository_root)
    before_identity = (before.st_dev, before.st_ino)
    if (
        before_identity != (after.st_dev, after.st_ino)
        or final_path != resolved
        or before.st_size != size
        or after.st_size != size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_nlink != 1
        or after.st_nlink != 1
    ):
        raise ArtifactPathError("external artifact changed while it was being hashed")
    return ExternalArtifactDigest(
        path=resolved,
        sha256=digest.hexdigest(),
        size=size,
        device=before.st_dev,
        inode=before.st_ino,
    )


def copy_external_file_to_stream_verified(
    path: Path,
    destination: BinaryIO,
    repository_root: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> ExternalArtifactDigest:
    """Copy one stable external handle into an already-open private stream."""

    digest = sha256()
    size = 0

    def clear_destination() -> None:
        destination.seek(0)
        destination.truncate(0)
        destination.flush()

    def copy_stream(source: BinaryIO) -> None:
        nonlocal size
        try:
            before_destination = os.fstat(destination.fileno())
            if before_destination.st_size != 0:
                raise ArtifactPathError("private verification snapshot is not empty")
            destination.seek(0)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = destination.write(view)
                    if written is None or written <= 0:
                        raise ArtifactPathError(
                            "private verification snapshot write made no progress"
                        )
                    view = view[written:]
            destination.flush()
            os.fsync(destination.fileno())
            if os.fstat(destination.fileno()).st_size != size:
                raise ArtifactPathError(
                    "private verification snapshot size changed during write"
                )
            destination.seek(0)
        except Exception:
            try:
                clear_destination()
            except OSError:
                pass
            raise

    result: ExternalArtifactDigest
    if sys.platform == "win32":
        _reject_reparse_components(path)
        handle = _windows_open(
            path,
            access=_GENERIC_READ,
            share=_FILE_SHARE_READ,
            creation=_OPEN_EXISTING,
            flags=_FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            resolved = _windows_final_path(handle)
            _reject_git_managed_location(resolved, repository_root)
            information = _windows_file_information(handle)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = _INVALID_HANDLE_VALUE
            with os.fdopen(descriptor, "rb", buffering=0) as source:
                before = os.fstat(source.fileno())
                copy_stream(source)
                after = os.fstat(source.fileno())
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != size
                or after.st_size != size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                clear_destination()
                raise ArtifactPathError(
                    "external artifact changed while the verifier copied it"
                )
            inode = (int(information.nFileIndexHigh) << 32) | int(
                information.nFileIndexLow
            )
            result = ExternalArtifactDigest(
                path=resolved,
                sha256=digest.hexdigest(),
                size=size,
                device=int(information.dwVolumeSerialNumber),
                inode=inode,
            )
        finally:
            if handle != _INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(handle)
    else:
        descriptor, resolved = _posix_open_existing(path, repository_root)
        with os.fdopen(descriptor, "rb", buffering=0) as source:
            before = os.fstat(source.fileno())
            copy_stream(source)
            after = os.fstat(source.fileno())
            final_path = _posix_final_path(source.fileno())
            _reject_git_managed_location(final_path, repository_root)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or final_path != resolved
            or before.st_size != size
            or after.st_size != size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_nlink != 1
            or after.st_nlink != 1
        ):
            clear_destination()
            raise ArtifactPathError(
                "external artifact changed while the verifier copied it"
            )
        result = ExternalArtifactDigest(
            path=resolved,
            sha256=digest.hexdigest(),
            size=size,
            device=before.st_dev,
            inode=before.st_ino,
        )
    if result.sha256 != expected_sha256 or result.size != expected_size:
        clear_destination()
        raise ArtifactPathError("external artifact binding mismatch")
    destination.seek(0)
    return result


def copy_external_file_verified(
    path: Path,
    destination: Path,
    repository_root: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> ExternalArtifactDigest:
    """Copy one stable external file handle into a private verifier path."""

    target = _safe_absolute(destination)
    if target.exists() or not target.parent.exists():
        raise ArtifactPathError("private verification snapshot target is invalid")
    try:
        with target.open("xb+", buffering=0) as output:
            return copy_external_file_to_stream_verified(
                path,
                output,
                repository_root,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
    except Exception:
        target.unlink(missing_ok=True)
        raise


def publish_external_stream_exclusive(
    path: Path,
    source: BinaryIO,
    repository_root: Path,
) -> ExternalArtifactDigest:
    """Publish a private stream to one exclusive, handle-validated external file."""

    target = (
        require_external_target(path, repository_root)
        if sys.platform == "win32"
        else _safe_absolute(path)
    )
    digest = sha256()
    size = 0
    source.seek(0)
    source_before = os.fstat(source.fileno())

    def copy_to(output: BinaryIO) -> None:
        nonlocal size
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written_count = output.write(view)
                if written_count is None or written_count <= 0:
                    raise ArtifactPathError(
                        "external artifact stream write made no progress"
                    )
                view = view[written_count:]
        output.flush()
        os.fsync(output.fileno())

    if sys.platform == "win32":
        handle = _windows_open(
            target,
            access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
            share=0,
            creation=_CREATE_NEW,
            flags=_FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        committed = False
        try:
            resolved = _windows_final_path(handle)
            _reject_git_managed_location(resolved, repository_root)
            information = _windows_file_information(handle)
            if information.nNumberOfLinks != 1:
                raise ArtifactPathError("external artifact output has multiple links")
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            handle = _INVALID_HANDLE_VALUE
            output = os.fdopen(descriptor, "w+b", buffering=0)
            try:
                copy_to(output)
                written = os.fstat(output.fileno())
                source_after = os.fstat(source.fileno())
                if (
                    written.st_size != size
                    or source_before.st_size != size
                    or source_after.st_size != size
                    or (source_before.st_dev, source_before.st_ino)
                    != (source_after.st_dev, source_after.st_ino)
                    or source_before.st_mtime_ns != source_after.st_mtime_ns
                ):
                    raise ArtifactPathError(
                        "external artifact source or output changed during publish"
                    )
                committed = True
            except Exception:
                native_handle = msvcrt.get_osfhandle(output.fileno())
                _windows_mark_delete(native_handle)
                raise
            finally:
                output.close()
            inode = (int(information.nFileIndexHigh) << 32) | int(
                information.nFileIndexLow
            )
            return ExternalArtifactDigest(
                path=resolved,
                sha256=digest.hexdigest(),
                size=size,
                device=int(information.dwVolumeSerialNumber),
                inode=inode,
            )
        finally:
            if handle != _INVALID_HANDLE_VALUE:
                try:
                    if not committed:
                        _windows_mark_delete(handle)
                finally:
                    _kernel32.CloseHandle(handle)
    descriptor, parent_descriptor, name, resolved = _posix_create_exclusive(
        target,
        repository_root,
    )
    committed = False
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as output:
            copy_to(output)
            written = os.fstat(output.fileno())
            final_path = _posix_final_path(output.fileno())
            _reject_git_managed_location(final_path, repository_root)
            source_after = os.fstat(source.fileno())
            if (
                final_path != resolved
                or written.st_size != size
                or written.st_nlink != 1
                or source_before.st_size != size
                or source_after.st_size != size
                or (source_before.st_dev, source_before.st_ino)
                != (source_after.st_dev, source_after.st_ino)
                or source_before.st_mtime_ns != source_after.st_mtime_ns
            ):
                raise ArtifactPathError(
                    "external artifact source or output changed during publish"
                )
            committed = True
    except Exception:
        try:
            os.unlink(name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_descriptor)
    if not committed:
        raise ArtifactPathError("external artifact publish did not commit")
    return ExternalArtifactDigest(
        path=resolved,
        sha256=digest.hexdigest(),
        size=size,
        device=written.st_dev,
        inode=written.st_ino,
    )


def require_external_target(path: Path, repository_root: Path) -> Path:
    _reject_reparse_components(path)
    absolute = _safe_absolute(path)
    if not absolute.parent.exists():
        raise ArtifactPathError("evaluation output parent must already exist")
    resolved = absolute.parent.resolve(strict=True) / absolute.name
    if resolved.exists():
        raise ArtifactPathError("evaluation output target already exists")
    existing_parent = resolved.parent
    while not existing_parent.exists():
        parent = existing_parent.parent
        if parent == existing_parent:
            raise ArtifactPathError("evaluation output has no existing parent boundary")
        existing_parent = parent
    existing_parent = existing_parent.resolve(strict=True)
    _reject_git_managed_location(existing_parent, repository_root)
    return resolved


def write_external_bytes_exclusive(
    path: Path,
    content: bytes,
    repository_root: Path,
) -> ArtifactSnapshot:
    target = (
        require_external_target(path, repository_root)
        if sys.platform == "win32"
        else _safe_absolute(path)
    )
    if sys.platform == "win32":
        handle = _windows_open(
            target,
            access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
            share=0,
            creation=_CREATE_NEW,
            flags=_FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        committed = False
        try:
            resolved = _windows_final_path(handle)
            _reject_git_managed_location(resolved, repository_root)
            information = _windows_file_information(handle)
            descriptor = msvcrt.open_osfhandle(
                handle,
                os.O_RDWR | os.O_BINARY,
            )
            handle = _INVALID_HANDLE_VALUE
            stream = os.fdopen(descriptor, "w+b", buffering=0)
            try:
                view = memoryview(content)
                while view:
                    written_count = stream.write(view)
                    if written_count is None or written_count <= 0:
                        raise ArtifactPathError(
                            "evaluation output write made no progress"
                        )
                    view = view[written_count:]
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
                if written.st_size != len(content):
                    raise ArtifactPathError("evaluation output size changed during write")
                committed = True
            except Exception:
                native_handle = msvcrt.get_osfhandle(stream.fileno())
                _windows_mark_delete(native_handle)
                raise
            finally:
                stream.close()
            inode = (int(information.nFileIndexHigh) << 32) | int(
                information.nFileIndexLow
            )
            return ArtifactSnapshot(
                path=resolved,
                content=content,
                sha256=sha256(content).hexdigest(),
                device=int(information.dwVolumeSerialNumber),
                inode=inode,
            )
        finally:
            if handle != _INVALID_HANDLE_VALUE:
                try:
                    if not committed:
                        _windows_mark_delete(handle)
                finally:
                    _kernel32.CloseHandle(handle)
    descriptor, parent_descriptor, name, resolved = _posix_create_exclusive(
        target,
        repository_root,
    )
    committed = False
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as handle:
            view = memoryview(content)
            while view:
                written_count = handle.write(view)
                if written_count is None or written_count <= 0:
                    raise ArtifactPathError("evaluation output write made no progress")
                view = view[written_count:]
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
            final_path = _posix_final_path(handle.fileno())
            _reject_git_managed_location(final_path, repository_root)
            if (
                final_path != resolved
                or written.st_size != len(content)
                or written.st_nlink != 1
            ):
                raise ArtifactPathError("evaluation output identity changed during write")
            committed = True
    except Exception:
        try:
            os.unlink(name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_descriptor)
    if not committed:
        raise ArtifactPathError("evaluation output write did not commit")
    return ArtifactSnapshot(
        path=resolved,
        content=content,
        sha256=sha256(content).hexdigest(),
        device=written.st_dev,
        inode=written.st_ino,
    )
