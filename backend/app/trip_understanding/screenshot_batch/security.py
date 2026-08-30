from __future__ import annotations

import os
import stat
from pathlib import Path

from app.trip_understanding.screenshot_batch.errors import ScreenshotPathSecurityError


def require_node_local(path: Path) -> None:
    """Reject Windows network shares before pixels or cleanup journals touch them."""

    if os.name != "nt":
        return
    raw_path = os.fspath(path)
    if raw_path.startswith("\\\\"):
        raise ScreenshotPathSecurityError(
            "screenshot temporary storage must be node-local"
        )
    anchor = path.anchor
    if not anchor:
        return
    import ctypes
    from ctypes import wintypes

    get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    if get_drive_type(anchor) == 4:  # DRIVE_REMOTE
        raise ScreenshotPathSecurityError(
            "screenshot temporary storage must be node-local"
        )


def _raise_windows_error(operation: str, result: int | None = None) -> None:
    import ctypes

    if result:
        error = OSError(result, f"Windows security API returned {result}")
    else:
        error = ctypes.WinError(ctypes.get_last_error())
    raise ScreenshotPathSecurityError(operation) from error


def _windows_current_user_sid() -> tuple[object, object]:
    """Return the current process user SID and buffers retaining its lifetime."""

    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user = 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
        _raise_windows_error("current process token could not be opened")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(required))
        if required.value == 0:
            _raise_windows_error("current process user SID could not be sized")
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user,
            token_buffer,
            required,
            ctypes.byref(required),
        ):
            _raise_windows_error("current process user SID could not be read")
    finally:
        kernel32.CloseHandle(token)

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    token_user_value = ctypes.cast(token_buffer, ctypes.POINTER(_TokenUser)).contents
    return token_user_value.user.sid, token_buffer


def _secure_windows_owner_only(path: Path, *, is_directory: bool) -> None:
    import ctypes
    from ctypes import wintypes

    access_allowed_ace_type = 0
    acl_size_information = 2
    dacl_security_information = 0x00000004
    file_all_access = 0x001F01FF
    owner_security_information = 0x00000001
    protected_dacl_security_information = 0x80000000
    se_dacl_protected = 0x1000
    se_file_object = 1

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", wintypes.WORD),
        ]

    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("header", _AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid, sid_lifetime = _windows_current_user_sid()

    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    sid_string_pointer = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_string_pointer)):
        _raise_windows_error("current process user SID could not be serialized")
    try:
        sid_string = sid_string_pointer.value
    finally:
        kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.LocalFree(sid_string_pointer)

    inheritance = "OICI" if is_directory else ""
    sddl = f"D:P(A;{inheritance};FA;;;{sid_string})"
    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        _raise_windows_error("owner-only security descriptor could not be built")
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        advapi32.GetSecurityDescriptorDacl.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        )
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value or not dacl:
            _raise_windows_error("owner-only DACL could not be read")

        advapi32.SetNamedSecurityInfoW.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            se_file_object,
            owner_security_information
            | dacl_security_information
            | protected_dacl_security_information,
            sid,
            None,
            dacl,
            None,
        )
        if result:
            _raise_windows_error("owner-only Windows ACL could not be applied", result)
    finally:
        kernel32.LocalFree(descriptor)

    owner = wintypes.LPVOID()
    actual_dacl = wintypes.LPVOID()
    actual_descriptor = wintypes.LPVOID()
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        se_file_object,
        owner_security_information | dacl_security_information,
        ctypes.byref(owner),
        None,
        ctypes.byref(actual_dacl),
        None,
        ctypes.byref(actual_descriptor),
    )
    if result:
        _raise_windows_error("owner-only Windows ACL could not be verified", result)
    try:
        advapi32.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
        advapi32.EqualSid.restype = wintypes.BOOL
        if not advapi32.EqualSid(owner, sid):
            raise ScreenshotPathSecurityError("staged screenshot owner is not the process user")

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        advapi32.GetSecurityDescriptorControl.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        if not advapi32.GetSecurityDescriptorControl(
            actual_descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            _raise_windows_error("staged screenshot DACL protection could not be verified")
        if not control.value & se_dacl_protected:
            raise ScreenshotPathSecurityError("staged screenshot DACL still inherits permissions")

        acl_info = _AclSizeInformation()
        advapi32.GetAclInformation.argtypes = (
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        advapi32.GetAclInformation.restype = wintypes.BOOL
        if not advapi32.GetAclInformation(
            actual_dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            acl_size_information,
        ):
            _raise_windows_error("staged screenshot DACL entries could not be counted")
        if acl_info.ace_count != 1:
            raise ScreenshotPathSecurityError("staged screenshot DACL is not owner-only")

        ace_pointer = wintypes.LPVOID()
        advapi32.GetAce.argtypes = (
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
        )
        advapi32.GetAce.restype = wintypes.BOOL
        if not advapi32.GetAce(actual_dacl, 0, ctypes.byref(ace_pointer)):
            _raise_windows_error("staged screenshot DACL entry could not be read")
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
        ace_sid = wintypes.LPVOID(ace_pointer.value + _AccessAllowedAce.sid_start.offset)
        if (
            ace.header.ace_type != access_allowed_ace_type
            or ace.mask & file_all_access != file_all_access
            or not advapi32.EqualSid(ace_sid, sid)
        ):
            raise ScreenshotPathSecurityError("staged screenshot DACL grants another principal")
        if is_directory and not stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode):
            raise ScreenshotPathSecurityError("secured screenshot directory changed type")
        if not is_directory and not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise ScreenshotPathSecurityError("secured screenshot file changed type")
    finally:
        kernel32.LocalFree(actual_descriptor)
        del sid_lifetime


def secure_owner_only(path: Path, *, is_directory: bool) -> None:
    """Apply and verify an owner-only boundary; mode bits alone are not proof on Windows."""

    if path.is_symlink():
        raise ScreenshotPathSecurityError("staged screenshot path cannot be a symbolic link")
    if os.name == "nt":
        _secure_windows_owner_only(path, is_directory=is_directory)
        return

    desired_mode = 0o700 if is_directory else 0o600
    try:
        os.chmod(path, desired_mode, follow_symlinks=False)
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ScreenshotPathSecurityError("owner-only screenshot permissions could not be applied") from exc
    if stat.S_IMODE(observed.st_mode) != desired_mode:
        raise ScreenshotPathSecurityError("owner-only screenshot permissions could not be verified")
    if hasattr(os, "geteuid") and observed.st_uid != os.geteuid():
        raise ScreenshotPathSecurityError("staged screenshot owner is not the process user")
