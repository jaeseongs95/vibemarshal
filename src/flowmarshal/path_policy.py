from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import re
import stat
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .canonical import sha256_digest


_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_RESERVED_DOS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_INVALID_COMPONENT_CHARS = re.compile(r'[<>"|?*]')


class PathDecisionCode(StrEnum):
    ALLOW = "ALLOW"
    PATH_NUL = "PATH_NUL"
    UNSUPPORTED_PATH_CLASS = "UNSUPPORTED_PATH_CLASS"
    PATH_NOT_ABSOLUTE = "PATH_NOT_ABSOLUTE"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    PATH_ADS = "PATH_ADS"
    PATH_TRAILING_DOT_SPACE = "PATH_TRAILING_DOT_SPACE"
    PATH_RESERVED_NAME = "PATH_RESERVED_NAME"
    PATH_INVALID_CHARACTER = "PATH_INVALID_CHARACTER"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    PATH_REPARSE_POINT = "PATH_REPARSE_POINT"
    PATH_HARDLINK = "PATH_HARDLINK"
    PATH_OUTSIDE_ROOT = "PATH_OUTSIDE_ROOT"
    PATH_IDENTITY_CONFLICT = "PATH_IDENTITY_CONFLICT"
    PATH_IDENTITY_DRIFT = "PATH_IDENTITY_DRIFT"
    PATH_BOUNDARY_UNCERTAIN = "PATH_BOUNDARY_UNCERTAIN"


class PathPolicyError(ValueError):
    def __init__(self, reason_code: PathDecisionCode, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class FrozenPathModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class PathIdentity(FrozenPathModel):
    canonical_path: str
    final_path: str
    volume_serial: str
    file_id: str
    is_directory: bool
    link_count: int = Field(ge=1)
    size: int = Field(ge=0)
    modified_100ns: int = Field(ge=0)
    attributes: int = Field(ge=0)
    streams: tuple[str, ...] = ()

    @property
    def object_key(self) -> tuple[str, str]:
        return self.volume_serial, self.file_id


class ManifestEntry(FrozenPathModel):
    relative_path: str
    identity: PathIdentity
    content_digest: str | None = None


class PathInspection(FrozenPathModel):
    requested_path: str
    lexical_path: str
    root_identity: PathIdentity
    entries: tuple[ManifestEntry, ...]
    manifest_digest: str

    @property
    def snapshot_digest(self) -> str:
        return sha256_digest(
            {
                "lexical_path": self.lexical_path,
                "root_identity": self.root_identity,
                "entries": self.entries,
                "manifest_digest": self.manifest_digest,
            }
        )


class BoundaryDecision(FrozenPathModel):
    allowed: bool
    reason_code: PathDecisionCode
    message: str
    inspection: PathInspection | None = None


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("volume_serial_number", ctypes.c_uint64), ("file_id", _FILE_ID_128)]


class _WIN32_FIND_STREAM_DATA(ctypes.Structure):
    _fields_ = [("stream_size", ctypes.c_int64), ("stream_name", ctypes.c_wchar * 296)]


def lexical_windows_path(value: str | os.PathLike[str]) -> str:
    """사용자 입력을 정규화하기 전에 Windows 경로 우회 문법을 거절한다."""

    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise PathPolicyError(
            PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            "경로를 문자열로 해석할 수 없습니다.",
        )
    if "\x00" in raw:
        raise PathPolicyError(PathDecisionCode.PATH_NUL, "경로에 NUL이 있습니다.")
    forward_normalized = raw.replace("/", "\\")
    lowered = forward_normalized.casefold()
    if (
        lowered.startswith("\\\\.\\")
        or lowered.startswith("\\\\?\\")
        or lowered.startswith("\\??\\")
        or "\\globalroot\\" in lowered
    ):
        raise PathPolicyError(
            PathDecisionCode.UNSUPPORTED_PATH_CLASS,
            "device 또는 NT namespace 경로는 지원하지 않습니다.",
        )
    if forward_normalized.startswith("\\\\"):
        raise PathPolicyError(
            PathDecisionCode.UNSUPPORTED_PATH_CLASS,
            "Gate 0C에서는 UNC 경로를 지원하지 않습니다.",
        )
    components = forward_normalized.split("\\")
    if ".." in components:
        raise PathPolicyError(
            PathDecisionCode.PATH_TRAVERSAL,
            "정규화 전 경로에 상위 이동(..)이 있습니다.",
        )
    drive, tail = ntpath.splitdrive(forward_normalized)
    if re.fullmatch(r"[A-Za-z]:", drive) is None or not tail.startswith("\\"):
        raise PathPolicyError(
            PathDecisionCode.PATH_NOT_ABSOLUTE,
            "일반 local drive 절대 경로만 지원합니다.",
        )
    if ":" in tail:
        raise PathPolicyError(
            PathDecisionCode.PATH_ADS,
            "drive letter 외 colon 또는 ADS 문법은 허용하지 않습니다.",
        )
    for component in components[1:]:
        if not component or component == ".":
            continue
        if component.endswith((".", " ")):
            raise PathPolicyError(
                PathDecisionCode.PATH_TRAILING_DOT_SPACE,
                "경로 구성요소 끝의 점 또는 공백은 허용하지 않습니다.",
            )
        if _INVALID_COMPONENT_CHARS.search(component):
            raise PathPolicyError(
                PathDecisionCode.PATH_INVALID_CHARACTER,
                "Windows 경로에서 허용되지 않는 문자가 있습니다.",
            )
        stem = component.split(".", 1)[0].upper()
        if stem in _RESERVED_DOS_NAMES:
            raise PathPolicyError(
                PathDecisionCode.PATH_RESERVED_NAME,
                f"예약된 DOS 이름({stem})은 허용하지 않습니다.",
            )
    normalized = ntpath.normpath(forward_normalized)
    return drive[0].upper() + normalized[1:]


def _require_windows() -> None:
    if os.name != "nt":
        raise PathPolicyError(
            PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            "handle 기반 경로 검사는 native Windows에서만 지원합니다.",
        )


def _extended(path: str | Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\" + value


def _kernel32() -> ctypes.WinDLL:
    _require_windows()
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _file_attributes(path: Path) -> int:
    kernel32 = _kernel32()
    function = kernel32.GetFileAttributesW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_uint32
    attributes = int(function(_extended(path)))
    if attributes == _INVALID_FILE_ATTRIBUTES:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise PathPolicyError(
                PathDecisionCode.PATH_NOT_FOUND,
                f"경로가 존재하지 않습니다: {path}",
            )
        raise PathPolicyError(
            PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            f"파일 속성을 읽지 못했습니다(WinError {error}).",
        )
    return attributes


def _assert_no_reparse_ancestor(path: Path) -> None:
    pure = PureWindowsPath(str(path))
    current = Path(pure.anchor)
    for component in pure.parts[1:]:
        current = current / component
        attributes = _file_attributes(current)
        if attributes & _REPARSE_ATTRIBUTE:
            raise PathPolicyError(
                PathDecisionCode.PATH_REPARSE_POINT,
                f"reparse point 경로는 허용하지 않습니다: {current}",
            )


def _strip_extended_prefix(value: str) -> str:
    if value.casefold().startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _stream_names(path: Path, *, is_directory: bool) -> tuple[str, ...]:
    kernel32 = _kernel32()
    first = kernel32.FindFirstStreamW
    first.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(_WIN32_FIND_STREAM_DATA), ctypes.c_uint32]
    first.restype = ctypes.c_void_p
    next_stream = kernel32.FindNextStreamW
    next_stream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WIN32_FIND_STREAM_DATA)]
    next_stream.restype = ctypes.c_int
    close = kernel32.FindClose
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int

    data = _WIN32_FIND_STREAM_DATA()
    handle = first(_extended(path), 0, ctypes.byref(data), 0)
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if is_directory and error in {2, 38}:
            return ()
        raise PathPolicyError(
            PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            f"ADS stream을 열거하지 못했습니다(WinError {error}).",
        )
    names: list[str] = []
    try:
        names.append(str(data.stream_name))
        while next_stream(handle, ctypes.byref(data)):
            names.append(str(data.stream_name))
        error = ctypes.get_last_error()
        if error not in {0, 38}:
            raise PathPolicyError(
                PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                f"ADS stream 열거가 불완전합니다(WinError {error}).",
            )
    finally:
        close(handle)
    return tuple(sorted(names, key=str.casefold))


def _identity_from_handle(path: Path) -> PathIdentity:
    kernel32 = _kernel32()
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    close = kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    get_basic = kernel32.GetFileInformationByHandle
    get_basic.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_basic.restype = ctypes.c_int
    get_extended = kernel32.GetFileInformationByHandleEx
    get_extended.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    get_extended.restype = ctypes.c_int
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final.restype = ctypes.c_uint32

    handle = create(
        _extended(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x1 | 0x2 | 0x4,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise PathPolicyError(PathDecisionCode.PATH_NOT_FOUND, "경로가 사라졌습니다.")
        raise PathPolicyError(
            PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            f"경로 handle을 열지 못했습니다(WinError {error}).",
        )
    try:
        basic = _BY_HANDLE_FILE_INFORMATION()
        if not get_basic(handle, ctypes.byref(basic)):
            raise PathPolicyError(
                PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                f"handle 기본 정보를 읽지 못했습니다(WinError {ctypes.get_last_error()}).",
            )
        extended = _FILE_ID_INFO()
        # FileIdInfo = 18. 구형 파일시스템이면 64-bit index를 결정적 fallback으로 사용한다.
        if get_extended(handle, 18, ctypes.byref(extended), ctypes.sizeof(extended)):
            volume_serial = f"{extended.volume_serial_number:016x}"
            file_id = bytes(extended.file_id.identifier).hex()
        else:
            error = ctypes.get_last_error()
            if error not in {1, 50, 87}:
                raise PathPolicyError(
                    PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                    f"FileIdInfo를 읽지 못했습니다(WinError {error}).",
                )
            volume_serial = f"{basic.dwVolumeSerialNumber:08x}"
            file_id = f"{basic.nFileIndexHigh:08x}{basic.nFileIndexLow:08x}"
        needed = int(get_final(handle, None, 0, 0))
        if needed <= 0:
            raise PathPolicyError(
                PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                f"최종 경로 길이를 읽지 못했습니다(WinError {ctypes.get_last_error()}).",
            )
        buffer = ctypes.create_unicode_buffer(needed + 1)
        copied = int(get_final(handle, buffer, len(buffer), 0))
        if copied <= 0 or copied >= len(buffer):
            raise PathPolicyError(
                PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                "최종 handle 경로를 안정적으로 읽지 못했습니다.",
            )
        final_path = _strip_extended_prefix(buffer.value)
        is_directory = bool(basic.dwFileAttributes & _DIRECTORY_ATTRIBUTE)
        streams = _stream_names(path, is_directory=is_directory)
        non_default_streams = [name for name in streams if name != "::$DATA"]
        if non_default_streams:
            raise PathPolicyError(
                PathDecisionCode.PATH_ADS,
                f"기본 data stream 외 ADS가 있습니다: {non_default_streams}",
            )
        modified = (basic.ftLastWriteTime.dwHighDateTime << 32) | basic.ftLastWriteTime.dwLowDateTime
        size = (basic.nFileSizeHigh << 32) | basic.nFileSizeLow
        return PathIdentity(
            canonical_path=ntpath.normcase(ntpath.normpath(final_path)),
            final_path=final_path,
            volume_serial=volume_serial,
            file_id=file_id,
            is_directory=is_directory,
            link_count=int(basic.nNumberOfLinks),
            size=size,
            modified_100ns=modified,
            attributes=int(basic.dwFileAttributes),
            streams=streams,
        )
    finally:
        close(handle)


def _inspect_identity(path: Path) -> PathIdentity:
    _assert_no_reparse_ancestor(path)
    identity = _identity_from_handle(path)
    if not identity.is_directory and identity.link_count > 1:
        raise PathPolicyError(
            PathDecisionCode.PATH_HARDLINK,
            f"hardlink count가 1보다 큽니다: {identity.link_count}",
        )
    return identity


def _hash_file_stably(path: Path, before: PathIdentity) -> str:
    digest = hashlib.sha256()
    try:
        with open(_extended(path), "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise PathPolicyError(
            PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            f"manifest 내용을 읽지 못했습니다: {error}",
        ) from error
    after = _inspect_identity(path)
    if before.object_key != after.object_key or before.modified_100ns != after.modified_100ns or before.size != after.size:
        raise PathPolicyError(
            PathDecisionCode.PATH_IDENTITY_DRIFT,
            "manifest 계산 중 파일 identity 또는 내용 메타데이터가 바뀌었습니다.",
        )
    return "sha256:" + digest.hexdigest()


def _is_within(root: PathIdentity, target: PathIdentity) -> bool:
    try:
        common = ntpath.commonpath([root.canonical_path, target.canonical_path])
    except ValueError:
        return False
    return ntpath.normcase(common) == root.canonical_path


def inspect_resource(
    value: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str] | None = None,
    max_entries: int = 10_000,
) -> PathInspection:
    _require_windows()
    requested = os.fspath(value)
    lexical = lexical_windows_path(requested)
    root_path = Path(lexical)
    root_identity = _inspect_identity(root_path)
    if allowed_root is not None:
        allowed_lexical = lexical_windows_path(allowed_root)
        allowed_identity = _inspect_identity(Path(allowed_lexical))
        if not allowed_identity.is_directory or not _is_within(allowed_identity, root_identity):
            raise PathPolicyError(
                PathDecisionCode.PATH_OUTSIDE_ROOT,
                "handle 기반 최종 경로가 허용 root 밖에 있습니다.",
            )

    entries: list[ManifestEntry] = []
    seen_objects: dict[tuple[str, str], str] = {root_identity.object_key: "."}

    def add_entry(path: Path, relative: str) -> None:
        if len(entries) >= max_entries:
            raise PathPolicyError(
                PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                f"manifest 항목 수가 제한({max_entries})을 넘었습니다.",
            )
        identity = _inspect_identity(path)
        if not _is_within(root_identity, identity):
            raise PathPolicyError(
                PathDecisionCode.PATH_OUTSIDE_ROOT,
                f"하위 항목의 최종 경로가 resource 밖입니다: {relative}",
            )
        previous = seen_objects.get(identity.object_key)
        if previous is not None:
            raise PathPolicyError(
                PathDecisionCode.PATH_IDENTITY_CONFLICT,
                f"resource 안에서 동일 object identity가 중복됩니다: {previous}, {relative}",
            )
        seen_objects[identity.object_key] = relative
        content_digest = None if identity.is_directory else _hash_file_stably(path, identity)
        entries.append(
            ManifestEntry(
                relative_path=relative.replace("\\", "/"),
                identity=identity,
                content_digest=content_digest,
            )
        )

    if root_identity.is_directory:
        pending = [root_path]
        while pending:
            directory = pending.pop()
            try:
                children = sorted(
                    os.scandir(_extended(directory)),
                    key=lambda item: item.name.casefold(),
                )
            except OSError as error:
                raise PathPolicyError(
                    PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
                    f"directory manifest를 열거하지 못했습니다: {error}",
                ) from error
            for child in children:
                # DirEntry.path에는 내부용 \\?\ prefix가 남을 수 있으므로 외부
                # 계약에 노출하지 않고 원래 local-drive 표기로 다시 조립한다.
                child_path = directory / child.name
                relative = ntpath.relpath(str(child_path), lexical)
                add_entry(child_path, relative)
                if entries[-1].identity.is_directory:
                    pending.append(child_path)

    sorted_entries = tuple(sorted(entries, key=lambda item: item.relative_path.casefold()))
    manifest_digest = sha256_digest(
        {
            "root": root_identity,
            "entries": sorted_entries,
        }
    )
    return PathInspection(
        requested_path=requested,
        lexical_path=lexical,
        root_identity=root_identity,
        entries=sorted_entries,
        manifest_digest=manifest_digest,
    )


def decide_resource(
    value: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str] | None = None,
    max_entries: int = 10_000,
) -> BoundaryDecision:
    try:
        inspection = inspect_resource(
            value,
            allowed_root=allowed_root,
            max_entries=max_entries,
        )
        return BoundaryDecision(
            allowed=True,
            reason_code=PathDecisionCode.ALLOW,
            message="lexical, reparse, handle identity, ADS, hardlink와 manifest 검사를 통과했습니다.",
            inspection=inspection,
        )
    except PathPolicyError as error:
        return BoundaryDecision(
            allowed=False,
            reason_code=error.reason_code,
            message=str(error),
        )
    except (OSError, ValueError) as error:
        return BoundaryDecision(
            allowed=False,
            reason_code=PathDecisionCode.PATH_BOUNDARY_UNCERTAIN,
            message=f"경계를 결정적으로 확인하지 못했습니다: {error}",
        )


def assert_distinct_resources(inspections: Iterable[PathInspection]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for inspection in inspections:
        identities = (inspection.root_identity,) + tuple(
            entry.identity for entry in inspection.entries
        )
        for identity in identities:
            previous = seen.get(identity.object_key)
            if previous is not None:
                raise PathPolicyError(
                    PathDecisionCode.PATH_IDENTITY_CONFLICT,
                    f"서로 다른 resource가 같은 file identity를 공유합니다: {previous}, {identity.final_path}",
                )
            seen[identity.object_key] = identity.final_path


def revalidate_unchanged(previous: PathInspection) -> PathInspection:
    current = inspect_resource(previous.lexical_path)
    if (
        previous.root_identity.object_key != current.root_identity.object_key
        or previous.snapshot_digest != current.snapshot_digest
    ):
        raise PathPolicyError(
            PathDecisionCode.PATH_IDENTITY_DRIFT,
            "사전 검사 뒤 resource identity 또는 manifest가 바뀌었습니다.",
        )
    return current
