from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from evals.agent_gate_v1.host_tools import TrustedHostToolError, trusted_host_tool
from evals.agent_gate_v1.path_security import (
    ArtifactPathError,
    ExternalArtifactDigest,
    copy_external_file_to_stream_verified,
    publish_external_stream_exclusive,
    require_external_target,
)


class AutomationIsolationError(ValueError):
    pass


_RUNNER_SHALLOW_BASELINE_COMMIT = "7bdd1a6abd9c10c6076aca67f08de785027501a0"


@dataclass(frozen=True)
class IsolatedCheckResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    runner_image_id: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _docker_environment() -> dict[str, str]:
    """Project only the values needed by the Docker client, never Gate secrets."""

    allowed = {
        "COMSPEC",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _run_docker(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [trusted_host_tool("docker"), *args],
            cwd=cwd,
            check=False,
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_docker_environment(),
        )
    except (FileNotFoundError, TrustedHostToolError) as exc:
        raise AutomationIsolationError(
            "formal Agent Gate requires an available OCI/Docker runner"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AutomationIsolationError("isolated automation runner timed out") from exc


def _archive_member_name(name: str) -> str:
    if not name or "\\" in name:
        raise AutomationIsolationError("automation image archive has an unsafe member")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AutomationIsolationError("automation image archive has an unsafe member")
    return path.as_posix().rstrip("/")


def _read_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    maximum_size: int,
) -> bytes:
    if not member.isfile() or member.size < 0 or member.size > maximum_size:
        raise AutomationIsolationError("automation image archive metadata is invalid")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise AutomationIsolationError("automation image archive member is unreadable")
    content = extracted.read(maximum_size + 1)
    if len(content) != member.size or len(content) > maximum_size:
        raise AutomationIsolationError("automation image archive member size is invalid")
    return content


def _hash_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
) -> str:
    if not member.isfile() or member.size < 0:
        raise AutomationIsolationError("automation image archive blob is invalid")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise AutomationIsolationError("automation image archive blob is unreadable")
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    if size != member.size:
        raise AutomationIsolationError("automation image archive blob size is invalid")
    return digest.hexdigest()


def _validate_docker_image_archive(
    stream: BinaryIO,
    *,
    expected_image_id: str,
) -> None:
    """Reject multi-image, wrong-ID, linked, duplicate, or traversing archives."""

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id):
        raise AutomationIsolationError("automation image ID is invalid")
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > 10000:
                raise AutomationIsolationError(
                    "automation image archive member count is invalid"
                )
            by_name: dict[str, tarfile.TarInfo] = {}
            for member in members:
                normalized = _archive_member_name(member.name)
                if normalized in by_name:
                    raise AutomationIsolationError(
                        "automation image archive contains duplicate members"
                    )
                if not (member.isfile() or member.isdir()):
                    raise AutomationIsolationError(
                        "automation image archive contains links or special files"
                    )
                by_name[normalized] = member
            manifest_member = by_name.get("manifest.json")
            if manifest_member is None:
                raise AutomationIsolationError(
                    "automation image archive has no manifest"
                )
            try:
                manifest = json.loads(
                    _read_archive_member(
                        archive,
                        manifest_member,
                        maximum_size=1024 * 1024,
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AutomationIsolationError(
                    "automation image archive manifest is invalid"
                ) from exc
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise AutomationIsolationError(
                    "automation image archive must contain exactly one image"
                )
            image = manifest[0]
            if not isinstance(image, dict):
                raise AutomationIsolationError(
                    "automation image archive manifest entry is invalid"
                )
            config_name = image.get("Config")
            layers = image.get("Layers")
            repo_tags = image.get("RepoTags")
            if (
                not isinstance(config_name, str)
                or not isinstance(layers, list)
                or any(not isinstance(item, str) for item in layers)
                or len(set(layers)) != len(layers)
                or repo_tags not in (None, [])
            ):
                raise AutomationIsolationError(
                    "automation image archive is not bound to the expected image"
                )
            normalized_config = _archive_member_name(config_name)
            normalized_layers = [_archive_member_name(item) for item in layers]
            config_member = by_name.get(normalized_config)
            if config_member is None:
                raise AutomationIsolationError(
                    "automation image archive configuration is missing"
                )
            config = _read_archive_member(
                archive,
                config_member,
                maximum_size=16 * 1024 * 1024,
            )
            config_digest = hashlib.sha256(config).hexdigest()
            config_path = PurePosixPath(normalized_config)
            if (
                config_path.parts == (f"{config_digest}.json",)
                or config_path.parts == ("blobs", "sha256", config_digest)
            ) is False:
                raise AutomationIsolationError(
                    "automation image archive configuration digest mismatch"
                )
            allowed_files = {"manifest.json", normalized_config, *normalized_layers}
            allowed_directories: set[str] = set()
            for layer_name in normalized_layers:
                layer = PurePosixPath(layer_name)
                layer_member = by_name.get(layer_name)
                if layer_member is None or not layer_member.isfile():
                    raise AutomationIsolationError(
                        "automation image archive layer is missing"
                    )
                parent = layer.parent
                if parent != PurePosixPath("."):
                    allowed_files.update(
                        {
                            (parent / "json").as_posix(),
                            (parent / "VERSION").as_posix(),
                        }
                    )
                for ancestor in parent.parents:
                    if ancestor != PurePosixPath("."):
                        allowed_directories.add(ancestor.as_posix())
                if parent != PurePosixPath("."):
                    allowed_directories.add(parent.as_posix())
            if "repositories" in by_name:
                raise AutomationIsolationError(
                    "automation image archive contains an unexpected tag index"
                )

            oci_files = {"index.json", "oci-layout"}
            has_oci_layout = oci_files.issubset(by_name)
            if bool(oci_files & set(by_name)) != has_oci_layout:
                raise AutomationIsolationError(
                    "automation image archive has an incomplete OCI layout"
                )
            if has_oci_layout:
                try:
                    layout = json.loads(
                        _read_archive_member(
                            archive,
                            by_name["oci-layout"],
                            maximum_size=4096,
                        )
                    )
                    oci_index = json.loads(
                        _read_archive_member(
                            archive,
                            by_name["index.json"],
                            maximum_size=1024 * 1024,
                        )
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AutomationIsolationError(
                        "automation image archive OCI metadata is invalid"
                    ) from exc
                if layout != {"imageLayoutVersion": "1.0.0"}:
                    raise AutomationIsolationError(
                        "automation image archive OCI layout version is invalid"
                    )
                root_descriptors = oci_index.get("manifests") if isinstance(
                    oci_index, dict
                ) else None
                forbidden_tag_annotations = {
                    "io.containerd.image.name",
                    "org.opencontainers.image.ref.name",
                }

                def has_forbidden_tag_annotation(value: object) -> bool:
                    if not isinstance(value, dict):
                        return value is not None
                    return bool(forbidden_tag_annotations & set(value))

                def has_invalid_annotations(value: dict[str, object]) -> bool:
                    return "annotations" in value and not isinstance(
                        value["annotations"],
                        dict,
                    )

                if (
                    not isinstance(oci_index, dict)
                    or oci_index.get("schemaVersion") != 2
                    or not isinstance(root_descriptors, list)
                    or len(root_descriptors) != 1
                    or not isinstance(root_descriptors[0], dict)
                    or root_descriptors[0].get("mediaType")
                    != "application/vnd.oci.image.index.v1+json"
                    or has_invalid_annotations(oci_index)
                    or has_invalid_annotations(root_descriptors[0])
                    or has_forbidden_tag_annotation(oci_index.get("annotations"))
                    or has_forbidden_tag_annotation(
                        root_descriptors[0].get("annotations")
                    )
                ):
                    raise AutomationIsolationError(
                        "automation image archive OCI index is not single-image"
                    )

                referenced_blobs: set[str] = set()

                def descriptor_blob(
                    descriptor: object,
                    *,
                    maximum_json_size: int | None = None,
                ) -> tuple[str, bytes | None]:
                    if not isinstance(descriptor, dict):
                        raise AutomationIsolationError(
                            "automation image archive OCI descriptor is invalid"
                        )
                    digest_value = descriptor.get("digest")
                    descriptor_size = descriptor.get("size")
                    if (
                        not isinstance(digest_value, str)
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value)
                        or not isinstance(descriptor_size, int)
                        or descriptor_size < 0
                    ):
                        raise AutomationIsolationError(
                            "automation image archive OCI descriptor binding is invalid"
                        )
                    digest_hex = digest_value.removeprefix("sha256:")
                    blob_name = f"blobs/sha256/{digest_hex}"
                    blob = by_name.get(blob_name)
                    if (
                        blob is None
                        or not blob.isfile()
                        or blob.size != descriptor_size
                        or _hash_archive_member(archive, blob) != digest_hex
                    ):
                        raise AutomationIsolationError(
                            "automation image archive OCI blob binding mismatch"
                        )
                    referenced_blobs.add(blob_name)
                    content = None
                    if maximum_json_size is not None:
                        content = _read_archive_member(
                            archive,
                            blob,
                            maximum_size=maximum_json_size,
                        )
                    return digest_value, content

                root_digest, root_content = descriptor_blob(
                    root_descriptors[0],
                    maximum_json_size=4 * 1024 * 1024,
                )
                if root_digest != expected_image_id or root_content is None:
                    raise AutomationIsolationError(
                        "automation image archive OCI root ID mismatch"
                    )
                try:
                    root_index = json.loads(root_content)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AutomationIsolationError(
                        "automation image archive OCI root is invalid"
                    ) from exc
                image_descriptors = root_index.get("manifests") if isinstance(
                    root_index, dict
                ) else None
                if (
                    not isinstance(root_index, dict)
                    or root_index.get("schemaVersion") != 2
                    or root_index.get("mediaType")
                    != "application/vnd.oci.image.index.v1+json"
                    or not isinstance(image_descriptors, list)
                    or not image_descriptors
                    or any(not isinstance(item, dict) for item in image_descriptors)
                    or any(has_invalid_annotations(item) for item in image_descriptors)
                    or any(
                        has_forbidden_tag_annotation(item.get("annotations"))
                        for item in image_descriptors
                    )
                ):
                    raise AutomationIsolationError(
                        "automation image archive OCI root index is invalid"
                    )
                primary_descriptors = [
                    item
                    for item in image_descriptors
                    if not (
                        isinstance(item, dict)
                        and isinstance(item.get("annotations"), dict)
                        and item["annotations"].get("vnd.docker.reference.type")
                        == "attestation-manifest"
                    )
                ]
                if len(primary_descriptors) != 1:
                    raise AutomationIsolationError(
                        "automation image archive OCI root contains extra images"
                    )
                if primary_descriptors[0].get("mediaType") != (
                    "application/vnd.oci.image.manifest.v1+json"
                ):
                    raise AutomationIsolationError(
                        "automation image archive primary manifest type is invalid"
                    )
                primary_digest = primary_descriptors[0].get("digest")
                primary_config_blob: str | None = None
                primary_layer_blobs: list[str] | None = None
                for descriptor in image_descriptors:
                    annotations = descriptor.get("annotations", {})
                    is_attestation = annotations.get(
                        "vnd.docker.reference.type"
                    ) == "attestation-manifest"
                    if descriptor.get("mediaType") != (
                        "application/vnd.oci.image.manifest.v1+json"
                    ):
                        raise AutomationIsolationError(
                            "automation image archive manifest descriptor type is invalid"
                        )
                    platform = descriptor.get("platform")
                    if is_attestation:
                        if (
                            annotations.get("vnd.docker.reference.digest")
                            != primary_digest
                            or platform
                            != {"architecture": "unknown", "os": "unknown"}
                        ):
                            raise AutomationIsolationError(
                                "automation image archive attestation target mismatch"
                            )
                    elif (
                        descriptor is not primary_descriptors[0]
                        or not isinstance(platform, dict)
                        or not isinstance(platform.get("architecture"), str)
                        or not isinstance(platform.get("os"), str)
                        or platform.get("architecture") == "unknown"
                        or platform.get("os") == "unknown"
                    ):
                        raise AutomationIsolationError(
                            "automation image archive primary platform is invalid"
                        )
                    _manifest_digest, manifest_content = descriptor_blob(
                        descriptor,
                        maximum_json_size=4 * 1024 * 1024,
                    )
                    try:
                        oci_manifest = json.loads(manifest_content or b"")
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise AutomationIsolationError(
                            "automation image archive OCI manifest is invalid"
                        ) from exc
                    oci_layers = oci_manifest.get("layers") if isinstance(
                        oci_manifest, dict
                    ) else None
                    if (
                        not isinstance(oci_manifest, dict)
                        or oci_manifest.get("schemaVersion") != 2
                        or oci_manifest.get("mediaType")
                        != "application/vnd.oci.image.manifest.v1+json"
                        or not isinstance(oci_manifest.get("config"), dict)
                        or not isinstance(oci_layers, list)
                    ):
                        raise AutomationIsolationError(
                            "automation image archive OCI manifest structure is invalid"
                        )
                    config_descriptor = oci_manifest["config"]
                    if config_descriptor.get("mediaType") != (
                        "application/vnd.oci.image.config.v1+json"
                    ):
                        raise AutomationIsolationError(
                            "automation image archive OCI config type is invalid"
                        )
                    config_digest_value, config_content = descriptor_blob(
                        config_descriptor,
                        maximum_json_size=16 * 1024 * 1024,
                    )
                    try:
                        oci_config = json.loads(config_content or b"")
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise AutomationIsolationError(
                            "automation image archive OCI config is invalid"
                        ) from exc
                    layer_blob_paths: list[str] = []
                    for layer_descriptor in oci_layers:
                        layer_digest, _layer_content = descriptor_blob(
                            layer_descriptor
                        )
                        layer_blob_paths.append(
                            f"blobs/sha256/{layer_digest.removeprefix('sha256:')}"
                        )
                        media_type = layer_descriptor.get("mediaType")
                        layer_annotations = layer_descriptor.get("annotations")
                        if is_attestation:
                            if (
                                media_type != "application/vnd.in-toto+json"
                                or not isinstance(layer_annotations, dict)
                                or not isinstance(
                                    layer_annotations.get(
                                        "in-toto.io/predicate-type"
                                    ),
                                    str,
                                )
                                or not layer_annotations[
                                    "in-toto.io/predicate-type"
                                ]
                            ):
                                raise AutomationIsolationError(
                                    "automation image archive attestation layer is invalid"
                                )
                        elif not isinstance(media_type, str) or re.fullmatch(
                            r"application/vnd\.oci\.image\.layer\.v1\.tar"
                            r"(?:\+gzip|\+zstd)?",
                            media_type,
                        ) is None:
                            raise AutomationIsolationError(
                                "automation image archive primary layer type is invalid"
                            )
                    rootfs = oci_config.get("rootfs") if isinstance(
                        oci_config, dict
                    ) else None
                    diff_ids = rootfs.get("diff_ids") if isinstance(
                        rootfs, dict
                    ) else None
                    if (
                        not isinstance(oci_config, dict)
                        or not isinstance(rootfs, dict)
                        or rootfs.get("type") != "layers"
                        or not isinstance(diff_ids, list)
                        or len(diff_ids) != len(oci_layers)
                        or any(
                            not isinstance(item, str)
                            or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                            for item in diff_ids
                        )
                    ):
                        raise AutomationIsolationError(
                            "automation image archive OCI layer bindings are invalid"
                        )
                    if is_attestation:
                        if (
                            oci_config.get("architecture") != "unknown"
                            or oci_config.get("os") != "unknown"
                            or oci_config.get("config") != {}
                            or diff_ids
                            != [
                                layer.get("digest")
                                for layer in oci_layers
                            ]
                        ):
                            raise AutomationIsolationError(
                                "automation image archive attestation config is invalid"
                            )
                    else:
                        if (
                            oci_config.get("architecture")
                            != platform.get("architecture")
                            or oci_config.get("os") != platform.get("os")
                        ):
                            raise AutomationIsolationError(
                                "automation image archive primary config platform mismatch"
                            )
                        primary_config_blob = (
                            f"blobs/sha256/"
                            f"{config_digest_value.removeprefix('sha256:')}"
                        )
                        primary_layer_blobs = layer_blob_paths
                if (
                    primary_config_blob is None
                    or primary_layer_blobs is None
                    or normalized_config != primary_config_blob
                    or normalized_layers != primary_layer_blobs
                ):
                    raise AutomationIsolationError(
                        "automation image archive legacy and OCI graphs disagree"
                    )
                allowed_files.update({"index.json", "oci-layout", *referenced_blobs})
                allowed_directories.update({"blobs", "blobs/sha256"})
            elif normalized_config != (
                f"{expected_image_id.removeprefix('sha256:')}.json"
            ):
                raise AutomationIsolationError(
                    "legacy automation image archive configuration ID mismatch"
                )
            else:
                try:
                    legacy_config = json.loads(config)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AutomationIsolationError(
                        "legacy automation image configuration is invalid"
                    ) from exc
                rootfs = legacy_config.get("rootfs") if isinstance(
                    legacy_config, dict
                ) else None
                diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
                if (
                    not isinstance(rootfs, dict)
                    or rootfs.get("type") != "layers"
                    or not isinstance(diff_ids, list)
                    or len(diff_ids) != len(normalized_layers)
                    or any(
                        not isinstance(item, str)
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", item)
                        for item in diff_ids
                    )
                ):
                    raise AutomationIsolationError(
                        "legacy automation image layer bindings are invalid"
                    )
                for layer_name, diff_id in zip(
                    normalized_layers,
                    diff_ids,
                    strict=True,
                ):
                    layer_member = by_name[layer_name]
                    if _hash_archive_member(archive, layer_member) != diff_id.removeprefix(
                        "sha256:"
                    ):
                        raise AutomationIsolationError(
                            "legacy automation image layer digest mismatch"
                        )
            for name, member in by_name.items():
                if member.isfile() and name not in allowed_files:
                    raise AutomationIsolationError(
                        "automation image archive contains unreferenced files"
                    )
                if member.isdir() and name not in allowed_directories:
                    raise AutomationIsolationError(
                        "automation image archive contains unreferenced directories"
                    )
    except tarfile.TarError as exc:
        raise AutomationIsolationError(
            "automation image archive is not a valid Docker tar"
        ) from exc
    finally:
        stream.seek(0)


def _materialize_candidate_git_context(
    *,
    repository_root: Path,
    candidate_commit: str,
    candidate_tree: str,
    destination: Path,
    shallow_baseline_commit: str = _RUNNER_SHALLOW_BASELINE_COMMIT,
) -> str:
    """Export the governed shallow history plus exact baseline/candidate trees."""

    git = trusted_host_tool("git")
    baseline_parent = subprocess.run(
        [
            git,
            "-C",
            str(repository_root),
            "rev-parse",
            "--verify",
            f"{shallow_baseline_commit}^",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    ancestry_args = [
        git,
        "-C",
        str(repository_root),
        "rev-list",
        "--objects",
        "--no-object-names",
        "--filter=blob:none",
        candidate_commit,
    ]
    if baseline_parent.returncode == 0:
        ancestry_args.append(f"^{baseline_parent.stdout.strip()}")
    ancestry = subprocess.run(
        ancestry_args,
        check=False,
        capture_output=True,
        text=True,
    )
    baseline_tree = subprocess.run(
        [
            git,
            "-C",
            str(repository_root),
            "rev-list",
            "--objects",
            "--no-object-names",
            f"{shallow_baseline_commit}^{{tree}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode != 0 or baseline_tree.returncode != 0:
        raise AutomationIsolationError("candidate Git tree export failed")
    object_ids = sorted(
        set(ancestry.stdout.splitlines()) | set(baseline_tree.stdout.splitlines())
    )
    candidate_tree_objects = subprocess.run(
        [
            git,
            "-C",
            str(repository_root),
            "rev-list",
            "--objects",
            "--no-object-names",
            f"{candidate_commit}^{{tree}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if candidate_tree_objects.returncode != 0:
        raise AutomationIsolationError("candidate Git candidate-tree export failed")
    object_ids = sorted(set(object_ids) | set(candidate_tree_objects.stdout.splitlines()))
    if candidate_tree not in object_ids:
        raise AutomationIsolationError("candidate Git tree export omitted the root tree")
    if shallow_baseline_commit not in object_ids:
        raise AutomationIsolationError("candidate Git export omitted the governed baseline")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    pack_path = destination / "candidate.pack"
    with pack_path.open("xb") as pack_handle:
        packed = subprocess.run(
            [git, "-C", str(repository_root), "pack-objects", "--stdout"],
            input=("\n".join([candidate_commit, *object_ids]) + "\n").encode("ascii"),
            stdout=pack_handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    if packed.returncode != 0 or pack_path.stat().st_size == 0:
        raise AutomationIsolationError("candidate Git object pack export failed")
    (destination / "candidate.commit").write_text(
        f"{candidate_commit}\n", encoding="ascii", newline="\n"
    )
    (destination / "candidate.tree").write_text(
        f"{candidate_tree}\n", encoding="ascii", newline="\n"
    )
    (destination / "candidate.shallow").write_text(
        f"{shallow_baseline_commit}\n",
        encoding="ascii",
        newline="\n",
    )
    source_root = destination / "source"
    initialized = subprocess.run(
        [git, "init", "--quiet", str(source_root)],
        check=False,
        capture_output=True,
    )
    if initialized.returncode != 0:
        raise AutomationIsolationError("candidate Git source checkout init failed")
    with pack_path.open("rb") as pack_handle:
        indexed = subprocess.run(
            [git, "-C", str(source_root), "index-pack", "--stdin"],
            stdin=pack_handle,
            check=False,
            capture_output=True,
        )
    if indexed.returncode != 0:
        raise AutomationIsolationError("candidate Git source checkout pack failed")
    (source_root / ".git" / "shallow").write_text(
        f"{shallow_baseline_commit}\n",
        encoding="ascii",
        newline="\n",
    )
    checkout_commands = (
        [git, "-C", str(source_root), "config", "core.autocrlf", "false"],
        [git, "-C", str(source_root), "config", "core.eol", "lf"],
        [git, "-C", str(source_root), "update-ref", "HEAD", candidate_commit],
        [git, "-C", str(source_root), "reset", "--hard", candidate_commit],
    )
    for command in checkout_commands:
        result = subprocess.run(command, check=False, capture_output=True)
        if result.returncode != 0:
            raise AutomationIsolationError("candidate Git source checkout failed")
    recovered = subprocess.run(
        [git, "-C", str(source_root), "show", "-s", "--format=%H:%T", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if recovered.returncode != 0 or recovered.stdout.strip() != (
        f"{candidate_commit}:{candidate_tree}"
    ):
        raise AutomationIsolationError("candidate Git source checkout identity mismatch")
    git_metadata = (source_root / ".git").resolve(strict=True)
    if git_metadata.parent != source_root.resolve(strict=True):
        raise AutomationIsolationError("candidate Git source metadata escaped checkout")
    def _remove_readonly(func, path, _exc_info) -> None:
        os.chmod(path, 0o700)
        func(path)

    shutil.rmtree(git_metadata, onerror=_remove_readonly)
    return _sha256_file(pack_path)


def build_isolated_candidate_image(
    *,
    repository_root: Path,
    candidate_commit: str,
    candidate_tree: str,
    recipe_path: str,
    recipe_sha256: str,
    entrypoint_path: str,
    entrypoint_sha256: str,
    context_policy_path: str,
    context_policy_sha256: str,
) -> tuple[str, str]:
    root = repository_root.resolve(strict=True)
    runner_assets = (
        ("recipe", recipe_path, recipe_sha256),
        ("entrypoint", entrypoint_path, entrypoint_sha256),
        ("context policy", context_policy_path, context_policy_sha256),
    )
    asset_bytes: dict[str, bytes] = {}
    for label, relative_path, expected_sha256 in runner_assets:
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise AutomationIsolationError(
                f"automation runner {label} escaped the checkout"
            )
        blob = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(root),
                "show",
                f"{candidate_commit}:{relative.as_posix()}",
            ],
            check=False,
            capture_output=True,
        )
        if (
            blob.returncode != 0
            or hashlib.sha256(blob.stdout).hexdigest() != expected_sha256
        ):
            raise AutomationIsolationError(
                f"automation runner {label} hash mismatch"
            )
        asset_bytes[label] = blob.stdout
    status = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
        capture_output=True,
    )
    if status.returncode != 0 or status.stdout:
        raise AutomationIsolationError("automation image must be built from a clean checkout")
    head = subprocess.run(
        [trusted_host_tool("git"), "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    tree = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(root),
            "show",
            "-s",
            "--format=%T",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if (
        head.returncode != 0
        or tree.returncode != 0
        or head.stdout.strip() != candidate_commit
        or tree.stdout.strip() != candidate_tree
    ):
        raise AutomationIsolationError("automation checkout is not the frozen candidate")
    tag = f"breezetravel-agent-gate:{candidate_tree[:20]}"
    with tempfile.TemporaryDirectory(prefix="breezetravel-candidate-git-") as temp_root:
        temporary_root = Path(temp_root)
        git_context = temporary_root / "candidate-git"
        pack_sha256 = _materialize_candidate_git_context(
            repository_root=root,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            destination=git_context,
        )
        materialized_entrypoint = git_context / "source" / Path(entrypoint_path)
        if (
            not materialized_entrypoint.is_file()
            or materialized_entrypoint.read_bytes() != asset_bytes["entrypoint"]
        ):
            raise AutomationIsolationError(
                "materialized automation entrypoint differs from the frozen candidate"
            )
        empty_context = temporary_root / "empty-context"
        empty_context.mkdir(mode=0o700)
        context_policy = empty_context / ".dockerignore"
        with context_policy.open("xb+", buffering=0) as policy_stream:
            policy_stream.write(asset_bytes["context policy"])
            policy_stream.flush()
            os.fsync(policy_stream.fileno())
            policy_stream.seek(0)
            if policy_stream.read() != asset_bytes["context policy"]:
                raise AutomationIsolationError(
                    "private automation context policy readback failed"
                )
        with tempfile.TemporaryFile(mode="w+b") as recipe_stream:
            recipe_stream.write(asset_bytes["recipe"])
            recipe_stream.flush()
            os.fsync(recipe_stream.fileno())
            recipe_stream.seek(0)
            if recipe_stream.read() != asset_bytes["recipe"]:
                raise AutomationIsolationError(
                    "private automation recipe readback failed"
                )
            recipe_stream.seek(0)
            build = _run_docker(
                [
                    "build",
                    "--file",
                    "-",
                    "--build-context",
                    f"candidate_git={git_context}",
                    "--label",
                    f"io.breezetravel.candidate.commit={candidate_commit}",
                    "--label",
                    f"io.breezetravel.candidate.tree={candidate_tree}",
                    "--label",
                    f"io.breezetravel.candidate.pack.sha256={pack_sha256}",
                    "--label",
                    f"io.breezetravel.runner.recipe.sha256={recipe_sha256}",
                    "--label",
                    f"io.breezetravel.runner.entrypoint.sha256={entrypoint_sha256}",
                    "--label",
                    f"io.breezetravel.runner.context-policy.sha256={context_policy_sha256}",
                    "--tag",
                    tag,
                    str(empty_context),
                ],
                cwd=root,
                timeout=3600,
                stdin=recipe_stream,
            )
    if build.returncode != 0:
        raise AutomationIsolationError("candidate automation image build failed")
    inspect = _run_docker(
        ["image", "inspect", "--format={{json .}}", tag],
        cwd=root,
        timeout=60,
    )
    if inspect.returncode != 0:
        raise AutomationIsolationError("candidate automation image readback failed")
    try:
        metadata = json.loads(inspect.stdout)
        image_id = str(metadata["Id"])
        labels = metadata["Config"]["Labels"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AutomationIsolationError("candidate automation image metadata is invalid") from exc
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise AutomationIsolationError("candidate automation image has no content digest")
    expected_labels = {
        "io.breezetravel.candidate.commit": candidate_commit,
        "io.breezetravel.candidate.tree": candidate_tree,
        "io.breezetravel.candidate.pack.sha256": pack_sha256,
        "io.breezetravel.runner.recipe.sha256": recipe_sha256,
        "io.breezetravel.runner.entrypoint.sha256": entrypoint_sha256,
        "io.breezetravel.runner.context-policy.sha256": context_policy_sha256,
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise AutomationIsolationError("candidate automation image labels are not bound")
    return tag, image_id


def run_isolated_check(
    *,
    repository_root: Path,
    expected_image_id: str,
    workdir: str,
    argv: list[str],
    timeout_seconds: int,
) -> IsolatedCheckResult:
    root = repository_root.resolve(strict=True)
    inspect = _run_docker(
        ["image", "inspect", "--format={{.Id}}", expected_image_id],
        cwd=root,
        timeout=60,
    )
    if inspect.returncode != 0 or inspect.stdout.decode().strip() != expected_image_id:
        raise AutomationIsolationError("automation runner image changed before execution")
    result = _run_docker(
        [
            "run",
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=512",
            "--memory=8g",
            "--cpus=4",
            "--env=HOME=/tmp/breezetravel-agent-gate-home",
            "--env=XDG_CONFIG_HOME=/tmp/breezetravel-agent-gate-home/.config",
            "--env=NPM_CONFIG_USERCONFIG=/tmp/breezetravel-agent-gate-home/.npmrc",
            "--env=PYTHONNOUSERSITE=1",
            f"--workdir=/workspace/{workdir}",
            expected_image_id,
            *argv,
        ],
        cwd=root,
        timeout=timeout_seconds,
    )
    return IsolatedCheckResult(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        runner_image_id=expected_image_id,
    )


def save_isolated_candidate_image(
    *,
    repository_root: Path,
    image_tag: str,
    expected_image_id: str,
    archive_output: Path,
) -> ExternalArtifactDigest:
    """Save the content-addressed image and publish its validated archive."""

    root = repository_root.resolve(strict=True)
    require_external_target(archive_output, root)
    inspect = _run_docker(
        ["image", "inspect", "--format={{.Id}}", image_tag],
        cwd=root,
        timeout=60,
    )
    if inspect.returncode != 0 or inspect.stdout.decode().strip() != expected_image_id:
        raise AutomationIsolationError(
            "automation image tag differs from the content-addressed image"
        )
    try:
        with tempfile.TemporaryFile(mode="w+b") as private_archive:
            result = _run_docker(
                ["image", "save", expected_image_id],
                cwd=root,
                timeout=3600,
                stdout=private_archive,
            )
            if result.returncode != 0:
                raise AutomationIsolationError(
                    "automation image archive export failed"
                )
            private_archive.flush()
            os.fsync(private_archive.fileno())
            if os.fstat(private_archive.fileno()).st_size <= 0:
                raise AutomationIsolationError("automation image archive is empty")
            _validate_docker_image_archive(
                private_archive,
                expected_image_id=expected_image_id,
            )
            return publish_external_stream_exclusive(
                archive_output,
                private_archive,
                root,
            )
    except (OSError, ArtifactPathError) as exc:
        raise AutomationIsolationError("automation image archive publication failed") from exc


def ensure_isolated_candidate_image(
    *,
    repository_root: Path,
    expected_image_id: str,
    image_archive_path: Path,
    image_archive_sha256: str,
    image_archive_size: int,
) -> str:
    """Load the exact signed archive through a private immutable read snapshot."""

    root = repository_root.resolve(strict=True)
    with tempfile.TemporaryFile(mode="w+b") as private_archive:
        try:
            copy_external_file_to_stream_verified(
                image_archive_path,
                private_archive,
                root,
                expected_sha256=image_archive_sha256,
                expected_size=image_archive_size,
            )
        except ArtifactPathError as exc:
            raise AutomationIsolationError(
                "automation image archive binding mismatch"
            ) from exc
        _validate_docker_image_archive(
            private_archive,
            expected_image_id=expected_image_id,
        )
        private_archive.seek(0)
        loaded = _run_docker(
            ["image", "load"],
            cwd=root,
            timeout=3600,
            stdin=private_archive,
        )
        if loaded.returncode != 0:
            raise AutomationIsolationError(
                "signed automation image archive failed to load"
            )
        loaded_id_lines = [
            line.strip()
            for line in loaded.stdout.decode("utf-8", errors="replace").splitlines()
            if line.startswith("Loaded image ID:")
        ]
        if loaded_id_lines != [f"Loaded image ID: {expected_image_id}"]:
            raise AutomationIsolationError(
                "loaded automation archive reported a different image identity"
            )
        recovered = _run_docker(
            ["image", "inspect", "--format={{.Id}}", expected_image_id],
            cwd=root,
            timeout=60,
        )
        if (
            recovered.returncode != 0
            or recovered.stdout.decode().strip() != expected_image_id
        ):
            raise AutomationIsolationError(
                "loaded automation image identity differs from the signed execution"
            )
    return expected_image_id
