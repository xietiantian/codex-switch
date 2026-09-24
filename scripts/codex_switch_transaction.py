from __future__ import annotations

import fcntl
import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Callable, Mapping

from codex_switch_constants import MANAGED_FILES, SwitchError
from codex_switch_io import atomic_write, ensure_private_dir, now_stamp, read_json, write_json
from codex_switch_protocol_adapter import (
    capability_receipt_path_for_launcher,
    prepare_capability_receipt_artifact,
)
from codex_switch_paths import resolve_internal_codex_bin
from codex_switch_restore import RestoreManifest, read_restore_manifest
from codex_switch_shim import render_codex_shim_payload
from codex_switch_store import Store
from codex_switch_toml_validate import validate_toml, validate_toml_text


@dataclass(frozen=True)
class TransactionRequest:
    operation: str
    profile: str
    options: Mapping[str, object]


@dataclass(frozen=True)
class TransactionReceipt:
    operation: str
    outcome: str
    preview_lines: tuple[str, ...]
    backup_id: str | None
    guidance_lines: tuple[str, ...] = tuple()


@dataclass(frozen=True)
class _FrozenSwitchInput:
    label: str
    path: Path
    before_state_json: bytes
    before_identity_json: bytes
    commit_state_json: bytes
    commit_replaces_identity: bool
    capture_kind: str = "path"


class _SwitchWrapperRenderStore:
    def __init__(
        self,
        store: Store,
        profile: str,
        planned_manifest: Mapping[str, object],
    ) -> None:
        self._store = store
        self._profile = profile
        self._planned_manifest = dict(planned_manifest)

    def __getattr__(self, name: str) -> object:
        return getattr(self._store, name)

    def load_manifest(self, name: str) -> dict[str, object]:
        if name == self._profile:
            return dict(self._planned_manifest)
        return dict(self._store.load_manifest(name))


def _required_capture_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int) or value == 0:
        raise SwitchError(f"Capture workspace requires os.{name}")
    return value


def _capture_file_open_flags(flags: int = os.O_RDONLY) -> int:
    return flags | _required_capture_open_flag("O_NOFOLLOW")


def _capture_directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | _required_capture_open_flag("O_DIRECTORY")
        | _required_capture_open_flag("O_NOFOLLOW")
    )


class FilesystemAdapter:
    def bind_capture_workspace(self, path: Path, descriptor: int) -> None:
        setattr(self, "_capture_workspace_path", path)
        setattr(self, "_capture_workspace_descriptor", descriptor)

    def unbind_capture_workspace(self) -> None:
        for name in ("_capture_workspace_path", "_capture_workspace_descriptor"):
            try:
                delattr(self, name)
            except AttributeError:
                pass

    def capture_workspace_bound(self, path: Path) -> bool:
        binding = _capture_binding_for_path(self, path)
        return binding is not None and binding[1] == tuple()

    def create_store_root(self, path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            return
        except FileNotFoundError as exc:
            raise SwitchError(
                f"Profile store parent does not exist: {path.parent}"
            ) from exc

    def capture_state(self, path: Path) -> dict[str, object]:
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            descriptor, relative = binding
            return _path_state_at(descriptor, relative, path)
        return _path_state(path)

    def capture_identity(self, path: Path) -> dict[str, object]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {"kind": "missing", "path": str(path)}
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        identity: dict[str, object] = {
            "kind": kind,
            "path": str(path),
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode),
        }
        if kind == "symlink":
            identity["symlink_target"] = os.readlink(path)
        return identity

    def bind_switch_effect(self, path: Path, parent_descriptor: int) -> None:
        if getattr(self, "_switch_effect_binding", None) is not None:
            raise SwitchError("Switch filesystem adapter already has a bound effect")
        setattr(
            self,
            "_switch_effect_binding",
            (str(path), parent_descriptor, path.name),
        )

    def _record_bound_switch_produced_identity(self, path: Path) -> None:
        binding = self._bound_switch_effect(path)
        if binding is None:
            raise SwitchError(f"Switch effect is not bound for identity: {path}")
        descriptor, leaf = binding
        setattr(
            self,
            "_switch_action_produced_identity",
            _node_identity_at(descriptor, leaf, path),
        )

    def bound_switch_produced_identity(
        self,
        path: Path,
    ) -> dict[str, object] | None:
        if self._bound_switch_effect(path) is None:
            raise SwitchError(f"Switch effect is not bound for identity: {path}")
        identity = getattr(self, "_switch_action_produced_identity", None)
        if identity is None:
            return None
        if not isinstance(identity, dict):
            raise SwitchError(
                f"Switch filesystem adapter has an invalid produced identity: {path}"
            )
        return dict(identity)

    def bind_switch_staged_artifact(
        self,
        artifact: Mapping[str, object] | None,
    ) -> None:
        if getattr(self, "_switch_staged_artifact", None) is not None:
            raise SwitchError("Switch filesystem adapter already has a staged artifact")
        if artifact is not None:
            setattr(self, "_switch_staged_artifact", dict(artifact))

    def unbind_switch_effect(self) -> None:
        for name in (
            "_switch_effect_binding",
            "_switch_staged_artifact",
            "_switch_action_produced_identity",
        ):
            try:
                delattr(self, name)
            except AttributeError:
                pass

    def before_switch_effect_action(
        self,
        path: Path,
        effect: Mapping[str, object],
    ) -> None:
        del path, effect

    def _bound_switch_effect(
        self,
        path: Path,
    ) -> tuple[int, str] | None:
        binding = getattr(self, "_switch_effect_binding", None)
        if (
            not isinstance(binding, tuple)
            or len(binding) != 3
            or binding[0] != str(path)
            or not isinstance(binding[1], int)
            or not isinstance(binding[2], str)
        ):
            return None
        return binding[1], binding[2]

    def _bound_switch_staged_artifact(
        self,
    ) -> dict[str, object] | None:
        artifact = getattr(self, "_switch_staged_artifact", None)
        if artifact is None:
            return None
        if not isinstance(artifact, dict):
            raise SwitchError("Switch filesystem adapter has an invalid staged artifact")
        return dict(artifact)

    def bind_restore_effect(
        self,
        path: Path,
        *,
        route_path: Path,
        route_guard: Mapping[str, object],
        before_state: Mapping[str, object],
        before_identity: Mapping[str, object],
        staged_artifact: Mapping[str, object] | None,
        expected_created_parent_identities: tuple[
            Mapping[str, object], ...
        ] = tuple(),
    ) -> None:
        if getattr(self, "_restore_effect_binding", None) is not None:
            raise SwitchError("Restore filesystem adapter already has a bound effect")
        anchor_descriptor = _open_restore_route_anchor(
            route_path,
            path,
            route_guard,
            expected_created_parent_identities,
        )
        setattr(
            self,
            "_restore_effect_binding",
            {
                "path": str(path),
                "route_path": str(route_path),
                "route_guard": dict(route_guard),
                "before_state": dict(before_state),
                "before_identity": dict(before_identity),
                "staged_artifact": (
                    dict(staged_artifact)
                    if staged_artifact is not None
                    else None
                ),
                "expected_created_parent_identities": [
                    dict(identity)
                    for identity in expected_created_parent_identities
                ],
                "anchor_descriptor": anchor_descriptor,
                "parent_descriptor": None,
                "created_parents": tuple(),
                "produced_identity": None,
            },
        )

    def _bound_restore_effect(self, path: Path) -> dict[str, object] | None:
        binding = getattr(self, "_restore_effect_binding", None)
        if binding is None:
            return None
        if not isinstance(binding, dict) or binding.get("path") != str(path):
            raise SwitchError(f"Restore effect binding is invalid: {path}")
        return binding

    def unbind_restore_effect(self) -> None:
        binding = getattr(self, "_restore_effect_binding", None)
        if isinstance(binding, dict):
            descriptors = {
                value
                for value in (
                    binding.get("parent_descriptor"),
                    binding.get("anchor_descriptor"),
                )
                if isinstance(value, int)
            }
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            delattr(self, "_restore_effect_binding")
        except AttributeError:
            pass

    def _open_bound_restore_parent(
        self,
        path: Path,
        binding: dict[str, object],
    ) -> tuple[int, tuple[Path, ...]]:
        raw_anchor_descriptor = binding.get("anchor_descriptor")
        route_guard = binding.get("route_guard")
        if not isinstance(raw_anchor_descriptor, int) or not isinstance(
            route_guard,
            dict,
        ):
            raise SwitchError(f"Restore effect has no pinned route: {path}")
        raw_missing = route_guard.get("missing_parent_paths")
        if not isinstance(raw_missing, list):
            raise SwitchError(f"Restore effect has an invalid parent plan: {path}")
        expected_identities = binding.get(
            "expected_created_parent_identities",
            [],
        )
        if not isinstance(expected_identities, list):
            raise SwitchError(
                f"Restore effect has invalid created-parent identities: {path}"
            )
        expected_by_path = {
            identity.get("path"): identity
            for identity in expected_identities
            if isinstance(identity, dict)
            and isinstance(identity.get("path"), str)
        }
        descriptor = os.dup(raw_anchor_descriptor)
        created: list[Path] = []
        try:
            for raw_parent in raw_missing:
                if not isinstance(raw_parent, str) or not raw_parent:
                    raise SwitchError(
                        f"Restore effect has an invalid missing parent: {path}"
                    )
                parent = Path(raw_parent)
                expected_identity = expected_by_path.get(raw_parent)
                try:
                    current = os.stat(
                        parent.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if expected_identity is not None:
                        raise SwitchError(
                            f"Restore created parent disappeared: {parent}"
                        )
                    os.mkdir(parent.name, mode=0o700, dir_fd=descriptor)
                    current = os.stat(
                        parent.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    created.append(parent)
                if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(
                    current.st_mode
                ):
                    raise SwitchError(
                        f"Restore parent is not a directory: {parent}"
                    )
                if expected_identity is None and parent not in created:
                    raise SwitchError(
                        f"Restore missing parent was created externally: {parent}"
                    )
                if expected_identity is not None and (
                    expected_identity.get("kind") != "directory"
                    or expected_identity.get("device") != current.st_dev
                    or expected_identity.get("inode") != current.st_ino
                ):
                    raise SwitchError(
                        f"Restore created-parent identity changed: {parent}"
                    )
                next_descriptor = os.open(
                    parent.name,
                    _capture_directory_open_flags(),
                    dir_fd=descriptor,
                )
                opened = os.fstat(next_descriptor)
                if (opened.st_dev, opened.st_ino) != (
                    current.st_dev,
                    current.st_ino,
                ):
                    os.close(next_descriptor)
                    raise SwitchError(
                        f"Restore parent changed while opening: {parent}"
                    )
                os.close(descriptor)
                descriptor = next_descriptor
            binding["parent_descriptor"] = descriptor
            binding["created_parents"] = tuple(reversed(created))
            return descriptor, tuple(reversed(created))
        except Exception:
            os.close(descriptor)
            raise

    def bound_restore_result(
        self,
        path: Path,
    ) -> tuple[
        dict[str, object] | None,
        dict[str, object] | None,
        tuple[Path, ...],
    ]:
        binding = self._bound_restore_effect(path)
        if binding is None:
            raise SwitchError(f"Restore effect is not bound: {path}")
        descriptor = binding.get("parent_descriptor")
        created = binding.get("created_parents", tuple())
        if not isinstance(created, tuple):
            raise SwitchError(f"Restore effect created-parent state is invalid: {path}")
        if not isinstance(descriptor, int):
            return None, None, created
        observed_state = _path_state_at(descriptor, (path.name,), path)
        observed_identity = _node_identity_at(descriptor, path.name, path)
        native_identity = binding.get("produced_identity")
        if isinstance(native_identity, dict) and observed_identity != native_identity:
            raise SwitchError(
                f"Restore effect identity changed after action: {path}"
            )
        return observed_state, observed_identity, created

    def _install_bound_switch_stage(
        self,
        path: Path,
        destination_descriptor: int,
        destination_leaf: str,
        data: bytes,
        *,
        mode: int,
        artifact_override: Mapping[str, object] | None = None,
    ) -> bool:
        artifact = (
            dict(artifact_override)
            if artifact_override is not None
            else self._bound_switch_staged_artifact()
        )
        if artifact is None:
            return False
        raw_stage_path = artifact.get("staged_path")
        staged_state = artifact.get("staged_state")
        staged_identity = artifact.get("staged_identity")
        staged_route_guard = artifact.get("staged_route_guard")
        if (
            not isinstance(raw_stage_path, str)
            or not isinstance(staged_state, dict)
            or not isinstance(staged_identity, dict)
            or not isinstance(staged_route_guard, dict)
        ):
            raise SwitchError(f"Switch staged artifact is incomplete: {path}")
        stage_path = Path(raw_stage_path)
        expected_payload_state = _expected_file_state(
            stage_path,
            data,
            mode=mode,
        )
        if not _states_match(staged_state, expected_payload_state):
            raise SwitchError(
                f"Switch action does not match its persisted stage: {path}"
            )
        stage_parent_descriptor = _open_guarded_parent(
            stage_path,
            staged_route_guard,
        )
        temporary_leaf: str | None = None
        try:
            current_stage_state = _path_state_at(
                stage_parent_descriptor,
                (stage_path.name,),
                stage_path,
            )
            current_stage_identity = _node_identity_at(
                stage_parent_descriptor,
                stage_path.name,
                stage_path,
            )
            if (
                not _states_match(current_stage_state, staged_state)
                or current_stage_identity != staged_identity
            ):
                raise SwitchError(
                    f"Switch persisted stage changed before install: {stage_path}"
                )
            for _ in range(16):
                candidate = f".{destination_leaf}.codex-switch-{uuid.uuid4().hex}.tmp"
                try:
                    os.link(
                        stage_path.name,
                        candidate,
                        src_dir_fd=stage_parent_descriptor,
                        dst_dir_fd=destination_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise SwitchError(
                        f"Cannot install persisted switch stage for {path}: {exc}"
                    ) from exc
                temporary_leaf = candidate
                break
            if temporary_leaf is None:
                raise SwitchError(
                    f"Cannot allocate switch stage install path for {path}"
                )
            os.replace(
                temporary_leaf,
                destination_leaf,
                src_dir_fd=destination_descriptor,
                dst_dir_fd=destination_descriptor,
            )
            temporary_leaf = None
            os.fsync(destination_descriptor)
        finally:
            if temporary_leaf is not None:
                try:
                    os.unlink(temporary_leaf, dir_fd=destination_descriptor)
                except FileNotFoundError:
                    pass
            os.close(stage_parent_descriptor)
        return True

    def capture_directory_identity(self, path: Path) -> tuple[int, int]:
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            descriptor, relative = binding
            return _directory_identity_at(descriptor, relative, path)
        raise SwitchError(
            f"Capture directory identity is outside pinned workspace: {path}"
        )

    def capture_parent_checkpoint(self, path: Path, *, phase: str) -> None:
        del path, phase

    def copy_material(
        self,
        source: Path,
        destination: Path,
        kind: object,
        *,
        phase: str,
    ) -> None:
        destination_binding = _capture_binding_for_path(self, destination)
        if phase.startswith("capture_") and destination_binding is None:
            raise SwitchError(
                f"Capture destination is outside the pinned workspace: {destination}"
            )
        if phase == "capture_clone":
            if kind != "directory":
                raise SwitchError(
                    f"Capture clone source is not a directory: {source}"
                )
            source_binding = _capture_binding_for_path(self, source)
            if destination_binding is not None and source_binding is not None:
                destination_descriptor, destination_relative = destination_binding
                source_descriptor, source_relative = source_binding
                if destination_descriptor != source_descriptor:
                    raise SwitchError("Capture clone crossed workspace descriptors")
                _copy_directory_contents_at(
                    source_descriptor,
                    source_relative,
                    destination_relative,
                )
                return
            shutil.copytree(
                source,
                destination,
                symlinks=True,
                dirs_exist_ok=True,
            )
            return
        if destination_binding is not None:
            if kind != "file":
                raise SwitchError(f"Cannot stage capture state kind: {kind}")
            descriptor, relative = destination_binding
            _copy_file_atomic_at(source, descriptor, relative)
            return
        _copy_material(source, destination, kind)

    def make_directory(
        self,
        path: Path,
        *,
        mode: int,
        phase: str,
    ) -> tuple[int, int]:
        del phase
        binding = _capture_binding_for_path(self, path)
        if binding is None:
            raise SwitchError(f"Capture directory is outside pinned workspace: {path}")
        descriptor, relative = binding
        return _mkdir_at(descriptor, relative, mode=mode)

    def chmod(self, path: Path, mode: int, *, phase: str) -> None:
        binding = _capture_binding_for_path(self, path)
        if binding is None:
            if phase.startswith("capture_"):
                raise SwitchError(f"Capture chmod is outside pinned workspace: {path}")
            path.chmod(mode)
            return
        descriptor, relative = binding
        target_descriptor = _open_path_at(descriptor, relative)
        try:
            os.fchmod(target_descriptor, mode)
        finally:
            os.close(target_descriptor)

    def remove_path(self, path: Path, *, phase: str) -> None:
        switch_binding = self._bound_switch_effect(path)
        if switch_binding is not None:
            descriptor, leaf = switch_binding
            _remove_path_at(descriptor, (leaf,))
            os.fsync(descriptor)
            self._record_bound_switch_produced_identity(path)
            return
        binding = _capture_binding_for_path(self, path)
        if binding is None:
            if phase.startswith("capture_"):
                raise SwitchError(
                    f"Capture removal is outside pinned workspace: {path}"
                )
            _remove_path(path)
            return
        descriptor, relative = binding
        _remove_path_at(descriptor, relative)

    def read_json(self, path: Path) -> dict[str, object]:
        binding = _capture_binding_for_path(self, path)
        if binding is None:
            raise SwitchError(f"Capture JSON is outside pinned workspace: {path}")
        descriptor, relative = binding
        try:
            value = json.loads(_read_file_at(descriptor, relative).decode())
        except json.JSONDecodeError as exc:
            raise SwitchError(f"Invalid JSON: {path}: {exc}") from exc
        if not isinstance(value, dict):
            return value  # type: ignore[return-value]
        return value

    def read_text(self, path: Path) -> str:
        binding = _capture_binding_for_path(self, path)
        if binding is None:
            raise SwitchError(f"Capture text is outside pinned workspace: {path}")
        descriptor, relative = binding
        try:
            return _read_file_at(descriptor, relative).decode()
        except UnicodeDecodeError as exc:
            raise SwitchError(f"Capture text is not UTF-8: {path}: {exc}") from exc

    def materialize(
        self,
        source: Path | None,
        destination: Path,
        state: Mapping[str, object],
        *,
        phase: str,
    ) -> None:
        restore_binding = self._bound_restore_effect(destination)
        if restore_binding is not None:
            raw_route_path = restore_binding.get("route_path")
            route_guard = restore_binding.get("route_guard")
            if not isinstance(raw_route_path, str) or not isinstance(
                route_guard,
                dict,
            ):
                raise SwitchError(
                    f"Restore effect has no attested route: {destination}"
                )
            _validate_restore_route_guard(
                Path(raw_route_path),
                destination,
                route_guard,
                tuple(
                    identity
                    for identity in restore_binding.get(
                        "expected_created_parent_identities",
                        [],
                    )
                    if isinstance(identity, dict)
                ),
            )
            parent_descriptor, created_parents = (
                self._open_bound_restore_parent(
                    destination,
                    restore_binding,
                )
            )
            created_by_phase = getattr(self, "_created_parents_by_phase", None)
            if not isinstance(created_by_phase, dict):
                created_by_phase = {}
                setattr(self, "_created_parents_by_phase", created_by_phase)
            created_by_phase[phase] = created_parents
            before_state = restore_binding.get("before_state")
            before_identity = restore_binding.get("before_identity")
            pinned_before_state = _path_state_at(
                parent_descriptor,
                (destination.name,),
                destination,
            )
            pinned_before_identity = _node_identity_at(
                parent_descriptor,
                destination.name,
                destination,
            )
            if (
                not isinstance(before_state, dict)
                or not _states_match(pinned_before_state, before_state)
                or not isinstance(before_identity, dict)
                or pinned_before_identity != before_identity
            ):
                raise SwitchError(
                    f"Restore predecessor identity changed before action: "
                    f"{destination}"
                )
            kind = state.get("kind")
            staged_artifact = restore_binding.get("staged_artifact")
            if kind == "file":
                if source is None or not isinstance(staged_artifact, dict):
                    raise SwitchError(
                        f"Restore file has no attested staged source: {destination}"
                    )
                mode = state.get("mode")
                if not isinstance(mode, int):
                    raise SwitchError(
                        f"Restore file mode is invalid: {destination}"
                    )
                payload = source.read_bytes()
                if not self._install_bound_switch_stage(
                    destination,
                    parent_descriptor,
                    destination.name,
                    payload,
                    mode=mode,
                    artifact_override=staged_artifact,
                ):
                    raise SwitchError(
                        f"Restore file stage is unavailable: {destination}"
                    )
            elif kind == "directory":
                if source is None or not isinstance(staged_artifact, dict):
                    raise SwitchError(
                        f"Restore directory has no attested staged source: "
                        f"{destination}"
                    )
                raw_stage_path = staged_artifact.get("staged_path")
                staged_state = staged_artifact.get("staged_state")
                staged_identity = staged_artifact.get("staged_identity")
                staged_route_guard = staged_artifact.get(
                    "staged_route_guard"
                )
                if (
                    not isinstance(raw_stage_path, str)
                    or not isinstance(staged_state, dict)
                    or not isinstance(staged_identity, dict)
                    or not isinstance(staged_route_guard, dict)
                ):
                    raise SwitchError(
                        f"Restore directory stage is incomplete: {destination}"
                    )
                stage_path = Path(raw_stage_path)
                stage_parent_descriptor = _open_guarded_parent(
                    stage_path,
                    staged_route_guard,
                )
                try:
                    current_stage_state = _path_state_at(
                        stage_parent_descriptor,
                        (stage_path.name,),
                        stage_path,
                    )
                    current_stage_identity = _node_identity_at(
                        stage_parent_descriptor,
                        stage_path.name,
                        stage_path,
                    )
                    if (
                        not _states_match(current_stage_state, staged_state)
                        or current_stage_identity != staged_identity
                    ):
                        raise SwitchError(
                            f"Restore directory stage changed: {stage_path}"
                        )
                    _remove_path_at(
                        parent_descriptor,
                        (destination.name,),
                    )
                    os.rename(
                        stage_path.name,
                        destination.name,
                        src_dir_fd=stage_parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    os.fsync(stage_parent_descriptor)
                    os.fsync(parent_descriptor)
                finally:
                    os.close(stage_parent_descriptor)
            elif kind == "symlink":
                target = state.get("symlink_target")
                if not isinstance(target, str):
                    raise SwitchError(
                        f"Restore symlink has no target: {destination}"
                    )
                _remove_path_at(parent_descriptor, (destination.name,))
                os.symlink(
                    target,
                    destination.name,
                    dir_fd=parent_descriptor,
                )
                os.fsync(parent_descriptor)
            elif kind == "missing":
                _remove_path_at(parent_descriptor, (destination.name,))
                os.fsync(parent_descriptor)
            else:
                raise SwitchError(
                    f"Unsupported restore state kind: {kind}: {destination}"
                )
            produced_identity = _node_identity_at(
                parent_descriptor,
                destination.name,
                destination,
            )
            restore_binding["produced_identity"] = produced_identity
            return
        created_parents: list[Path] = []
        created_by_phase = getattr(self, "_created_parents_by_phase", None)
        if not isinstance(created_by_phase, dict):
            created_by_phase = {}
            setattr(self, "_created_parents_by_phase", created_by_phase)
        created_by_phase[phase] = tuple()
        if _canonical_destination(destination) != destination:
            raise SwitchError(
                f"Restore destination parent changed after preflight: {destination}"
            )
        kind = state.get("kind")
        if kind == "missing":
            _remove_path(destination)
            return
        for parent in reversed(_missing_parent_paths(destination)):
            try:
                parent.mkdir()
            except FileExistsError:
                continue
            created_parents.append(parent)
            created_by_phase[phase] = tuple(created_parents)
        if _canonical_destination(destination) != destination:
            raise SwitchError(
                f"Restore destination parent changed while materializing: "
                f"{destination}"
            )
        _remove_path(destination)
        if kind == "symlink":
            target = state.get("symlink_target")
            if not isinstance(target, str):
                raise SwitchError(
                    f"Backup symlink state has no target: {destination}"
                )
            destination.symlink_to(target)
            return
        if source is None:
            raise SwitchError(f"No staged payload for restore target: {destination}")
        if kind == "file":
            shutil.copy2(source, destination)
        elif kind == "directory":
            shutil.copytree(source, destination, symlinks=True)
        else:
            raise SwitchError(f"Unsupported backup state kind: {kind}")
        mode = state.get("mode")
        if isinstance(mode, int):
            destination.chmod(mode)

    def consume_created_parents(self, *, phase: str) -> tuple[Path, ...]:
        created_by_phase = getattr(self, "_created_parents_by_phase", None)
        if not isinstance(created_by_phase, dict):
            return tuple()
        value = created_by_phase.pop(phase, tuple())
        if not isinstance(value, tuple) or not all(
            isinstance(path, Path) for path in value
        ):
            raise SwitchError(
                f"Filesystem adapter returned invalid created-parent journal: {phase}"
            )
        return value

    def remove_tree(self, path: Path, *, phase: str) -> None:
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            descriptor, relative = binding
            _remove_tree_at(descriptor, relative)
            return
        if phase.startswith("capture_"):
            raise SwitchError(f"Capture tree is outside pinned workspace: {path}")
        shutil.rmtree(path)

    def remove_file(self, path: Path, *, phase: str) -> None:
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            descriptor, relative = binding
            _unlink_at(descriptor, relative, missing_ok=True)
            return
        if phase.startswith("capture_"):
            raise SwitchError(f"Capture file is outside pinned workspace: {path}")
        path.unlink(missing_ok=True)

    def rename(self, source: Path, destination: Path, *, phase: str) -> None:
        source_binding = _capture_binding_for_path(self, source)
        destination_binding = _capture_binding_for_path(self, destination)
        if source_binding is not None and destination_binding is not None:
            source_descriptor, source_relative = source_binding
            destination_descriptor, destination_relative = destination_binding
            os.rename(
                _relative_name(source_relative),
                _relative_name(destination_relative),
                src_dir_fd=source_descriptor,
                dst_dir_fd=destination_descriptor,
            )
            return
        if phase.startswith("capture_"):
            raise SwitchError("Capture rename is outside pinned workspace")
        os.replace(source, destination)

    def sync_file(self, path: Path, *, phase: str) -> None:
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            root_descriptor, relative = binding
            descriptor = _open_file_at(root_descriptor, relative, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        if phase.startswith("capture_"):
            raise SwitchError(f"Capture fsync is outside pinned workspace: {path}")
        flags = _capture_file_open_flags()
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SwitchError(f"Cannot open capture file for fsync: {path}: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SwitchError(f"Capture fsync target is not a file: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def sync_directory(self, path: Path, *, phase: str) -> None:
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            root_descriptor, relative = binding
            descriptor = _open_directory_at(root_descriptor, relative)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        if phase.startswith("capture_"):
            raise SwitchError(
                f"Capture directory fsync is outside pinned workspace: {path}"
            )
        flags = _capture_directory_open_flags()
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SwitchError(
                f"Cannot open capture directory for fsync: {path}: {exc}"
            ) from exc
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise SwitchError(f"Capture fsync target is not a directory: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def sync_tree(
        self,
        path: Path,
        *,
        file_phase: str,
        directory_phase: str,
    ) -> None:
        binding = _capture_binding_for_path(self, path)
        if binding is None:
            if file_phase.startswith("capture_"):
                raise SwitchError(
                    f"Capture tree fsync is outside pinned workspace: {path}"
                )
            _sync_capture_tree_path(
                self,
                path,
                file_phase=file_phase,
                directory_phase=directory_phase,
            )
            return
        descriptor, relative = binding
        _sync_tree_at(
            self,
            descriptor,
            relative,
            path,
            file_phase=file_phase,
            directory_phase=directory_phase,
        )

    def remove_empty_dir(self, path: Path, *, phase: str) -> None:
        del phase
        cleanup_binding = getattr(self, "_restore_parent_cleanup_binding", None)
        if cleanup_binding is not None:
            if (
                not isinstance(cleanup_binding, dict)
                or cleanup_binding.get("path") != str(path)
                or not isinstance(cleanup_binding.get("parent_descriptor"), int)
                or not isinstance(cleanup_binding.get("before_identity"), dict)
            ):
                raise SwitchError(
                    f"Restore parent cleanup binding is invalid: {path}"
                )
            parent_descriptor = cleanup_binding["parent_descriptor"]
            before_identity = cleanup_binding["before_identity"]
            current_identity = _node_identity_at(
                parent_descriptor,
                path.name,
                path,
            )
            if current_identity != before_identity:
                raise SwitchError(
                    f"Restore parent cleanup identity changed: {path}"
                )
            directory_descriptor = os.open(
                path.name,
                _capture_directory_open_flags(),
                dir_fd=parent_descriptor,
            )
            try:
                if os.listdir(directory_descriptor):
                    raise SwitchError(
                        f"Restore parent cleanup directory is not empty: {path}"
                    )
            finally:
                os.close(directory_descriptor)
            os.rmdir(path.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            return
        try:
            path.rmdir()
        except FileNotFoundError:
            return

    def bind_restore_parent_cleanup(
        self,
        path: Path,
        parent_descriptor: int,
        before_identity: Mapping[str, object],
    ) -> None:
        if getattr(self, "_restore_parent_cleanup_binding", None) is not None:
            raise SwitchError("Restore parent cleanup is already bound")
        setattr(
            self,
            "_restore_parent_cleanup_binding",
            {
                "path": str(path),
                "parent_descriptor": parent_descriptor,
                "before_identity": dict(before_identity),
            },
        )

    def unbind_restore_parent_cleanup(self) -> None:
        binding = getattr(self, "_restore_parent_cleanup_binding", None)
        if isinstance(binding, dict):
            descriptor = binding.get("parent_descriptor")
            if isinstance(descriptor, int):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            delattr(self, "_restore_parent_cleanup_binding")
        except AttributeError:
            pass

    def durable_unlink(self, path: Path, *, phase: str) -> None:
        del phase
        path.unlink(missing_ok=True)
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write_manifest(
        self,
        path: Path,
        data: Mapping[str, object],
        *,
        phase: str,
    ) -> None:
        switch_binding = self._bound_switch_effect(path)
        if switch_binding is not None:
            descriptor, leaf = switch_binding
            payload = json.dumps(data, indent=2, sort_keys=True).encode() + b"\n"
            if self._install_bound_switch_stage(
                path,
                descriptor,
                leaf,
                payload,
                mode=0o600,
            ):
                self._record_bound_switch_produced_identity(path)
                return
            _atomic_write_at(descriptor, (leaf,), payload, mode=0o600)
            self._record_bound_switch_produced_identity(path)
            return
        binding = _capture_binding_for_path(self, path)
        if binding is not None:
            descriptor, relative = binding
            payload = json.dumps(data, indent=2, sort_keys=True).encode() + b"\n"
            _atomic_write_at(descriptor, relative, payload, mode=0o600)
            return
        if phase.startswith("capture_"):
            raise SwitchError(f"Capture manifest is outside pinned workspace: {path}")
        write_json(path, data)

    def write_bytes(
        self,
        path: Path,
        data: bytes,
        *,
        mode: int,
        phase: str,
    ) -> None:
        switch_binding = self._bound_switch_effect(path)
        if switch_binding is not None:
            descriptor, leaf = switch_binding
            if self._install_bound_switch_stage(
                path,
                descriptor,
                leaf,
                data,
                mode=mode,
            ):
                self._record_bound_switch_produced_identity(path)
                return
            _atomic_write_at(descriptor, (leaf,), data, mode=mode)
            self._record_bound_switch_produced_identity(path)
            return
        if phase.startswith("capture_"):
            raise SwitchError(f"Capture byte write is outside pinned workspace: {path}")
        atomic_write(path, data, mode=mode)

    def sync_shared_entry(
        self,
        source: Path,
        target: Path,
        *,
        prefer_link: bool,
        phase: str,
    ) -> None:
        del phase
        switch_binding = self._bound_switch_effect(target)
        if switch_binding is None:
            raise SwitchError(
                f"Shared switch target is outside its pinned effect: {target}"
            )
        parent_descriptor, leaf = switch_binding
        source_info = source.lstat()
        source_kind = _filesystem_kind(source_info)
        current_state = _path_state_at(
            parent_descriptor,
            (leaf,),
            target,
        )

        def finish() -> None:
            self._record_bound_switch_produced_identity(target)

        def install_symlink(link_target: str) -> None:
            temporary = f".{leaf}.codex-switch-{uuid.uuid4().hex}.tmp"
            temporary_created = False
            try:
                os.symlink(
                    link_target,
                    temporary,
                    dir_fd=parent_descriptor,
                )
                temporary_created = True
                _remove_path_at(parent_descriptor, (leaf,))
                os.replace(
                    temporary,
                    leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                temporary_created = False
                os.fsync(parent_descriptor)
            finally:
                if temporary_created:
                    try:
                        os.unlink(temporary, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass

        if source_kind == "symlink":
            from codex_switch_home_sync import (
                shareable_symlink_rejection_reason,
                should_remove_rejected_shareable_target,
            )

            if shareable_symlink_rejection_reason(
                source,
                source.parent,
                target.parent,
            ):
                if should_remove_rejected_shareable_target(
                    target,
                    source.parent,
                    target.parent,
                ):
                    _remove_path_at(parent_descriptor, (leaf,))
                    os.fsync(parent_descriptor)
                finish()
                return
            link_target = os.readlink(source)
            if (
                current_state.get("kind") == "symlink"
                and current_state.get("symlink_target") == link_target
            ):
                finish()
                return
            install_symlink(link_target)
            finish()
            return

        if source_kind == "directory" and prefer_link:
            from codex_switch_home_sync import path_is_within

            if path_is_within(source, target.parent):
                if current_state.get("kind") == "symlink":
                    _remove_path_at(parent_descriptor, (leaf,))
                    os.fsync(parent_descriptor)
                finish()
                return
            if (
                current_state.get("kind") == "symlink"
                and current_state.get("symlink_target") == str(source)
            ):
                finish()
                return
            if current_state.get("kind") not in {"missing", "symlink"}:
                finish()
                return
            install_symlink(str(source))
            finish()
            return

        if source_kind == "directory":
            if current_state.get("kind") != "missing":
                finish()
                return
            temporary = f".{leaf}.codex-switch-{uuid.uuid4().hex}.tmp"
            temporary_created = False
            source_descriptor = os.open(
                source,
                _capture_directory_open_flags(),
            )
            try:
                opened_source = os.fstat(source_descriptor)
                if (opened_source.st_dev, opened_source.st_ino) != (
                    source_info.st_dev,
                    source_info.st_ino,
                ):
                    raise SwitchError(
                        f"Shared support source changed before copy: {source}"
                    )
                os.mkdir(
                    temporary,
                    mode=stat.S_IMODE(source_info.st_mode),
                    dir_fd=parent_descriptor,
                )
                temporary_created = True
                destination_descriptor = os.open(
                    temporary,
                    _capture_directory_open_flags(),
                    dir_fd=parent_descriptor,
                )
                try:
                    _copy_directory_contents_fd(
                        source_descriptor,
                        destination_descriptor,
                    )
                    os.fchmod(
                        destination_descriptor,
                        stat.S_IMODE(source_info.st_mode),
                    )
                    _fsync_tree_fd(destination_descriptor)
                finally:
                    os.close(destination_descriptor)
                os.replace(
                    temporary,
                    leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                temporary_created = False
                os.fsync(parent_descriptor)
            finally:
                os.close(source_descriptor)
                if temporary_created:
                    _remove_path_at(parent_descriptor, (temporary,))
            finish()
            return

        raise SwitchError(f"Unsupported shared support source: {source}")

    def ensure_directory(
        self,
        path: Path,
        *,
        mode: int,
        phase: str,
    ) -> None:
        switch_binding = self._bound_switch_effect(path)
        if switch_binding is not None:
            parent_descriptor, leaf = switch_binding
            try:
                os.mkdir(leaf, mode=mode, dir_fd=parent_descriptor)
            except FileExistsError:
                info = os.stat(
                    leaf,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(info.st_mode):
                    raise SwitchError(
                        f"Switch directory path is not a directory: {path}"
                    )
            descriptor = os.open(
                leaf,
                _capture_directory_open_flags(),
                dir_fd=parent_descriptor,
            )
            try:
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_descriptor)
            self._record_bound_switch_produced_identity(path)
            return
        del phase
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SwitchError(
                    f"Switch directory path is not a directory: {path}"
                )
        path.chmod(mode)


class _StoreLock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._descriptor: int | None = None

    def __enter__(self) -> _StoreLock:
        try:
            descriptor = os.open(self.root, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise SwitchError(f"Profile store does not exist: {self.root}") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise SwitchError(f"profile store is busy: {self.root}") from exc
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def revalidate(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise SwitchError("Profile store lock is not active")
        try:
            locked = os.fstat(descriptor)
        except OSError as exc:
            raise SwitchError("Profile store lock descriptor is unavailable") from exc
        try:
            current = self.root.lstat()
        except FileNotFoundError as exc:
            raise SwitchError(
                f"Profile store changed while locked: {self.root}"
            ) from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino)
        ):
            raise SwitchError(f"Profile store changed while locked: {self.root}")

    def _active_root_descriptor(self) -> int:
        """Return the exact locked root FD for one private inheritance seam."""
        self.revalidate()
        descriptor = self._descriptor
        if descriptor is None:
            raise SwitchError("Profile store lock is not active")
        try:
            locked = os.fstat(descriptor)
        except OSError as exc:
            raise SwitchError("Profile store lock descriptor is unavailable") from exc
        if not stat.S_ISDIR(locked.st_mode):
            raise SwitchError("Profile store lock descriptor is not a directory")
        return descriptor


def _raise_blocking_store_evidence(
    classification: _StoreRecoveryClassification,
    *,
    operation: str,
) -> None:
    if classification.pending_transaction is not None:
        raise SwitchError(
            f"Pending transaction blocks {operation}: "
            f"{classification.pending_transaction.backup_dir.name}"
        )
    if classification.pending_capture_profiles:
        raise SwitchError(
            f"Pending capture blocks {operation}: "
            f"{classification.pending_capture_profiles[0]}"
        )
    if classification.unmarked_transactions:
        raise SwitchError(
            f"Unmarked transaction blocks {operation}: "
            f"{classification.unmarked_transactions[0].backup_dir.name}"
        )


_RUNTIME_REBIND_MARKER = ".runtime-binding-rebind.json"
_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES = 16 * 1024 * 1024
_RUNTIME_BINDING_EXECUTABLE_STAGE_PREFIX = ".codex-internal-update-"
_RUNTIME_BINDING_BUNDLE_REQUIRED_ROLES = frozenset(
    {
        "manifest",
        "launcher",
        "capability_receipt",
        "parity_receipt",
        "parity_overlay",
        "profile_config",
    }
)
_RUNTIME_BINDING_BUNDLE_OPTIONAL_ROLES = frozenset(
    {
        "official_manifest",
        "shared_config",
        "active_runtime_config",
        "profile_auth",
        "active_runtime_auth",
        "parity_internal_model_source",
        "parity_official_model_source",
    }
)
_RUNTIME_BINDING_BUNDLE_SCOPE_FULL = "full"
_RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY = "cli-only"
_RUNTIME_BINDING_BUNDLE_CLI_ONLY_REQUIRED_ROLES = frozenset(
    {"manifest"}
)
_RUNTIME_BINDING_BUNDLE_ACTIVATION_ORDER = (
    "parity_internal_model_source",
    "parity_official_model_source",
    "parity_overlay",
    "capability_receipt",
    "parity_receipt",
    "shared_config",
    "profile_config",
    "profile_auth",
    "active_runtime_auth",
    "active_runtime_config",
    "launcher",
    "official_manifest",
    "manifest",
)
_MAX_RUNTIME_REBIND_MARKER_BYTES = (
    len(_RUNTIME_BINDING_BUNDLE_ACTIVATION_ORDER)
    * 2
    * (
        (
            (_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES + 2)
            // 3
            * 4
        )
        + 2048
    )
    + 64 * 1024
)


@dataclass(frozen=True)
class RuntimeBindingTextArtifact:
    role: str
    path: Path
    payload: bytes | None
    mode: int


@dataclass(frozen=True)
class RuntimeBindingExecutableSwap:
    bound_path: Path
    candidate_path: Path
    backup_path: Path
    old_mode: int | None
    old_sha256: str | None
    new_mode: int
    new_sha256: str


def _runtime_binding_bundle_expected_paths(store: Store) -> dict[str, Path]:
    from codex_switch_home_select import resolve_runtime_homes

    homes = resolve_runtime_homes(store)
    profile_dir = store.profile_dir("internal")
    launcher = store.bin_dir / "codex-internal-app"
    parity_dir = profile_dir / "parity"
    internal_home = homes.internal.path
    return {
        "manifest": store.manifest_path("internal"),
        "official_manifest": store.manifest_path("openai-official"),
        "launcher": launcher,
        "capability_receipt": capability_receipt_path_for_launcher(launcher),
        "parity_receipt": parity_dir / "receipt.json",
        "parity_overlay": parity_dir / "model-catalog.json",
        "parity_internal_model_source": parity_dir / "internal-model-source.json",
        "parity_official_model_source": parity_dir / "official-model-source.json",
        "profile_config": profile_dir / "config.toml",
        "profile_auth": profile_dir / "auth.json",
        "active_runtime_auth": internal_home / "auth.json",
        "shared_config": homes.official.path / "config.toml",
        "active_runtime_config": internal_home / "config.toml",
    }


def _runtime_binding_bundle_expected_mode(role: str) -> int:
    return 0o755 if role == "launcher" else 0o600


def _runtime_binding_bundle_paths_overlap(
    first: Path,
    second: Path,
) -> bool:
    return (
        first == second
        or first in second.parents
        or second in first.parents
    )


def _runtime_rebind_bundle_route_anchor(store: Store, path: Path) -> Path:
    try:
        anchor = Path(
            os.path.commonpath(
                (
                    str(store.root),
                    str(path.parent),
                )
            )
        )
    except ValueError as exc:
        raise SwitchError(
            f"Runtime rebind target has no trusted route anchor: {path}"
        ) from exc
    if not anchor.is_absolute():
        raise SwitchError(
            f"Runtime rebind target route anchor is not absolute: {path}"
        )
    return anchor


def _open_runtime_rebind_bundle_parent(
    store: Store,
    path: Path,
    *,
    create_missing: bool,
) -> tuple[int, str] | None:
    if not path.is_absolute() or not path.name:
        raise SwitchError(f"Runtime rebind target path is invalid: {path}")
    anchor = _runtime_rebind_bundle_route_anchor(store, path)
    try:
        anchor_info = anchor.lstat()
    except FileNotFoundError as exc:
        raise SwitchError(
            f"Runtime rebind target route anchor is missing: {anchor}"
        ) from exc
    if stat.S_ISLNK(anchor_info.st_mode) or not stat.S_ISDIR(
        anchor_info.st_mode
    ):
        raise SwitchError(
            f"Runtime rebind target route anchor is unsafe: {anchor}"
        )
    try:
        descriptor = os.open(anchor, _capture_directory_open_flags())
    except OSError as exc:
        raise SwitchError(
            f"Runtime rebind target route cannot be opened safely: {anchor}"
        ) from exc
    try:
        opened_anchor = os.fstat(descriptor)
        if (opened_anchor.st_dev, opened_anchor.st_ino) != (
            anchor_info.st_dev,
            anchor_info.st_ino,
        ):
            raise SwitchError(
                f"Runtime rebind target route anchor changed: {anchor}"
            )
        try:
            relative_parent = path.parent.relative_to(anchor)
        except ValueError as exc:
            raise SwitchError(
                f"Runtime rebind target escaped its route anchor: {path}"
            ) from exc
        for part in relative_parent.parts:
            if part in {"", ".", ".."} or "/" in part:
                raise SwitchError(
                    f"Runtime rebind target route is invalid: {path}"
                )
            try:
                before = os.stat(
                    part,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create_missing:
                    os.close(descriptor)
                    return None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    before = os.stat(
                        part,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise SwitchError(
                        f"Runtime rebind target parent cannot be created safely: "
                        f"{path.parent}"
                    ) from exc
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(
                before.st_mode
            ):
                raise SwitchError(
                    f"Runtime rebind target ancestor is unsafe: "
                    f"{anchor / relative_parent}"
                )
            try:
                next_descriptor = os.open(
                    part,
                    _capture_directory_open_flags(),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise SwitchError(
                    f"Runtime rebind target ancestor cannot be opened safely: "
                    f"{path.parent}"
                ) from exc
            opened = os.fstat(next_descriptor)
            if (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                os.close(next_descriptor)
                raise SwitchError(
                    f"Runtime rebind target ancestor changed while opening: "
                    f"{path.parent}"
                )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, path.name
    except Exception:
        os.close(descriptor)
        raise


def _validate_runtime_rebind_bundle_target_route(
    store: Store,
    path: Path,
) -> None:
    opened = _open_runtime_rebind_bundle_parent(
        store,
        path,
        create_missing=False,
    )
    if opened is not None:
        os.close(opened[0])


def _validated_runtime_binding_bundle_artifacts(
    store: Store,
    artifacts: tuple[RuntimeBindingTextArtifact, ...],
    *,
    bundle_scope: str = _RUNTIME_BINDING_BUNDLE_SCOPE_FULL,
) -> tuple[RuntimeBindingTextArtifact, ...]:
    if bundle_scope == _RUNTIME_BINDING_BUNDLE_SCOPE_FULL:
        required_roles = _RUNTIME_BINDING_BUNDLE_REQUIRED_ROLES
        allowed_roles = (
            _RUNTIME_BINDING_BUNDLE_REQUIRED_ROLES
            | _RUNTIME_BINDING_BUNDLE_OPTIONAL_ROLES
        )
    elif bundle_scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY:
        required_roles = _RUNTIME_BINDING_BUNDLE_CLI_ONLY_REQUIRED_ROLES
        allowed_roles = _RUNTIME_BINDING_BUNDLE_CLI_ONLY_REQUIRED_ROLES
    else:
        raise SwitchError("Runtime binding bundle scope is invalid")
    if not isinstance(artifacts, tuple) or not artifacts:
        raise SwitchError("Runtime binding bundle artifacts are invalid")
    validated: list[RuntimeBindingTextArtifact] = []
    for artifact in artifacts:
        if not isinstance(artifact, RuntimeBindingTextArtifact):
            raise SwitchError("Runtime binding bundle artifact type is invalid")
        if not isinstance(artifact.role, str) or not artifact.role:
            raise SwitchError("Runtime binding bundle artifact role is invalid")
        if not isinstance(artifact.path, Path) or not artifact.path.is_absolute():
            raise SwitchError("Runtime binding bundle artifact path is invalid")
        if type(artifact.payload) is not bytes and not (artifact.payload is None and artifact.role in {"profile_auth", "active_runtime_auth"}):
            raise SwitchError("Runtime binding bundle artifact payload is invalid")
        if type(artifact.mode) is not int:
            raise SwitchError("Runtime binding bundle artifact mode is invalid")
        validated.append(artifact)

    roles = [artifact.role for artifact in validated]
    paths = [artifact.path for artifact in validated]
    if len(set(roles)) != len(roles) or len(set(paths)) != len(paths):
        raise SwitchError("Runtime binding bundle has duplicate targets")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if _runtime_binding_bundle_paths_overlap(path, other):
                raise SwitchError(
                    "Runtime binding bundle has overlapping targets"
                )

    expected_paths = _runtime_binding_bundle_expected_paths(store)
    supplied_roles = set(roles)
    unexpected_roles = supplied_roles - allowed_roles
    if unexpected_roles:
        raise SwitchError("Runtime binding bundle has unexpected targets")
    missing_roles = required_roles - supplied_roles
    if missing_roles:
        raise SwitchError("Runtime binding bundle is missing required targets")
    for artifact in validated:
        if artifact.path != expected_paths[artifact.role]:
            raise SwitchError(
                f"Runtime binding bundle target is unexpected: {artifact.role}"
            )
        if artifact.mode != _runtime_binding_bundle_expected_mode(
            artifact.role
        ):
            raise SwitchError(
                f"Runtime binding bundle mode is invalid: {artifact.role}"
            )
        if artifact.payload is not None and len(artifact.payload) > _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES:
            raise SwitchError(
                f"Runtime binding bundle payload is oversized: {artifact.role}"
            )
        _validate_runtime_rebind_bundle_target_route(store, artifact.path)

    by_role = {artifact.role: artifact for artifact in validated}
    return tuple(
        by_role[role]
        for role in _RUNTIME_BINDING_BUNDLE_ACTIVATION_ORDER
        if role in by_role
    )


def _validate_runtime_binding_executable_path(
    path: object,
    *,
    label: str,
) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not path.name
        or path != Path(os.path.normpath(str(path)))
    ):
        raise SwitchError(
            f"Runtime binding executable swap {label} path is invalid"
        )
    return path


def _validate_runtime_binding_executable_mode(
    value: object,
    *,
    label: str,
) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > 0o777
        or value & 0o111 == 0
    ):
        raise SwitchError(
            f"Runtime binding executable swap {label} mode is invalid"
        )
    return value


def _validate_runtime_binding_executable_digest(
    value: object,
    *,
    label: str,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SwitchError(
            f"Runtime binding executable swap {label} digest is invalid"
        )
    return value


def _validate_runtime_binding_executable_swap_paths(
    store: Store,
    *,
    bound_path: Path,
    candidate_path: Path,
    backup_path: Path,
) -> None:
    if len({bound_path, candidate_path, backup_path}) != 3:
        raise SwitchError(
            "Runtime binding executable swap paths must be distinct"
        )
    if bound_path.name != "codex" or candidate_path.name != bound_path.name:
        raise SwitchError(
            "Runtime binding executable swap executable names are invalid"
        )
    if backup_path.parent != bound_path.parent:
        raise SwitchError(
            "Runtime binding executable swap backup is not a sibling"
        )
    stage_dir = candidate_path.parent
    if (
        stage_dir.parent != bound_path.parent
        or not stage_dir.name.startswith(
            _RUNTIME_BINDING_EXECUTABLE_STAGE_PREFIX
        )
    ):
        raise SwitchError(
            "Runtime binding executable swap candidate is not in a "
            "private sibling stage"
        )
    for path in (bound_path, backup_path):
        _validate_runtime_rebind_bundle_target_route(store, path)
    opened_candidate_parent = _open_runtime_rebind_bundle_parent(
        store,
        candidate_path,
        create_missing=False,
    )
    if opened_candidate_parent is None:
        raise SwitchError(
            "Runtime binding executable swap candidate stage is unavailable"
        )
    candidate_parent_descriptor, _candidate_leaf = opened_candidate_parent
    try:
        stage_info = os.fstat(candidate_parent_descriptor)
        if (
            not stat.S_ISDIR(stage_info.st_mode)
            or stat.S_IMODE(stage_info.st_mode) != 0o700
        ):
            raise SwitchError(
                "Runtime binding executable swap candidate stage is unsafe"
            )
    finally:
        os.close(candidate_parent_descriptor)


def _validated_runtime_binding_executable_swap(
    store: Store,
    executable_swap: RuntimeBindingExecutableSwap,
) -> dict[str, object]:
    if not isinstance(executable_swap, RuntimeBindingExecutableSwap):
        raise SwitchError(
            "Runtime binding executable swap type is invalid"
        )
    bound_path = _validate_runtime_binding_executable_path(
        executable_swap.bound_path,
        label="bound",
    )
    candidate_path = _validate_runtime_binding_executable_path(
        executable_swap.candidate_path,
        label="candidate",
    )
    backup_path = _validate_runtime_binding_executable_path(
        executable_swap.backup_path,
        label="backup",
    )
    _validate_runtime_binding_executable_swap_paths(
        store,
        bound_path=bound_path,
        candidate_path=candidate_path,
        backup_path=backup_path,
    )
    return {
        "bound_path": str(bound_path),
        "candidate_path": str(candidate_path),
        "backup_path": str(backup_path),
        "old_mode": None if executable_swap.old_mode is None and executable_swap.old_sha256 is None else _validate_runtime_binding_executable_mode(executable_swap.old_mode, label="old"),
        "old_sha256": None if executable_swap.old_mode is None and executable_swap.old_sha256 is None else _validate_runtime_binding_executable_digest(executable_swap.old_sha256, label="old"),
        "new_mode": _validate_runtime_binding_executable_mode(
            executable_swap.new_mode,
            label="new",
        ),
        "new_sha256": _validate_runtime_binding_executable_digest(
            executable_swap.new_sha256,
            label="new",
        ),
    }


def _validated_runtime_rebind_executable_swap_marker(
    store: Store,
    raw: object,
) -> dict[str, object]:
    expected_fields = {
        "bound_path",
        "candidate_path",
        "backup_path",
        "old_mode",
        "old_sha256",
        "new_mode",
        "new_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise SwitchError(
            "Runtime rebind executable swap marker fields are invalid"
        )
    paths: dict[str, Path] = {}
    for field in ("bound_path", "candidate_path", "backup_path"):
        raw_path = raw.get(field)
        if not isinstance(raw_path, str):
            raise SwitchError(
                f"Runtime rebind executable swap marker {field} is invalid"
            )
        paths[field] = _validate_runtime_binding_executable_path(
            Path(raw_path),
            label=field.removesuffix("_path"),
        )
    _validate_runtime_binding_executable_swap_paths(
        store,
        bound_path=paths["bound_path"],
        candidate_path=paths["candidate_path"],
        backup_path=paths["backup_path"],
    )
    return {
        "bound_path": str(paths["bound_path"]),
        "candidate_path": str(paths["candidate_path"]),
        "backup_path": str(paths["backup_path"]),
        "old_mode": None if raw.get("old_mode") is None and raw.get("old_sha256") is None else _validate_runtime_binding_executable_mode(raw.get("old_mode"), label="old"),
        "old_sha256": None if raw.get("old_mode") is None and raw.get("old_sha256") is None else _validate_runtime_binding_executable_digest(raw.get("old_sha256"), label="old"),
        "new_mode": _validate_runtime_binding_executable_mode(
            raw.get("new_mode"),
            label="new",
        ),
        "new_sha256": _validate_runtime_binding_executable_digest(
            raw.get("new_sha256"),
            label="new",
        ),
    }


def _runtime_rebind_file_state(
    path: Path,
    *,
    max_payload_bytes: int | None = None,
) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return {"kind": "other"}
    if (
        max_payload_bytes is not None
        and before.st_size > max_payload_bytes
    ):
        raise SwitchError(f"Runtime rebind target payload is oversized: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SwitchError(
            f"Runtime rebind target cannot be opened safely: {path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise SwitchError(f"Runtime rebind target identity changed: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if (
                max_payload_bytes is not None
                and total > max_payload_bytes
            ):
                raise SwitchError(
                    f"Runtime rebind target payload is oversized: {path}"
                )
            chunks.append(chunk)
        after_open = os.fstat(descriptor)
        try:
            after_path = path.lstat()
        except OSError as exc:
            raise SwitchError(
                f"Runtime rebind target identity changed: {path}"
            ) from exc
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(opened, field) != getattr(after_open, field)
            for field in stable_fields
        ) or any(
            getattr(opened, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise SwitchError(f"Runtime rebind target identity changed: {path}")
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    return {
        "kind": "file",
        "mode": stat.S_IMODE(opened.st_mode),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "payload": base64.b64encode(payload).decode("ascii"),
    }


def _runtime_rebind_bundle_file_state(
    store: Store,
    path: Path,
    *,
    max_payload_bytes: int | None = None,
) -> dict[str, object]:
    opened_parent = _open_runtime_rebind_bundle_parent(
        store,
        path,
        create_missing=False,
    )
    if opened_parent is None:
        return {"kind": "missing"}
    parent_descriptor, leaf = opened_parent
    descriptor: int | None = None
    try:
        try:
            before = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return {"kind": "missing"}
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return {"kind": "other"}
        if (
            max_payload_bytes is not None
            and before.st_size > max_payload_bytes
        ):
            raise SwitchError(
                f"Runtime rebind target payload is oversized: {path}"
            )
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | _required_capture_open_flag("O_NOFOLLOW"),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise SwitchError(
                f"Runtime rebind target cannot be opened safely: {path}"
            ) from exc
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise SwitchError(
                f"Runtime rebind target identity changed: {path}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if (
                max_payload_bytes is not None
                and total > max_payload_bytes
            ):
                raise SwitchError(
                    f"Runtime rebind target payload is oversized: {path}"
                )
            chunks.append(chunk)
        after_open = os.fstat(descriptor)
        try:
            after_path = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SwitchError(
                f"Runtime rebind target identity changed: {path}"
            ) from exc
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(opened, field) != getattr(after_open, field)
            for field in stable_fields
        ) or any(
            getattr(opened, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise SwitchError(
                f"Runtime rebind target identity changed: {path}"
            )
        payload = b"".join(chunks)
        return {
            "kind": "file",
            "mode": stat.S_IMODE(opened.st_mode),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "payload": base64.b64encode(payload).decode("ascii"),
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _runtime_rebind_executable_state_at(
    parent_descriptor: int,
    leaf: str,
    path: Path,
) -> dict[str, object]:
    descriptor: int | None = None
    try:
        try:
            before = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return {"kind": "missing"}
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return {"kind": "other"}
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | _required_capture_open_flag("O_NOFOLLOW"),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise SwitchError(
                "Runtime rebind executable cannot be opened safely: "
                f"{path}"
            ) from exc
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise SwitchError(
                f"Runtime rebind executable identity changed: {path}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after_open = os.fstat(descriptor)
        try:
            after_path = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SwitchError(
                f"Runtime rebind executable identity changed: {path}"
            ) from exc
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(opened, field) != getattr(after_open, field)
            for field in stable_fields
        ) or any(
            getattr(opened, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise SwitchError(
                f"Runtime rebind executable identity changed: {path}"
            )
        return {
            "kind": "file",
            "mode": stat.S_IMODE(opened.st_mode),
            "sha256": digest.hexdigest(),
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _runtime_rebind_executable_file_state(
    store: Store,
    path: Path,
) -> dict[str, object]:
    opened_parent = _open_runtime_rebind_bundle_parent(
        store,
        path,
        create_missing=False,
    )
    if opened_parent is None:
        return {"kind": "missing"}
    parent_descriptor, leaf = opened_parent
    try:
        return _runtime_rebind_executable_state_at(
            parent_descriptor,
            leaf,
            path,
        )
    finally:
        os.close(parent_descriptor)


def _runtime_rebind_payload_state(payload: bytes, mode: int) -> dict[str, object]:
    return {
        "kind": "file",
        "mode": mode,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "payload": base64.b64encode(payload).decode("ascii"),
    }


def _validated_runtime_rebind_state(
    raw: object,
    *,
    label: str,
    allow_missing: bool,
    max_payload_bytes: int | None = None,
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise SwitchError(f"Runtime rebind {label} state must be an object")
    kind = raw.get("kind")
    if kind == "missing" and allow_missing:
        return {"kind": "missing"}
    if kind != "file":
        raise SwitchError(f"Runtime rebind {label} state kind is invalid")
    mode = raw.get("mode")
    digest = raw.get("sha256")
    encoded = raw.get("payload")
    if type(mode) is not int or mode < 0 or mode > 0o7777:
        raise SwitchError(f"Runtime rebind {label} mode is invalid")
    if not isinstance(digest, str) or len(digest) != 64:
        raise SwitchError(f"Runtime rebind {label} digest is invalid")
    if not isinstance(encoded, str):
        raise SwitchError(f"Runtime rebind {label} payload is invalid")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise SwitchError(f"Runtime rebind {label} payload is invalid") from exc
    if (
        max_payload_bytes is not None
        and len(payload) > max_payload_bytes
    ):
        raise SwitchError(f"Runtime rebind {label} payload is oversized")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise SwitchError(f"Runtime rebind {label} payload digest is invalid")
    return {
        "kind": "file",
        "mode": mode,
        "sha256": digest,
        "payload": encoded,
    }


def _runtime_rebind_state_matches(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if current.get("kind") != expected.get("kind"):
        return False
    if expected.get("kind") == "missing":
        return True
    return (
        current.get("mode") == expected.get("mode")
        and current.get("sha256") == expected.get("sha256")
    )


def _runtime_rebind_state_payload(state: Mapping[str, object]) -> bytes:
    encoded = state.get("payload")
    if not isinstance(encoded, str):
        raise SwitchError("Runtime rebind file state has no payload")
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise SwitchError("Runtime rebind file state payload is invalid") from exc


def _runtime_rebind_marker_identity(
    info: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require_runtime_rebind_marker_identity(
    path: Path,
    expected_identity: tuple[int, int, int, int, int, int],
    *,
    phase: str,
) -> None:
    descriptor = os.open(
        path.parent,
        _capture_directory_open_flags(),
    )
    try:
        try:
            current = os.stat(
                path.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise SwitchError(
                f"Runtime rebind marker changed during {phase}: {path}"
            ) from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or _runtime_rebind_marker_identity(current)
            != expected_identity
        ):
            raise SwitchError(
                f"Runtime rebind marker changed during {phase}: {path}"
            )
    finally:
        os.close(descriptor)


def _durable_unlink_runtime_rebind_path(
    path: Path,
    *,
    expected_identity: tuple[int, int, int, int, int, int] | None = None,
) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        if expected_identity is not None:
            raise SwitchError(
                f"Runtime rebind marker changed before retirement: {path}"
            ) from exc
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        raise SwitchError(f"Runtime rebind refuses to unlink directory: {path}")
    if expected_identity is None:
        path.unlink()
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    if (
        expected_identity is not None
        and _runtime_rebind_marker_identity(info) != expected_identity
    ):
        raise SwitchError(
            f"Runtime rebind marker changed before retirement: {path}"
        )
    descriptor = os.open(
        path.parent,
        _capture_directory_open_flags(),
    )
    try:
        try:
            current = os.stat(
                path.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            if expected_identity is not None:
                raise SwitchError(
                    f"Runtime rebind marker changed before retirement: {path}"
                ) from exc
            return
        if (
            expected_identity is not None
            and _runtime_rebind_marker_identity(current)
            != expected_identity
        ):
            raise SwitchError(
                f"Runtime rebind marker changed before retirement: {path}"
            )
        if stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(
            current.st_mode
        ):
            raise SwitchError(
                f"Runtime rebind refuses to unlink directory: {path}"
            )
        os.unlink(path.name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _apply_runtime_rebind_state(
    path: Path,
    state: Mapping[str, object],
) -> None:
    if state.get("kind") == "missing":
        _durable_unlink_runtime_rebind_path(path)
        return
    mode = state.get("mode")
    if type(mode) is not int:
        raise SwitchError("Runtime rebind file mode is invalid")
    atomic_write(path, _runtime_rebind_state_payload(state), mode=mode)


def _durable_unlink_runtime_rebind_bundle_path(
    store: Store,
    path: Path,
) -> None:
    opened_parent = _open_runtime_rebind_bundle_parent(
        store,
        path,
        create_missing=False,
    )
    if opened_parent is None:
        return
    parent_descriptor, leaf = opened_parent
    try:
        try:
            info = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            raise SwitchError(
                f"Runtime rebind refuses to unlink directory: {path}"
            )
        os.unlink(leaf, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _atomic_write_runtime_rebind_bundle_path(
    store: Store,
    path: Path,
    payload: bytes,
    *,
    mode: int,
) -> None:
    opened_parent = _open_runtime_rebind_bundle_parent(
        store,
        path,
        create_missing=True,
    )
    if opened_parent is None:
        raise SwitchError(
            f"Runtime rebind target parent is unavailable: {path.parent}"
        )
    parent_descriptor, leaf = opened_parent
    temporary = f".{leaf}.{os.getpid()}.{os.urandom(8).hex()}"
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _required_capture_open_flag("O_NOFOLLOW"),
            mode,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        if current is not None and stat.S_ISDIR(current.st_mode):
            raise SwitchError(
                f"Runtime rebind target is a directory: {path}"
            )
        os.rename(
            temporary,
            leaf,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        temporary_created = False
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def _apply_runtime_rebind_bundle_state(
    store: Store,
    path: Path,
    state: Mapping[str, object],
) -> None:
    if state.get("kind") == "missing":
        _durable_unlink_runtime_rebind_bundle_path(store, path)
        return
    mode = state.get("mode")
    if type(mode) is not int:
        raise SwitchError("Runtime rebind file mode is invalid")
    _atomic_write_runtime_rebind_bundle_path(
        store,
        path,
        _runtime_rebind_state_payload(state),
        mode=mode,
    )


def _require_runtime_rebind_bundle_state(
    store: Store,
    path: Path,
    expected: Mapping[str, object],
    *,
    phase: str,
    max_payload_bytes: int | None = None,
) -> None:
    if not _runtime_rebind_state_matches(
        _runtime_rebind_bundle_file_state(
            store,
            path,
            max_payload_bytes=max_payload_bytes,
        ),
        expected,
    ):
        raise SwitchError(
            f"Runtime rebind target changed during {phase}: {path}"
        )


def _require_runtime_rebind_state(
    path: Path,
    expected: Mapping[str, object],
    *,
    phase: str,
    max_payload_bytes: int | None = None,
) -> None:
    if not _runtime_rebind_state_matches(
        _runtime_rebind_file_state(
            path,
            max_payload_bytes=max_payload_bytes,
        ),
        expected,
    ):
        raise SwitchError(
            f"Runtime rebind target changed during {phase}: {path}"
        )


def _runtime_rebind_executable_swap_paths(
    executable_swap: Mapping[str, object],
) -> tuple[Path, Path, Path]:
    raw_bound = executable_swap.get("bound_path")
    raw_candidate = executable_swap.get("candidate_path")
    raw_backup = executable_swap.get("backup_path")
    if not (
        isinstance(raw_bound, str)
        and isinstance(raw_candidate, str)
        and isinstance(raw_backup, str)
    ):
        raise SwitchError(
            "Runtime rebind executable swap paths are invalid"
        )
    return Path(raw_bound), Path(raw_candidate), Path(raw_backup)


def _runtime_rebind_executable_swap_expected_states(
    executable_swap: Mapping[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    old_mode = executable_swap.get("old_mode")
    old_sha256 = executable_swap.get("old_sha256")
    new_mode = executable_swap.get("new_mode")
    new_sha256 = executable_swap.get("new_sha256")
    if (
        (not (old_mode is None and old_sha256 is None) and (type(old_mode) is not int or not isinstance(old_sha256, str)))
        or type(new_mode) is not int
        or not isinstance(new_sha256, str)
    ):
        raise SwitchError(
            "Runtime rebind executable swap evidence is invalid"
        )
    return (
        ({"kind": "missing"} if old_mode is None else {
            "kind": "file",
            "mode": old_mode,
            "sha256": old_sha256,
        }),
        {
            "kind": "file",
            "mode": new_mode,
            "sha256": new_sha256,
        },
        {"kind": "missing"},
    )


def _runtime_rebind_executable_swap_phase(
    store: Store,
    executable_swap: Mapping[str, object],
    *,
    foreign_message: str,
) -> str:
    bound_path, candidate_path, backup_path = (
        _runtime_rebind_executable_swap_paths(executable_swap)
    )
    _validate_runtime_binding_executable_swap_paths(
        store,
        bound_path=bound_path,
        candidate_path=candidate_path,
        backup_path=backup_path,
    )
    old_state, new_state, missing_state = (
        _runtime_rebind_executable_swap_expected_states(executable_swap)
    )
    current = (
        _runtime_rebind_executable_file_state(store, bound_path),
        _runtime_rebind_executable_file_state(store, candidate_path),
        _runtime_rebind_executable_file_state(store, backup_path),
    )
    expected = {
        "initial": (old_state, new_state, missing_state),
        "old_backed_up": (missing_state, new_state, old_state),
        "new_bound": (new_state, missing_state, old_state),
    }
    for phase, states in expected.items():
        if all(
            _runtime_rebind_state_matches(observed, wanted)
            for observed, wanted in zip(current, states)
        ):
            return phase
    raise SwitchError(
        f"{foreign_message}: {bound_path}, {candidate_path}, {backup_path}"
    )


def _require_runtime_rebind_executable_swap_phase(
    store: Store,
    executable_swap: Mapping[str, object],
    expected_phase: str,
    *,
    phase: str,
) -> None:
    observed = _runtime_rebind_executable_swap_phase(
        store,
        executable_swap,
        foreign_message=(
            f"Runtime rebind executable swap changed during {phase}"
        ),
    )
    if observed != expected_phase and not (executable_swap.get("old_mode") is None and {observed, expected_phase} == {"initial", "old_backed_up"}):
        raise SwitchError(
            "Runtime rebind executable swap phase changed during "
            f"{phase}: expected {expected_phase}, found {observed}"
        )


def _rename_runtime_rebind_executable(
    store: Store,
    *,
    source_path: Path,
    destination_path: Path,
    expected_source_state: Mapping[str, object],
    phase: str,
) -> None:
    source_parent = _open_runtime_rebind_bundle_parent(
        store,
        source_path,
        create_missing=False,
    )
    destination_parent = _open_runtime_rebind_bundle_parent(
        store,
        destination_path,
        create_missing=False,
    )
    if source_parent is None or destination_parent is None:
        if source_parent is not None:
            os.close(source_parent[0])
        if destination_parent is not None:
            os.close(destination_parent[0])
        raise SwitchError(
            f"Runtime rebind executable route is unavailable during {phase}"
        )
    source_descriptor, source_leaf = source_parent
    destination_descriptor, destination_leaf = destination_parent
    try:
        source_state = _runtime_rebind_executable_state_at(
            source_descriptor,
            source_leaf,
            source_path,
        )
        destination_state = _runtime_rebind_executable_state_at(
            destination_descriptor,
            destination_leaf,
            destination_path,
        )
        if not _runtime_rebind_state_matches(
            source_state,
            expected_source_state,
        ) or destination_state.get("kind") != "missing":
            raise SwitchError(
                "Runtime rebind executable changed before "
                f"{phase}: {source_path} -> {destination_path}"
            )
        try:
            os.rename(
                source_leaf,
                destination_leaf,
                src_dir_fd=source_descriptor,
                dst_dir_fd=destination_descriptor,
            )
            os.fsync(source_descriptor)
            os.fsync(destination_descriptor)
        except OSError as exc:
            raise SwitchError(
                f"Runtime rebind executable rename failed during {phase}: "
                f"{source_path} -> {destination_path}"
            ) from exc
        if (
            _runtime_rebind_executable_state_at(
                source_descriptor,
                source_leaf,
                source_path,
            ).get("kind")
            != "missing"
            or not _runtime_rebind_state_matches(
                _runtime_rebind_executable_state_at(
                    destination_descriptor,
                    destination_leaf,
                    destination_path,
                ),
                expected_source_state,
            )
        ):
            raise SwitchError(
                "Runtime rebind executable rename did not converge during "
                f"{phase}: {source_path} -> {destination_path}"
            )
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _recover_runtime_rebind_executable_swap(
    store: Store,
    executable_swap: Mapping[str, object],
    *,
    marker_state: str,
    marker_path: Path,
    marker_identity: tuple[int, int, int, int, int, int],
) -> None:
    bound_path, candidate_path, backup_path = (
        _runtime_rebind_executable_swap_paths(executable_swap)
    )
    old_state, new_state, _missing_state = (
        _runtime_rebind_executable_swap_expected_states(executable_swap)
    )
    phase = _runtime_rebind_executable_swap_phase(
        store,
        executable_swap,
        foreign_message=(
            "Runtime rebind recovery found foreign binary state"
        ),
    )
    if marker_state == "prepared":
        if phase == "new_bound":
            _require_runtime_rebind_marker_identity(
                marker_path,
                marker_identity,
                phase="prepared executable candidate restoration",
            )
            _rename_runtime_rebind_executable(
                store,
                source_path=bound_path,
                destination_path=candidate_path,
                expected_source_state=new_state,
                phase="prepared executable candidate restoration",
            )
            phase = "old_backed_up"
        if phase == "old_backed_up" and old_state["kind"] != "missing":
            _require_runtime_rebind_marker_identity(
                marker_path,
                marker_identity,
                phase="prepared executable bound restoration",
            )
            _rename_runtime_rebind_executable(
                store,
                source_path=backup_path,
                destination_path=bound_path,
                expected_source_state=old_state,
                phase="prepared executable bound restoration",
            )
        desired_phase = "initial"
    elif marker_state == "committed":
        if phase == "initial" and old_state["kind"] == "missing":
            phase = "old_backed_up"
        if phase == "initial":
            _require_runtime_rebind_marker_identity(
                marker_path,
                marker_identity,
                phase="committed executable backup promotion",
            )
            _rename_runtime_rebind_executable(
                store,
                source_path=bound_path,
                destination_path=backup_path,
                expected_source_state=old_state,
                phase="committed executable backup promotion",
            )
            phase = "old_backed_up"
        if phase == "old_backed_up":
            _require_runtime_rebind_marker_identity(
                marker_path,
                marker_identity,
                phase="committed executable candidate promotion",
            )
            _rename_runtime_rebind_executable(
                store,
                source_path=candidate_path,
                destination_path=bound_path,
                expected_source_state=new_state,
                phase="committed executable candidate promotion",
            )
        desired_phase = "new_bound"
    else:
        raise SwitchError(
            "Runtime rebind executable swap marker state is invalid"
        )
    _require_runtime_rebind_executable_swap_phase(
        store,
        executable_swap,
        desired_phase,
        phase=f"{marker_state} recovery",
    )


def _retire_runtime_rebind_executable_backup(
    store: Store,
    executable_swap: Mapping[str, object],
) -> None:
    _bound_path, _candidate_path, backup_path = (
        _runtime_rebind_executable_swap_paths(executable_swap)
    )
    old_state, _new_state, _missing_state = (
        _runtime_rebind_executable_swap_expected_states(executable_swap)
    )
    if old_state["kind"] == "missing":
        if os.path.lexists(backup_path):
            raise SwitchError("First publication backup unexpectedly exists")
        return
    try:
        before = backup_path.lstat()
    except OSError as exc:
        raise SwitchError(
            "Runtime rebind executable backup is unavailable for retirement"
        ) from exc
    expected_identity = _runtime_rebind_marker_identity(before)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or not _runtime_rebind_state_matches(
            _runtime_rebind_executable_file_state(store, backup_path),
            old_state,
        )
    ):
        raise SwitchError(
            "Runtime rebind executable backup changed before retirement"
        )
    try:
        after = backup_path.lstat()
    except OSError as exc:
        raise SwitchError(
            "Runtime rebind executable backup changed before retirement"
        ) from exc
    if _runtime_rebind_marker_identity(after) != expected_identity:
        raise SwitchError(
            "Runtime rebind executable backup changed before retirement"
        )
    _durable_unlink_runtime_rebind_path(
        backup_path,
        expected_identity=expected_identity,
    )
    if os.path.lexists(backup_path):
        raise SwitchError(
            "Runtime rebind executable backup was not retired"
        )


def _runtime_rebind_marker_path(store: Store) -> Path:
    return store.root / _RUNTIME_REBIND_MARKER


def _runtime_rebind_marker_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _read_runtime_rebind_marker(
    path: Path,
) -> tuple[object, tuple[int, int, int, int, int, int]]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise SwitchError(f"Runtime rebind marker is missing: {path}")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SwitchError(f"Runtime rebind marker must be a regular file: {path}")
    if before.st_size > _MAX_RUNTIME_REBIND_MARKER_BYTES:
        raise SwitchError(f"Runtime rebind marker is oversized: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | _required_capture_open_flag("O_NOFOLLOW"),
        )
    except OSError as exc:
        raise SwitchError(f"Runtime rebind marker cannot be opened safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        identity = _runtime_rebind_marker_identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity
            != _runtime_rebind_marker_identity(before)
        ):
            raise SwitchError(f"Runtime rebind marker identity changed: {path}")
        if opened.st_size > _MAX_RUNTIME_REBIND_MARKER_BYTES:
            raise SwitchError(f"Runtime rebind marker is oversized: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_RUNTIME_REBIND_MARKER_BYTES:
                raise SwitchError(
                    f"Runtime rebind marker is oversized: {path}"
                )
            chunks.append(chunk)
        payload = b"".join(chunks)
        read_complete = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise SwitchError(
                f"Runtime rebind marker changed while reading: {path}"
            ) from exc
        if (
            _runtime_rebind_marker_identity(read_complete) != identity
            or _runtime_rebind_marker_identity(after) != identity
        ):
            raise SwitchError(
                f"Runtime rebind marker changed while reading: {path}"
            )
    finally:
        os.close(descriptor)
    try:
        marker = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SwitchError(f"Runtime rebind marker JSON is invalid: {path}") from exc
    return marker, identity


def _validated_runtime_rebind_bundle_marker(
    store: Store,
    raw: Mapping[str, object],
) -> dict[str, object]:
    base_fields = {"schema_version", "state", "artifacts"}
    marker_fields = set(raw)
    schema_version = raw.get("schema_version")
    if schema_version == 3 or (schema_version == 5 and "bundle_scope" not in raw):
        bundle_scope = _RUNTIME_BINDING_BUNDLE_SCOPE_FULL
        allowed_marker_fields = (
            base_fields,
            base_fields | {"executable_swap"},
        )
        required_roles = _RUNTIME_BINDING_BUNDLE_REQUIRED_ROLES
        allowed_roles = (
            _RUNTIME_BINDING_BUNDLE_REQUIRED_ROLES
            | _RUNTIME_BINDING_BUNDLE_OPTIONAL_ROLES
        )
    elif schema_version in {4, 5}:
        bundle_scope = raw.get("bundle_scope")
        if bundle_scope != _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY:
            raise SwitchError("Runtime rebind bundle marker scope is invalid")
        scoped_fields = base_fields | {"bundle_scope"}
        allowed_marker_fields = (
            scoped_fields,
            scoped_fields | {"executable_swap"},
        )
        required_roles = _RUNTIME_BINDING_BUNDLE_CLI_ONLY_REQUIRED_ROLES
        allowed_roles = _RUNTIME_BINDING_BUNDLE_CLI_ONLY_REQUIRED_ROLES
    else:
        raise SwitchError("Runtime rebind bundle marker schema is invalid")
    if schema_version == 5:
        allowed_marker_fields = tuple(fields | {"update", "allow_absent_profile", "directories"} for fields in allowed_marker_fields)
        _validate_runtime_binding_update_identity(store, raw.get("update"))
        if type(raw.get("allow_absent_profile")) is not bool:
            raise SwitchError("Runtime rebind absence contract is invalid")
    if marker_fields not in allowed_marker_fields:
        raise SwitchError("Runtime rebind bundle marker fields are invalid")
    raw_entries = raw.get("artifacts")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SwitchError("Runtime rebind bundle marker artifacts are invalid")
    expected_paths = _runtime_binding_bundle_expected_paths(store)
    validated_entries: list[dict[str, object]] = []
    roles: list[str] = []
    paths: list[Path] = []
    for raw_entry in raw_entries:
        if (
            not isinstance(raw_entry, dict)
            or set(raw_entry)
            != {"role", "path", "old_state", "new_state"}
        ):
            raise SwitchError("Runtime rebind bundle marker entry is invalid")
        role = raw_entry.get("role")
        raw_path = raw_entry.get("path")
        if not isinstance(role, str) or not role:
            raise SwitchError("Runtime rebind bundle marker role is invalid")
        if not isinstance(raw_path, str) or not raw_path:
            raise SwitchError("Runtime rebind bundle marker path is invalid")
        path = Path(raw_path)
        roles.append(role)
        paths.append(path)
        validated_entries.append(
            {
                "role": role,
                "path": raw_path,
                "old_state": _validated_runtime_rebind_state(
                    raw_entry.get("old_state"),
                    label=f"{role} old state",
                    allow_missing=role != "manifest" or raw.get("allow_absent_profile") is True,
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                ),
                "new_state": _validated_runtime_rebind_state(
                    raw_entry.get("new_state"),
                    label=f"{role} new state",
                    allow_missing=role in {"profile_auth", "active_runtime_auth"},
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                ),
            }
        )

    if len(set(roles)) != len(roles) or len(set(paths)) != len(paths):
        raise SwitchError("Runtime rebind bundle marker has duplicate targets")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if _runtime_binding_bundle_paths_overlap(path, other):
                raise SwitchError(
                    "Runtime rebind bundle marker has overlapping targets"
                )
    supplied_roles = set(roles)
    if supplied_roles - allowed_roles:
        raise SwitchError("Runtime rebind bundle marker has unexpected targets")
    if required_roles - supplied_roles:
        raise SwitchError(
            "Runtime rebind bundle marker is missing required targets"
        )
    expected_order = [
        role
        for role in _RUNTIME_BINDING_BUNDLE_ACTIVATION_ORDER
        if role in supplied_roles
    ]
    if roles != expected_order:
        raise SwitchError(
            "Runtime rebind bundle marker activation order is invalid"
        )
    for entry, path in zip(validated_entries, paths):
        role = entry["role"]
        if path != expected_paths[role]:  # type: ignore[index]
            raise SwitchError(
                f"Runtime rebind bundle marker target is invalid: {role}"
            )
        _validate_runtime_rebind_bundle_target_route(store, path)
        new_state = entry["new_state"]
        if (
            not isinstance(new_state, dict)
            or (new_state.get("kind") != "missing" and new_state.get("mode")
            != _runtime_binding_bundle_expected_mode(role))  # type: ignore[arg-type]
        ):
            raise SwitchError(
                f"Runtime rebind bundle marker mode is invalid: {role}"
            )
    if schema_version == 5:
        _validate_runtime_binding_directories(store, raw.get("directories"), paths)
    validated = dict(raw)
    validated["artifacts"] = validated_entries
    if "executable_swap" in raw:
        executable_swap = (
            _validated_runtime_rebind_executable_swap_marker(
                store,
                raw.get("executable_swap"),
            )
        )
        executable_paths = _runtime_rebind_executable_swap_paths(
            executable_swap
        )
        for executable_path in executable_paths:
            for artifact_path in paths:
                if _runtime_binding_bundle_paths_overlap(
                    executable_path,
                    artifact_path,
                ):
                    raise SwitchError(
                        "Runtime rebind executable swap overlaps a bundle "
                        "target"
                    )
        validated["executable_swap"] = executable_swap
    if (
        bundle_scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY
        and "executable_swap" not in validated
    ):
        raise SwitchError(
            "Runtime rebind CLI-only marker requires an executable swap"
        )
    return validated


def _validated_runtime_rebind_marker(
    store: Store,
    raw: object,
) -> dict[str, object]:
    schema_version = (
        raw.get("schema_version")
        if isinstance(raw, dict)
        else None
    )
    if (
        not isinstance(raw, dict)
        or type(schema_version) is not int
        or schema_version not in {1, 2, 3, 4, 5}
    ):
        raise SwitchError("Runtime rebind marker schema is invalid")
    state = raw.get("state")
    if state not in {"prepared", "committed"}:
        raise SwitchError("Runtime rebind marker state is invalid")
    if raw.get("schema_version") in {3, 4, 5}:
        return _validated_runtime_rebind_bundle_marker(store, raw)
    expected_manifest = store.manifest_path("internal")
    expected_launcher = store.bin_dir / "codex-internal-app"
    if raw.get("manifest_path") != str(expected_manifest):
        raise SwitchError("Runtime rebind manifest target is invalid")
    if raw.get("launcher_path") != str(expected_launcher):
        raise SwitchError("Runtime rebind launcher target is invalid")
    validated = dict(raw)
    validated["old_manifest"] = _validated_runtime_rebind_state(
        raw.get("old_manifest"),
        label="old manifest",
        allow_missing=False,
    )
    validated["old_launcher"] = _validated_runtime_rebind_state(
        raw.get("old_launcher"),
        label="old launcher",
        allow_missing=True,
    )
    validated["new_manifest"] = _validated_runtime_rebind_state(
        raw.get("new_manifest"),
        label="new manifest",
        allow_missing=False,
    )
    validated["new_launcher"] = _validated_runtime_rebind_state(
        raw.get("new_launcher"),
        label="new launcher",
        allow_missing=False,
    )
    if raw.get("schema_version") == 2:
        expected_receipt = capability_receipt_path_for_launcher(
            expected_launcher
        )
        if raw.get("receipt_path") != str(expected_receipt):
            raise SwitchError("Runtime rebind receipt target is invalid")
        validated["old_receipt"] = _validated_runtime_rebind_state(
            raw.get("old_receipt"),
            label="old receipt",
            allow_missing=True,
        )
        validated["new_receipt"] = _validated_runtime_rebind_state(
            raw.get("new_receipt"),
            label="new receipt",
            allow_missing=False,
        )
    return validated


def _recover_runtime_binding_rebind(
    store: Store,
    *,
    expected_marker_identity: (
        tuple[int, int, int, int, int, int] | None
    ) = None,
) -> None:
    marker_path = _runtime_rebind_marker_path(store)
    if not _runtime_rebind_marker_present(marker_path):
        if expected_marker_identity is not None:
            raise SwitchError(
                "Runtime rebind marker changed before recovery: "
                f"{marker_path}"
            )
        return
    raw_marker, marker_identity = _read_runtime_rebind_marker(marker_path)
    if (
        expected_marker_identity is not None
        and marker_identity != expected_marker_identity
    ):
        raise SwitchError(
            f"Runtime rebind marker changed before recovery: {marker_path}"
        )
    marker = _validated_runtime_rebind_marker(
        store,
        raw_marker,
    )
    if marker["schema_version"] in {3, 4, 5}:
        raw_entries = marker["artifacts"]
        if not isinstance(raw_entries, list):
            raise SwitchError(
                "Runtime rebind bundle marker artifacts are invalid"
            )
        entries = [
            (
                Path(entry["path"]),
                entry["old_state"],
                entry["new_state"],
            )
            for entry in raw_entries
        ]
        if marker.get("schema_version") == 5:
            _require_runtime_binding_directories(store, marker["directories"])
        executable_swap = marker.get("executable_swap")
        if executable_swap is not None:
            if not isinstance(executable_swap, dict):
                raise SwitchError(
                    "Runtime rebind executable swap marker is invalid"
                )
            _runtime_rebind_executable_swap_phase(
                store,
                executable_swap,
                foreign_message=(
                    "Runtime rebind recovery found foreign binary state"
                ),
            )
        for path, old_state, new_state in entries:
            current = _runtime_rebind_bundle_file_state(
                store,
                path,
                max_payload_bytes=_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES,
            )
            if not (
                _runtime_rebind_state_matches(current, old_state)
                or _runtime_rebind_state_matches(current, new_state)
            ):
                raise SwitchError(
                    "Runtime rebind recovery found foreign target state: "
                    f"{path}"
                )
        if executable_swap is not None:
            marker_state = marker["state"]
            if not isinstance(marker_state, str):
                raise SwitchError(
                    "Runtime rebind executable swap marker state is invalid"
                )
            _recover_runtime_rebind_executable_swap(
                store,
                executable_swap,
                marker_state=marker_state,
                marker_path=marker_path,
                marker_identity=marker_identity,
            )
        desired_entries = [
            (
                path,
                new_state
                if marker["state"] == "committed"
                else old_state,
            )
            for path, old_state, new_state in entries
        ]
        recorded_states = {
            path: (old_state, new_state)
            for path, old_state, new_state in entries
        }
        for path, desired_state in desired_entries:
            current = _runtime_rebind_bundle_file_state(
                store,
                path,
                max_payload_bytes=_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES,
            )
            old_state, new_state = recorded_states[path]
            if not (
                _runtime_rebind_state_matches(current, old_state)
                or _runtime_rebind_state_matches(current, new_state)
            ):
                raise SwitchError(
                    "Runtime rebind recovery found foreign target state: "
                    f"{path}"
                )
            if not _runtime_rebind_state_matches(current, desired_state):
                _require_runtime_rebind_marker_identity(
                    marker_path,
                    marker_identity,
                    phase=f"recovery of {path}",
                )
                _apply_runtime_rebind_bundle_state(
                    store,
                    path,
                    desired_state,
                )
        for path, desired_state in desired_entries:
            if not _runtime_rebind_state_matches(
                _runtime_rebind_bundle_file_state(
                    store,
                    path,
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                ),
                desired_state,
            ):
                raise SwitchError(
                    f"Runtime rebind recovery did not converge: {path}"
                )
        if executable_swap is not None:
            _require_runtime_rebind_executable_swap_phase(
                store,
                executable_swap,
                (
                    "new_bound"
                    if marker["state"] == "committed"
                    else "initial"
                ),
                phase="recovery completion",
            )
        if marker.get("schema_version") == 5 and marker["state"] == "prepared":
            _rollback_runtime_binding_directories(store, marker["directories"])
        if marker.get("update") is not None:
            _write_runtime_binding_terminal_receipt(store, marker)
        _durable_unlink_runtime_rebind_path(
            marker_path,
            expected_identity=marker_identity,
        )
        return
    manifest_path = store.manifest_path("internal")
    launcher_path = store.bin_dir / "codex-internal-app"
    pairs = [
        (
            manifest_path,
            marker["old_manifest"],
            marker["new_manifest"],
        ),
        (
            launcher_path,
            marker["old_launcher"],
            marker["new_launcher"],
        ),
    ]
    if marker["schema_version"] == 2:
        receipt_path = capability_receipt_path_for_launcher(launcher_path)
        pairs.append(
            (
                receipt_path,
                marker["old_receipt"],
                marker["new_receipt"],
            )
        )
    for path, old_state, new_state in pairs:
        current = _runtime_rebind_file_state(path)
        if not (
            _runtime_rebind_state_matches(current, old_state)  # type: ignore[arg-type]
            or _runtime_rebind_state_matches(current, new_state)  # type: ignore[arg-type]
        ):
            raise SwitchError(
                f"Runtime rebind recovery found foreign target state: {path}"
            )
    desired_key = "new" if marker["state"] == "committed" else "old"
    desired_pairs = [
        (manifest_path, marker[f"{desired_key}_manifest"]),
        (launcher_path, marker[f"{desired_key}_launcher"]),
    ]
    if marker["schema_version"] == 2:
        desired_pairs.append(
            (
                capability_receipt_path_for_launcher(launcher_path),
                marker[f"{desired_key}_receipt"],
            )
        )
    for path, desired_state in desired_pairs:
        if not _runtime_rebind_state_matches(
            _runtime_rebind_file_state(path),
            desired_state,  # type: ignore[arg-type]
        ):
            _require_runtime_rebind_marker_identity(
                marker_path,
                marker_identity,
                phase=f"legacy recovery of {path}",
            )
            _apply_runtime_rebind_state(path, desired_state)  # type: ignore[arg-type]
    for path, desired_state in desired_pairs:
        if not _runtime_rebind_state_matches(
            _runtime_rebind_file_state(path),
            desired_state,  # type: ignore[arg-type]
        ):
            raise SwitchError(f"Runtime rebind recovery did not converge: {path}")
    _durable_unlink_runtime_rebind_path(
        marker_path,
        expected_identity=marker_identity,
    )


def _runtime_binding_missing_directories(paths: tuple[Path, ...]) -> list[dict[str, object]]:
    missing: set[Path] = set()
    for target in paths:
        path = target.parent
        while not os.path.lexists(path):
            missing.add(path)
            path = path.parent
    return [{"path": str(path), "identity": None} for path in sorted(missing, key=lambda item: (len(item.parts), str(item)))]


def _validate_runtime_binding_directories(store: Store, raw: object, targets: list[Path]) -> None:
    if not isinstance(raw, list):
        raise SwitchError("Runtime binding directory journal is invalid")
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"path", "identity"} or not isinstance(entry["path"], str):
            raise SwitchError("Runtime binding directory journal entry is invalid")
        path = Path(entry["path"])
        identity = entry["identity"]
        if path in seen or not path.is_absolute() or path == store.root or not any(path in target.parents for target in targets):
            raise SwitchError("Runtime binding directory ownership path is invalid")
        if identity is not None and (not isinstance(identity, list) or len(identity) != 2 or any(type(value) is not int or value < 0 for value in identity)):
            raise SwitchError("Runtime binding directory identity is invalid")
        _validate_runtime_rebind_bundle_target_route(store, path)
        seen.add(path)
    if [entry["path"] for entry in raw] != [str(path) for path in sorted(seen, key=lambda item: (len(item.parts), str(item)))]:
        raise SwitchError("Runtime binding directory journal order is invalid")


def _require_runtime_binding_directories(store: Store, directories: list[dict[str, object]]) -> None:
    for entry in directories:
        path = Path(entry["path"])
        _validate_runtime_rebind_bundle_target_route(store, path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != entry["identity"]:
            raise SwitchError("Runtime binding recovery found foreign directory state")


def _rollback_runtime_binding_directories(store: Store, directories: list[dict[str, object]]) -> None:
    _require_runtime_binding_directories(store, directories)
    for entry in reversed(directories):
        path = Path(entry["path"])
        opened = _open_runtime_rebind_bundle_parent(store, path, create_missing=False)
        if opened is None:
            continue
        descriptor, leaf = opened
        try:
            try:
                info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != entry["identity"]:
                raise SwitchError("Runtime binding rollback found foreign directory state")
            try:
                os.rmdir(leaf, dir_fd=descriptor)
                os.fsync(descriptor)
            except OSError as exc:
                raise SwitchError("Runtime binding rollback directory contains foreign state") from exc
        finally:
            os.close(descriptor)


def runtime_binding_terminal_receipt_path(store: Store, update_id: str) -> Path:
    if not isinstance(update_id, str) or not update_id or len(update_id) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in update_id):
        raise SwitchError("Runtime binding update identity is invalid")
    return store.root / "runtime-binding-receipts" / (update_id + ".json")


def _validate_runtime_binding_update_identity(store: Store, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"update_id", "input_fingerprint", "transaction_id", "receipt_path"}:
        raise SwitchError("Runtime binding update evidence is invalid")
    path = runtime_binding_terminal_receipt_path(store, raw.get("update_id"))
    _validate_runtime_binding_executable_digest(raw.get("input_fingerprint"), label="update input")
    transaction_id = raw.get("transaction_id")
    if not isinstance(transaction_id, str) or len(transaction_id) != 32 or any(c not in "0123456789abcdef" for c in transaction_id):
        raise SwitchError("Runtime binding transaction identity is invalid")
    if raw.get("receipt_path") != str(path):
        raise SwitchError("Runtime binding terminal receipt destination is invalid")
    _validate_runtime_rebind_bundle_target_route(store, path)
    return dict(raw)


def read_runtime_binding_terminal_receipt(store: Store, update_id: str, *, input_fingerprint: str | None = None, _from_pending: bool = True) -> dict[str, object] | None:
    path = runtime_binding_terminal_receipt_path(store, update_id)
    state = _runtime_rebind_bundle_file_state(store, path, max_payload_bytes=_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES)
    if state.get("kind") == "missing":
        marker_path = _runtime_rebind_marker_path(store)
        if not _from_pending or not _runtime_rebind_marker_present(marker_path):
            return None
        raw, _identity = _read_runtime_rebind_marker(marker_path)
        marker = _validated_runtime_rebind_marker(store, raw)
        if marker.get("state") != "committed" or marker.get("update", {}).get("update_id") != update_id:
            return None
        receipt = _runtime_binding_terminal_receipt_payload(store, marker)
        if input_fingerprint is not None and receipt["input_fingerprint"] != input_fingerprint:
            raise SwitchError("Runtime binding terminal receipt input identity changed")
        return receipt
    try:
        receipt = json.loads(_runtime_rebind_state_payload(state))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SwitchError("Runtime binding terminal receipt is malformed") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("state") not in {"committed", "rolled_back"}:
        raise SwitchError("Runtime binding terminal receipt is invalid")
    identity = _validate_runtime_binding_update_identity(store, {key: receipt.get(key) for key in ("update_id", "input_fingerprint", "transaction_id", "receipt_path")})
    if identity["update_id"] != update_id or (input_fingerprint is not None and identity["input_fingerprint"] != input_fingerprint):
        raise SwitchError("Runtime binding terminal receipt input identity changed")
    if set(receipt) != {"schema_version", "state", "bundle_scope", "update_id", "input_fingerprint", "transaction_id", "receipt_path", "artifacts"} or state.get("mode") != 0o600:
        raise SwitchError("Runtime binding terminal receipt fields or mode are invalid")
    scope = receipt["bundle_scope"]
    if scope not in {_RUNTIME_BINDING_BUNDLE_SCOPE_FULL, _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY}:
        raise SwitchError("Runtime binding terminal receipt scope is invalid")
    required_roles = (_RUNTIME_BINDING_BUNDLE_CLI_ONLY_REQUIRED_ROLES
        if scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY else _RUNTIME_BINDING_BUNDLE_REQUIRED_ROLES)
    allowed_roles = (required_roles if scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY
        else required_roles | _RUNTIME_BINDING_BUNDLE_OPTIONAL_ROLES)
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SwitchError("Runtime binding terminal receipt artifacts are invalid")
    roles, paths = set(), set()
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"role", "path", "state"}:
            raise SwitchError("Runtime binding terminal receipt artifact is invalid")
        role, raw_path, artifact_state = entry["role"], entry["path"], entry["state"]
        if not isinstance(role, str) or role not in allowed_roles or role in roles:
            raise SwitchError("Runtime binding terminal receipt role is invalid")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or os.path.normpath(raw_path) != raw_path or raw_path in paths:
            raise SwitchError("Runtime binding terminal receipt path is invalid")
        if not isinstance(artifact_state, dict):
            raise SwitchError("Runtime binding terminal receipt state is invalid")
        if artifact_state.get("kind") == "missing":
            if set(artifact_state) != {"kind"} or (receipt["state"] == "committed" and role not in {"profile_auth", "active_runtime_auth"}):
                raise SwitchError("Runtime binding terminal receipt absence is invalid")
        elif artifact_state.get("kind") == "file":
            if set(artifact_state) != {"kind", "mode", "sha256"} or type(artifact_state["mode"]) is not int:
                raise SwitchError("Runtime binding terminal receipt file state is invalid")
            _validate_runtime_binding_executable_digest(artifact_state["sha256"], label="terminal receipt artifact")
            if receipt["state"] == "committed" and artifact_state["mode"] != _runtime_binding_bundle_expected_mode(role):
                raise SwitchError("Runtime binding terminal receipt file mode is invalid")
        else:
            raise SwitchError("Runtime binding terminal receipt state kind is invalid")
        roles.add(role)
        paths.add(raw_path)
    if not required_roles <= roles:
        raise SwitchError("Runtime binding terminal receipt is missing required artifacts")
    return receipt


def _runtime_binding_terminal_receipt_payload(store: Store, marker: Mapping[str, object]) -> dict[str, object]:
    update = _validate_runtime_binding_update_identity(store, marker.get("update"))
    receipt = {"schema_version": 1, **update, "bundle_scope": marker.get("bundle_scope", _RUNTIME_BINDING_BUNDLE_SCOPE_FULL), "state": "committed" if marker["state"] == "committed" else "rolled_back",
        "artifacts": [{"role": entry["role"], "path": entry["path"], "state": {key: value for key, value in entry["new_state" if marker["state"] == "committed" else "old_state"].items() if key in {"kind", "mode", "sha256"}}} for entry in marker["artifacts"]]}
    return receipt


def _write_runtime_binding_terminal_receipt(store: Store, marker: Mapping[str, object]) -> dict[str, object]:
    receipt = _runtime_binding_terminal_receipt_payload(store, marker)
    update = marker["update"]
    existing = read_runtime_binding_terminal_receipt(store, update["update_id"], input_fingerprint=update["input_fingerprint"], _from_pending=False)
    if existing is not None and existing != receipt:
        raise SwitchError("Runtime binding terminal receipt conflicts with transaction")
    if existing is None:
        _atomic_write_runtime_rebind_bundle_path(store, Path(update["receipt_path"]), (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(), mode=0o600)
    return receipt


def commit_runtime_binding_bundle(
    locked_store: LockedStoreMutation,
    *,
    artifacts: tuple[RuntimeBindingTextArtifact, ...],
    executable_swap: RuntimeBindingExecutableSwap | None = None,
    input_validator: Callable[[], None] | None = None,
    prepared_validator: Callable[[], None] | None = None,
    retire_executable_backup: bool = False,
    fault_hook: Callable[[str], None] | None = None,
    bundle_scope: str = _RUNTIME_BINDING_BUNDLE_SCOPE_FULL,
    allow_absent_profile: bool = False,
    update_id: str | None = None,
    input_fingerprint: str | None = None,
) -> dict[str, object] | None:
    locked_store.revalidate()
    if input_validator is not None and not callable(input_validator):
        raise SwitchError("Runtime binding bundle input validator is invalid")
    if prepared_validator is not None and not callable(prepared_validator):
        raise SwitchError(
            "Runtime binding bundle prepared validator is invalid"
        )
    if type(retire_executable_backup) is not bool:
        raise SwitchError(
            "Runtime binding bundle backup retirement flag is invalid"
        )
    if retire_executable_backup and executable_swap is None:
        raise SwitchError(
            "Runtime binding bundle backup retirement requires an executable swap"
        )
    if bundle_scope not in {
        _RUNTIME_BINDING_BUNDLE_SCOPE_FULL,
        _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY,
    }:
        raise SwitchError("Runtime binding bundle scope is invalid")
    if (
        bundle_scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY
        and executable_swap is None
    ):
        raise SwitchError(
            "Runtime binding CLI-only bundle requires an executable swap"
        )
    store = locked_store.store
    update = None
    if update_id is not None:
        update = _validate_runtime_binding_update_identity(store, {
            "update_id": update_id, "input_fingerprint": input_fingerprint,
            "transaction_id": uuid.uuid4().hex,
            "receipt_path": str(runtime_binding_terminal_receipt_path(store, update_id)),
        })
        if read_runtime_binding_terminal_receipt(store, update_id, input_fingerprint=input_fingerprint) is not None:
            raise SwitchError("Runtime binding update already has a terminal receipt")
    if type(allow_absent_profile) is not bool or (allow_absent_profile and update is None):
        raise SwitchError("Absent profile publication requires a bound update")
    marker_path = _runtime_rebind_marker_path(store)
    if _runtime_rebind_marker_present(marker_path):
        raise SwitchError("Pending runtime rebind requires recovery before commit")
    ordered_artifacts = _validated_runtime_binding_bundle_artifacts(
        store,
        artifacts,
        bundle_scope=bundle_scope,
    )
    executable_swap_marker: dict[str, object] | None = None
    if executable_swap is not None:
        executable_swap_marker = (
            _validated_runtime_binding_executable_swap(
                store,
                executable_swap,
            )
        )
        executable_paths = _runtime_rebind_executable_swap_paths(
            executable_swap_marker
        )
        for executable_path in executable_paths:
            for artifact in ordered_artifacts:
                if _runtime_binding_bundle_paths_overlap(
                    executable_path,
                    artifact.path,
                ):
                    raise SwitchError(
                        "Runtime binding executable swap overlaps a bundle "
                        "target"
                    )
        _require_runtime_rebind_executable_swap_phase(
            store,
            executable_swap_marker,
            "initial",
            phase="preflight",
        )
    if input_validator is not None:
        input_validator()
    entries: list[dict[str, object]] = []
    for artifact in ordered_artifacts:
        old_state = _runtime_rebind_bundle_file_state(
            store,
            artifact.path,
            max_payload_bytes=_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES,
        )
        if artifact.role == "manifest":
            if old_state.get("kind") not in ({"file", "missing"} if allow_absent_profile else {"file"}):
                raise SwitchError("Internal manifest is not a regular file")
        elif old_state.get("kind") not in {"file", "missing"}:
            raise SwitchError(
                "Runtime binding bundle target is not a regular file: "
                f"{artifact.role}"
            )
        entries.append(
            {
                "role": artifact.role,
                "path": str(artifact.path),
                "old_state": old_state,
                "new_state": ({"kind": "missing"} if artifact.payload is None else _runtime_rebind_payload_state(
                    artifact.payload,
                    artifact.mode,
                )),
            }
        )
    if input_validator is not None:
        input_validator()
    if executable_swap_marker is not None:
        _require_runtime_rebind_executable_swap_phase(
            store,
            executable_swap_marker,
            "initial",
            phase="marker publication",
        )
    marker: dict[str, object] = {
        "schema_version": (
            4
            if bundle_scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY
            else 3
        ),
        "state": "prepared",
        "artifacts": entries,
    }
    if update is not None:
        marker["schema_version"] = 5
        marker["update"] = update
        marker["allow_absent_profile"] = allow_absent_profile
        marker["directories"] = _runtime_binding_missing_directories(tuple(artifact.path for artifact in ordered_artifacts))
    if bundle_scope == _RUNTIME_BINDING_BUNDLE_SCOPE_CLI_ONLY:
        marker["bundle_scope"] = bundle_scope
    if executable_swap_marker is not None:
        marker["executable_swap"] = executable_swap_marker
    write_json(marker_path, marker)
    written_marker, prepared_marker_identity = (
        _read_runtime_rebind_marker(marker_path)
    )
    if written_marker != marker:
        raise SwitchError(
            f"Runtime rebind marker changed after publication: {marker_path}"
        )
    terminal_committed = False
    marker_retired = False
    backup_retired = False
    committed_marker_identity: (
        tuple[int, int, int, int, int, int] | None
    ) = None

    def require_promoted_generation(phase: str) -> None:
        if input_validator is not None:
            input_validator()
        if executable_swap_marker is not None:
            _require_runtime_rebind_executable_swap_phase(
                store,
                executable_swap_marker,
                "new_bound",
                phase=phase,
            )
        for entry in entries:
            _require_runtime_rebind_bundle_state(
                store,
                Path(entry["path"]),
                entry["new_state"],
                phase=phase,
                max_payload_bytes=(
                    _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                ),
            )
        _require_runtime_rebind_marker_identity(
            marker_path,
            prepared_marker_identity,
            phase=phase,
        )

    try:
        if fault_hook is not None:
            fault_hook("after_marker")
        if update is not None:
            for directory in marker["directories"]:
                path = Path(directory["path"])
                opened = _open_runtime_rebind_bundle_parent(store, path, create_missing=False)
                if opened is None:
                    raise SwitchError("Runtime binding directory parent is unavailable")
                descriptor, leaf = opened
                try:
                    os.mkdir(leaf, mode=0o700, dir_fd=descriptor)
                    info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory["identity"] = [info.st_dev, info.st_ino]
                _require_runtime_rebind_marker_identity(marker_path, prepared_marker_identity, phase="directory creation")
                write_json(marker_path, marker)
                written, prepared_marker_identity = _read_runtime_rebind_marker(marker_path)
                if written != marker:
                    raise SwitchError("Runtime binding directory journal changed")
        if executable_swap_marker is not None:
            bound_path, candidate_path, backup_path = (
                _runtime_rebind_executable_swap_paths(
                    executable_swap_marker
                )
            )
            old_state, new_state, _missing_state = (
                _runtime_rebind_executable_swap_expected_states(
                    executable_swap_marker
                )
            )
            if input_validator is not None:
                input_validator()
            for entry in entries:
                _require_runtime_rebind_bundle_state(
                    store,
                    Path(entry["path"]),
                    entry["old_state"],
                    phase="bound-to-backup prepromotion",
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                )
            _require_runtime_rebind_executable_swap_phase(
                store,
                executable_swap_marker,
                "initial",
                phase="bound-to-backup prepromotion",
            )
            if fault_hook is not None:
                fault_hook("before_bound_to_backup")
            _require_runtime_rebind_marker_identity(
                marker_path,
                prepared_marker_identity,
                phase="bound-to-backup promotion",
            )
            if old_state["kind"] != "missing":
                _rename_runtime_rebind_executable(
                    store,
                    source_path=bound_path,
                    destination_path=backup_path,
                    expected_source_state=old_state,
                    phase="bound-to-backup promotion",
                )
            _require_runtime_rebind_executable_swap_phase(
                store,
                executable_swap_marker,
                "old_backed_up",
                phase="bound-to-backup promotion",
            )
            if fault_hook is not None:
                fault_hook("after_bound_to_backup")
            if input_validator is not None:
                input_validator()
            for entry in entries:
                _require_runtime_rebind_bundle_state(
                    store,
                    Path(entry["path"]),
                    entry["old_state"],
                    phase="candidate-to-bound prepromotion",
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                )
            _require_runtime_rebind_executable_swap_phase(
                store,
                executable_swap_marker,
                "old_backed_up",
                phase="candidate-to-bound prepromotion",
            )
            if fault_hook is not None:
                fault_hook("before_candidate_to_bound")
            _require_runtime_rebind_marker_identity(
                marker_path,
                prepared_marker_identity,
                phase="candidate-to-bound promotion",
            )
            _rename_runtime_rebind_executable(
                store,
                source_path=candidate_path,
                destination_path=bound_path,
                expected_source_state=new_state,
                phase="candidate-to-bound promotion",
            )
            _require_runtime_rebind_executable_swap_phase(
                store,
                executable_swap_marker,
                "new_bound",
                phase="candidate-to-bound promotion",
            )
            if fault_hook is not None:
                fault_hook("after_candidate_to_bound")
        for index, entry in enumerate(entries):
            if input_validator is not None:
                input_validator()
            if executable_swap_marker is not None:
                _require_runtime_rebind_executable_swap_phase(
                    store,
                    executable_swap_marker,
                    "new_bound",
                    phase=f"{entry['role']} prepromotion",
                )
            for prior in entries[:index]:
                _require_runtime_rebind_bundle_state(
                    store,
                    Path(prior["path"]),
                    prior["new_state"],
                    phase=f"{entry['role']} prepromotion",
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                )
            for pending in entries[index:]:
                _require_runtime_rebind_bundle_state(
                    store,
                    Path(pending["path"]),
                    pending["old_state"],
                    phase=f"{entry['role']} prepromotion",
                    max_payload_bytes=(
                        _MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES
                    ),
                )
            _require_runtime_rebind_marker_identity(
                marker_path,
                prepared_marker_identity,
                phase=f"{entry['role']} promotion",
            )
            _apply_runtime_rebind_bundle_state(
                store,
                Path(entry["path"]),
                entry["new_state"],
            )
            _require_runtime_rebind_bundle_state(
                store,
                Path(entry["path"]),
                entry["new_state"],
                phase=f"{entry['role']} promotion",
                max_payload_bytes=_MAX_RUNTIME_BINDING_TEXT_ARTIFACT_BYTES,
            )
            if fault_hook is not None:
                fault_hook(f"after_{entry['role']}")
        require_promoted_generation("prepared generation validation")
        if prepared_validator is not None:
            if fault_hook is not None:
                fault_hook("before_prepared_validator")
            prepared_validator()
            if fault_hook is not None:
                fault_hook("after_prepared_validator")
            require_promoted_generation(
                "post-validator committed marker publication"
            )
        marker["state"] = "committed"
        write_json(marker_path, marker)
        written_marker, committed_marker_identity = (
            _read_runtime_rebind_marker(marker_path)
        )
        if written_marker != marker:
            raise SwitchError(
                "Runtime rebind committed marker changed after publication: "
                f"{marker_path}"
            )
        terminal_committed = True
        if fault_hook is not None:
            fault_hook("after_committed_marker")
        _recover_runtime_binding_rebind(
            store,
            expected_marker_identity=committed_marker_identity,
        )
        marker_retired = True
        if retire_executable_backup:
            if executable_swap_marker is None:
                raise SwitchError(
                    "Runtime binding bundle executable swap is unavailable"
                )
            _retire_runtime_rebind_executable_backup(
                store,
                executable_swap_marker,
            )
            backup_retired = True
        if fault_hook is not None:
            fault_hook("after_marker_retirement")
    except Exception as commit_error:
        if marker_retired and terminal_committed:
            if retire_executable_backup and not backup_retired:
                raise
            return read_runtime_binding_terminal_receipt(store, update_id, input_fingerprint=input_fingerprint) if update_id else None
        try:
            _recover_runtime_binding_rebind(
                store,
                expected_marker_identity=(
                    committed_marker_identity
                    if terminal_committed
                    else prepared_marker_identity
                ),
            )
        except Exception as recovery_error:
            raise SwitchError(
                f"Runtime rebind commit failed: {commit_error}; recovery failed: "
                f"{recovery_error}"
            ) from commit_error
        if terminal_committed:
            if retire_executable_backup:
                if executable_swap_marker is None:
                    raise SwitchError(
                        "Runtime binding bundle executable swap is unavailable"
                    )
                _retire_runtime_rebind_executable_backup(
                    store,
                    executable_swap_marker,
                )
            return read_runtime_binding_terminal_receipt(store, update_id, input_fingerprint=input_fingerprint) if update_id else None
        raise

    return read_runtime_binding_terminal_receipt(store, update_id, input_fingerprint=input_fingerprint) if update_id else None


class LockedStoreMutation:
    """Own one store lock across a legacy prewrite and transaction dispatch."""

    def __init__(
        self,
        store: Store,
        *,
        operation: str,
        create_if_missing: bool,
        recovery_guard: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.operation = operation
        self.create_if_missing = create_if_missing
        self.recovery_guard = recovery_guard
        self._lock = _StoreLock(store.root)
        self._active = False
        self._root_created = False

    def __enter__(self) -> LockedStoreMutation:
        try:
            self.store.root.lstat()
        except FileNotFoundError:
            if not self.create_if_missing:
                raise SwitchError(f"Profile store does not exist: {self.store.root}")
            self.store.root.parent.mkdir(parents=True, exist_ok=True)
            FilesystemAdapter().create_store_root(self.store.root)
            self._root_created = True
        self._lock.__enter__()
        try:
            self._lock.revalidate()
            if self.recovery_guard is not None:
                self.recovery_guard()
            _recover_runtime_binding_rebind(self.store)
            classification = _classify_store_recovery(self.store)
            _raise_blocking_store_evidence(
                classification,
                operation=self.operation,
            )
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        self._active = True
        return self

    @property
    def root_created(self) -> bool:
        return self._root_created

    def revalidate(self) -> None:
        if not self._active:
            raise SwitchError("Store mutation lock is not active")
        self._lock.revalidate()

    def _shared_materializer_lease_descriptor(self, store: Store) -> int:
        """Expose the active exact-root lease only to locked shared reconcile."""
        if not self._active or store is not self.store:
            raise SwitchError("Store mutation lock is not active for this store")
        descriptor = self._lock._active_root_descriptor()
        try:
            current = store.root.lstat()
            locked = os.fstat(descriptor)
        except OSError as exc:
            raise SwitchError("Profile store lock descriptor is unavailable") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or not stat.S_ISDIR(locked.st_mode)
            or (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino)
        ):
            raise SwitchError("Profile store lock descriptor does not match store root")
        return descriptor

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._active = False
        self._lock.__exit__(exc_type, exc, traceback)

    def _reuse_context(self, store: Store) -> object:
        if not self._active or store is not self.store:
            raise SwitchError("Already-locked transaction dispatch is not active")
        self._lock.revalidate()
        return nullcontext(self)

    def execute_transaction(
        self,
        request: TransactionRequest,
        *,
        dry_run: bool = False,
    ) -> TransactionReceipt:
        return _execute_transaction(
            self.store,
            request,
            dry_run=dry_run,
            locked_store=self,
        )


def locked_store_mutation(
    store: Store,
    *,
    operation: str,
    create_if_missing: bool = False,
    recovery_guard: Callable[[], None] | None = None,
) -> LockedStoreMutation:
    return LockedStoreMutation(
        store,
        operation=operation,
        create_if_missing=create_if_missing,
        recovery_guard=recovery_guard,
    )


class _CustomSwitchMutationGate:
    """Keep the compatibility switch route behind the shared mutation lock."""

    def __init__(self, store: Store, *, dry_run: bool) -> None:
        self.store = store
        self.dry_run = dry_run
        self._lock = _StoreLock(store.root)

    def __enter__(self) -> _CustomSwitchMutationGate:
        self._lock.__enter__()
        try:
            runtime_rebind_marker = _runtime_rebind_marker_path(self.store)
            if _runtime_rebind_marker_present(runtime_rebind_marker):
                if self.dry_run:
                    raise SwitchError(
                        "Pending runtime rebind recovery blocks custom switch dry-run"
                    )
                _recover_runtime_binding_rebind(self.store)
            classification = _classify_store_recovery(self.store)
            pending = classification.pending_transaction
            captures = classification.pending_capture_profiles
            unmarked = classification.unmarked_transactions
            if captures:
                raise SwitchError(
                    "Pending capture recovery blocks custom switch for profile "
                    f"{captures[0]}"
                )
            if pending is not None and pending.lifecycle in {
                "prepared",
                "rollback_failed",
            }:
                raise SwitchError(
                    "Pending transaction recovery blocks custom switch: "
                    f"{pending.backup_dir.name}"
                )
            if pending is not None and not self.dry_run:
                cleanup_warning = _retire_pending_marker(
                    FilesystemAdapter(),
                    pending.marker,
                    outcome=pending.lifecycle,
                )
                if cleanup_warning is not None:
                    raise SwitchError(cleanup_warning)
            if unmarked:
                raise SwitchError(
                    "Unmarked transaction recovery blocks custom switch: "
                    f"{unmarked[0].backup_dir.name}"
                )
        except Exception:
            self._lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.__exit__(exc_type, exc, traceback)


def custom_switch_mutation_gate(
    store: Store,
    *,
    dry_run: bool = False,
) -> _CustomSwitchMutationGate:
    """Return the minimal locking/recovery seam for the legacy custom route."""

    return _CustomSwitchMutationGate(store, dry_run=dry_run)


def preflight_store_mutation(store: Store, *, operation: str) -> None:
    """Fail closed on unresolved store-wide evidence before a legacy writer."""

    try:
        store.root.lstat()
    except FileNotFoundError:
        return
    with _StoreLock(store.root):
        classification = _classify_store_recovery(store)
        _raise_blocking_store_evidence(classification, operation=operation)


class _PinnedCaptureParent:
    def __init__(self, path: Path, adapter: FilesystemAdapter) -> None:
        self.path = path
        self.adapter = adapter
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None
        self._canonical: Path | None = None

    def __enter__(self) -> _PinnedCaptureParent:
        flags = _capture_directory_open_flags()
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise SwitchError(
                f"Capture profiles directory cannot be pinned: {self.path}: {exc}"
            ) from exc
        info = os.fstat(descriptor)
        self._descriptor = descriptor
        self._identity = (info.st_dev, info.st_ino)
        self._canonical = self.path.resolve()
        self.adapter.bind_capture_workspace(self.path, descriptor)
        try:
            self.revalidate("capture_parent_opened")
        except Exception:
            self.adapter.unbind_capture_workspace()
            os.close(descriptor)
            self._descriptor = None
            raise
        return self

    def validate_descriptor(self, phase: str) -> None:
        descriptor = self._descriptor
        identity = self._identity
        if descriptor is None or identity is None:
            raise SwitchError("Capture profiles directory is not pinned")
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            raise SwitchError(
                f"Capture profiles descriptor changed during {phase}: {self.path}"
            )

    def revalidate(self, phase: str) -> None:
        identity = self._identity
        canonical = self._canonical
        if identity is None or canonical is None:
            raise SwitchError("Capture profiles directory is not pinned")
        self.validate_descriptor(phase)
        self.adapter.capture_parent_checkpoint(self.path, phase=phase)
        try:
            info = self.path.lstat()
        except FileNotFoundError as exc:
            raise SwitchError(
                f"Capture profiles directory changed during {phase}: {self.path}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SwitchError(
                f"Capture profiles directory changed during {phase}: {self.path}"
            )
        if (info.st_dev, info.st_ino) != identity or self.path.resolve() != canonical:
            raise SwitchError(
                f"Capture profiles directory changed during {phase}: {self.path}"
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        descriptor = self._descriptor
        self._descriptor = None
        self.adapter.unbind_capture_workspace()
        if descriptor is not None:
            os.close(descriptor)


def _capture_binding_for_path(
    adapter: FilesystemAdapter,
    path: Path,
) -> tuple[int, tuple[str, ...]] | None:
    root = getattr(adapter, "_capture_workspace_path", None)
    descriptor = getattr(adapter, "_capture_workspace_descriptor", None)
    if not isinstance(root, Path) or not isinstance(descriptor, int):
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    parts = tuple(relative.parts)
    if any(part in {"", ".", ".."} or "/" in part for part in parts):
        raise SwitchError(f"Capture workspace path is invalid: {path}")
    return descriptor, parts


def _relative_name(parts: tuple[str, ...]) -> str:
    if len(parts) != 1:
        raise SwitchError(f"Capture artifact is not a direct workspace leaf: {parts}")
    return parts[0]


def _open_directory_at(root_descriptor: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_descriptor)
    flags = _capture_directory_open_flags()
    try:
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SwitchError("Capture workspace component is not a directory")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_parent_at(
    root_descriptor: int,
    parts: tuple[str, ...],
) -> tuple[int, str]:
    if not parts:
        raise SwitchError("Capture operation cannot target the workspace root")
    return _open_directory_at(root_descriptor, parts[:-1]), parts[-1]


def _open_path_at(root_descriptor: int, parts: tuple[str, ...]) -> int:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    try:
        return os.open(
            leaf,
            _capture_file_open_flags(),
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)


def _open_file_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    flags: int,
) -> int:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    try:
        descriptor = os.open(
            leaf,
            _capture_file_open_flags(flags),
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SwitchError(f"Capture workspace file is not regular: {parts}")
    return descriptor


def _mkdir_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    mode: int,
) -> tuple[int, int]:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    try:
        os.mkdir(leaf, mode=mode, dir_fd=parent_descriptor)
        directory_descriptor = os.open(
            leaf,
            _capture_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        try:
            info = os.fstat(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return info.st_dev, info.st_ino
    finally:
        os.close(parent_descriptor)


def _unlink_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    missing_ok: bool,
) -> None:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    try:
        try:
            os.unlink(leaf, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise
    finally:
        os.close(parent_descriptor)


def _remove_tree_contents_fd(directory_descriptor: int) -> None:
    flags = _capture_directory_open_flags()
    for name in sorted(os.listdir(directory_descriptor)):
        info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            try:
                child_identity = os.fstat(child_descriptor)
                _remove_tree_contents_fd(child_descriptor)
            finally:
                os.close(child_descriptor)
            current = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (
                child_identity.st_dev,
                child_identity.st_ino,
            ):
                raise SwitchError(
                    f"Capture directory changed before removal: {name}"
                )
            os.rmdir(name, dir_fd=directory_descriptor)
        else:
            os.unlink(name, dir_fd=directory_descriptor)


def _remove_tree_at(root_descriptor: int, parts: tuple[str, ...]) -> None:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    flags = _capture_directory_open_flags()
    try:
        directory_descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
        try:
            directory_identity = os.fstat(directory_descriptor)
            _remove_tree_contents_fd(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        current = os.stat(
            leaf,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (
            directory_identity.st_dev,
            directory_identity.st_ino,
        ):
            raise SwitchError(f"Capture directory changed before removal: {parts}")
        os.rmdir(leaf, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _remove_path_at(root_descriptor: int, parts: tuple[str, ...]) -> None:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    try:
        try:
            info = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            directory_descriptor = os.open(
                leaf,
                _capture_directory_open_flags(),
                dir_fd=parent_descriptor,
            )
            try:
                directory_identity = os.fstat(directory_descriptor)
                _remove_tree_contents_fd(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            current = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (
                directory_identity.st_dev,
                directory_identity.st_ino,
            ):
                raise SwitchError(
                    f"Capture directory changed before removal: {parts}"
                )
            os.rmdir(leaf, dir_fd=parent_descriptor)
        else:
            os.unlink(leaf, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _require_regular_descriptor(descriptor: int, label: object) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise SwitchError(f"Capture source is not a regular file: {label}")
    return info


def _read_file_at(root_descriptor: int, parts: tuple[str, ...]) -> bytes:
    descriptor = _open_file_at(root_descriptor, parts, os.O_RDONLY)
    try:
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        offset += os.write(descriptor, data[offset:])


def _atomic_write_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    data: bytes,
    *,
    mode: int,
) -> None:
    parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
    temporary = f".{leaf}.{os.getpid()}.{os.urandom(8).hex()}"
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _required_capture_open_flag("O_NOFOLLOW"),
            mode,
            dir_fd=parent_descriptor,
        )
        temporary_created = True
        try:
            _write_all(descriptor, data)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(
            temporary,
            leaf,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        temporary_created = False
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def _copy_file_atomic_at(
    source: Path,
    root_descriptor: int,
    destination_parts: tuple[str, ...],
) -> None:
    source_descriptor = os.open(
        source,
        _capture_file_open_flags(),
    )
    try:
        info = _require_regular_descriptor(source_descriptor, source)
        data = _read_descriptor(source_descriptor)
    finally:
        os.close(source_descriptor)
    _atomic_write_at(
        root_descriptor,
        destination_parts,
        data,
        mode=stat.S_IMODE(info.st_mode),
    )


def _copy_directory_contents_fd(
    source_descriptor: int,
    destination_descriptor: int,
) -> None:
    directory_flags = _capture_directory_open_flags()
    for name in sorted(os.listdir(source_descriptor)):
        info = os.stat(name, dir_fd=source_descriptor, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(
                os.readlink(name, dir_fd=source_descriptor),
                name,
                dir_fd=destination_descriptor,
            )
        elif stat.S_ISREG(info.st_mode):
            source_file = os.open(
                name,
                _capture_file_open_flags(),
                dir_fd=source_descriptor,
            )
            try:
                opened = _require_regular_descriptor(source_file, name)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise SwitchError(f"Capture source changed while cloning: {name}")
                data = _read_descriptor(source_file)
            finally:
                os.close(source_file)
            destination_file = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _required_capture_open_flag("O_NOFOLLOW"),
                mode,
                dir_fd=destination_descriptor,
            )
            try:
                _write_all(destination_file, data)
                os.fchmod(destination_file, mode)
            finally:
                os.close(destination_file)
        elif stat.S_ISDIR(info.st_mode):
            os.mkdir(name, mode=mode, dir_fd=destination_descriptor)
            source_child = os.open(name, directory_flags, dir_fd=source_descriptor)
            destination_child = os.open(
                name,
                directory_flags,
                dir_fd=destination_descriptor,
            )
            try:
                opened_source = os.fstat(source_child)
                if (opened_source.st_dev, opened_source.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    raise SwitchError(
                        f"Capture source directory changed while cloning: {name}"
                    )
                _copy_directory_contents_fd(source_child, destination_child)
                os.fchmod(destination_child, mode)
            finally:
                os.close(source_child)
                os.close(destination_child)
        else:
            raise SwitchError(f"Unsupported filesystem object kind: {name}")


def _fsync_tree_fd(directory_descriptor: int) -> None:
    directory_flags = _capture_directory_open_flags()
    for name in sorted(os.listdir(directory_descriptor)):
        info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISREG(info.st_mode):
            descriptor = os.open(
                name,
                _capture_file_open_flags(),
                dir_fd=directory_descriptor,
            )
            try:
                opened = _require_regular_descriptor(descriptor, name)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise SwitchError(
                        f"Switch staged file changed before fsync: {name}"
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif stat.S_ISDIR(info.st_mode):
            descriptor = os.open(
                name,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise SwitchError(
                        f"Switch staged directory changed before fsync: {name}"
                    )
                _fsync_tree_fd(descriptor)
            finally:
                os.close(descriptor)
        elif not stat.S_ISLNK(info.st_mode):
            raise SwitchError(f"Unsupported switch staged entry: {name}")
    os.fsync(directory_descriptor)


def _copy_directory_contents_at(
    root_descriptor: int,
    source_parts: tuple[str, ...],
    destination_parts: tuple[str, ...],
) -> None:
    source_descriptor = _open_directory_at(root_descriptor, source_parts)
    destination_descriptor = _open_directory_at(root_descriptor, destination_parts)
    try:
        if os.listdir(destination_descriptor):
            raise SwitchError("Capture stage is not empty before clone")
        _copy_directory_contents_fd(source_descriptor, destination_descriptor)
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _file_sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _directory_records_fd(
    directory_descriptor: int,
    *,
    prefix: str = "",
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    directory_flags = _capture_directory_open_flags()
    for name in sorted(os.listdir(directory_descriptor)):
        info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "mode": mode,
                    "symlink_target": os.readlink(
                        name,
                        dir_fd=directory_descriptor,
                    ),
                }
            )
        elif stat.S_ISREG(info.st_mode):
            file_descriptor = os.open(
                name,
                _capture_file_open_flags(),
                dir_fd=directory_descriptor,
            )
            try:
                opened = _require_regular_descriptor(file_descriptor, relative)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise SwitchError(
                        f"Capture file changed while attesting: {relative}"
                    )
                digest = _file_sha256_descriptor(file_descriptor)
            finally:
                os.close(file_descriptor)
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "size": info.st_size,
                    "sha256": digest,
                }
            )
        elif stat.S_ISDIR(info.st_mode):
            records.append({"path": relative, "kind": "directory", "mode": mode})
            child_descriptor = os.open(name, directory_flags, dir_fd=directory_descriptor)
            try:
                opened_child = os.fstat(child_descriptor)
                if (opened_child.st_dev, opened_child.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    raise SwitchError(
                        f"Capture directory changed while attesting: {relative}"
                    )
                records.extend(
                    _directory_records_fd(child_descriptor, prefix=relative)
                )
            finally:
                os.close(child_descriptor)
        else:
            raise SwitchError(f"Unsupported filesystem object kind: {relative}")
    return records


def _path_state_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    display_path: Path,
) -> dict[str, object]:
    if not parts:
        info = os.fstat(root_descriptor)
        parent_descriptor = None
        leaf = None
    else:
        parent_descriptor, leaf = _open_parent_at(root_descriptor, parts)
        try:
            info = os.stat(
                leaf,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            os.close(parent_descriptor)
            return {"kind": "missing", "path": str(display_path)}
    try:
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            if parent_descriptor is None or leaf is None:
                raise SwitchError("Capture workspace root cannot be a symlink")
            return {
                "kind": "symlink",
                "path": str(display_path),
                "mode": mode,
                "symlink_target": os.readlink(leaf, dir_fd=parent_descriptor),
            }
        if stat.S_ISREG(info.st_mode):
            if parent_descriptor is None or leaf is None:
                raise SwitchError("Capture workspace root cannot be a file")
            file_descriptor = os.open(
                leaf,
                _capture_file_open_flags(),
                dir_fd=parent_descriptor,
            )
            try:
                opened = _require_regular_descriptor(file_descriptor, display_path)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise SwitchError(
                        f"Capture file changed while attesting: {display_path}"
                    )
                digest = _file_sha256_descriptor(file_descriptor)
            finally:
                os.close(file_descriptor)
            return {
                "kind": "file",
                "path": str(display_path),
                "mode": mode,
                "size": info.st_size,
                "sha256": digest,
            }
        if stat.S_ISDIR(info.st_mode):
            directory_descriptor = (
                os.dup(root_descriptor)
                if not parts
                else os.open(
                    leaf,
                    _capture_directory_open_flags(),
                    dir_fd=parent_descriptor,
                )
            )
            try:
                opened_directory = os.fstat(directory_descriptor)
                if (opened_directory.st_dev, opened_directory.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    raise SwitchError(
                        f"Capture directory changed while attesting: {display_path}"
                    )
                records = _directory_records_fd(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            tree_payload = json.dumps(
                records,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            return {
                "kind": "directory",
                "path": str(display_path),
                "mode": mode,
                "entry_count": len(records),
                "tree_sha256": hashlib.sha256(tree_payload).hexdigest(),
            }
        raise SwitchError(f"Unsupported filesystem object kind: {display_path}")
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _node_identity_at(
    parent_descriptor: int,
    leaf: str,
    display_path: Path,
) -> dict[str, object]:
    try:
        info = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return {"kind": "missing", "path": str(display_path)}
    kind = _filesystem_kind(info)
    identity: dict[str, object] = {
        "kind": kind,
        "path": str(display_path),
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }
    if kind == "symlink":
        identity["symlink_target"] = os.readlink(
            leaf,
            dir_fd=parent_descriptor,
        )
    return identity


def _directory_identity_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    display_path: Path,
) -> tuple[int, int]:
    descriptor = _open_directory_at(root_descriptor, parts)
    try:
        info = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    os.close(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise SwitchError(f"Capture stage root is not a directory: {display_path}")
    return info.st_dev, info.st_ino


def _sync_capture_tree_path(
    adapter: FilesystemAdapter,
    directory: Path,
    *,
    file_phase: str,
    directory_phase: str,
) -> None:
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        info = child.lstat()
        if stat.S_ISREG(info.st_mode):
            adapter.sync_file(child, phase=file_phase)
        elif stat.S_ISDIR(info.st_mode):
            _sync_capture_tree_path(
                adapter,
                child,
                file_phase=file_phase,
                directory_phase=directory_phase,
            )
        elif stat.S_ISLNK(info.st_mode):
            continue
        else:
            raise SwitchError(f"Unsupported staged capture entry: {child}")
    adapter.sync_directory(directory, phase=directory_phase)


def _sync_tree_at(
    adapter: FilesystemAdapter,
    root_descriptor: int,
    parts: tuple[str, ...],
    display_path: Path,
    *,
    file_phase: str,
    directory_phase: str,
) -> None:
    directory_descriptor = _open_directory_at(root_descriptor, parts)
    try:
        names = sorted(os.listdir(directory_descriptor))
        for name in names:
            info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            child_path = display_path / name
            if stat.S_ISREG(info.st_mode):
                adapter.sync_file(child_path, phase=file_phase)
            elif stat.S_ISDIR(info.st_mode):
                _sync_tree_at(
                    adapter,
                    root_descriptor,
                    parts + (name,),
                    child_path,
                    file_phase=file_phase,
                    directory_phase=directory_phase,
                )
            elif stat.S_ISLNK(info.st_mode):
                continue
            else:
                raise SwitchError(f"Unsupported staged capture entry: {child_path}")
    finally:
        os.close(directory_descriptor)
    adapter.sync_directory(display_path, phase=directory_phase)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda path: path.name):
            info = child.lstat()
            relative = child.relative_to(root).as_posix()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                records.append(
                    {
                        "path": relative,
                        "kind": "symlink",
                        "mode": mode,
                        "symlink_target": os.readlink(child),
                    }
                )
            elif stat.S_ISREG(info.st_mode):
                records.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": mode,
                        "size": info.st_size,
                        "sha256": _file_sha256(child),
                    }
                )
            elif stat.S_ISDIR(info.st_mode):
                records.append(
                    {"path": relative, "kind": "directory", "mode": mode}
                )
                visit(child)
            else:
                raise SwitchError(f"Unsupported filesystem object kind: {child}")

    visit(root)
    return records


def _path_state(path: Path) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "path": str(path)}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {
            "kind": "symlink",
            "path": str(path),
            "mode": mode,
            "symlink_target": os.readlink(path),
        }
    if stat.S_ISREG(info.st_mode):
        return {
            "kind": "file",
            "path": str(path),
            "mode": mode,
            "size": info.st_size,
            "sha256": _file_sha256(path),
        }
    if stat.S_ISDIR(info.st_mode):
        records = _directory_records(path)
        tree_payload = json.dumps(
            records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return {
            "kind": "directory",
            "path": str(path),
            "mode": mode,
            "entry_count": len(records),
            "tree_sha256": hashlib.sha256(tree_payload).hexdigest(),
        }
    raise SwitchError(f"Unsupported filesystem object kind: {path}")


def capture_path_state(path: Path) -> dict[str, object]:
    return _path_state(path)


def _states_match(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    kind = expected.get("kind")
    if current.get("kind") != kind:
        return False
    if kind == "missing":
        return True
    if "mode" in expected and current.get("mode") != expected.get("mode"):
        return False
    if kind == "file":
        if current.get("sha256") != expected.get("sha256"):
            return False
        return "size" not in expected or current.get("size") == expected.get("size")
    if kind == "symlink":
        return current.get("symlink_target") == expected.get("symlink_target")
    if kind == "directory":
        if current.get("tree_sha256") != expected.get("tree_sha256"):
            return False
        return (
            "entry_count" not in expected
            or current.get("entry_count") == expected.get("entry_count")
        )
    return False


def _node_identities_match(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    return {
        key: value for key, value in current.items() if key != "path"
    } == {
        key: value for key, value in expected.items() if key != "path"
    }


def _capture_state_at_path(
    expected: Mapping[str, object],
    path: Path,
) -> dict[str, object]:
    return {**dict(expected), "path": str(path)}


def _require_exact_capture_state(
    adapter: FilesystemAdapter,
    path: Path,
    expected: Mapping[str, object],
    *,
    phase: str,
) -> None:
    if adapter.capture_state(path) != dict(expected):
        raise SwitchError(f"Staged capture state changed during {phase}: {path}")


def _capture_vector_mismatches(
    adapter: FilesystemAdapter,
    expected_states: tuple[tuple[Path, Mapping[str, object]], ...],
) -> tuple[Path, ...]:
    return tuple(
        path
        for path, expected in expected_states
        if adapter.capture_state(path) != dict(expected)
    )


def _capture_vector_error(label: str, mismatches: tuple[Path, ...]) -> SwitchError:
    return SwitchError(
        f"{label}: " + ", ".join(str(path) for path in mismatches)
    )


def _canonical_json_bytes(data: Mapping[str, object]) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True).encode() + b"\n"


def _canonical_json_object(data: Mapping[str, object]) -> dict[str, object]:
    value = json.loads(_canonical_json_bytes(data))
    if not isinstance(value, dict):
        raise SwitchError("Canonical capture JSON must be an object")
    return value


@dataclass(frozen=True)
class _PendingTransactionMarker:
    path: Path
    payload: Mapping[str, object]


@dataclass(frozen=True)
class _PendingTransactionEvidence:
    marker: _PendingTransactionMarker
    backup_dir: Path
    manifest: Mapping[str, object]
    journal: Mapping[str, object]
    operation: str
    lifecycle: str


@dataclass(frozen=True)
class _UnmarkedTransactionEvidence:
    backup_dir: Path
    manifest: Mapping[str, object]
    journal: Mapping[str, object]
    operation: str
    lifecycle: str
    marker_required: bool
    effects_begun: bool


@dataclass(frozen=True)
class _StoreRecoveryClassification:
    pending_transaction: _PendingTransactionEvidence | None
    pending_capture_profiles: tuple[str, ...]
    unmarked_transactions: tuple[_UnmarkedTransactionEvidence, ...]


def _validated_pending_backup_id(backup_id: str) -> str:
    raw = Path(backup_id)
    if (
        not backup_id
        or backup_id in {".", ".."}
        or len(raw.parts) != 1
        or raw.name != backup_id
    ):
        raise SwitchError(f"Pending transaction has an invalid backup ID: {backup_id}")
    return backup_id


def _pending_marker_name(backup_id: str) -> str:
    return f".pending-transaction-{_validated_pending_backup_id(backup_id)}.json"


def _prepared_journal_snapshot(
    journal: Mapping[str, object],
) -> dict[str, object]:
    snapshot = _canonical_json_object(journal)
    snapshot.pop("prepared_journal_sha256", None)
    snapshot["state"] = "prepared"
    snapshot["effects"] = []
    return snapshot


def _prepared_journal_sha256(journal: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(_prepared_journal_snapshot(journal))
    ).hexdigest()


def _bind_pending_transaction(
    store: Store,
    backup_dir: Path,
    operation: str,
    journal: dict[str, object],
) -> _PendingTransactionMarker:
    effects = journal.get("effects")
    if not isinstance(effects, list) or effects:
        raise SwitchError("Pending transaction must be bound before its first effect")
    transaction_id = uuid.uuid4().hex
    marker_name = _pending_marker_name(backup_dir.name)
    created_at = now_stamp()
    journal.update(
        {
            "operation": operation,
            "backup_id": backup_dir.name,
            "transaction_id": transaction_id,
            "marker_name": marker_name,
            "recovery_marker_required": True,
            "prepared_at": created_at,
        }
    )
    digest = _prepared_journal_sha256(journal)
    journal["prepared_journal_sha256"] = digest
    payload: dict[str, object] = {
        "schema_version": 1,
        "operation": operation,
        "backup_id": backup_dir.name,
        "transaction_id": transaction_id,
        "created_at": created_at,
        "marker_name": marker_name,
        "prepared_journal_sha256": digest,
        "recovery_marker_required": True,
    }
    return _PendingTransactionMarker(
        path=store.root / marker_name,
        payload=MappingProxyType(payload),
    )


def _publish_pending_transaction(
    adapter: FilesystemAdapter,
    marker: _PendingTransactionMarker,
) -> None:
    adapter.write_manifest(
        marker.path,
        dict(marker.payload),
        phase="pending_marker_publish",
    )


def _durably_sync_transaction_backup(
    adapter: FilesystemAdapter,
    store: Store,
    backup_dir: Path,
) -> None:
    adapter.sync_tree(
        backup_dir,
        file_phase="transaction_backup_file",
        directory_phase="transaction_backup_directory",
    )
    adapter.sync_directory(
        store.backups_dir,
        phase="transaction_backups_directory",
    )


def _prepare_switch_staged_payloads(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    payloads: Mapping[tuple[Path, str], tuple[bytes, int]],
) -> dict[tuple[Path, str], dict[str, object]]:
    if not payloads:
        return {}
    stage_root = backup_dir / "switch-effect-stages"
    ensure_private_dir(stage_root)
    staged: dict[tuple[Path, str], dict[str, object]] = {}
    for index, ((destination, effect_phase), (payload, mode)) in enumerate(
        payloads.items()
    ):
        stage_path = stage_root / f"{index:04d}-{destination.name}"
        adapter.write_bytes(
            stage_path,
            payload,
            mode=mode,
            phase="switch_stage_prepare",
        )
        expected_state = _expected_file_state(stage_path, payload, mode=mode)
        staged_state = adapter.capture_state(stage_path)
        if not isinstance(staged_state, dict) or not _states_match(
            staged_state,
            expected_state,
        ):
            raise SwitchError(
                f"Switch staged payload does not match its plan: {destination}"
            )
        staged[(destination, effect_phase)] = {
            "path": str(stage_path),
            "expected_state": expected_state,
        }
    return staged


def _freeze_switch_staged_payloads(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    prepared: Mapping[tuple[Path, str], Mapping[str, object]],
) -> dict[tuple[Path, str], dict[str, object]]:
    frozen: dict[tuple[Path, str], dict[str, object]] = {}
    for (destination, effect_phase), artifact in prepared.items():
        raw_stage_path = artifact.get("path")
        expected_state = artifact.get("expected_state")
        if not isinstance(raw_stage_path, str) or not isinstance(
            expected_state,
            dict,
        ):
            raise SwitchError(
                f"Switch staged payload plan is invalid: {destination}"
            )
        stage_path = Path(raw_stage_path)
        try:
            relative_stage = stage_path.relative_to(backup_dir)
        except ValueError as exc:
            raise SwitchError(
                f"Switch staged payload escapes its backup: {stage_path}"
            ) from exc
        contained_stage = _contained_payload_path(
            backup_dir,
            str(relative_stage),
        )
        if contained_stage != stage_path.resolve():
            raise SwitchError(
                f"Switch staged payload path changed: {stage_path}"
            )
        staged_state = adapter.capture_state(stage_path)
        if not isinstance(staged_state, dict) or not _states_match(
            staged_state,
            expected_state,
        ):
            raise SwitchError(
                f"Switch staged payload changed before identity freeze: "
                f"{destination}"
            )
        staged_identity = adapter.capture_identity(stage_path)
        frozen[(destination, effect_phase)] = {
            "path": str(stage_path),
            "state": staged_state,
            "identity": staged_identity,
            "route_guard": _capture_route_guard(stage_path),
        }
    return frozen


def _retire_pending_marker(
    adapter: FilesystemAdapter,
    marker: _PendingTransactionMarker,
    *,
    outcome: str,
) -> str | None:
    try:
        adapter.durable_unlink(marker.path, phase="pending_marker_remove")
    except Exception as cleanup_error:
        try:
            marker.path.lstat()
        except FileNotFoundError:
            try:
                adapter.write_manifest(
                    marker.path,
                    dict(marker.payload),
                    phase="pending_marker_republish",
                )
                adapter.sync_file(
                    marker.path,
                    phase="pending_marker_republish_manifest",
                )
                adapter.sync_directory(
                    marker.path.parent,
                    phase="pending_marker_republish_parent",
                )
            except Exception as republish_error:
                return (
                    f"{outcome}; pending recovery marker cleanup durability "
                    f"failed at {marker.path}: {cleanup_error}; marker "
                    f"republish also failed: {republish_error}; manual recovery "
                    "is required before another mutation"
                )
        return (
            f"{outcome}; pending recovery marker retained at {marker.path}: "
            f"{cleanup_error}; the next applying command will retry cleanup"
        )
    return None


def _set_switch_effect_recovery_state(
    journal: dict[str, object],
    recovery_state: str,
) -> None:
    effects = journal.get("effects")
    if not isinstance(effects, list):
        raise SwitchError("Switch terminal journal has no effect list")
    for index, effect in enumerate(effects):
        if not isinstance(effect, dict) or effect.get("id") != index:
            raise SwitchError("Switch terminal journal has an invalid effect order")
        effect["recovery_state"] = recovery_state


def _filesystem_kind(info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _capture_route_guard(path: Path) -> dict[str, object]:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise SwitchError(f"Switch route must be absolute: {path}")
    components: list[dict[str, object]] = []
    parent = expanded.parent
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            components.append(
                {"path": str(current), "name": part, "kind": "missing"}
            )
            continue
        kind = _filesystem_kind(info)
        component: dict[str, object] = {
            "path": str(current),
            "name": part,
            "kind": kind,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode),
        }
        if kind == "symlink":
            component["symlink_target"] = os.readlink(current)
        components.append(component)
    canonical_parent = expanded.parent.resolve()
    canonical_components: list[dict[str, object]] = []
    canonical_current = Path(canonical_parent.anchor)
    canonical_paths = [canonical_current]
    for part in canonical_parent.parts[1:]:
        canonical_current = canonical_current / part
        canonical_paths.append(canonical_current)
    for canonical_path in canonical_paths:
        try:
            info = canonical_path.lstat()
        except FileNotFoundError as exc:
            raise SwitchError(
                f"Switch canonical route parent is missing: {canonical_path}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise SwitchError(
                f"Switch canonical route component is not a directory: "
                f"{canonical_path}"
            )
        canonical_components.append(
            {
                "path": str(canonical_path),
                "name": canonical_path.name,
                "kind": "directory",
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
            }
        )
    return {
        "schema_version": 1,
        "path": str(expanded),
        "canonical_parent": str(canonical_parent),
        "canonical_components": canonical_components,
        "components": components,
    }


def _capture_restore_route_guard(
    route_path: Path,
    destination: Path,
) -> dict[str, object]:
    expanded = route_path.expanduser()
    if not expanded.is_absolute():
        raise SwitchError(f"Restore route must be absolute: {route_path}")
    if expanded.name != destination.name:
        raise SwitchError(
            f"Restore route leaf changed during canonicalization: {route_path}"
        )
    components: list[dict[str, object]] = []
    current = Path(expanded.parent.anchor)
    for part in expanded.parent.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            components.append(
                {"path": str(current), "name": part, "kind": "missing"}
            )
            continue
        kind = _filesystem_kind(info)
        component: dict[str, object] = {
            "path": str(current),
            "name": part,
            "kind": kind,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode),
        }
        if kind == "symlink":
            component["symlink_target"] = os.readlink(current)
        components.append(component)

    canonical_parent = expanded.parent.resolve()
    if destination != canonical_parent / expanded.name:
        raise SwitchError(
            f"Restore route no longer resolves to its destination: {route_path}"
        )
    missing_parent_paths: list[Path] = []
    anchor = canonical_parent
    while True:
        try:
            anchor_info = anchor.lstat()
        except FileNotFoundError:
            if anchor == anchor.parent:
                raise SwitchError(
                    f"Restore route has no existing anchor: {route_path}"
                )
            missing_parent_paths.append(anchor)
            anchor = anchor.parent
            continue
        if stat.S_ISLNK(anchor_info.st_mode) or not stat.S_ISDIR(
            anchor_info.st_mode
        ):
            raise SwitchError(
                f"Restore route anchor is not a directory: {anchor}"
            )
        break
    missing_parent_paths.reverse()

    canonical_components: list[dict[str, object]] = []
    canonical_current = Path(anchor.anchor)
    canonical_paths = [canonical_current]
    for part in anchor.parts[1:]:
        canonical_current = canonical_current / part
        canonical_paths.append(canonical_current)
    for canonical_path in canonical_paths:
        info = canonical_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SwitchError(
                f"Restore canonical route component is not a directory: "
                f"{canonical_path}"
            )
        canonical_components.append(
            {
                "path": str(canonical_path),
                "name": canonical_path.name,
                "kind": "directory",
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
            }
        )
    return {
        "schema_version": 2,
        "path": str(expanded),
        "destination": str(destination),
        "canonical_parent": str(canonical_parent),
        "canonical_anchor": str(anchor),
        "canonical_components": canonical_components,
        "missing_parent_paths": [
            str(path) for path in missing_parent_paths
        ],
        "components": components,
    }


def _validate_restore_route_guard(
    route_path: Path,
    destination: Path,
    guard: Mapping[str, object],
    expected_created_parent_identities: tuple[
        Mapping[str, object], ...
    ] = tuple(),
) -> None:
    if expected_created_parent_identities:
        expected_by_path = {
            identity.get("path"): identity
            for identity in expected_created_parent_identities
            if isinstance(identity.get("path"), str)
        }

        def expected_identity(path: Path) -> Mapping[str, object] | None:
            direct = expected_by_path.get(str(path))
            if direct is not None:
                return direct
            try:
                return expected_by_path.get(str(path.resolve()))
            except OSError:
                return None

        raw_components = guard.get("components")
        raw_canonical_components = guard.get("canonical_components")
        raw_missing = guard.get("missing_parent_paths")
        if (
            guard.get("schema_version") != 2
            or guard.get("path") != str(route_path.expanduser())
            or guard.get("destination") != str(destination)
            or not isinstance(raw_components, list)
            or not isinstance(raw_canonical_components, list)
            or not isinstance(raw_missing, list)
        ):
            raise SwitchError(f"Restore route evidence is invalid: {route_path}")
        for raw_component in raw_components:
            if not isinstance(raw_component, dict):
                raise SwitchError(
                    f"Restore route component is invalid: {route_path}"
                )
            raw_path = raw_component.get("path")
            if not isinstance(raw_path, str):
                raise SwitchError(
                    f"Restore route component has no path: {route_path}"
                )
            component_path = Path(raw_path)
            try:
                info = component_path.lstat()
            except FileNotFoundError:
                if raw_component.get("kind") != "missing":
                    raise SwitchError(
                        f"Restore route component disappeared: {component_path}"
                    )
                continue
            expected = (
                expected_identity(component_path)
                if raw_component.get("kind") in {"directory", "missing"}
                else None
            )
            if expected is not None:
                if (
                    expected.get("kind") != "directory"
                    or not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or expected.get("device") != info.st_dev
                    or expected.get("inode") != info.st_ino
                    or expected.get("mode") != stat.S_IMODE(info.st_mode)
                ):
                    raise SwitchError(
                        f"Restore authorized route component changed: "
                        f"{component_path}"
                    )
                continue
            if raw_component.get("kind") == "missing":
                canonical_component = component_path.resolve()
                expected = expected_by_path.get(str(canonical_component))
                if (
                    not isinstance(expected, dict)
                    or expected.get("kind") != "directory"
                    or expected.get("device") != info.st_dev
                    or expected.get("inode") != info.st_ino
                    or expected.get("mode") != stat.S_IMODE(info.st_mode)
                ):
                    raise SwitchError(
                        f"Restore created route component changed: "
                        f"{component_path}"
                    )
                continue
            current_component: dict[str, object] = {
                "path": raw_path,
                "name": raw_component.get("name"),
                "kind": _filesystem_kind(info),
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
            }
            if current_component["kind"] == "symlink":
                current_component["symlink_target"] = os.readlink(
                    component_path
                )
            if current_component != raw_component:
                raise SwitchError(
                    f"Restore route component changed: {component_path}"
                )
        for raw_component in raw_canonical_components:
            if not isinstance(raw_component, dict):
                raise SwitchError(
                    f"Restore canonical route is invalid: {route_path}"
                )
            component_path = raw_component.get("path")
            if not isinstance(component_path, str):
                raise SwitchError(
                    f"Restore canonical route has no path: {route_path}"
                )
            canonical_component = Path(component_path)
            info = canonical_component.lstat()
            expected = expected_identity(canonical_component)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or (
                    expected.get("device") if expected is not None
                    else raw_component.get("device")
                )
                != info.st_dev
                or (
                    expected.get("inode") if expected is not None
                    else raw_component.get("inode")
                )
                != info.st_ino
                or (
                    expected is not None
                    and expected.get("mode") != stat.S_IMODE(info.st_mode)
                )
            ):
                raise SwitchError(
                    f"Restore canonical route changed: {component_path}"
                )
        if route_path.expanduser().parent.resolve() / route_path.name != destination:
            raise SwitchError(f"Restore route destination changed: {route_path}")
        return
    current = _capture_restore_route_guard(route_path, destination)
    if _immutable_state_bytes(current) != _immutable_state_bytes(guard):
        raise SwitchError(f"Restore route identity changed: {route_path}")


def _open_restore_route_anchor(
    route_path: Path,
    destination: Path,
    guard: Mapping[str, object],
    expected_created_parent_identities: tuple[
        Mapping[str, object], ...
    ] = tuple(),
) -> int:
    _validate_restore_route_guard(
        route_path,
        destination,
        guard,
        expected_created_parent_identities,
    )
    raw_components = guard.get("canonical_components")
    if not isinstance(raw_components, list) or not raw_components:
        raise SwitchError(f"Restore route has no canonical anchor: {route_path}")
    flags = _capture_directory_open_flags()
    expected_by_path = {
        identity.get("path"): identity
        for identity in expected_created_parent_identities
        if isinstance(identity.get("path"), str)
    }
    descriptor: int | None = None
    try:
        for index, raw_component in enumerate(raw_components):
            if not isinstance(raw_component, dict):
                raise SwitchError(
                    f"Restore route has an invalid component: {route_path}"
                )
            component_path = raw_component.get("path")
            component_name = raw_component.get("name")
            expected_device = raw_component.get("device")
            expected_inode = raw_component.get("inode")
            replacement_identity = (
                expected_by_path.get(component_path)
                if isinstance(component_path, str)
                else None
            )
            if replacement_identity is not None:
                expected_device = replacement_identity.get("device")
                expected_inode = replacement_identity.get("inode")
            if (
                not isinstance(component_path, str)
                or not isinstance(component_name, str)
                or type(expected_device) is not int
                or type(expected_inode) is not int
            ):
                raise SwitchError(
                    f"Restore route has an invalid identity: {route_path}"
                )
            if index == 0:
                next_descriptor = os.open(component_path, flags)
            else:
                if descriptor is None:
                    raise SwitchError(
                        f"Restore route descriptor is unavailable: {route_path}"
                    )
                next_descriptor = os.open(
                    component_name,
                    flags,
                    dir_fd=descriptor,
                )
            opened = os.fstat(next_descriptor)
            if (opened.st_dev, opened.st_ino) != (
                expected_device,
                expected_inode,
            ):
                os.close(next_descriptor)
                raise SwitchError(
                    f"Restore route component changed: {component_path}"
                )
            if descriptor is not None:
                os.close(descriptor)
            descriptor = next_descriptor
        if descriptor is None:
            raise SwitchError(f"Restore route anchor is unavailable: {route_path}")
        return descriptor
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _validate_route_guard(path: Path, guard: Mapping[str, object]) -> None:
    current = _capture_route_guard(path)
    if _immutable_state_bytes(current) != _immutable_state_bytes(guard):
        raise SwitchError(f"Switch effect route identity changed: {path}")


def _open_guarded_parent(path: Path, guard: Mapping[str, object]) -> int:
    _validate_route_guard(path, guard)
    raw_parent = guard.get("canonical_parent")
    raw_components = guard.get("canonical_components")
    if (
        not isinstance(raw_parent, str)
        or not raw_parent
        or not isinstance(raw_components, list)
        or not raw_components
    ):
        raise SwitchError(f"Switch route has no canonical parent: {path}")
    flags = _capture_directory_open_flags()
    descriptor: int | None = None
    try:
        for index, raw_component in enumerate(raw_components):
            if not isinstance(raw_component, dict):
                raise SwitchError(
                    f"Switch route has an invalid canonical component: {path}"
                )
            component_path = raw_component.get("path")
            component_name = raw_component.get("name")
            expected_device = raw_component.get("device")
            expected_inode = raw_component.get("inode")
            if (
                not isinstance(component_path, str)
                or not isinstance(component_name, str)
                or raw_component.get("kind") != "directory"
                or type(expected_device) is not int
                or type(expected_inode) is not int
            ):
                raise SwitchError(
                    f"Switch route has an invalid canonical identity: {path}"
                )
            if index == 0:
                next_descriptor = os.open(component_path, flags)
            else:
                if descriptor is None:
                    raise SwitchError(
                        f"Switch route traversal lost its anchor: {path}"
                    )
                next_descriptor = os.open(
                    component_name,
                    flags,
                    dir_fd=descriptor,
                )
            opened = os.fstat(next_descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (expected_device, expected_inode)
            ):
                os.close(next_descriptor)
                raise SwitchError(
                    f"Switch route canonical identity changed while opening: "
                    f"{component_path}"
                )
            if descriptor is not None:
                os.close(descriptor)
            descriptor = next_descriptor
        if descriptor is None or raw_components[-1].get("path") != raw_parent:
            raise SwitchError(
                f"Switch route canonical parent is inconsistent: {path}"
            )
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    return descriptor


def _pending_marker_backup_id_from_name(name: str) -> str:
    prefix = ".pending-transaction-"
    suffix = ".json"
    if not name.startswith(prefix) or not name.endswith(suffix):
        raise SwitchError(f"Invalid pending transaction marker name: {name}")
    return _validated_pending_backup_id(name[len(prefix) : -len(suffix)])


def _require_pending_string(
    data: Mapping[str, object],
    name: str,
    *,
    backup_id: str,
) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise SwitchError(
            f"Pending transaction {backup_id} has an invalid {name}"
        )
    return value


def _validate_bound_switch_failure_record(
    backup_dir: Path,
    *,
    backup_id: str,
    marker_name: str,
    transaction_id: str,
    prepared_journal_sha256: str,
) -> tuple[dict[str, object], dict[str, object]]:
    failure_path = backup_dir / "failure.json"
    failure_info = failure_path.lstat()
    if stat.S_ISLNK(failure_info.st_mode) or not stat.S_ISREG(
        failure_info.st_mode
    ):
        raise SwitchError("bound failure record is not a regular file")
    raw_record = read_json(failure_path)
    if not isinstance(raw_record, dict):
        raise SwitchError("bound failure record is not an object")
    record = _canonical_json_object(raw_record)
    if (
        record.get("schema_version") != 1
        or record.get("record_kind") != "switch_terminal_failure_receipt"
        or record.get("backup_schema_version") != 2
        or record.get("lifecycle") != "rolled_back"
        or record.get("id") != backup_id
        or record.get("backup_id") != backup_id
        or record.get("operation") != "switch"
        or record.get("transaction_id") != transaction_id
        or record.get("marker_name") != marker_name
        or record.get("prepared_journal_sha256")
        != prepared_journal_sha256
        or record.get("recovery_marker_required") is not True
        or record.get("rollback_verified") is not True
        or not isinstance(record.get("failed_at"), str)
        or not record.get("failed_at")
        or not isinstance(record.get("entries"), list)
    ):
        raise SwitchError("bound failure record identity is inconsistent")
    journal = record.get("switch_journal")
    if not isinstance(journal, dict):
        raise SwitchError("bound failure record has no switch journal")
    effects = journal.get("effects")
    if (
        journal.get("schema_version") != 1
        or journal.get("operation") != "switch"
        or journal.get("backup_id") != backup_id
        or journal.get("transaction_id") != transaction_id
        or journal.get("marker_name") != marker_name
        or journal.get("prepared_journal_sha256")
        != prepared_journal_sha256
        or journal.get("recovery_marker_required") is not True
        or journal.get("state") != "recovered"
        or not isinstance(effects, list)
        or any(
            not isinstance(effect, dict)
            or effect.get("id") != index
            or effect.get("recovery_state") != "recovered"
            for index, effect in enumerate(effects)
        )
        or _prepared_journal_sha256(journal) != prepared_journal_sha256
    ):
        raise SwitchError("bound failure record journal is inconsistent")
    return record, _canonical_json_object(journal)


def _validate_pending_transaction_evidence(
    store: Store,
    marker_path: Path,
) -> _PendingTransactionEvidence:
    backup_id = _pending_marker_backup_id_from_name(marker_path.name)
    try:
        marker_info = marker_path.lstat()
        if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(
            marker_info.st_mode
        ):
            raise SwitchError("marker is symlinked or not a regular file")
        raw_marker = read_json(marker_path)
        if not isinstance(raw_marker, dict):
            raise SwitchError("marker is not an object")
        marker = _canonical_json_object(raw_marker)
        if (
            type(marker.get("schema_version")) is not int
            or marker.get("schema_version") != 1
        ):
            raise SwitchError("marker schema is unsupported")
        operation = marker.get("operation")
        if operation not in {"switch", "restore"}:
            raise SwitchError("marker operation is unsupported")
        if marker.get("backup_id") != backup_id:
            raise SwitchError("marker backup ID does not match its name")
        marker_name = _require_pending_string(
            marker,
            "marker_name",
            backup_id=backup_id,
        )
        if marker_name != marker_path.name:
            raise SwitchError("marker name binding is inconsistent")
        transaction_id = _require_pending_string(
            marker,
            "transaction_id",
            backup_id=backup_id,
        )
        _require_pending_string(marker, "created_at", backup_id=backup_id)
        digest = _require_pending_string(
            marker,
            "prepared_journal_sha256",
            backup_id=backup_id,
        )
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise SwitchError("marker journal digest is invalid")
        if marker.get("recovery_marker_required") is not True:
            raise SwitchError("marker does not require bound recovery evidence")

        backup_dir = store.backups_dir / backup_id
        backup_info = backup_dir.lstat()
        if stat.S_ISLNK(backup_info.st_mode) or not stat.S_ISDIR(
            backup_info.st_mode
        ):
            raise SwitchError("matching backup is symlinked or not a directory")
        manifest_path = backup_dir / "backup.json"
        manifest_read_error: Exception | None = None
        try:
            manifest_info = manifest_path.lstat()
        except OSError as exc:
            manifest_read_error = exc
        else:
            if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(
                manifest_info.st_mode
            ):
                raise SwitchError(
                    "matching backup manifest is not a regular file"
                )
            try:
                raw_manifest = read_json(manifest_path)
            except (OSError, SwitchError, ValueError, TypeError) as exc:
                manifest_read_error = exc

        if manifest_read_error is not None:
            if operation != "switch":
                raise SwitchError(
                    f"matching backup manifest is unreadable: {manifest_read_error}"
                )
            try:
                manifest, journal = _validate_bound_switch_failure_record(
                    backup_dir,
                    backup_id=backup_id,
                    marker_name=marker_name,
                    transaction_id=transaction_id,
                    prepared_journal_sha256=digest,
                )
            except (OSError, SwitchError, ValueError, TypeError) as fallback_error:
                raise SwitchError(
                    "matching backup manifest is unreadable and its bound "
                    f"failure record is invalid: {fallback_error}"
                ) from fallback_error
            lifecycle = "rolled_back"
        else:
            if not isinstance(raw_manifest, dict):
                raise SwitchError("matching backup manifest is not an object")
            manifest = _canonical_json_object(raw_manifest)
            if (
                type(manifest.get("schema_version")) is not int
                or manifest.get("schema_version") != 2
                or manifest.get("id") != backup_id
                or manifest.get("operation") != operation
            ):
                raise SwitchError("matching backup identity is inconsistent")
            lifecycle = manifest.get("lifecycle")
            if lifecycle not in {
                "prepared",
                "committed",
                "rolled_back",
                "rollback_failed",
            }:
                raise SwitchError("matching backup lifecycle is invalid")
            journal_name = (
                "switch_journal"
                if operation == "switch"
                else "restore_journal"
            )
            journal = manifest.get(journal_name)
            if not isinstance(journal, dict):
                raise SwitchError(f"matching backup has no {journal_name}")
            if (
                type(journal.get("schema_version")) is not int
                or journal.get("schema_version") != 1
                or journal.get("operation") != operation
                or journal.get("backup_id") != backup_id
                or journal.get("transaction_id") != transaction_id
                or journal.get("marker_name") != marker_name
                or journal.get("prepared_journal_sha256") != digest
                or journal.get("recovery_marker_required") is not True
            ):
                raise SwitchError("matching journal binding is inconsistent")
            expected_states = {
                "prepared": {"prepared"},
                "committed": {"committed"},
                "rolled_back": {"recovered"},
                "rollback_failed": {"rollback_failed"},
            }
            if journal.get("state") not in expected_states[str(lifecycle)]:
                raise SwitchError("matching journal lifecycle is inconsistent")
            effects = journal.get("effects")
            if not isinstance(effects, list):
                raise SwitchError("matching journal has no effect list")
            if lifecycle == "rolled_back" and any(
                not isinstance(effect, dict)
                or effect.get("id") != index
                or effect.get("recovery_state") != "recovered"
                for index, effect in enumerate(effects)
            ):
                raise SwitchError(
                    "matching rolled-back journal has incomplete effect recovery"
                )
            if _prepared_journal_sha256(journal) != digest:
                raise SwitchError("matching journal digest is inconsistent")
            if lifecycle == "committed" and operation == "restore":
                _validate_complete_committed_restore(
                    store,
                    backup_dir,
                    manifest,
                    journal,
                    FilesystemAdapter(),
                )
            if lifecycle == "committed" and operation == "switch":
                _validate_complete_committed_switch(
                    store,
                    backup_dir,
                    manifest,
                    journal,
                    FilesystemAdapter(),
                )
    except (OSError, SwitchError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SwitchError(
            f"Pending transaction {backup_id} is corrupt or ambiguous: {exc}"
        ) from exc
    return _PendingTransactionEvidence(
        marker=_PendingTransactionMarker(
            path=marker_path,
            payload=MappingProxyType(marker),
        ),
        backup_dir=backup_dir,
        manifest=MappingProxyType(manifest),
        journal=MappingProxyType(journal),
        operation=str(operation),
        lifecycle=str(lifecycle),
    )


def _pending_transaction_evidence(
    store: Store,
) -> _PendingTransactionEvidence | None:
    marker_paths: list[Path] = []
    for candidate in store.root.iterdir():
        if candidate.name.startswith(".pending-transaction-") and candidate.name.endswith(
            ".json"
        ):
            marker_paths.append(candidate)
    marker_paths.sort(key=lambda path: path.name)
    if len(marker_paths) > 1:
        backup_ids = ", ".join(
            _pending_marker_backup_id_from_name(path.name) for path in marker_paths
        )
        raise SwitchError(
            f"Multiple pending transactions require manual recovery: {backup_ids}"
        )
    if not marker_paths:
        return None
    return _validate_pending_transaction_evidence(store, marker_paths[0])


def _unmarked_transaction_evidence(
    store: Store,
    *,
    marker_backup_id: str | None,
) -> tuple[_UnmarkedTransactionEvidence, ...]:
    try:
        backups_info = store.backups_dir.lstat()
    except FileNotFoundError:
        return tuple()
    if stat.S_ISLNK(backups_info.st_mode) or not stat.S_ISDIR(
        backups_info.st_mode
    ):
        raise SwitchError(
            f"Transaction backup store is unsafe: {store.backups_dir}"
        )

    unresolved: list[_UnmarkedTransactionEvidence] = []
    for backup_dir in sorted(store.backups_dir.iterdir(), key=lambda path: path.name):
        if backup_dir.name == marker_backup_id:
            continue
        try:
            backup_info = backup_dir.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(backup_info.st_mode) or not stat.S_ISDIR(
            backup_info.st_mode
        ):
            continue
        manifest_path = backup_dir / "backup.json"
        try:
            manifest_info = manifest_path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(
            manifest_info.st_mode
        ):
            continue
        try:
            raw_manifest = read_json(manifest_path)
        except Exception:
            # A marker is the authoritative signal for an unreadable current
            # transaction. Historical corrupt backups must remain restorable by
            # hand without blocking unrelated mutations forever.
            continue
        if not isinstance(raw_manifest, dict):
            continue
        operation = raw_manifest.get("operation")
        lifecycle = raw_manifest.get("lifecycle")
        if operation not in {"switch", "restore"} or lifecycle not in {
            "prepared",
            "rollback_failed",
        }:
            continue
        backup_id = raw_manifest.get("id")
        journal_name = (
            "switch_journal" if operation == "switch" else "restore_journal"
        )
        journal = raw_manifest.get(journal_name)
        if (
            raw_manifest.get("schema_version") != 2
            or backup_id != backup_dir.name
            or not isinstance(journal, dict)
            or journal.get("schema_version") != 1
            or journal.get("state") not in {"prepared", "rollback_failed"}
        ):
            raise SwitchError(
                f"Unmarked transaction {backup_dir.name} is corrupt or ambiguous"
            )
        effects = journal.get("effects")
        if not isinstance(effects, list):
            raise SwitchError(
                f"Unmarked transaction {backup_dir.name} has no effect journal"
            )
        unresolved.append(
            _UnmarkedTransactionEvidence(
                backup_dir=backup_dir,
                manifest=MappingProxyType(_canonical_json_object(raw_manifest)),
                journal=MappingProxyType(_canonical_json_object(journal)),
                operation=str(operation),
                lifecycle=str(lifecycle),
                marker_required=journal.get("recovery_marker_required") is True,
                effects_begun=bool(effects),
            )
        )
    return tuple(unresolved)


def _close_effect_free_unmarked_transaction(
    store: Store,
    evidence: _UnmarkedTransactionEvidence,
    adapter: FilesystemAdapter,
) -> None:
    journal = _canonical_json_object(dict(evidence.journal))
    backup_id = evidence.backup_dir.name
    marker_name = _pending_marker_name(backup_id)
    digest = journal.get("prepared_journal_sha256")
    if (
        evidence.lifecycle != "prepared"
        or not evidence.marker_required
        or evidence.effects_begun
        or journal.get("state") != "prepared"
        or journal.get("operation") != evidence.operation
        or journal.get("backup_id") != backup_id
        or journal.get("marker_name") != marker_name
        or journal.get("recovery_marker_required") is not True
        or not isinstance(journal.get("transaction_id"), str)
        or not journal.get("transaction_id")
        or not isinstance(digest, str)
        or digest != _prepared_journal_sha256(journal)
    ):
        raise SwitchError(
            f"Unmarked transaction {backup_id} cannot be closed as never started"
        )
    marker_path = store.root / marker_name
    try:
        marker_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise SwitchError(
            f"Unmarked transaction {backup_id} unexpectedly regained its marker"
        )

    terminal = _canonical_json_object(dict(evidence.manifest))
    terminal_journal = terminal.get(
        "switch_journal"
        if evidence.operation == "switch"
        else "restore_journal"
    )
    if not isinstance(terminal_journal, dict):
        raise SwitchError(
            f"Unmarked transaction {backup_id} lost its journal"
        )
    terminal["lifecycle"] = "rolled_back"
    terminal["rolled_back_at"] = now_stamp()
    terminal["recovery_note"] = "closed without effects after required marker loss"
    terminal_journal["state"] = "recovered"
    manifest_path = evidence.backup_dir / "backup.json"
    adapter.write_manifest(
        manifest_path,
        terminal,
        phase="unmarked_transaction_close",
    )
    adapter.sync_file(
        manifest_path,
        phase="unmarked_transaction_close_manifest",
    )
    adapter.sync_directory(
        evidence.backup_dir,
        phase="unmarked_transaction_close_backup",
    )
    adapter.sync_directory(
        store.backups_dir,
        phase="unmarked_transaction_close_backups",
    )
    observed = read_json(manifest_path)
    if (
        not isinstance(observed, dict)
        or observed.get("lifecycle") != "rolled_back"
        or not isinstance(
            observed.get(
                "switch_journal"
                if evidence.operation == "switch"
                else "restore_journal"
            ),
            dict,
        )
        or observed[
            "switch_journal"
            if evidence.operation == "switch"
            else "restore_journal"
        ].get("state")
        != "recovered"
    ):
        raise SwitchError(
            f"Unmarked transaction {backup_id} closure is not durable"
        )


def _classify_store_recovery(
    store: Store,
) -> _StoreRecoveryClassification:
    pending_transaction = _pending_transaction_evidence(store)
    pending_capture_profiles = _capture_recovery_profiles(store)
    unmarked_transactions = _unmarked_transaction_evidence(
        store,
        marker_backup_id=(
            pending_transaction.backup_dir.name
            if pending_transaction is not None
            else None
        ),
    )
    if len(pending_capture_profiles) > 1:
        raise SwitchError(
            "Multiple pending captures require manual recovery: "
            + ", ".join(pending_capture_profiles)
        )
    unresolved_count = (
        int(pending_transaction is not None)
        + len(pending_capture_profiles)
        + len(unmarked_transactions)
    )
    if unresolved_count > 1:
        labels = [
            *(
                [pending_transaction.backup_dir.name]
                if pending_transaction is not None
                else []
            ),
            *(f"capture {profile}" for profile in pending_capture_profiles),
            *(evidence.backup_dir.name for evidence in unmarked_transactions),
        ]
        raise SwitchError(
            "Multiple unresolved transactions require manual recovery: "
            + ", ".join(labels)
        )
    return _StoreRecoveryClassification(
        pending_transaction=pending_transaction,
        pending_capture_profiles=pending_capture_profiles,
        unmarked_transactions=unmarked_transactions,
    )


def _immutable_state_bytes(state: Mapping[str, object]) -> bytes:
    return json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _immutable_state_object(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise SwitchError("Frozen switch state must be an object")
    return value


def _switch_effect_states_match(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if "kind" in expected:
        return _states_match(current, expected)
    return _immutable_state_bytes(current) == _immutable_state_bytes(expected)


def _expected_file_state(path: Path, payload: bytes, *, mode: int) -> dict[str, object]:
    return {
        "kind": "file",
        "path": str(path),
        "mode": mode,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _expected_missing_state(path: Path) -> dict[str, object]:
    return {"kind": "missing", "path": str(path)}


def _expected_directory_ensure_state(
    path: Path,
    before_state: Mapping[str, object],
) -> dict[str, object]:
    kind = before_state.get("kind")
    if kind == "directory":
        return dict(before_state)
    if kind != "missing":
        raise SwitchError(f"Switch directory predecessor is invalid: {path}")
    empty_payload = json.dumps(
        [],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {
        "kind": "directory",
        "path": str(path),
        "mode": 0o700,
        "entry_count": 0,
        "tree_sha256": hashlib.sha256(empty_payload).hexdigest(),
    }


def _state_at_destination(
    state: Mapping[str, object],
    destination: Path,
) -> dict[str, object]:
    return {**dict(state), "path": str(destination)}


def _default_symlink_mode() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-switch-symlink-mode-") as raw:
        root = Path(raw)
        link = root / "link"
        link.symlink_to("target")
        return stat.S_IMODE(link.lstat().st_mode)


def _capture_shared_support_entry_set_state(
    adapter: FilesystemAdapter,
    home: Path,
) -> dict[str, object]:
    from codex_switch_home_sync import (
        is_non_shareable_home_entry_name,
        is_runtime_state_name,
        shared_support_entries,
    )

    def planning_entries() -> tuple[tuple[str, Path], ...]:
        entries = [
            ("shared", entry)
            for entry in shared_support_entries(home)
        ]
        if home.exists():
            entries.extend(
                (
                    "stale-link-candidate",
                    entry,
                )
                for entry in home.iterdir()
                if entry.is_symlink()
                and (
                    is_runtime_state_name(entry.name)
                    or is_non_shareable_home_entry_name(entry.name)
                )
            )
        return tuple(sorted(entries, key=lambda item: (item[1].name, item[0])))

    home_identity_before = adapter.capture_identity(home)
    if not isinstance(home_identity_before, dict):
        raise SwitchError(f"Shared support home has no identity: {home}")
    entries_before = planning_entries()
    records: list[dict[str, object]] = []
    for role, entry in entries_before:
        if entry.parent != home:
            raise SwitchError(f"Shared support entry escaped its home: {entry}")
        identity_before = adapter.capture_identity(entry)
        identity_after = adapter.capture_identity(entry)
        if (
            not isinstance(identity_before, dict)
            or not isinstance(identity_after, dict)
            or identity_before != identity_after
        ):
            raise SwitchError(f"Shared support entry changed during capture: {entry}")
        records.append(
            {
                "name": entry.name,
                "role": role,
                "identity": identity_before,
            }
        )
    entries_after = planning_entries()
    home_identity_after = adapter.capture_identity(home)
    if (
        not isinstance(home_identity_after, dict)
        or home_identity_before != home_identity_after
        or tuple((role, entry.name) for role, entry in entries_before)
        != tuple((role, entry.name) for role, entry in entries_after)
    ):
        raise SwitchError(f"Shared support entry set changed during capture: {home}")
    return {
        "kind": home_identity_before.get("kind"),
        "path": str(home),
        "home_identity": home_identity_before,
        "entry_count": len(records),
        "entries": records,
    }


def _capture_switch_input_state(
    adapter: FilesystemAdapter,
    path: Path,
    *,
    capture_kind: str,
) -> dict[str, object]:
    if capture_kind == "path":
        return adapter.capture_state(path)
    if capture_kind == "shared_support_entry_set":
        return _capture_shared_support_entry_set_state(adapter, path)
    raise SwitchError(f"Unsupported switch input capture kind: {capture_kind}")


def _freeze_switch_input(
    adapter: FilesystemAdapter,
    path: Path,
    *,
    label: str,
    commit_state: Mapping[str, object] | None = None,
    capture_kind: str = "path",
) -> _FrozenSwitchInput:
    identity_before = adapter.capture_identity(path)
    before_state = _capture_switch_input_state(
        adapter,
        path,
        capture_kind=capture_kind,
    )
    identity_after = adapter.capture_identity(path)
    if (
        not isinstance(before_state, dict)
        or not isinstance(identity_before, dict)
        or not isinstance(identity_after, dict)
        or identity_before != identity_after
        or before_state.get("kind") != identity_before.get("kind")
    ):
        raise SwitchError(f"Switch adapter returned invalid state for {label}: {path}")
    return _FrozenSwitchInput(
        label=label,
        path=path,
        before_state_json=_immutable_state_bytes(before_state),
        before_identity_json=_immutable_state_bytes(identity_before),
        commit_state_json=_immutable_state_bytes(commit_state or before_state),
        commit_replaces_identity=commit_state is not None,
        capture_kind=capture_kind,
    )


def _require_frozen_switch_inputs(
    adapter: FilesystemAdapter,
    inputs: tuple[_FrozenSwitchInput, ...],
    *,
    phase: str,
    commit: bool,
    complete: bool = True,
) -> None:
    for frozen in inputs:
        expected = _immutable_state_object(
            frozen.commit_state_json if commit else frozen.before_state_json
        )
        require_state = (
            complete
            or commit
            or frozen.capture_kind != "path"
            or expected.get("kind") != "directory"
        )
        state_matches = True
        if require_state:
            current = _capture_switch_input_state(
                adapter,
                frozen.path,
                capture_kind=frozen.capture_kind,
            )
            state_matches = (
                isinstance(current, dict)
                and _immutable_state_bytes(current)
                == _immutable_state_bytes(expected)
            )
        identity_matches = True
        if not commit or not frozen.commit_replaces_identity:
            current_identity = adapter.capture_identity(frozen.path)
            identity_matches = (
                isinstance(current_identity, dict)
                and _immutable_state_bytes(current_identity)
                == frozen.before_identity_json
            )
        if not state_matches or not identity_matches:
            raise SwitchError(
                f"Required switch input changed {phase}: "
                f"{frozen.label}: {frozen.path}"
            )


class _SwitchPlanningInputTracker:
    def __init__(self, adapter: FilesystemAdapter) -> None:
        self.adapter = adapter
        self._inputs: dict[tuple[str, str], _FrozenSwitchInput] = {}

    def begin(
        self,
        specs: tuple[tuple[Path, str], ...],
        *,
        capture_kind: str = "path",
    ) -> tuple[_FrozenSwitchInput, ...]:
        observations: list[_FrozenSwitchInput] = []
        seen: set[tuple[str, str]] = set()
        for path, label in specs:
            raw_path = str(path)
            key = (raw_path, capture_kind)
            if key in seen:
                continue
            seen.add(key)
            existing = self._inputs.get(key)
            if existing is not None:
                _require_frozen_switch_inputs(
                    self.adapter,
                    (existing,),
                    phase="before producing read",
                    commit=False,
                )
                observations.append(existing)
                continue
            observations.append(
                _freeze_switch_input(
                    self.adapter,
                    path,
                    label=label,
                    capture_kind=capture_kind,
                )
            )
        return tuple(observations)

    def finish(
        self,
        observations: tuple[_FrozenSwitchInput, ...],
        *,
        retain: bool = True,
    ) -> None:
        _require_frozen_switch_inputs(
            self.adapter,
            observations,
            phase="after producing read",
            commit=False,
        )
        if not retain:
            return
        for observation in observations:
            raw_path = str(observation.path)
            key = (raw_path, observation.capture_kind)
            existing = self._inputs.get(key)
            if existing is not None and (
                existing.before_state_json != observation.before_state_json
                or existing.before_identity_json
                != observation.before_identity_json
            ):
                raise SwitchError(
                    f"Switch planning input changed between producing reads: "
                    f"{observation.label}: {observation.path}"
                )
            self._inputs.setdefault(key, observation)

    def frozen(
        self,
        commit_states: Mapping[Path, Mapping[str, object]],
    ) -> tuple[_FrozenSwitchInput, ...]:
        frozen: list[_FrozenSwitchInput] = []
        for observation in self._inputs.values():
            commit_state = commit_states.get(observation.path)
            frozen.append(
                _FrozenSwitchInput(
                    label=observation.label,
                    path=observation.path,
                    before_state_json=observation.before_state_json,
                    before_identity_json=observation.before_identity_json,
                    commit_state_json=(
                        _immutable_state_bytes(commit_state)
                        if commit_state is not None
                        else observation.before_state_json
                    ),
                    commit_replaces_identity=commit_state is not None,
                    capture_kind=observation.capture_kind,
                )
            )
        return tuple(frozen)


def _expected_json_file_state(
    path: Path,
    data: Mapping[str, object],
) -> dict[str, object]:
    payload = _canonical_json_bytes(data)
    return {
        "kind": "file",
        "path": str(path),
        "mode": 0o600,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _unmanaged_capture_projection(
    records: list[dict[str, object]],
) -> dict[str, object]:
    managed_top_level = frozenset((*MANAGED_FILES, "manifest.json"))
    unmanaged_records: list[dict[str, object]] = []
    for record in records:
        relative = record.get("path")
        if not isinstance(relative, str):
            raise SwitchError("Capture directory record has no relative path")
        if relative.split("/", 1)[0] not in managed_top_level:
            unmanaged_records.append(record)
    payload = json.dumps(
        unmanaged_records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {
        "entry_count": len(unmanaged_records),
        "tree_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _capture_unmanaged_projection(
    adapter: FilesystemAdapter,
    path: Path,
) -> dict[str, object]:
    binding = _capture_binding_for_path(adapter, path)
    if binding is None:
        raise SwitchError(
            f"Capture unmanaged projection is outside pinned workspace: {path}"
        )
    root_descriptor, relative = binding
    descriptor = _open_directory_at(root_descriptor, relative)
    try:
        return _unmanaged_capture_projection(_directory_records_fd(descriptor))
    finally:
        os.close(descriptor)


def _validated_recorded_mode(
    state: Mapping[str, object],
    path: Path | str,
    *,
    label: str,
    required: bool,
) -> int | None:
    mode = state.get("mode")
    if mode is None and not required:
        return None
    if (
        not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o7777
    ):
        raise SwitchError(f"{label} has an invalid permission mode: {path}")
    return mode


def _preflight_manifest_states(manifest: RestoreManifest) -> None:
    for entry in manifest.entries:
        for state in (entry.before_state, entry.committed_after_state):
            kind = state.get("kind")
            if kind not in {"missing", "file", "symlink", "directory"}:
                raise SwitchError(f"Unsupported backup state kind: {kind}")
            _validated_recorded_mode(
                state,
                entry.path,
                label="Backup state",
                required=kind in {"file", "directory"},
            )
            if kind == "symlink" and not isinstance(
                state.get("symlink_target"), str
            ):
                raise SwitchError(
                    f"Symlink backup state has no attested target: {entry.path}"
                )
            if kind == "file":
                digest = state.get("sha256")
                if not (
                    isinstance(digest, str)
                    and len(digest) == 64
                    and all(character in "0123456789abcdef" for character in digest)
                ):
                    raise SwitchError(
                        f"File backup state has no valid SHA-256 attestation: "
                        f"{entry.path}"
                    )
                if manifest.schema_version == 2:
                    size = state.get("size")
                    if (
                        not isinstance(size, int)
                        or isinstance(size, bool)
                        or size < 0
                    ):
                        raise SwitchError(
                            f"Schema-v2 file state has no valid size: {entry.path}"
                        )
            if kind == "directory" and manifest.schema_version == 2:
                digest = state.get("tree_sha256")
                if not (
                    isinstance(digest, str)
                    and len(digest) == 64
                    and all(character in "0123456789abcdef" for character in digest)
                ):
                    raise SwitchError(
                        f"Directory backup state has no valid tree SHA-256 "
                        f"attestation: {entry.path}"
                    )
                entry_count = state.get("entry_count")
                if (
                    not isinstance(entry_count, int)
                    or isinstance(entry_count, bool)
                    or entry_count < 0
                ):
                    raise SwitchError(
                        f"Directory backup state has no valid entry count: {entry.path}"
                    )
        if manifest.schema_version == 1 and (
            entry.before_state.get("kind") == "directory"
            or entry.committed_after_state.get("kind") == "directory"
        ):
            raise SwitchError(
                f"Backup {manifest.backup_id} has a v1 directory entry that is "
                "not recursively attested; preserve it for manual recovery."
            )


def _validate_attested_path_state(
    state: object,
    path: Path,
    *,
    label: str,
) -> dict[str, object]:
    if not isinstance(state, dict):
        raise SwitchError(f"{label} has no attested state: {path}")
    kind = state.get("kind")
    if kind not in {"missing", "file", "symlink", "directory"}:
        raise SwitchError(f"{label} has an invalid state kind for {path}: {kind}")
    raw_path = state.get("path")
    if raw_path is not None and raw_path != str(path):
        raise SwitchError(f"{label} state path is inconsistent: {path}")
    if kind == "missing":
        _validated_recorded_mode(
            state,
            path,
            label=label,
            required=False,
        )
        return dict(state)
    _validated_recorded_mode(
        state,
        path,
        label=label,
        required=True,
    )
    if kind == "symlink":
        if not isinstance(state.get("symlink_target"), str):
            raise SwitchError(f"{label} has an invalid symlink target: {path}")
    elif kind == "file":
        digest = state.get("sha256")
        size = state.get("size")
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            and isinstance(size, int)
            and not isinstance(size, bool)
            and size >= 0
        ):
            raise SwitchError(f"{label} has an invalid file attestation: {path}")
    elif kind == "directory":
        digest = state.get("tree_sha256")
        entry_count = state.get("entry_count")
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            and isinstance(entry_count, int)
            and not isinstance(entry_count, bool)
            and entry_count >= 0
        ):
            raise SwitchError(
                f"{label} has an invalid directory attestation: {path}"
            )
    return dict(state)


def _preflight_current_states(
    manifest: RestoreManifest,
    destinations: tuple[Path, ...],
    *,
    force: bool,
    adapter: FilesystemAdapter,
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
]:
    if len(manifest.entries) != len(destinations):
        raise SwitchError("Restore destination count does not match restore plan")
    initial_states: list[dict[str, object]] = []
    initial_identities: list[dict[str, object]] = []
    for entry, destination in zip(manifest.entries, destinations):
        identity_before = adapter.capture_identity(destination)
        current = adapter.capture_state(destination)
        identity_after = adapter.capture_identity(destination)
        if (
            not isinstance(current, dict)
            or not isinstance(identity_before, dict)
            or identity_before != identity_after
            or current.get("kind") != identity_before.get("kind")
        ):
            raise SwitchError(
                f"Filesystem adapter returned invalid state for {entry.path}"
            )
        initial_states.append(current)
        initial_identities.append(identity_before)
        committed_after = entry.committed_after_state
        if (
            current.get("kind") == "directory"
            and committed_after.get("kind") == "directory"
            and current.get("tree_sha256")
            == committed_after.get("tree_sha256")
            and current.get("entry_count")
            != committed_after.get("entry_count")
        ):
            raise SwitchError(
                f"Directory committed state has an inconsistent entry count: "
                f"{entry.path}"
            )
        if not force and not _states_match(current, entry.committed_after_state):
            raise SwitchError(
                f"Current path changed since backup was committed: {entry.path}. "
                "Use --force to restore anyway."
            )
    return tuple(initial_states), tuple(initial_identities)


def _contained_payload_path(backup_dir: Path, payload: str) -> Path:
    relative = Path(payload)
    if relative.is_absolute():
        raise SwitchError(
            f"Backup payload escapes backup directory: {payload}"
        )
    base = backup_dir.resolve()
    candidate = (backup_dir / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise SwitchError(
            f"Backup payload escapes backup directory: {payload}"
        ) from exc
    if candidate == base:
        raise SwitchError(f"Backup payload is not a file or directory: {payload}")
    return candidate


def _preflight_payload_paths(backup_dir: Path, manifest: RestoreManifest) -> None:
    for entry in manifest.entries:
        kind = entry.before_state.get("kind")
        if kind in {"file", "directory"}:
            if not entry.payload:
                raise SwitchError(f"Backup entry is missing payload: {entry.path}")
            payload_path = _contained_payload_path(backup_dir, entry.payload)
            payload_state = _path_state(payload_path)
            if payload_state.get("kind") == "missing":
                raise SwitchError(
                    f"Backup payload is missing for {entry.path}: {entry.payload}"
                )
            if not _states_match(payload_state, entry.before_state):
                raise SwitchError(
                    f"Backup payload digest or state mismatch for {entry.path}: "
                    f"{entry.payload}"
                )
        elif entry.payload is not None:
            raise SwitchError(
                f"Backup entry has unexpected payload for {kind}: {entry.path}"
            )


def _canonical_destination(path: Path) -> Path:
    if not path.is_absolute():
        raise SwitchError(f"Restore target is not approved: {path}")
    if path.name in {"", ".", ".."}:
        raise SwitchError(f"Restore target is not approved: {path}")
    return path.parent.resolve() / path.name


def _historical_backup_dir(store: Store, backup_id: str) -> Path:
    raw = Path(backup_id)
    if backup_id in {".", ".."} or len(raw.parts) != 1 or raw.name != backup_id:
        raise SwitchError(f"Backup id is not contained in the store: {backup_id}")
    backups_root = store.backups_dir.resolve()
    candidate = (store.backups_dir / backup_id).resolve()
    if candidate.parent != backups_root:
        raise SwitchError(f"Backup id is not contained in the store: {backup_id}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _canonical_manifest_home(
    store: Store,
    raw_home: object,
    *,
    label: str,
) -> Path:
    if not isinstance(raw_home, str) or not raw_home:
        raise SwitchError(f"{label} has an invalid home")
    home = Path(raw_home).expanduser()
    if not home.is_absolute():
        raise SwitchError(f"{label} home must be absolute")
    normalized = Path(os.path.normpath(str(home)))
    if normalized != home or home.is_symlink():
        raise SwitchError(f"{label} home must be canonical: {raw_home}")
    canonical = home.resolve()
    backups_root = store.backups_dir.resolve()
    if canonical == backups_root or _is_within(canonical, backups_root):
        raise SwitchError(f"{label} home is not approved: {raw_home}")
    return canonical


def _validated_manifest_home(store: Store, profile: str) -> Path | None:
    manifest_path = store.manifest_path(profile)
    try:
        raw_manifest = read_json(manifest_path)
    except FileNotFoundError:
        return None
    if not isinstance(raw_manifest, dict):
        raise SwitchError(f"Profile manifest must be an object: {manifest_path}")
    raw_home = raw_manifest.get("codex_home")
    if raw_home is None:
        return None
    return _canonical_manifest_home(
        store,
        raw_home,
        label=f"Profile manifest {manifest_path}",
    )


def _transaction_destination_authority(
    store: Store,
) -> tuple[set[Path], set[Path]]:
    from codex_switch_app_wrapper import managed_profile_app_cli_path
    from codex_switch_shell import shell_cli_bootstrap_path

    allowed_roots = {
        store.profile_dir("internal").resolve(),
        store.profile_dir("openai-official").resolve(),
        store.managed_home("internal").resolve(),
        store.managed_home("openai-official").resolve(),
        store.official_codex_home.resolve(),
    }
    if store.internal_codex_home is not None:
        allowed_roots.add(store.internal_codex_home.resolve())
    for profile in ("internal", "openai-official"):
        manifest_home = _validated_manifest_home(store, profile)
        if manifest_home is not None:
            allowed_roots.add(manifest_home)

    exact_targets = {
        _canonical_destination(store.launch_agent_path.absolute()),
        _canonical_destination(store.active_path.absolute()),
        _canonical_destination((store.bin_dir / "codex").absolute()),
        _canonical_destination(
            managed_profile_app_cli_path(store, "internal").absolute()
        ),
        _canonical_destination(
            managed_profile_app_cli_path(store, "openai-official").absolute()
        ),
        _canonical_destination(
            capability_receipt_path_for_launcher(
                managed_profile_app_cli_path(store, "internal")
            ).absolute()
        ),
        _canonical_destination(
            capability_receipt_path_for_launcher(
                managed_profile_app_cli_path(store, "openai-official")
            ).absolute()
        ),
    }
    shell_profile = shell_cli_bootstrap_path()
    if shell_profile is not None:
        exact_targets.add(_canonical_destination(shell_profile.absolute()))
    return allowed_roots, exact_targets


def _preflight_restore_targets(
    store: Store,
    manifest: RestoreManifest,
) -> tuple[tuple[Path, ...], tuple[tuple[Path, ...], ...]]:
    allowed_roots, exact_targets = _transaction_destination_authority(store)
    backups_root = store.backups_dir.resolve()
    destinations: list[Path] = []
    parent_cleanup_plans: list[tuple[Path, ...]] = []
    for entry in manifest.entries:
        destination = _canonical_destination(Path(entry.path))
        if any(
            destination == existing
            or _is_within(destination, existing)
            or _is_within(existing, destination)
            for existing in destinations
        ):
            raise SwitchError(
                f"Restore plan contains duplicate or overlapping targets: "
                f"{entry.path}"
            )
        destinations.append(destination)
        if destination == backups_root or _is_within(destination, backups_root):
            raise SwitchError(f"Restore target is not approved: {entry.path}")
        if destination not in exact_targets and not any(
            destination == root or _is_within(destination, root)
            for root in allowed_roots
        ):
            raise SwitchError(f"Restore target is not approved: {entry.path}")
        cleanup_parents: set[Path] = set()
        if entry.created_parent_paths and entry.before_state.get("kind") != "missing":
            raise SwitchError(
                f"Backup entry has created-parent cleanup for a non-missing "
                f"state: {entry.path}"
            )
        declared_parents: list[Path] = []
        for raw_parent in entry.created_parent_paths:
            parent = _canonical_destination(Path(raw_parent))
            if parent == parent.parent:
                raise SwitchError(
                    f"Backup created-parent path reaches filesystem root: {raw_parent}"
                )
            if parent in declared_parents:
                raise SwitchError(
                    f"Backup created-parent path is duplicated: {raw_parent}"
                )
            declared_parents.append(parent)
        expected_parent = destination.parent
        for parent in sorted(
            declared_parents,
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            if parent != expected_parent:
                raise SwitchError(
                    f"Backup created-parent path is not a contiguous target ancestor: "
                    f"{parent}"
                )
            cleanup_parents.add(parent)
            expected_parent = parent.parent
        parent_cleanup_plans.append(
            tuple(
                sorted(
                    cleanup_parents,
                    key=lambda path: len(path.parts),
                    reverse=True,
                )
            )
        )
    return tuple(destinations), tuple(parent_cleanup_plans)


def _remove_path(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_material(source: Path, destination: Path, kind: object) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        shutil.copy2(source, destination)
    elif kind == "directory":
        shutil.copytree(source, destination, symlinks=True)
    else:
        raise SwitchError(f"Cannot stage backup state kind: {kind}")


def _missing_parent_paths(path: Path) -> tuple[Path, ...]:
    missing: list[Path] = []
    current = path.parent
    while current != current.parent:
        try:
            current.lstat()
            break
        except FileNotFoundError:
            missing.append(current)
            current = current.parent
    return tuple(missing)


def _unique_backup_dir(store: Store, stem: str) -> Path:
    ensure_private_dir(store.backups_dir)
    candidate = store.backups_dir / f"{now_stamp()}-{stem}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = store.backups_dir / f"{now_stamp()}-{stem}-{suffix}"
    ensure_private_dir(candidate)
    return candidate


def _frozen_restore_authority(
    store: Store,
    destinations: tuple[Path, ...],
    staged_sources: tuple[Path | None, ...],
    *,
    cleanup_targets: tuple[Path, ...] = tuple(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    allowed_roots, exact_targets = _transaction_destination_authority(store)
    backups_root = store.backups_dir.resolve()
    manifest_paths = {
        _canonical_destination(store.manifest_path("internal").absolute()): "internal",
        _canonical_destination(
            store.manifest_path("openai-official").absolute()
        ): "openai-official",
    }
    for destination, staged_source in zip(destinations, staged_sources):
        if destination not in manifest_paths or staged_source is None:
            continue
        planned_manifest = read_json(staged_source)
        if not isinstance(planned_manifest, dict):
            raise SwitchError(
                f"Planned profile manifest must be an object: {destination}"
            )
        raw_home = planned_manifest.get("codex_home")
        if raw_home is None:
            continue
        canonical = _canonical_manifest_home(
            store,
            raw_home,
            label=f"Planned profile manifest {destination}",
        )
        allowed_roots.add(canonical)
    for cleanup_target in cleanup_targets:
        canonical_cleanup = _canonical_destination(cleanup_target)
        if canonical_cleanup == backups_root or _is_within(
            canonical_cleanup,
            backups_root,
        ):
            raise SwitchError(
                f"Restore cleanup target is not approved: {cleanup_target}"
            )
        exact_targets.add(canonical_cleanup)
    return (
        tuple(sorted(str(path) for path in allowed_roots)),
        tuple(sorted(str(path) for path in exact_targets)),
    )


def _prepare_restore_safety_backup(
    store: Store,
    historical_backup_dir: Path,
    manifest: RestoreManifest,
    destinations: tuple[Path, ...],
    adapter: FilesystemAdapter,
    initial_states: tuple[dict[str, object], ...],
    initial_identities: tuple[dict[str, object], ...],
    target_route_guards: tuple[dict[str, object], ...],
    parent_cleanup_plans: tuple[tuple[Path, ...], ...],
) -> tuple[
    Path,
    dict[str, object],
    tuple[dict[str, object], ...],
    tuple[Path | None, ...],
    tuple[dict[str, object] | None, ...],
    _PendingTransactionMarker,
]:
    if (
        len(manifest.entries) != len(destinations)
        or len(manifest.entries) != len(initial_states)
        or len(manifest.entries) != len(initial_identities)
        or len(manifest.entries) != len(target_route_guards)
        or len(manifest.entries) != len(parent_cleanup_plans)
    ):
        raise SwitchError("Initial restore state count does not match restore plan")
    for entry, destination, initial_state, initial_identity in zip(
        manifest.entries,
        destinations,
        initial_states,
        initial_identities,
    ):
        current = adapter.capture_state(destination)
        current_identity = adapter.capture_identity(destination)
        if (
            not _states_match(current, initial_state)
            or current_identity != initial_identity
        ):
            raise SwitchError(
                f"Restore target changed after initial preflight: {entry.path}"
            )

    safety_dir = _unique_backup_dir(store, f"restore-{manifest.backup_id}")
    stage_dir = safety_dir / "restore-stage"
    ensure_private_dir(stage_dir)
    payload_dir = safety_dir / "payloads"
    observed_states: list[dict[str, object]] = []
    staged_sources: list[Path | None] = []
    staged_artifacts: list[dict[str, object] | None] = []
    safety_entries: list[dict[str, object]] = []

    for index, entry in enumerate(manifest.entries):
        destination = destinations[index]
        desired_kind = entry.before_state.get("kind")
        staged_source: Path | None = None
        staged_artifact: dict[str, object] | None = None
        if desired_kind in {"file", "directory"}:
            if entry.payload is None:
                raise SwitchError(f"Backup entry is missing payload: {entry.path}")
            historical_source = _contained_payload_path(
                historical_backup_dir, entry.payload
            )
            staged_source = stage_dir / f"{index:04d}-{destination.name}"
            adapter.copy_material(
                historical_source,
                staged_source,
                desired_kind,
                phase="restore_stage",
            )
            if not _states_match(
                adapter.capture_state(staged_source), entry.before_state
            ):
                raise SwitchError(
                    f"Staged restore payload changed for {entry.path}: {entry.payload}"
                )
            staged_state = adapter.capture_state(staged_source)
            staged_identity = adapter.capture_identity(staged_source)
            if not isinstance(staged_state, dict) or not isinstance(
                staged_identity,
                dict,
            ):
                raise SwitchError(
                    f"Staged restore identity is invalid for {entry.path}"
                )
            staged_artifact = {
                "staged_path": str(staged_source),
                "staged_state": staged_state,
                "staged_identity": staged_identity,
                "staged_route_guard": _capture_route_guard(staged_source),
            }
        staged_sources.append(staged_source)
        staged_artifacts.append(staged_artifact)

        observed = initial_states[index]
        observed_states.append(observed)
        safety_payload: str | None = None
        observed_kind = observed.get("kind")
        if observed_kind in {"file", "directory"}:
            safety_source = payload_dir / f"{index:04d}-{destination.name}"
            adapter.copy_material(
                destination,
                safety_source,
                observed_kind,
                phase="safety_backup",
            )
            if not _states_match(
                adapter.capture_state(safety_source), observed
            ):
                raise SwitchError(
                    f"Safety backup payload does not match initial state: "
                    f"{destination}; safety backup: {safety_dir.name}"
                )
            safety_payload = safety_source.relative_to(safety_dir).as_posix()
        desired_state: dict[str, object]
        if staged_source is not None:
            staged_state = adapter.capture_state(staged_source)
            if not isinstance(staged_state, dict):
                raise SwitchError(
                    f"Staged restore state is invalid for {destination}"
                )
            desired_state = _capture_state_at_path(staged_state, destination)
        elif desired_kind == "symlink":
            desired_state = _capture_state_at_path(
                entry.before_state,
                destination,
            )
            desired_state.setdefault("mode", _default_symlink_mode())
        else:
            desired_state = _expected_missing_state(destination)
        safety_entry: dict[str, object] = {
            "path": str(destination),
            "before_state": observed,
            "before_identity": dict(initial_identities[index]),
            "committed_after_state": desired_state,
            "route_path": str(Path(entry.path).expanduser()),
            "route_guard": dict(target_route_guards[index]),
        }
        if safety_payload is not None:
            safety_entry["payload"] = safety_payload
            safety_payload_path = safety_dir / safety_payload
            safety_payload_state = adapter.capture_state(safety_payload_path)
            safety_payload_identity = adapter.capture_identity(
                safety_payload_path
            )
            if not isinstance(safety_payload_state, dict) or not isinstance(
                safety_payload_identity,
                dict,
            ):
                raise SwitchError(
                    f"Safety payload identity is invalid: {safety_payload_path}"
                )
            safety_entry["payload_state"] = safety_payload_state
            safety_entry["payload_identity"] = safety_payload_identity
            safety_entry["payload_route_guard"] = _capture_route_guard(
                safety_payload_path
            )
        safety_entries.append(safety_entry)

    safety_manifest: dict[str, object] = {
        "schema_version": 2,
        "lifecycle": "prepared",
        "id": safety_dir.name,
        "operation": "restore",
        "source_backup_id": manifest.backup_id,
        "to_profile": manifest.profile,
        "created_at": now_stamp(),
        "tool": "codex-switch",
        "entries": safety_entries,
        "restore_journal": {
            "schema_version": 1,
            "state": "prepared",
            "effects": [],
        },
    }
    frozen_roots, frozen_exact_targets = _frozen_restore_authority(
        store,
        destinations,
        tuple(staged_sources),
        cleanup_targets=tuple(
            parent
            for cleanup_plan in parent_cleanup_plans
            for parent in cleanup_plan
        ),
    )
    restore_journal = safety_manifest["restore_journal"]
    if not isinstance(restore_journal, dict):
        raise SwitchError(f"Invalid restore journal: {safety_dir.name}")
    restore_journal["authority_schema_version"] = 1
    restore_journal["allowed_roots"] = list(frozen_roots)
    restore_journal["exact_targets"] = list(frozen_exact_targets)
    adapter.write_manifest(
        safety_dir / "backup.json",
        safety_manifest,
        phase="prepare_manifest",
    )
    _durably_sync_transaction_backup(adapter, store, safety_dir)
    for artifact in staged_artifacts:
        if artifact is None:
            continue
        stage_path = Path(str(artifact["staged_path"]))
        if (
            adapter.capture_state(stage_path) != artifact["staged_state"]
            or adapter.capture_identity(stage_path)
            != artifact["staged_identity"]
        ):
            raise SwitchError(
                f"Restore staged identity changed before journal: {stage_path}"
            )
    restore_journal = safety_manifest.get("restore_journal")
    if not isinstance(restore_journal, dict):
        raise SwitchError(f"Invalid restore journal: {safety_dir.name}")
    pending_marker = _bind_pending_transaction(
        store,
        safety_dir,
        "restore",
        restore_journal,
    )
    adapter.write_manifest(
        safety_dir / "backup.json",
        safety_manifest,
        phase="restore_journal_prepare",
    )
    _publish_pending_transaction(adapter, pending_marker)
    return (
        safety_dir,
        safety_manifest,
        tuple(observed_states),
        tuple(staged_sources),
        tuple(staged_artifacts),
        pending_marker,
    )


def _safety_payload_path(
    safety_dir: Path,
    safety_entry: Mapping[str, object],
) -> Path | None:
    payload = safety_entry.get("payload")
    if not isinstance(payload, str):
        return None
    return _contained_payload_path(safety_dir, payload)


def _safety_payload_artifact(
    safety_dir: Path,
    safety_entry: Mapping[str, object],
) -> dict[str, object] | None:
    payload_path = _safety_payload_path(safety_dir, safety_entry)
    if payload_path is None:
        return None
    payload_state = safety_entry.get("payload_state")
    payload_identity = safety_entry.get("payload_identity")
    payload_route_guard = safety_entry.get("payload_route_guard")
    if (
        not isinstance(payload_state, dict)
        or not isinstance(payload_identity, dict)
        or not isinstance(payload_route_guard, dict)
    ):
        raise SwitchError(
            f"Restore safety payload identity is incomplete: {payload_path}"
        )
    raw_lexical_path = payload_identity.get("path")
    if (
        not isinstance(raw_lexical_path, str)
        or Path(raw_lexical_path).resolve() != payload_path
    ):
        raise SwitchError(
            f"Restore safety payload path is inconsistent: {payload_path}"
        )
    return {
        "staged_path": raw_lexical_path,
        "staged_state": dict(payload_state),
        "staged_identity": dict(payload_identity),
        "staged_route_guard": dict(payload_route_guard),
    }


class _RestoreMutationJournal:
    def __init__(
        self,
        adapter: FilesystemAdapter,
        safety_dir: Path,
        manifest: dict[str, object],
    ) -> None:
        self.adapter = adapter
        self.safety_dir = safety_dir
        self.manifest = manifest
        journal = manifest.get("restore_journal")
        if not isinstance(journal, dict):
            raise SwitchError(f"Invalid restore journal: {safety_dir.name}")
        effects = journal.get("effects")
        if not isinstance(effects, list):
            raise SwitchError(f"Invalid restore effect list: {safety_dir.name}")
        self.journal = journal
        self.effects = effects

    def persist(self, *, phase: str) -> None:
        self.adapter.write_manifest(
            self.safety_dir / "backup.json",
            self.manifest,
            phase=phase,
        )

    def begin_target(
        self,
        *,
        entry_index: int,
        path: Path,
        route_path: Path,
        route_guard: Mapping[str, object],
        before_state: Mapping[str, object],
        before_identity: Mapping[str, object],
        planned_after_state: Mapping[str, object],
        staged_artifact: Mapping[str, object] | None,
        planned_created_parents: tuple[Path, ...],
    ) -> dict[str, object]:
        effect: dict[str, object] = {
            "id": len(self.effects),
            "kind": "filesystem",
            "phase": f"restore_apply_{entry_index}",
            "status": "intent",
            "entry_index": entry_index,
            "path": str(path),
            "route_path": str(route_path),
            "route_guard": dict(route_guard),
            "before_state": dict(before_state),
            "before_identity": dict(before_identity),
            "planned_after_state": dict(planned_after_state),
            "planned_created_parent_paths": [
                str(parent) for parent in planned_created_parents
            ],
        }
        if staged_artifact is not None:
            for key in (
                "staged_path",
                "staged_state",
                "staged_identity",
                "staged_route_guard",
            ):
                value = staged_artifact.get(key)
                if value is None:
                    raise SwitchError(
                        f"Restore staged artifact is incomplete: {path}"
                    )
                effect[key] = (
                    dict(value) if isinstance(value, dict) else value
                )
        self.effects.append(effect)
        self.persist(phase="restore_journal_intent")
        return effect

    def complete_target(
        self,
        effect: dict[str, object],
        *,
        observed_after_state: Mapping[str, object],
        produced_identity: Mapping[str, object],
        created_parents: tuple[Path, ...],
    ) -> None:
        planned_after_state = effect.get("planned_after_state")
        if not isinstance(planned_after_state, dict) or not _states_match(
            observed_after_state,
            planned_after_state,
        ):
            raise SwitchError(
                f"Restore effect produced an unexpected state: "
                f"{effect.get('path')}"
            )
        effect["observed_after_state"] = dict(observed_after_state)
        effect["produced_identity"] = dict(produced_identity)
        effect["created_parent_paths"] = [str(path) for path in created_parents]
        effect["status"] = "applied"
        self.persist(phase="restore_journal_applied")

    def begin_parent_cleanup(
        self,
        *,
        path: Path,
        route_guard: Mapping[str, object],
        before_state: Mapping[str, object],
        before_identity: Mapping[str, object],
    ) -> dict[str, object]:
        effect: dict[str, object] = {
            "id": len(self.effects),
            "kind": "filesystem",
            "phase": "restore_parent_cleanup",
            "status": "intent",
            "path": str(path),
            "route_guard": dict(route_guard),
            "before_state": dict(before_state),
            "before_identity": dict(before_identity),
            "planned_after_state": _expected_missing_state(path),
        }
        self.effects.append(effect)
        self.persist(phase="restore_journal_intent")
        return effect

    def observe_parent_cleanup(
        self,
        effect: dict[str, object],
        observed_state: Mapping[str, object],
    ) -> None:
        effect["action_observed_state"] = dict(observed_state)
        self.persist(phase="restore_journal_action_observed")

    def complete_parent_cleanup(
        self,
        effect: dict[str, object],
        observed_state: Mapping[str, object],
    ) -> None:
        planned_after_state = effect.get("planned_after_state")
        if not isinstance(planned_after_state, dict) or not _states_match(
            observed_state,
            planned_after_state,
        ):
            raise SwitchError(
                f"Restore parent cleanup produced an unexpected state: "
                f"{effect.get('path')}"
            )
        effect["observed_after_state"] = dict(observed_state)
        effect["status"] = "applied"
        self.persist(phase="restore_journal_applied")


def _make_restore_effect_durable(
    adapter: FilesystemAdapter,
    path: Path,
    state: Mapping[str, object],
    *,
    phase: str,
) -> None:
    kind = state.get("kind")
    if kind == "file":
        adapter.sync_file(path, phase=f"{phase}_file")
    elif kind == "directory":
        adapter.sync_tree(
            path,
            file_phase=f"{phase}_tree_file",
            directory_phase=f"{phase}_tree_directory",
        )
    adapter.sync_directory(path.parent, phase=f"{phase}_parent")


def _write_safety_lifecycle(
    safety_dir: Path,
    safety_manifest: dict[str, object],
    lifecycle: str,
    *,
    adapter: FilesystemAdapter,
    phase: str,
    error: str | None = None,
) -> None:
    terminal_manifest = _canonical_json_object(safety_manifest)
    terminal_manifest["lifecycle"] = lifecycle
    terminal_manifest[f"{lifecycle}_at"] = now_stamp()
    restore_journal = terminal_manifest.get("restore_journal")
    if isinstance(restore_journal, dict):
        terminal_states = {
            "committed": "committed",
            "rolled_back": "recovered",
            "rollback_failed": "rollback_failed",
        }
        terminal_state = terminal_states.get(lifecycle)
        if terminal_state is None:
            raise SwitchError(
                f"Unsupported restore terminal lifecycle: {lifecycle}"
            )
        restore_journal["state"] = terminal_state
        effects = restore_journal.get("effects")
        if not isinstance(effects, list):
            raise SwitchError("Restore terminal journal has no effect list")
        for index, effect in enumerate(effects):
            if not isinstance(effect, dict) or effect.get("id") != index:
                raise SwitchError(
                    "Restore terminal journal has an invalid effect order"
                )
            effect["recovery_state"] = terminal_state
    if error is not None:
        terminal_manifest["error"] = error
    adapter.write_manifest(
        safety_dir / "backup.json",
        terminal_manifest,
        phase=phase,
    )
    adapter.sync_file(
        safety_dir / "backup.json",
        phase=f"{phase}_file",
    )
    adapter.sync_directory(
        safety_dir,
        phase=f"{phase}_parent",
    )


def _read_authoritative_committed_restore(
    store: Store,
    safety_dir: Path,
    marker: _PendingTransactionMarker,
) -> dict[str, object] | None:
    try:
        evidence = _validate_pending_transaction_evidence(store, marker.path)
    except (OSError, SwitchError, ValueError, TypeError):
        return None
    if (
        evidence.backup_dir != safety_dir
        or evidence.operation != "restore"
        or evidence.lifecycle != "committed"
    ):
        return None
    return dict(evidence.manifest)


def _frozen_restore_destination_authority(
    store: Store,
    backup_id: str,
    journal: Mapping[str, object],
) -> tuple[set[Path], set[Path]]:
    if journal.get("authority_schema_version") != 1:
        raise SwitchError(
            f"Prepared restore {backup_id} has no frozen destination authority"
        )
    raw_roots = journal.get("allowed_roots")
    raw_exact = journal.get("exact_targets")
    if (
        not isinstance(raw_roots, list)
        or not raw_roots
        or not isinstance(raw_exact, list)
    ):
        raise SwitchError(
            f"Prepared restore {backup_id} has invalid destination authority"
        )
    backups_root = store.backups_dir.resolve()

    def validate(raw_values: list[object], *, label: str) -> set[Path]:
        values: set[Path] = set()
        for raw_value in raw_values:
            if not isinstance(raw_value, str) or not raw_value:
                raise SwitchError(
                    f"Prepared restore {backup_id} has invalid {label} authority"
                )
            path = Path(raw_value)
            if not path.is_absolute() or path.resolve() != path:
                raise SwitchError(
                    f"Prepared restore {backup_id} has non-canonical {label}: "
                    f"{raw_value}"
                )
            if path == backups_root or _is_within(path, backups_root):
                raise SwitchError(
                    f"Prepared restore {backup_id} authorizes backup storage: "
                    f"{raw_value}"
                )
            values.add(path)
        return values

    return validate(raw_roots, label="root"), validate(raw_exact, label="target")


def _restore_recovery_destination(
    store: Store,
    backup_id: str,
    raw_path: object,
    allowed_roots: set[Path],
    exact_targets: set[Path],
) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise SwitchError(f"Prepared restore {backup_id} has an invalid path")
    destination = _canonical_destination(Path(raw_path))
    backups_root = store.backups_dir.resolve()
    if destination == backups_root or _is_within(destination, backups_root):
        raise SwitchError(
            f"Prepared restore {backup_id} target is not approved: {raw_path}"
        )
    if destination not in exact_targets and not any(
        destination == root or _is_within(destination, root)
        for root in allowed_roots
    ):
        raise SwitchError(
            f"Prepared restore {backup_id} target is not approved: {raw_path}"
        )
    return destination


def _restore_route_was_removed_by_cleanup(
    destination: Path,
    cleanup_paths: tuple[Path, ...],
    adapter: FilesystemAdapter,
) -> bool:
    return any(
        cleanup_path in destination.parents
        and adapter.capture_state(cleanup_path).get("kind") == "missing"
        for cleanup_path in cleanup_paths
    )


def _validate_complete_committed_switch(
    store: Store,
    backup_dir: Path,
    manifest: Mapping[str, object],
    journal: Mapping[str, object],
    adapter: FilesystemAdapter,
) -> None:
    backup_id = backup_dir.name
    if (
        manifest.get("schema_version") != 2
        or manifest.get("id") != backup_id
        or manifest.get("operation") != "switch"
        or manifest.get("lifecycle") != "committed"
        or journal.get("schema_version") != 1
        or journal.get("state") != "committed"
    ):
        raise SwitchError(
            f"Committed switch {backup_id} terminal identity is invalid"
        )

    allowed_roots, exact_targets = _transaction_destination_authority(store)
    backups_root = store.backups_dir.resolve()

    def require_destination(
        raw_path: object,
        *,
        label: str,
        extra_targets: set[Path] | None = None,
    ) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise SwitchError(
                f"Committed switch {backup_id} {label} has no path"
            )
        destination = _canonical_destination(Path(raw_path))
        if destination == backups_root or _is_within(destination, backups_root):
            raise SwitchError(
                f"Committed switch {backup_id} {label} is not approved: "
                f"{raw_path}"
            )
        normally_allowed = destination in exact_targets or any(
            destination == root or _is_within(destination, root)
            for root in allowed_roots
        )
        if not normally_allowed and (
            extra_targets is None or destination not in extra_targets
        ):
            raise SwitchError(
                f"Committed switch {backup_id} {label} is not approved: "
                f"{raw_path}"
            )
        return destination

    def validate_identity(
        raw_identity: object,
        path: Path,
        *,
        label: str,
    ) -> dict[str, object]:
        if not isinstance(raw_identity, dict):
            raise SwitchError(f"{label} has no node identity: {path}")
        identity = dict(raw_identity)
        kind = identity.get("kind")
        if kind not in {"missing", "file", "directory", "symlink"}:
            raise SwitchError(f"{label} has an invalid node kind: {path}")
        if identity.get("path") != str(path):
            raise SwitchError(f"{label} node path is inconsistent: {path}")
        if kind != "missing" and (
            type(identity.get("device")) is not int
            or type(identity.get("inode")) is not int
        ):
            raise SwitchError(f"{label} has an invalid node inode: {path}")
        if kind != "missing":
            _validated_recorded_mode(
                identity,
                path,
                label=label,
                required=True,
            )
        if kind == "symlink" and not isinstance(
            identity.get("symlink_target"), str
        ):
            raise SwitchError(f"{label} has an invalid symlink target: {path}")
        return identity

    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SwitchError(
            f"Committed switch {backup_id} has no complete backup entries"
        )
    entries: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    entry_records: dict[str, dict[str, object]] = {}
    seen_destinations: set[Path] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise SwitchError(
                f"Committed switch {backup_id} entry {index} is invalid"
            )
        raw_path = raw_entry.get("path")
        destination = require_destination(raw_path, label="entry path")
        if destination in seen_destinations:
            raise SwitchError(
                f"Committed switch {backup_id} has duplicate destinations"
            )
        seen_destinations.add(destination)
        before_state = _validate_attested_path_state(
            raw_entry.get("before_state"),
            Path(str(raw_path)),
            label=f"Committed switch {backup_id} entry",
        )
        committed_after = _validate_attested_path_state(
            raw_entry.get("committed_after_state"),
            Path(str(raw_path)),
            label=f"Committed switch {backup_id} committed entry",
        )
        payload = raw_entry.get("payload")
        if before_state.get("kind") in {"file", "directory"}:
            if not isinstance(payload, str) or not payload:
                raise SwitchError(
                    f"Committed switch {backup_id} backup payload is missing"
                )
            payload_path = _contained_payload_path(backup_dir, payload)
            payload_state = adapter.capture_state(payload_path)
            if not isinstance(payload_state, dict) or not _states_match(
                payload_state,
                before_state,
            ):
                raise SwitchError(
                    f"Committed switch {backup_id} backup payload is corrupt: "
                    f"{payload}"
                )
        elif payload is not None:
            raise SwitchError(
                f"Committed switch {backup_id} has an unexpected payload"
            )
        entries[str(raw_path)] = (before_state, committed_after)
        entry_records[str(raw_path)] = dict(raw_entry)

    created_parent_authority = _switch_created_parent_authority(
        backup_dir,
        entry_records,
    )

    raw_effects = journal.get("effects")
    if not isinstance(raw_effects, list) or not raw_effects:
        raise SwitchError(
            f"Committed switch {backup_id} has no complete effect chain"
    )
    last_observed_by_path: dict[str, dict[str, object]] = {}
    observed_created_parents: set[Path] = set()
    finalize_count = 0
    for effect_index, raw_effect in enumerate(raw_effects):
        if (
            not isinstance(raw_effect, dict)
            or raw_effect.get("id") != effect_index
            or raw_effect.get("kind")
            not in {"filesystem", "desktop", "finalize"}
            or raw_effect.get("status") != "applied"
            or not isinstance(raw_effect.get("phase"), str)
            or not raw_effect.get("phase")
        ):
            raise SwitchError(
                f"Committed switch {backup_id} effect {effect_index} is incomplete"
            )
        kind = raw_effect.get("kind")
        if kind == "finalize":
            finalize_count += 1
            if (
                effect_index != len(raw_effects) - 1
                or raw_effect.get("phase") != "backup_finalize"
                or raw_effect.get("before_state")
                != {"lifecycle": "prepared"}
                or raw_effect.get("planned_after_state")
                != {"lifecycle": "committed"}
                or raw_effect.get("observed_after_state")
                != {"lifecycle": "committed"}
            ):
                raise SwitchError(
                    f"Committed switch {backup_id} finalize effect is invalid"
                )
            continue

        if kind == "desktop":
            before_state = raw_effect.get("before_state")
            planned_after = raw_effect.get("planned_after_state")
            observed_after = raw_effect.get("observed_after_state")
            if (
                not isinstance(before_state, dict)
                or not isinstance(planned_after, dict)
                or not isinstance(observed_after, dict)
                or not _switch_effect_states_match(
                    observed_after,
                    planned_after,
                )
            ):
                raise SwitchError(
                    f"Committed switch {backup_id} desktop effect is incomplete"
                )
            continue

        raw_path = raw_effect.get("path")
        path = Path(str(raw_path))
        effect_destination = require_destination(
            raw_path,
            label="effect path",
            extra_targets=(
                created_parent_authority
                if raw_effect.get("phase") == "target_home_ensure"
                else None
            ),
        )
        route_guard = raw_effect.get("route_guard")
        if (
            not isinstance(route_guard, dict)
            or route_guard.get("schema_version") != 1
            or route_guard.get("path") != str(raw_path)
            or not isinstance(route_guard.get("canonical_parent"), str)
            or not isinstance(route_guard.get("canonical_components"), list)
            or not isinstance(route_guard.get("components"), list)
        ):
            raise SwitchError(
                f"Committed switch {backup_id} effect has no route identity: "
                f"{raw_path}"
            )
        before_state = _validate_attested_path_state(
            raw_effect.get("before_state"),
            path,
            label=f"Committed switch {backup_id} effect",
        )
        if effect_destination in created_parent_authority:
            if (
                raw_effect.get("phase") != "target_home_ensure"
                or before_state.get("kind") != "missing"
            ):
                raise SwitchError(
                    f"Committed switch {backup_id} created-parent effect is "
                    f"invalid: {raw_path}"
                )
            observed_created_parents.add(effect_destination)
        planned_after = _validate_attested_path_state(
            raw_effect.get("planned_after_state"),
            path,
            label=f"Committed switch {backup_id} planned effect",
        )
        observed_after = _validate_attested_path_state(
            raw_effect.get("observed_after_state"),
            path,
            label=f"Committed switch {backup_id} observed effect",
        )
        if not _switch_effect_states_match(observed_after, planned_after):
            raise SwitchError(
                f"Committed switch {backup_id} effect state is incomplete: "
                f"{raw_path}"
            )
        previous = last_observed_by_path.get(str(raw_path))
        entry = entries.get(str(raw_path))
        if previous is not None and not _states_match(before_state, previous):
            raise SwitchError(
                f"Committed switch {backup_id} effect chain is discontinuous: "
                f"{raw_path}"
            )
        if previous is None and entry is not None and not _states_match(
            before_state,
            entry[0],
        ):
            raise SwitchError(
                f"Committed switch {backup_id} effect predecessor is unbound: "
                f"{raw_path}"
            )
        validate_identity(
            raw_effect.get("before_identity"),
            path,
            label=f"Committed switch {backup_id} predecessor",
        )
        produced_identity = validate_identity(
            raw_effect.get("produced_identity"),
            path,
            label=f"Committed switch {backup_id} result",
        )
        staged_values = (
            raw_effect.get("staged_path"),
            raw_effect.get("staged_state"),
            raw_effect.get("staged_identity"),
            raw_effect.get("staged_route_guard"),
        )
        if any(value is not None for value in staged_values) and not all(
            value is not None for value in staged_values
        ):
            raise SwitchError(
                f"Committed switch {backup_id} staged identity is incomplete: "
                f"{raw_path}"
            )
        if all(value is not None for value in staged_values):
            staged_path, staged_state, staged_identity, staged_route_guard = (
                staged_values
            )
            if (
                not isinstance(staged_path, str)
                or not isinstance(staged_state, dict)
                or not isinstance(staged_identity, dict)
                or not isinstance(staged_route_guard, dict)
            ):
                raise SwitchError(
                    f"Committed switch {backup_id} staged identity is invalid: "
                    f"{raw_path}"
                )
            try:
                staged_relative = Path(staged_path).relative_to(backup_dir)
            except ValueError as exc:
                raise SwitchError(
                    f"Committed switch {backup_id} staged path escapes backup: "
                    f"{staged_path}"
                ) from exc
            contained_stage = _contained_payload_path(
                backup_dir,
                str(staged_relative),
            )
            if contained_stage != Path(staged_path).resolve():
                raise SwitchError(
                    f"Committed switch {backup_id} staged path is invalid: "
                    f"{staged_path}"
                )
            validated_staged_state = _validate_attested_path_state(
                staged_state,
                Path(staged_path),
                label=f"Committed switch {backup_id} staged effect",
            )
            validated_staged_identity = validate_identity(
                staged_identity,
                Path(staged_path),
                label=f"Committed switch {backup_id} staged effect",
            )
            _validate_route_guard(Path(staged_path), staged_route_guard)
            if (
                not _states_match(
                    adapter.capture_state(Path(staged_path)),
                    validated_staged_state,
                )
                or adapter.capture_identity(Path(staged_path))
                != validated_staged_identity
                or not _node_identities_match(
                    produced_identity,
                    validated_staged_identity,
                )
            ):
                raise SwitchError(
                    f"Committed switch {backup_id} staged artifact is not "
                    f"authoritative: {raw_path}"
                )
        last_observed_by_path[str(raw_path)] = observed_after

    if finalize_count != 1:
        raise SwitchError(
            f"Committed switch {backup_id} has no unique finalize effect"
        )
    if observed_created_parents != created_parent_authority:
        raise SwitchError(
            f"Committed switch {backup_id} effect chain does not cover its "
            "created-parent authority"
        )
    for raw_path, (before_state, committed_after) in entries.items():
        last_observed = last_observed_by_path.get(raw_path)
        expected_after = last_observed if last_observed is not None else before_state
        if not _states_match(committed_after, expected_after):
            raise SwitchError(
                f"Committed switch {backup_id} entry is not covered by its "
                f"effect chain: {raw_path}"
            )


def _validate_complete_committed_restore(
    store: Store,
    safety_dir: Path,
    manifest: Mapping[str, object],
    journal: Mapping[str, object],
    adapter: FilesystemAdapter,
) -> None:
    backup_id = safety_dir.name
    if (
        manifest.get("schema_version") != 2
        or manifest.get("id") != backup_id
        or manifest.get("operation") != "restore"
        or manifest.get("lifecycle") != "committed"
        or journal.get("schema_version") != 1
        or journal.get("state") != "committed"
    ):
        raise SwitchError(
            f"Committed restore {backup_id} terminal identity is invalid"
        )
    allowed_roots, exact_targets = _frozen_restore_destination_authority(
        store,
        backup_id,
        journal,
    )
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SwitchError(
            f"Committed restore {backup_id} has no complete safety entries"
        )
    entries: list[dict[str, object]] = []
    destinations: list[Path] = []
    seen_destinations: set[Path] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise SwitchError(
                f"Committed restore {backup_id} entry {index} is invalid"
            )
        destination = _restore_recovery_destination(
            store,
            backup_id,
            raw_entry.get("path"),
            allowed_roots,
            exact_targets,
        )
        if destination in seen_destinations:
            raise SwitchError(
                f"Committed restore {backup_id} has duplicate destinations"
            )
        seen_destinations.add(destination)
        before_state = _validate_attested_path_state(
            raw_entry.get("before_state"),
            destination,
            label=f"Committed restore {backup_id} entry",
        )
        _validate_attested_path_state(
            raw_entry.get("committed_after_state"),
            destination,
            label=f"Committed restore {backup_id} committed entry",
        )
        payload = raw_entry.get("payload")
        if before_state.get("kind") in {"file", "directory"}:
            if not isinstance(payload, str) or not payload:
                raise SwitchError(
                    f"Committed restore {backup_id} safety payload is missing"
                )
            payload_path = _contained_payload_path(safety_dir, payload)
            payload_state = adapter.capture_state(payload_path)
            if not isinstance(payload_state, dict) or not _states_match(
                payload_state,
                before_state,
            ):
                raise SwitchError(
                    f"Committed restore {backup_id} safety payload is corrupt: "
                    f"{payload}"
                )
        elif payload is not None:
            raise SwitchError(
                f"Committed restore {backup_id} has an unexpected payload"
            )
        entries.append(dict(raw_entry))
        destinations.append(destination)

    raw_effects = journal.get("effects")
    if not isinstance(raw_effects, list) or not raw_effects:
        raise SwitchError(
            f"Committed restore {backup_id} has no complete effect chain"
        )
    cleanup_paths = tuple(
        _restore_recovery_destination(
            store,
            backup_id,
            raw_effect.get("path"),
            allowed_roots,
            exact_targets,
        )
        for raw_effect in raw_effects
        if isinstance(raw_effect, dict)
        and raw_effect.get("phase") == "restore_parent_cleanup"
    )
    target_indices: set[int] = set()
    for effect_index, raw_effect in enumerate(raw_effects):
        if (
            not isinstance(raw_effect, dict)
            or raw_effect.get("id") != effect_index
            or raw_effect.get("kind") != "filesystem"
            or raw_effect.get("status") != "applied"
            or raw_effect.get("recovery_state") != "committed"
        ):
            raise SwitchError(
                f"Committed restore {backup_id} effect {effect_index} is incomplete"
            )
        if raw_effect.get("phase") == "restore_parent_cleanup":
            parent = _restore_recovery_destination(
                store,
                backup_id,
                raw_effect.get("path"),
                allowed_roots,
                exact_targets,
            )
            before_state = _validate_attested_path_state(
                raw_effect.get("before_state"),
                parent,
                label=f"Committed restore {backup_id} parent cleanup",
            )
            planned_after = _validate_attested_path_state(
                raw_effect.get("planned_after_state"),
                parent,
                label=f"Committed restore {backup_id} planned parent cleanup",
            )
            observed_after = _validate_attested_path_state(
                raw_effect.get("observed_after_state"),
                parent,
                label=f"Committed restore {backup_id} observed parent cleanup",
            )
            before_identity = raw_effect.get("before_identity")
            cleanup_route_guard = raw_effect.get("route_guard")
            if (
                before_state.get("kind") != "directory"
                or before_state.get("entry_count") != 0
                or planned_after.get("kind") != "missing"
                or observed_after.get("kind") != "missing"
                or not isinstance(before_identity, dict)
                or before_identity.get("kind") != "directory"
                or before_identity.get("path") != str(parent)
                or type(before_identity.get("device")) is not int
                or type(before_identity.get("inode")) is not int
                or before_identity.get("mode") != before_state.get("mode")
                or not isinstance(cleanup_route_guard, dict)
                or adapter.capture_state(parent).get("kind") != "missing"
            ):
                raise SwitchError(
                    f"Committed restore {backup_id} parent cleanup is incomplete"
                )
            if not _restore_route_was_removed_by_cleanup(
                parent,
                tuple(path for path in cleanup_paths if path != parent),
                adapter,
            ):
                _validate_route_guard(parent, cleanup_route_guard)
            continue

        entry_index = raw_effect.get("entry_index")
        if (
            type(entry_index) is not int
            or entry_index < 0
            or entry_index >= len(entries)
            or entry_index in target_indices
        ):
            raise SwitchError(
                f"Committed restore {backup_id} target effect index is invalid"
            )
        destination = _restore_recovery_destination(
            store,
            backup_id,
            raw_effect.get("path"),
            allowed_roots,
            exact_targets,
        )
        if destination != destinations[entry_index]:
            raise SwitchError(
                f"Committed restore {backup_id} target effect path is inconsistent"
            )
        before_state = _validate_attested_path_state(
            raw_effect.get("before_state"),
            destination,
            label=f"Committed restore {backup_id} target effect",
        )
        planned_after = _validate_attested_path_state(
            raw_effect.get("planned_after_state"),
            destination,
            label=f"Committed restore {backup_id} planned target effect",
        )
        observed_after = _validate_attested_path_state(
            raw_effect.get("observed_after_state"),
            destination,
            label=f"Committed restore {backup_id} observed target effect",
        )
        entry_before = entries[entry_index].get("before_state")
        entry_after = entries[entry_index].get("committed_after_state")
        current = adapter.capture_state(destination)
        current_identity = adapter.capture_identity(destination)
        route_path = raw_effect.get("route_path")
        route_guard = raw_effect.get("route_guard")
        before_identity = raw_effect.get("before_identity")
        produced_identity = raw_effect.get("produced_identity")
        created_parent_identities = raw_effect.get(
            "created_parent_identities",
            [],
        )
        if (
            not isinstance(entry_before, dict)
            or not isinstance(entry_after, dict)
            or not _states_match(before_state, entry_before)
            or not _states_match(planned_after, entry_after)
            or not _states_match(observed_after, planned_after)
            or not isinstance(current, dict)
            or not _states_match(current, observed_after)
            or not isinstance(route_path, str)
            or not isinstance(route_guard, dict)
            or not isinstance(before_identity, dict)
            or before_identity != entries[entry_index].get("before_identity")
            or not isinstance(produced_identity, dict)
            or current_identity != produced_identity
            or not isinstance(created_parent_identities, list)
        ):
            raise SwitchError(
                f"Committed restore {backup_id} target effect is incomplete: "
                f"{destination}"
            )
        if not _restore_route_was_removed_by_cleanup(
            destination,
            cleanup_paths,
            adapter,
        ):
            _validate_restore_route_guard(
                Path(route_path),
                destination,
                route_guard,
                tuple(
                    identity
                    for identity in created_parent_identities
                    if isinstance(identity, dict)
                ),
            )
        staged_identity = raw_effect.get("staged_identity")
        if planned_after.get("kind") in {"file", "directory"} and (
            not isinstance(staged_identity, dict)
            or not _node_identities_match(
                produced_identity,
                staged_identity,
            )
        ):
            raise SwitchError(
                f"Committed restore {backup_id} did not install its stage: "
                f"{destination}"
            )
        target_indices.add(entry_index)
    if target_indices != set(range(len(entries))):
        raise SwitchError(
            f"Committed restore {backup_id} effect chain does not cover all entries"
        )
    if adapter.capture_state(safety_dir / "restore-stage").get("kind") != "missing":
        raise SwitchError(
            f"Committed restore {backup_id} retained its restore stage"
        )


def _preflight_prepared_restore_recovery(
    store: Store,
    safety_dir: Path,
    manifest: Mapping[str, object],
    adapter: FilesystemAdapter,
) -> tuple[
    list[dict[str, object]],
    tuple[dict[str, object], ...],
    tuple[tuple[dict[str, object], dict[str, object]], ...],
    tuple[tuple[Path, dict[str, object], dict[str, object]], ...],
]:
    backup_id = safety_dir.name
    if (
        manifest.get("schema_version") != 2
        or manifest.get("id") != backup_id
        or manifest.get("operation") != "restore"
        or manifest.get("lifecycle") != "prepared"
    ):
        raise SwitchError(f"Prepared restore {backup_id} identity is invalid")
    journal = manifest.get("restore_journal")
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or journal.get("state") != "prepared"
    ):
        raise SwitchError(f"Prepared restore {backup_id} journal is invalid")
    allowed_roots, exact_targets = _frozen_restore_destination_authority(
        store,
        backup_id,
        journal,
    )
    raw_effects = journal.get("effects")
    if not isinstance(raw_effects, list):
        raise SwitchError(f"Prepared restore {backup_id} has no effect list")
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SwitchError(f"Prepared restore {backup_id} has no safety entries")
    entries: list[dict[str, object]] = []
    seen_paths: set[Path] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise SwitchError(
                f"Prepared restore {backup_id} entry {index} is invalid"
            )
        destination = _restore_recovery_destination(
            store,
            backup_id,
            raw_entry.get("path"),
            allowed_roots,
            exact_targets,
        )
        if destination in seen_paths:
            raise SwitchError(
                f"Prepared restore {backup_id} has duplicate destinations"
            )
        seen_paths.add(destination)
        before_state = _validate_attested_path_state(
            raw_entry.get("before_state"),
            destination,
            label=f"Prepared restore {backup_id} entry",
        )
        _validate_attested_path_state(
            raw_entry.get("committed_after_state"),
            destination,
            label=f"Prepared restore {backup_id} committed entry",
        )
        payload = raw_entry.get("payload")
        if before_state.get("kind") in {"file", "directory"}:
            if not isinstance(payload, str) or not payload:
                raise SwitchError(
                    f"Prepared restore {backup_id} safety payload is missing: "
                    f"{destination}"
                )
            payload_path = _contained_payload_path(safety_dir, payload)
            payload_artifact = _safety_payload_artifact(
                safety_dir,
                raw_entry,
            )
            if payload_artifact is None:
                raise SwitchError(
                    f"Prepared restore {backup_id} safety payload is unattested"
                )
            expected_payload_identity = payload_artifact["staged_identity"]
            payload_route_guard = payload_artifact["staged_route_guard"]
            payload_state = adapter.capture_state(payload_path)
            payload_identity = adapter.capture_identity(payload_path)
            payload_is_attested = (
                isinstance(payload_state, dict)
                and _states_match(payload_state, before_state)
                and isinstance(expected_payload_identity, dict)
                and _node_identities_match(
                    payload_identity,
                    expected_payload_identity,
                )
            )
            recovery_consumed_payload = False
            if (
                not payload_is_attested
                and before_state.get("kind") == "directory"
                and payload_identity.get("kind") == "missing"
            ):
                recovery_candidates = [
                    raw_effect
                    for raw_effect in raw_effects
                    if isinstance(raw_effect, dict)
                    and raw_effect.get("phase") != "restore_parent_cleanup"
                    and raw_effect.get("entry_index") == index
                ]
                if len(recovery_candidates) == 1:
                    recovery_effect = recovery_candidates[0]
                    recovery_observed = recovery_effect.get(
                        "recovery_observed_state"
                    )
                    recovery_identity = recovery_effect.get(
                        "recovery_produced_identity"
                    )
                    current = adapter.capture_state(destination)
                    current_identity = adapter.capture_identity(destination)
                    recovery_consumed_payload = (
                        recovery_effect.get("recovery_status")
                        in {"intent", "applied"}
                        and isinstance(recovery_observed, dict)
                        and _states_match(recovery_observed, before_state)
                        and isinstance(recovery_identity, dict)
                        and current_identity == recovery_identity
                        and _states_match(current, recovery_observed)
                        and isinstance(expected_payload_identity, dict)
                        and _node_identities_match(
                            recovery_identity,
                            expected_payload_identity,
                        )
                    )
            if not payload_is_attested and not recovery_consumed_payload:
                raise SwitchError(
                    f"Prepared restore {backup_id} safety payload is missing or "
                    f"corrupt: {payload}"
                )
            if not isinstance(payload_route_guard, dict):
                raise SwitchError(
                    f"Prepared restore {backup_id} safety payload identity "
                    f"changed: {payload}"
                )
            _validate_route_guard(
                Path(str(payload_artifact["staged_path"])),
                payload_route_guard,
            )
        elif payload is not None:
            raise SwitchError(
                f"Prepared restore {backup_id} has an unexpected safety payload"
            )
        entries.append(dict(raw_entry))

    cleanup_paths = tuple(
        _restore_recovery_destination(
            store,
            backup_id,
            raw_effect.get("path"),
            allowed_roots,
            exact_targets,
        )
        for raw_effect in raw_effects
        if isinstance(raw_effect, dict)
        and raw_effect.get("phase") == "restore_parent_cleanup"
    )
    recovered_cleanup_route_identities = tuple(
        dict(recovery_identity)
        for raw_effect in raw_effects
        if isinstance(raw_effect, dict)
        and raw_effect.get("phase") == "restore_parent_cleanup"
        and isinstance(
            recovery_identity := raw_effect.get("recovery_produced_identity"),
            dict,
        )
        and isinstance(recovery_identity.get("path"), str)
        and adapter.capture_identity(
            Path(str(recovery_identity["path"]))
        )
        == recovery_identity
    )
    effects: list[dict[str, object]] = []
    restore_actions: list[tuple[dict[str, object], dict[str, object]]] = []
    created_parent_candidates: dict[Path, dict[str, object]] = {}
    created_parent_route_guards: dict[Path, dict[str, object]] = {}
    for effect_index, raw_effect in enumerate(raw_effects):
        if (
            not isinstance(raw_effect, dict)
            or raw_effect.get("id") != effect_index
            or raw_effect.get("status") not in {"intent", "applied"}
            or raw_effect.get("kind") != "filesystem"
        ):
            raise SwitchError(
                f"Prepared restore {backup_id} effect {effect_index} is invalid"
            )
        if raw_effect.get("phase") == "restore_parent_cleanup":
            destination = _restore_recovery_destination(
                store,
                backup_id,
                raw_effect.get("path"),
                allowed_roots,
                exact_targets,
            )
            before_state = _validate_attested_path_state(
                raw_effect.get("before_state"),
                destination,
                label=f"Prepared restore {backup_id} parent cleanup",
            )
            planned_after = _validate_attested_path_state(
                raw_effect.get("planned_after_state"),
                destination,
                label=f"Prepared restore {backup_id} planned parent cleanup",
            )
            if (
                before_state.get("kind") != "directory"
                or before_state.get("entry_count") != 0
                or planned_after.get("kind") != "missing"
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} parent cleanup state is invalid"
                )
            before_identity = raw_effect.get("before_identity")
            cleanup_route_guard = raw_effect.get("route_guard")
            if (
                not isinstance(before_identity, dict)
                or before_identity.get("kind") != "directory"
                or before_identity.get("path") != str(destination)
                or type(before_identity.get("device")) is not int
                or type(before_identity.get("inode")) is not int
                or before_identity.get("mode") != before_state.get("mode")
                or not isinstance(cleanup_route_guard, dict)
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} parent cleanup identity is invalid"
                )
            if not _restore_route_was_removed_by_cleanup(
                destination,
                tuple(path for path in cleanup_paths if path != destination),
                adapter,
            ):
                _validate_route_guard(destination, cleanup_route_guard)
            observed_after = raw_effect.get("observed_after_state")
            action_observed = raw_effect.get("action_observed_state")
            for label, candidate in (
                ("observed", observed_after),
                ("action-observed", action_observed),
            ):
                if candidate is None:
                    continue
                validated = _validate_attested_path_state(
                    candidate,
                    destination,
                    label=(
                        f"Prepared restore {backup_id} parent cleanup {label}"
                    ),
                )
                if not (
                    _states_match(validated, before_state)
                    or _states_match(validated, planned_after)
                ):
                    raise SwitchError(
                        f"Prepared restore {backup_id} parent cleanup {label} "
                        "state is inconsistent"
                    )
            recovery_status = raw_effect.get("recovery_status")
            if recovery_status not in {None, "intent", "applied"}:
                raise SwitchError(
                    f"Prepared restore {backup_id} parent cleanup recovery state "
                    "is invalid"
                )
            recovery_observed = raw_effect.get("recovery_observed_state")
            recovery_identity = raw_effect.get("recovery_produced_identity")
            if (recovery_observed is None) != (recovery_identity is None):
                raise SwitchError(
                    f"Prepared restore {backup_id} parent cleanup recovery "
                    "evidence is incomplete"
                )
            if recovery_observed is not None:
                recovery_observed = _validate_attested_path_state(
                    recovery_observed,
                    destination,
                    label=f"Prepared restore {backup_id} recovered parent",
                )
                if (
                    not _states_match(recovery_observed, before_state)
                    or recovery_observed.get("mode") != before_state.get("mode")
                    or not isinstance(recovery_identity, dict)
                    or recovery_identity.get("kind") != "directory"
                    or recovery_identity.get("path") != str(destination)
                    or type(recovery_identity.get("device")) is not int
                    or type(recovery_identity.get("inode")) is not int
                    or recovery_identity.get("mode") != before_state.get("mode")
                ):
                    raise SwitchError(
                        f"Prepared restore {backup_id} recovered parent identity "
                        "is invalid"
                    )
            current = adapter.capture_state(destination)
            current_identity = adapter.capture_identity(destination)
            current_matches_before = (
                isinstance(current, dict)
                and _states_match(current, before_state)
                and current.get("mode") == before_state.get("mode")
                and current.get("entry_count") == before_state.get("entry_count")
            )
            if current_matches_before:
                if current_identity == before_identity:
                    pass
                elif (
                    isinstance(recovery_identity, dict)
                    and current_identity == recovery_identity
                    and recovery_status in {"intent", "applied"}
                ):
                    if recovery_status != "applied":
                        restore_actions.append((dict(raw_effect), before_state))
                else:
                    raise SwitchError(
                        f"Prepared restore {backup_id} parent cleanup identity "
                        f"changed: {destination}"
                    )
            elif isinstance(current, dict) and (
                _states_match(current, planned_after)
                or (
                    isinstance(observed_after, dict)
                    and _states_match(current, observed_after)
                )
                or (
                    isinstance(action_observed, dict)
                    and _states_match(current, action_observed)
                )
            ):
                restore_actions.append((dict(raw_effect), before_state))
            else:
                raise SwitchError(
                    f"Prepared restore {backup_id} parent cleanup is ambiguous: "
                    f"{destination}"
                )
            effects.append(dict(raw_effect))
            continue
        entry_index = raw_effect.get("entry_index")
        if (
            type(entry_index) is not int
            or entry_index < 0
            or entry_index >= len(entries)
        ):
            raise SwitchError(
                f"Prepared restore {backup_id} effect has an invalid entry index"
            )
        destination = _restore_recovery_destination(
            store,
            backup_id,
            raw_effect.get("path"),
            allowed_roots,
            exact_targets,
        )
        if destination != _canonical_destination(Path(str(entries[entry_index]["path"]))):
            raise SwitchError(
                f"Prepared restore {backup_id} effect path is inconsistent"
            )
        before_state = _validate_attested_path_state(
            raw_effect.get("before_state"),
            destination,
            label=f"Prepared restore {backup_id} effect",
        )
        planned_after = _validate_attested_path_state(
            raw_effect.get("planned_after_state"),
            destination,
            label=f"Prepared restore {backup_id} planned effect",
        )
        observed_after = raw_effect.get("observed_after_state")
        if observed_after is not None:
            observed_after = _validate_attested_path_state(
                observed_after,
                destination,
                label=f"Prepared restore {backup_id} observed effect",
            )
            if not _states_match(observed_after, planned_after):
                raise SwitchError(
                    f"Prepared restore {backup_id} observed effect is inconsistent"
                )
        raw_planned_parents = raw_effect.get("planned_created_parent_paths", [])
        if not isinstance(raw_planned_parents, list):
            raise SwitchError(
                f"Prepared restore {backup_id} has an invalid planned-parent journal"
            )
        for raw_parent in raw_planned_parents:
            parent = _restore_recovery_destination(
                store,
                backup_id,
                raw_parent,
                allowed_roots,
                exact_targets,
            )
            if parent not in destination.parents:
                raise SwitchError(
                    f"Prepared restore {backup_id} parent is not an ancestor"
                )

        raw_created_parents = raw_effect.get("created_parent_paths", [])
        raw_parent_identities = raw_effect.get("created_parent_identities", [])
        if (
            not isinstance(raw_created_parents, list)
            or not isinstance(raw_parent_identities, list)
            or len(raw_created_parents) != len(raw_parent_identities)
        ):
            raise SwitchError(
                f"Prepared restore {backup_id} has an invalid created-parent "
                "identity journal"
            )
        for raw_parent, raw_identity in zip(
            raw_created_parents,
            raw_parent_identities,
        ):
            parent = _restore_recovery_destination(
                store,
                backup_id,
                raw_parent,
                allowed_roots,
                exact_targets,
            )
            if parent not in destination.parents or not isinstance(
                raw_identity,
                dict,
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} created-parent identity is "
                    "invalid"
                )
            if (
                raw_identity.get("kind") != "directory"
                or raw_identity.get("path") != str(parent)
                or type(raw_identity.get("device")) is not int
                or type(raw_identity.get("inode")) is not int
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} created-parent identity is "
                    f"invalid: {parent}"
                )
            persisted_identity = dict(raw_identity)
            existing_identity = created_parent_candidates.get(parent)
            if existing_identity is not None and existing_identity != persisted_identity:
                raise SwitchError(
                    f"Prepared restore {backup_id} created-parent identity "
                    f"conflicts: {parent}"
                )
            current_identity = adapter.capture_identity(parent)
            if current_identity.get("kind") != "missing" and current_identity != persisted_identity:
                raise SwitchError(
                    f"Prepared restore {backup_id} created-parent identity changed: "
                    f"{parent}"
                )
            created_parent_candidates[parent] = persisted_identity
            created_parent_route_guards[parent] = _capture_route_guard(parent)
        raw_route_path = raw_effect.get("route_path")
        route_guard = raw_effect.get("route_guard")
        before_identity = raw_effect.get("before_identity")
        if (
            not isinstance(raw_route_path, str)
            or not isinstance(route_guard, dict)
            or not isinstance(before_identity, dict)
            or before_identity.get("path") != str(destination)
            or before_identity.get("kind") != before_state.get("kind")
        ):
            raise SwitchError(
                f"Prepared restore {backup_id} target route or identity is invalid"
            )
        entry_route_path = entries[entry_index].get("route_path")
        entry_route_guard = entries[entry_index].get("route_guard")
        entry_before_identity = entries[entry_index].get("before_identity")
        if (
            entry_route_path != raw_route_path
            or not isinstance(entry_route_guard, dict)
            or _immutable_state_bytes(entry_route_guard)
            != _immutable_state_bytes(route_guard)
            or entry_before_identity != before_identity
        ):
            raise SwitchError(
                f"Prepared restore {backup_id} target evidence is inconsistent"
            )
        staged_path = raw_effect.get("staged_path")
        staged_state = raw_effect.get("staged_state")
        staged_identity = raw_effect.get("staged_identity")
        staged_route_guard = raw_effect.get("staged_route_guard")
        staged_values = (
            staged_path,
            staged_state,
            staged_identity,
            staged_route_guard,
        )
        if any(value is not None for value in staged_values):
            if (
                not isinstance(staged_path, str)
                or not isinstance(staged_state, dict)
                or not isinstance(staged_identity, dict)
                or not isinstance(staged_route_guard, dict)
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} staged identity is incomplete"
                )
            stage = Path(staged_path)
            try:
                relative_stage = stage.relative_to(safety_dir)
            except ValueError as exc:
                raise SwitchError(
                    f"Prepared restore {backup_id} staged path escapes safety "
                    "backup"
                ) from exc
            if _contained_payload_path(
                safety_dir,
                str(relative_stage),
            ) != stage.resolve():
                raise SwitchError(
                    f"Prepared restore {backup_id} staged path is invalid"
                )
        recovery_status = raw_effect.get("recovery_status")
        if recovery_status not in {None, "intent", "applied"}:
            raise SwitchError(
                f"Prepared restore {backup_id} target recovery state is invalid"
            )
        recovery_observed = raw_effect.get("recovery_observed_state")
        recovery_identity = raw_effect.get("recovery_produced_identity")
        if (recovery_observed is None) != (recovery_identity is None):
            raise SwitchError(
                f"Prepared restore {backup_id} target recovery evidence is "
                "incomplete"
            )
        if recovery_observed is not None:
            recovery_observed = _validate_attested_path_state(
                recovery_observed,
                destination,
                label=f"Prepared restore {backup_id} recovered target",
            )
            if (
                recovery_status is None
                or not _states_match(recovery_observed, before_state)
                or not isinstance(recovery_identity, dict)
                or recovery_identity.get("kind") != before_state.get("kind")
                or recovery_identity.get("path") != str(destination)
                or (
                    before_state.get("kind") != "missing"
                    and (
                        type(recovery_identity.get("device")) is not int
                        or type(recovery_identity.get("inode")) is not int
                        or recovery_identity.get("mode")
                        != before_state.get("mode")
                    )
                )
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} recovered target identity is "
                    f"invalid: {destination}"
                )
        elif recovery_status == "applied":
            raise SwitchError(
                f"Prepared restore {backup_id} applied target recovery has no "
                "evidence"
            )
        current = adapter.capture_state(destination)
        current_identity = adapter.capture_identity(destination)
        if not isinstance(current, dict):
            raise SwitchError(
                f"Prepared restore {backup_id} current state is invalid"
            )
        current_is_original = (
            _states_match(current, before_state)
            and current_identity == before_identity
        )
        if current_is_original:
            pass
        elif (
            isinstance(recovery_observed, dict)
            and isinstance(recovery_identity, dict)
            and _states_match(current, recovery_observed)
            and current_identity == recovery_identity
        ):
            if recovery_status != "applied":
                restore_actions.append((dict(raw_effect), before_state))
        elif _states_match(current, planned_after) or (
            isinstance(observed_after, dict)
            and _states_match(current, observed_after)
        ):
            produced_identity = raw_effect.get("produced_identity")
            if (
                not isinstance(produced_identity, dict)
                or current_identity != produced_identity
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} produced identity changed: "
                    f"{destination}"
                )
            if (
                isinstance(staged_identity, dict)
                and planned_after.get("kind") in {"file", "directory"}
                and not _node_identities_match(
                    produced_identity,
                    staged_identity,
                )
            ):
                raise SwitchError(
                    f"Prepared restore {backup_id} did not install its stage: "
                    f"{destination}"
                )
            if not _restore_route_was_removed_by_cleanup(
                destination,
                cleanup_paths,
                adapter,
            ):
                _validate_restore_route_guard(
                    Path(raw_route_path),
                    destination,
                    route_guard,
                tuple(
                    identity
                    for identity in raw_parent_identities
                    if isinstance(identity, dict)
                )
                + tuple(
                    identity
                    for identity in recovered_cleanup_route_identities
                    if Path(str(identity["path"])) in destination.parents
                ),
            )
            restore_actions.append((dict(raw_effect), before_state))
        else:
            raise SwitchError(
                f"Prepared restore {backup_id} has ambiguous state for "
                f"{destination}"
            )
        effects.append(dict(raw_effect))
    return (
        entries,
        tuple(effects),
        tuple(restore_actions),
        tuple(
            sorted(
                (
                    (
                        parent,
                        identity,
                        created_parent_route_guards[parent],
                    )
                    for parent, identity in created_parent_candidates.items()
                ),
                key=lambda item: len(item[0].parts),
                reverse=True,
            )
        ),
    )


def _recover_prepared_restore(
    store: Store,
    safety_dir: Path,
    manifest: Mapping[str, object],
    adapter: FilesystemAdapter,
    *,
    phase_prefix: str = "restore_recovery",
    terminal_phase: str = "restore_recovery_finalize",
    error: str | None = None,
) -> None:
    entries, effects, restore_actions, created_parents = (
        _preflight_prepared_restore_recovery(
            store,
            safety_dir,
            manifest,
            adapter,
        )
    )
    recovery_manifest = _canonical_json_object(dict(manifest))
    recovery_journal = recovery_manifest.get("restore_journal")
    if not isinstance(recovery_journal, dict):
        raise SwitchError(
            f"Prepared restore {safety_dir.name} has no recovery journal"
        )
    recovery_effects = recovery_journal.get("effects")
    if not isinstance(recovery_effects, list):
        raise SwitchError(
            f"Prepared restore {safety_dir.name} has no recovery effect list"
        )

    def persisted_effect(effect: Mapping[str, object]) -> dict[str, object]:
        effect_id = effect.get("id")
        if (
            type(effect_id) is not int
            or effect_id < 0
            or effect_id >= len(recovery_effects)
            or not isinstance(recovery_effects[effect_id], dict)
        ):
            raise SwitchError(
                f"Prepared restore {safety_dir.name} recovery effect is invalid"
            )
        return recovery_effects[effect_id]

    def persist_recovery(phase: str) -> None:
        adapter.write_manifest(
            safety_dir / "backup.json",
            recovery_manifest,
            phase=phase,
        )

    reverse_actions = tuple(reversed(restore_actions))
    active_actions = tuple(
        action
        for action in reverse_actions
        if action[0].get("path") == str(store.active_path)
    )
    ordinary_actions = tuple(
        action
        for action in reverse_actions
        if action[0].get("path") != str(store.active_path)
    )
    ordered_actions = ordinary_actions + active_actions
    recovered_cleanup_parent_identities: dict[Path, dict[str, object]] = {}
    for effect in effects:
        if effect.get("phase") != "restore_parent_cleanup":
            continue
        raw_parent = effect.get("path")
        recovery_identity = effect.get("recovery_produced_identity")
        if not isinstance(raw_parent, str) or not isinstance(
            recovery_identity,
            dict,
        ):
            continue
        parent = _canonical_destination(Path(raw_parent))
        if adapter.capture_identity(parent) == recovery_identity:
            recovered_cleanup_parent_identities[parent] = dict(
                recovery_identity
            )
    for recovery_index, (effect, before_state) in enumerate(ordered_actions):
        if effect.get("phase") == "restore_parent_cleanup":
            raw_path = effect.get("path")
            mode = before_state.get("mode")
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or type(mode) is not int
            ):
                raise SwitchError(
                    f"Prepared restore {safety_dir.name} parent recovery is invalid"
                )
            parent = _canonical_destination(Path(raw_path))
            journal_effect = persisted_effect(effect)
            current = adapter.capture_state(parent)
            if not isinstance(current, dict):
                raise SwitchError(
                    f"Prepared restore {safety_dir.name} parent recovery state "
                    "is invalid"
                )
            if current.get("kind") == "missing":
                journal_effect["recovery_status"] = "intent"
                persist_recovery("restore_recovery_journal_intent")
                cleanup_route_guard = effect.get("route_guard")
                if not isinstance(cleanup_route_guard, dict):
                    raise SwitchError(
                        f"Prepared restore {safety_dir.name} parent recovery "
                        "has no route guard"
                    )
                cleanup_parent_descriptor = _open_guarded_parent(
                    parent,
                    cleanup_route_guard,
                )
                adapter.bind_switch_effect(
                    parent,
                    cleanup_parent_descriptor,
                )
                try:
                    try:
                        adapter.ensure_directory(
                            parent,
                            mode=mode,
                            phase=f"{phase_prefix}_parent_{recovery_index}",
                        )
                    finally:
                        adapter.unbind_switch_effect()
                        os.close(cleanup_parent_descriptor)
                finally:
                    recovery_observed = adapter.capture_state(parent)
                    recovery_identity = adapter.capture_identity(parent)
                    if isinstance(recovery_observed, dict):
                        journal_effect["recovery_observed_state"] = dict(
                            recovery_observed
                        )
                    if isinstance(recovery_identity, dict):
                        journal_effect["recovery_produced_identity"] = dict(
                            recovery_identity
                        )
                    persist_recovery("restore_recovery_journal_action_observed")
            observed_parent = adapter.capture_state(parent)
            if (
                not isinstance(observed_parent, dict)
                or not _states_match(observed_parent, before_state)
                or observed_parent.get("mode") != mode
                or observed_parent.get("entry_count")
                != before_state.get("entry_count")
            ):
                raise SwitchError(
                    f"Prepared restore {safety_dir.name} parent recovery state "
                    f"mismatch: {parent}"
                )
            _make_restore_effect_durable(
                adapter,
                parent,
                observed_parent,
                phase=f"{phase_prefix}_parent_{recovery_index}",
            )
            journal_effect["recovery_observed_state"] = dict(observed_parent)
            recovered_parent_identity = adapter.capture_identity(parent)
            journal_effect["recovery_produced_identity"] = dict(
                recovered_parent_identity
            )
            journal_effect["recovery_status"] = "applied"
            persist_recovery("restore_recovery_journal_applied")
            recovered_cleanup_parent_identities[parent] = dict(
                recovered_parent_identity
            )
            continue
        entry_index = effect.get("entry_index")
        if type(entry_index) is not int:
            raise SwitchError(
                f"Prepared restore {safety_dir.name} effect index is invalid"
            )
        entry = entries[entry_index]
        destination = _canonical_destination(Path(str(entry["path"])))
        raw_route_path = effect.get("route_path")
        route_guard = effect.get("route_guard")
        raw_created_identities = effect.get("created_parent_identities", [])
        if (
            not isinstance(raw_route_path, str)
            or not isinstance(route_guard, dict)
            or not isinstance(raw_created_identities, list)
        ):
            raise SwitchError(
                f"Prepared restore {safety_dir.name} recovery route is invalid"
            )
        recovery_phase = (
            "rollback"
            if phase_prefix == "rollback"
            else f"{phase_prefix}_{recovery_index}"
        )
        current_state = adapter.capture_state(destination)
        current_identity = adapter.capture_identity(destination)
        if not isinstance(current_state, dict) or not isinstance(
            current_identity,
            dict,
        ):
            raise SwitchError(
                f"Prepared restore {safety_dir.name} recovery predecessor is "
                "invalid"
            )
        journal_effect = persisted_effect(effect)
        recovery_observed = effect.get("recovery_observed_state")
        recovery_identity = effect.get("recovery_produced_identity")
        if (
            isinstance(recovery_observed, dict)
            and isinstance(recovery_identity, dict)
            and _states_match(current_state, recovery_observed)
            and current_identity == recovery_identity
        ):
            _make_restore_effect_durable(
                adapter,
                destination,
                current_state,
                phase=f"{phase_prefix}_{recovery_index}",
            )
            journal_effect["recovery_status"] = "applied"
            persist_recovery("restore_recovery_journal_applied")
            continue
        journal_effect["recovery_status"] = "intent"
        persist_recovery("restore_recovery_journal_intent")
        expected_route_identities = tuple(
            identity
            for identity in raw_created_identities
            if isinstance(identity, dict)
        ) + tuple(
            identity
            for parent, identity in recovered_cleanup_parent_identities.items()
            if parent in destination.parents
        )
        adapter.bind_restore_effect(
            destination,
            route_path=Path(raw_route_path),
            route_guard=route_guard,
            before_state=current_state,
            before_identity=current_identity,
            staged_artifact=_safety_payload_artifact(safety_dir, entry),
            expected_created_parent_identities=expected_route_identities,
        )
        observed: dict[str, object] | None = None
        recovered_identity: dict[str, object] | None = None
        try:
            try:
                adapter.materialize(
                    _safety_payload_path(safety_dir, entry),
                    destination,
                    before_state,
                    phase=recovery_phase,
                )
            finally:
                observed, recovered_identity, _ = adapter.bound_restore_result(
                    destination
                )
                if isinstance(observed, dict):
                    journal_effect["recovery_observed_state"] = dict(observed)
                if isinstance(recovered_identity, dict):
                    journal_effect["recovery_produced_identity"] = dict(
                        recovered_identity
                    )
                if isinstance(observed, dict) or isinstance(
                    recovered_identity,
                    dict,
                ):
                    persist_recovery(
                        "restore_recovery_journal_action_observed"
                    )
        finally:
            adapter.unbind_restore_effect()
        if (
            not isinstance(observed, dict)
            or not _states_match(observed, before_state)
            or not isinstance(recovered_identity, dict)
        ):
            raise SwitchError(
                f"Prepared restore {safety_dir.name} recovery state mismatch: "
                f"{destination}"
            )
        _make_restore_effect_durable(
            adapter,
            destination,
            observed,
            phase=f"{phase_prefix}_{recovery_index}",
        )
        journal_effect["recovery_status"] = "applied"
        persist_recovery("restore_recovery_journal_applied")

    for parent, persisted_identity, parent_route_guard in created_parents:
        current_identity = adapter.capture_identity(parent)
        if current_identity.get("kind") == "missing":
            continue
        if current_identity != persisted_identity:
            raise SwitchError(
                f"Prepared restore {safety_dir.name} created-parent identity "
                f"changed: {parent}"
            )
        current = adapter.capture_state(parent)
        if not isinstance(current, dict):
            raise SwitchError(
                f"Prepared restore {safety_dir.name} parent state is invalid"
            )
        if current.get("kind") != "directory" or current.get("entry_count") != 0:
            raise SwitchError(
                f"Prepared restore {safety_dir.name} created parent is ambiguous: "
                f"{parent}"
            )
        cleanup_parent_descriptor = _open_guarded_parent(
            parent,
            parent_route_guard,
        )
        adapter.bind_restore_parent_cleanup(
            parent,
            cleanup_parent_descriptor,
            persisted_identity,
        )
        try:
            adapter.remove_empty_dir(
                parent,
                phase=(
                    "rollback_parent_cleanup"
                    if phase_prefix == "rollback"
                    else f"{phase_prefix}_parent_cleanup"
                ),
            )
        finally:
            adapter.unbind_restore_parent_cleanup()
        adapter.sync_directory(
            parent.parent,
            phase=f"{phase_prefix}_parent_cleanup_parent",
        )
        if adapter.capture_state(parent).get("kind") != "missing":
            raise SwitchError(
                f"Prepared restore {safety_dir.name} parent cleanup failed: {parent}"
            )

    restore_stage = safety_dir / "restore-stage"
    if restore_stage.exists():
        adapter.remove_tree(
            restore_stage,
            phase=(
                "rollback_stage_cleanup"
                if phase_prefix == "rollback"
                else f"{phase_prefix}_stage_cleanup"
            ),
        )
        adapter.sync_directory(
            safety_dir,
            phase=f"{phase_prefix}_stage_cleanup_parent",
        )

    _write_safety_lifecycle(
        safety_dir,
        recovery_manifest,
        "rolled_back",
        adapter=adapter,
        phase=terminal_phase,
        error=error,
    )


def _filesystem_adapter(request: TransactionRequest) -> FilesystemAdapter:
    adapter = request.options.get("filesystem_adapter")
    if adapter is None:
        return FilesystemAdapter()
    for method_name in (
        "bind_restore_effect",
        "bind_restore_parent_cleanup",
        "bound_restore_result",
        "capture_state",
        "capture_identity",
        "copy_material",
        "durable_unlink",
        "ensure_directory",
        "materialize",
        "consume_created_parents",
        "remove_tree",
        "remove_empty_dir",
        "sync_directory",
        "sync_file",
        "sync_shared_entry",
        "sync_tree",
        "unbind_restore_effect",
        "unbind_restore_parent_cleanup",
        "write_manifest",
    ):
        if not callable(getattr(adapter, method_name, None)):
            raise SwitchError(
                f"Restore filesystem_adapter must provide {method_name}()"
            )
    return adapter  # type: ignore[return-value]


def _execute_restore_apply(
    store: Store,
    manifest: RestoreManifest,
    historical_backup_dir: Path,
    preview_lines: tuple[str, ...],
    destinations: tuple[Path, ...],
    parent_cleanup_plans: tuple[tuple[Path, ...], ...],
    adapter: FilesystemAdapter,
    initial_states: tuple[dict[str, object], ...],
    initial_identities: tuple[dict[str, object], ...],
    target_route_guards: tuple[dict[str, object], ...],
) -> TransactionReceipt:
    (
        safety_dir,
        safety_manifest,
        observed_states,
        staged_sources,
        staged_artifacts,
        pending_marker,
    ) = (
        _prepare_restore_safety_backup(
            store,
            historical_backup_dir,
            manifest,
            destinations,
            adapter,
            initial_states,
            initial_identities,
            target_route_guards,
            parent_cleanup_plans,
        )
    )
    safety_entries = safety_manifest["entries"]
    if not isinstance(safety_entries, list):
        raise SwitchError(f"Invalid prepared safety backup: {safety_dir.name}")
    restore_journal = _RestoreMutationJournal(
        adapter,
        safety_dir,
        safety_manifest,
    )

    if (
        len(manifest.entries) != len(destinations)
        or len(manifest.entries) != len(parent_cleanup_plans)
        or len(manifest.entries) != len(observed_states)
        or len(manifest.entries) != len(initial_identities)
        or len(manifest.entries) != len(target_route_guards)
        or len(manifest.entries) != len(staged_artifacts)
    ):
        raise SwitchError(
            f"Prepared safety backup state count does not match restore plan; "
            f"safety backup: {safety_dir.name}"
        )
    for entry, destination, observed in zip(
        manifest.entries,
        destinations,
        observed_states,
    ):
        if not _states_match(adapter.capture_state(destination), observed):
            raise SwitchError(
                f"Restore target changed during staging: {entry.path}; "
                f"safety backup: {safety_dir.name}"
            )

    applied: list[int] = []
    marker_cleanup_warning: str | None = None
    try:
        for index in range(len(manifest.entries)):
            entry = manifest.entries[index]
            if not _states_match(
                adapter.capture_state(destinations[index]),
                initial_states[index],
            ):
                raise SwitchError(
                    f"Restore target changed immediately before apply: {entry.path}"
                )
            planned_created_parents = _missing_parent_paths(destinations[index])
            effect = restore_journal.begin_target(
                entry_index=index,
                path=destinations[index],
                route_path=Path(entry.path).expanduser(),
                route_guard=target_route_guards[index],
                before_state=initial_states[index],
                before_identity=initial_identities[index],
                planned_after_state=(
                    safety_entries[index]["committed_after_state"]
                    if isinstance(safety_entries[index], dict)
                    and isinstance(
                        safety_entries[index].get("committed_after_state"),
                        dict,
                    )
                    else _capture_state_at_path(
                        entry.before_state,
                        destinations[index],
                    )
                ),
                staged_artifact=staged_artifacts[index],
                planned_created_parents=planned_created_parents,
            )
            applied.append(index)
            action_phase = "apply"
            adapter.consume_created_parents(phase=action_phase)
            created_parents: tuple[Path, ...] = tuple()
            action_observed: dict[str, object] | None = None
            produced_identity: dict[str, object] | None = None
            adapter.bind_restore_effect(
                destinations[index],
                route_path=Path(entry.path).expanduser(),
                route_guard=target_route_guards[index],
                before_state=initial_states[index],
                before_identity=initial_identities[index],
                staged_artifact=staged_artifacts[index],
            )
            try:
                adapter.materialize(
                    staged_sources[index],
                    destinations[index],
                    entry.before_state,
                    phase=action_phase,
                )
            finally:
                try:
                    (
                        action_observed,
                        produced_identity,
                        bound_created_parents,
                    ) = adapter.bound_restore_result(
                        destinations[index]
                    )
                    consumed_created_parents = adapter.consume_created_parents(
                        phase=action_phase
                    )
                    created_parents = (
                        bound_created_parents or consumed_created_parents
                    )
                    safety_entry = safety_entries[index]
                    if isinstance(safety_entry, dict):
                        safety_entry["apply_attempted"] = True
                        if created_parents:
                            safety_entry["created_parent_paths"] = [
                                str(path) for path in created_parents
                            ]
                    effect["created_parent_paths"] = [
                        str(path) for path in created_parents
                    ]
                    created_parent_identities: list[dict[str, object]] = []
                    for created_parent in created_parents:
                        identity = adapter.capture_identity(created_parent)
                        if identity.get("kind") != "directory":
                            raise SwitchError(
                                "Restore created parent identity is invalid: "
                                f"{created_parent}"
                            )
                        created_parent_identities.append(identity)
                    effect["created_parent_identities"] = (
                        created_parent_identities
                    )
                    if isinstance(action_observed, dict):
                        effect["action_observed_state"] = dict(action_observed)
                    if isinstance(produced_identity, dict):
                        effect["produced_identity"] = dict(produced_identity)
                    restore_journal.persist(
                        phase="restore_journal_action_observed"
                    )
                finally:
                    adapter.unbind_restore_effect()
            observed_after = adapter.capture_state(destinations[index])
            observed_identity = adapter.capture_identity(destinations[index])
            if not isinstance(observed_after, dict) or not _states_match(
                observed_after,
                entry.before_state,
            ) or not isinstance(produced_identity, dict) or (
                observed_identity != produced_identity
            ):
                raise SwitchError(f"Restore result does not match backup: {entry.path}")
            _make_restore_effect_durable(
                adapter,
                destinations[index],
                observed_after,
                phase=action_phase,
            )
            restore_journal.complete_target(
                effect,
                observed_after_state=observed_after,
                produced_identity=observed_identity,
                created_parents=created_parents,
            )
        cleanup_parents = {
            parent
            for cleanup_plan in parent_cleanup_plans
            for parent in cleanup_plan
        }
        for parent in sorted(
            cleanup_parents,
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            parent_route_guard = _capture_route_guard(parent)
            parent_before_state = adapter.capture_state(parent)
            parent_before_identity = adapter.capture_identity(parent)
            if (
                not isinstance(parent_before_state, dict)
                or parent_before_state.get("kind") != "directory"
                or parent_before_state.get("entry_count") != 0
                or parent_before_identity.get("kind") != "directory"
            ):
                raise SwitchError(
                    f"Restore created-parent cleanup is ambiguous: {parent}"
                )
            cleanup_effect = restore_journal.begin_parent_cleanup(
                path=parent,
                route_guard=parent_route_guard,
                before_state=parent_before_state,
                before_identity=parent_before_identity,
            )
            if (
                adapter.capture_state(parent) != parent_before_state
                or adapter.capture_identity(parent) != parent_before_identity
            ):
                raise SwitchError(
                    f"Restore created-parent cleanup changed before removal: "
                    f"{parent}"
                )
            cleanup_parent_descriptor = _open_guarded_parent(
                parent,
                parent_route_guard,
            )
            adapter.bind_restore_parent_cleanup(
                parent,
                cleanup_parent_descriptor,
                parent_before_identity,
            )
            try:
                try:
                    adapter.remove_empty_dir(
                        parent,
                        phase="restore_parent_cleanup",
                    )
                finally:
                    cleanup_observed = _path_state_at(
                        cleanup_parent_descriptor,
                        (parent.name,),
                        parent,
                    )
                    if not isinstance(cleanup_observed, dict):
                        raise SwitchError(
                            f"Restore parent cleanup state is invalid: {parent}"
                        )
                    restore_journal.observe_parent_cleanup(
                        cleanup_effect,
                        cleanup_observed,
                    )
            finally:
                adapter.unbind_restore_parent_cleanup()
            if cleanup_observed.get("kind") != "missing":
                raise SwitchError(
                    f"Restore created-parent cleanup did not remove: {parent}"
                )
            adapter.sync_directory(
                parent.parent,
                phase="restore_parent_cleanup_parent",
            )
            restore_journal.complete_parent_cleanup(
                cleanup_effect,
                cleanup_observed,
            )
        for entry, destination in zip(manifest.entries, destinations):
            if not _states_match(
                adapter.capture_state(destination),
                entry.before_state,
            ):
                raise SwitchError(
                    f"Restore target changed before commit: {entry.path}"
                )
        restore_stage = safety_dir / "restore-stage"
        if restore_stage.exists():
            adapter.remove_tree(restore_stage, phase="commit_stage_cleanup")
        _write_safety_lifecycle(
            safety_dir,
            safety_manifest,
            "committed",
            adapter=adapter,
            phase="committed_manifest",
        )
        if (
            _read_authoritative_committed_restore(
                store,
                safety_dir,
                pending_marker,
            )
            is None
        ):
            raise SwitchError(
                f"Restore terminal evidence is incomplete: {safety_dir.name}"
            )
        marker_cleanup_warning = _retire_pending_marker(
            adapter,
            pending_marker,
            outcome="committed",
        )
    except Exception as apply_error:
        authoritative_commit = _read_authoritative_committed_restore(
            store,
            safety_dir,
            pending_marker,
        )
        if authoritative_commit is not None:
            cleanup_warning = _retire_pending_marker(
                adapter,
                pending_marker,
                outcome="committed",
            )
            cleanup_lines = (
                (cleanup_warning,) if cleanup_warning is not None else tuple()
            )
            return TransactionReceipt(
                operation="restore",
                outcome="committed",
                preview_lines=preview_lines
                + (
                    "restore committed; terminal write reported an error: "
                    f"{apply_error}",
                )
                + cleanup_lines,
                backup_id=safety_dir.name,
            )
        try:
            recovery_manifest = read_json(safety_dir / "backup.json")
            if not isinstance(recovery_manifest, dict):
                raise SwitchError(
                    f"Prepared restore {safety_dir.name} manifest is invalid"
                )
            _recover_prepared_restore(
                store,
                safety_dir,
                recovery_manifest,
                adapter,
                phase_prefix="rollback",
                terminal_phase="rolled_back_manifest",
                error=str(apply_error),
            )
        except Exception as rollback_error:
            detail = f"rollback recovery: {rollback_error}"
            try:
                rollback_failed_manifest = read_json(safety_dir / "backup.json")
                if not isinstance(rollback_failed_manifest, dict):
                    raise SwitchError("restore manifest is not an object")
                _write_safety_lifecycle(
                    safety_dir,
                    rollback_failed_manifest,
                    "rollback_failed",
                    adapter=adapter,
                    phase="rollback_failed_manifest",
                    error=f"apply: {apply_error}; {detail}",
                )
            except Exception as record_error:
                detail = f"{detail}; receipt persistence: {record_error}"
            return TransactionReceipt(
                operation="restore",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (
                    f"restore failed: {apply_error}",
                    f"rollback failed: {detail}",
                ),
                backup_id=safety_dir.name,
            )
        cleanup_warning = _retire_pending_marker(
            adapter,
            pending_marker,
            outcome="rolled_back",
        )
        cleanup_lines = (
            (cleanup_warning,) if cleanup_warning is not None else tuple()
        )
        return TransactionReceipt(
            operation="restore",
            outcome="rolled_back",
            preview_lines=preview_lines
            + (f"restore failed: {apply_error}", "rollback completed")
            + cleanup_lines,
            backup_id=safety_dir.name,
        )

    completion_lines = (
        (marker_cleanup_warning,) if marker_cleanup_warning is not None else tuple()
    )
    return TransactionReceipt(
        operation="restore",
        outcome="committed",
        preview_lines=preview_lines + completion_lines,
        backup_id=safety_dir.name,
    )


def _capture_path_option(request: TransactionRequest, name: str) -> Path:
    value = request.options.get(name)
    if not isinstance(value, (str, os.PathLike)):
        raise SwitchError(f"Capture transaction requires {name}")
    return Path(value)


def _capture_string_option(request: TransactionRequest, name: str) -> str:
    value = request.options.get(name)
    if not isinstance(value, str):
        raise SwitchError(f"Capture transaction {name} option must be a string")
    return value


def _capture_bool_option(request: TransactionRequest, name: str) -> bool:
    value = request.options.get(name)
    if not isinstance(value, bool):
        raise SwitchError(f"Capture transaction {name} option must be boolean")
    return value


def _capture_filesystem_adapter(request: TransactionRequest) -> FilesystemAdapter:
    adapter = _filesystem_adapter(request)
    for method_name in (
        "bind_capture_workspace",
        "capture_directory_identity",
        "capture_workspace_bound",
        "create_store_root",
        "capture_parent_checkpoint",
        "chmod",
        "make_directory",
        "read_json",
        "read_text",
        "remove_file",
        "remove_path",
        "rename",
        "sync_directory",
        "sync_file",
        "sync_tree",
        "unbind_capture_workspace",
    ):
        if not callable(getattr(adapter, method_name, None)):
            raise SwitchError(
                f"Capture filesystem_adapter must provide {method_name}()"
            )
    return adapter


def _capture_artifact_paths(
    store: Store,
    profile: str,
) -> tuple[Path, Path, Path, Path]:
    profile_dir = store.profile_dir(profile)
    return (
        profile_dir,
        store.profiles_dir / f".{profile}.capture-stage",
        store.profiles_dir / f".{profile}.capture-previous",
        store.profiles_dir / f".{profile}.capture-journal.json",
    )


def _capture_artifact_exists(adapter: FilesystemAdapter, path: Path) -> bool:
    return adapter.capture_state(path).get("kind") != "missing"


def _capture_recovery_profiles(store: Store) -> tuple[str, ...]:
    try:
        profiles_info = store.profiles_dir.lstat()
    except FileNotFoundError:
        return tuple()
    if stat.S_ISLNK(profiles_info.st_mode) or not stat.S_ISDIR(
        profiles_info.st_mode
    ):
        raise SwitchError(
            f"Capture profiles directory is unsafe: {store.profiles_dir}"
        )

    pending: list[str] = []
    for profile in ("internal", "openai-official"):
        _, stage_dir, previous_dir, journal_path = _capture_artifact_paths(
            store,
            profile,
        )
        for candidate in (stage_dir, previous_dir, journal_path):
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            pending.append(profile)
            break
    return tuple(pending)


def _sync_capture_stage(
    adapter: FilesystemAdapter,
    stage_dir: Path,
) -> None:
    adapter.sync_tree(
        stage_dir,
        file_phase="capture_stage_data",
        directory_phase="capture_stage_directory",
    )


def _sync_capture_parent(
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent,
    *,
    phase: str,
) -> None:
    workspace.revalidate(f"{phase}_before_sync")
    adapter.sync_directory(workspace.path, phase=phase)
    workspace.revalidate(f"{phase}_after_sync")


def _remove_capture_journal(
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent,
    journal_path: Path,
    expected: Mapping[str, object],
    *,
    phase: str,
    parent_sync_phase: str,
) -> None:
    workspace.revalidate(f"{phase}_before_remove")
    if adapter.capture_state(journal_path) != dict(expected):
        raise SwitchError(f"Capture journal changed before cleanup: {journal_path}")
    adapter.remove_file(journal_path, phase=phase)
    _sync_capture_parent(adapter, workspace, phase=parent_sync_phase)
    if _capture_artifact_exists(adapter, journal_path):
        raise SwitchError(f"Capture journal cleanup did not finish: {journal_path}")


def _retain_capture_journal(
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent,
    journal_path: Path,
    journal_data: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    replace_changed: bool = False,
) -> None:
    workspace.revalidate("capture_retain_journal_before_write")
    current = adapter.capture_state(journal_path)
    if current == dict(expected):
        return
    if (
        current != {"kind": "missing", "path": str(journal_path)}
        and not replace_changed
    ):
        raise SwitchError(
            f"Capture journal cannot be safely rebuilt over changed evidence: "
            f"{journal_path}"
        )
    adapter.write_manifest(
        journal_path,
        _canonical_json_object(journal_data),
        phase="capture_retain_journal",
    )
    if adapter.capture_state(journal_path) != dict(expected):
        raise SwitchError(
            f"Rebuilt capture journal does not match transaction: {journal_path}"
        )
    adapter.sync_file(journal_path, phase="capture_retain_journal")
    _sync_capture_parent(
        adapter,
        workspace,
        phase="capture_retain_journal_parent",
    )
    workspace.revalidate("capture_retain_journal_after_sync")
    if adapter.capture_state(journal_path) != dict(expected):
        raise SwitchError(
            f"Rebuilt capture journal changed after fsync: {journal_path}"
        )


def _ensure_capture_directory(path: Path, *, label: str, mode: int = 0o700) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            info = path.lstat()
        else:
            info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SwitchError(f"Capture {label} path is a symlink or not a directory: {path}")
    path.chmod(mode)


def _ensure_capture_store_layout(store: Store) -> None:
    try:
        root_info = store.root.lstat()
    except FileNotFoundError as exc:
        raise SwitchError(f"Profile store does not exist: {store.root}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise SwitchError(
            f"Capture store root is a symlink or not a directory: {store.root}"
        )

    _ensure_capture_directory(store.profiles_dir, label="profiles")
    store.root.chmod(0o700)
    _ensure_capture_directory(store.backups_dir, label="backups")
    _ensure_capture_directory(store.bin_dir, label="bin")
    _ensure_capture_directory(store.homes_dir, label="homes")


def _validate_capture_journal_state(
    state: object,
    *,
    label: str,
    expected_path: Path,
    allow_missing: bool,
) -> dict[str, object]:
    if not isinstance(state, dict):
        raise SwitchError(f"Capture journal {label} state must be an object")
    kind = state.get("kind")
    allowed_kinds = {"directory", "missing"} if allow_missing else {"directory"}
    if kind not in allowed_kinds:
        raise SwitchError(f"Capture journal {label} state has invalid kind: {kind}")
    if state.get("path") != str(expected_path):
        raise SwitchError(f"Capture journal {label} state path is not approved")
    if kind == "directory":
        digest = state.get("tree_sha256")
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            raise SwitchError(
                f"Capture journal {label} directory has no valid tree digest"
            )
        mode = state.get("mode")
        if not isinstance(mode, int) or isinstance(mode, bool):
            raise SwitchError(
                f"Capture journal {label} directory has no valid mode"
            )
        entry_count = state.get("entry_count")
        if (
            not isinstance(entry_count, int)
            or isinstance(entry_count, bool)
            or entry_count < 0
        ):
            raise SwitchError(
                f"Capture journal {label} directory has no valid entry count"
            )
    return state


def _read_capture_journal(
    journal_path: Path,
    *,
    adapter: FilesystemAdapter,
    expected_journal_state: Mapping[str, object],
    profile: str,
    profile_dir: Path,
    stage_dir: Path,
    previous_dir: Path,
) -> tuple[
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    if expected_journal_state.get("kind") != "file":
        raise SwitchError(f"Capture journal is not a regular file: {journal_path}")
    journal = adapter.read_json(journal_path)
    if adapter.capture_state(journal_path) != dict(expected_journal_state):
        raise SwitchError(f"Capture journal changed while reading: {journal_path}")
    if not isinstance(journal, dict):
        raise SwitchError(f"Capture journal must be an object: {journal_path}")
    schema_version = journal.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise SwitchError(f"Unsupported capture journal schema: {journal_path}")
    if journal.get("operation") != "capture" or journal.get("profile") != profile:
        raise SwitchError(f"Capture journal identity mismatch: {journal_path}")
    lifecycle = journal.get("lifecycle")
    if lifecycle not in {"prepared", "committed"}:
        raise SwitchError(f"Capture journal lifecycle is invalid: {lifecycle}")
    expected_paths = {
        "destination": profile_dir,
        "stage": stage_dir,
        "previous": previous_dir,
    }
    for key, expected in expected_paths.items():
        if journal.get(key) != str(expected):
            raise SwitchError(f"Capture journal {key} path is not approved")
    before_state = _validate_capture_journal_state(
        journal.get("before_state"),
        label="before",
        expected_path=profile_dir,
        allow_missing=True,
    )
    staged_state = _validate_capture_journal_state(
        journal.get("staged_state"),
        label="staged",
        expected_path=stage_dir,
        allow_missing=False,
    )
    canonical_journal = _canonical_json_object(journal)
    if _expected_json_file_state(journal_path, canonical_journal) != dict(
        expected_journal_state
    ):
        raise SwitchError(
            f"Capture journal content does not match file state: {journal_path}"
        )
    return (
        str(lifecycle),
        dict(before_state),
        dict(staged_state),
        canonical_journal,
    )


def _remove_attested_capture_tree(
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent,
    path: Path,
    expected: Mapping[str, object],
    *,
    phase: str,
    parent_sync_phase: str,
    journal_path: Path | None = None,
    journal_state: Mapping[str, object] | None = None,
    protected_path: Path | None = None,
    protected_state: Mapping[str, object] | None = None,
) -> None:
    workspace.revalidate(f"{phase}_before_remove")
    if (journal_path is None) != (journal_state is None):
        raise SwitchError("Capture cleanup journal attestation is incomplete")
    if (
        journal_path is not None
        and journal_state is not None
        and adapter.capture_state(journal_path) != dict(journal_state)
    ):
        raise SwitchError(f"Capture journal changed before cleanup: {journal_path}")
    if (protected_path is None) != (protected_state is None):
        raise SwitchError("Capture cleanup protected-state attestation is incomplete")
    if (
        protected_path is not None
        and protected_state is not None
        and adapter.capture_state(protected_path) != dict(protected_state)
    ):
        raise SwitchError(
            f"Capture protected state changed before cleanup: {protected_path}"
        )
    if adapter.capture_state(path) != dict(expected):
        raise SwitchError(f"Capture recovery artifact changed: {path}")
    adapter.remove_tree(path, phase=phase)
    _sync_capture_parent(adapter, workspace, phase=parent_sync_phase)
    if _capture_artifact_exists(adapter, path):
        raise SwitchError(f"Capture recovery could not remove artifact: {path}")


def _capture_directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SwitchError(f"Capture stage disappeared after creation: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SwitchError(f"Capture stage root is not a directory: {path}")
    return info.st_dev, info.st_ino


def _remove_owned_unjournaled_capture_stage(
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent,
    stage_dir: Path,
    expected_identity: tuple[int, int],
) -> None:
    def require_owned_root() -> bool:
        try:
            identity = adapter.capture_directory_identity(stage_dir)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SwitchError(
                f"Unjournaled capture stage root identity changed: {stage_dir}"
            ) from exc
        if identity != expected_identity:
            raise SwitchError(
                f"Unjournaled capture stage root identity changed: {stage_dir}"
            )
        return True

    workspace.validate_descriptor("capture_stage_cleanup_before_identity_check")
    if not require_owned_root():
        return
    workspace.validate_descriptor("capture_stage_cleanup_before_owned_remove")
    if not require_owned_root():
        return
    adapter.remove_tree(stage_dir, phase="capture_stage_cleanup")
    workspace.validate_descriptor("capture_stage_cleanup_before_parent_sync")
    adapter.sync_directory(
        workspace.path,
        phase="capture_stage_cleanup_parent",
    )
    workspace.validate_descriptor("capture_stage_cleanup_after_parent_sync")
    if not require_owned_root():
        return
    raise SwitchError(f"Capture stage cleanup did not finish: {stage_dir}")


def _recover_capture_journal(
    store: Store,
    profile: str,
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent,
) -> None:
    profile_dir, stage_dir, previous_dir, journal_path = _capture_artifact_paths(
        store, profile
    )
    workspace.revalidate("capture_recovery_before_artifact_read")
    journal_exists = _capture_artifact_exists(adapter, journal_path)
    stage_exists = _capture_artifact_exists(adapter, stage_dir)
    previous_exists = _capture_artifact_exists(adapter, previous_dir)
    if not journal_exists:
        if stage_exists or previous_exists:
            raise SwitchError(
                f"Incomplete capture artifacts have no journal for profile {profile}"
            )
        return

    journal_state: Mapping[str, object] = MappingProxyType(
        dict(adapter.capture_state(journal_path))
    )
    lifecycle, before_state, staged_state, journal_data = _read_capture_journal(
        journal_path,
        adapter=adapter,
        expected_journal_state=journal_state,
        profile=profile,
        profile_dir=profile_dir,
        stage_dir=stage_dir,
        previous_dir=previous_dir,
    )
    installed_staged_state = _capture_state_at_path(staged_state, profile_dir)
    previous_before_state = _capture_state_at_path(before_state, previous_dir)
    missing_destination_state = {"kind": "missing", "path": str(profile_dir)}
    missing_stage_state = {"kind": "missing", "path": str(stage_dir)}
    missing_previous_state = {"kind": "missing", "path": str(previous_dir)}
    missing_journal_state = {"kind": "missing", "path": str(journal_path)}

    def require_journal(phase: str) -> None:
        if adapter.capture_state(journal_path) != dict(journal_state):
            raise SwitchError(
                f"Capture journal changed during {phase}: {journal_path}"
            )

    destination_state = adapter.capture_state(profile_dir)
    stage_state = adapter.capture_state(stage_dir)
    previous_state = adapter.capture_state(previous_dir)

    if lifecycle == "committed":
        if destination_state != installed_staged_state:
            raise SwitchError(
                f"Committed capture destination does not match journal: {profile_dir}"
            )
        if previous_state != missing_previous_state:
            if previous_state != previous_before_state:
                raise SwitchError(
                    f"Committed capture previous directory changed: {previous_dir}"
                )
            _remove_attested_capture_tree(
                adapter,
                workspace,
                previous_dir,
                previous_before_state,
                phase="capture_recover_previous_cleanup",
                parent_sync_phase="capture_recover_previous_cleanup_parent",
                journal_path=journal_path,
                journal_state=journal_state,
                protected_path=profile_dir,
                protected_state=installed_staged_state,
            )
            if adapter.capture_state(profile_dir) != installed_staged_state:
                raise SwitchError(
                    f"Committed capture destination changed during recovery cleanup: "
                    f"{profile_dir}"
                )
        if stage_state != missing_stage_state:
            require_journal("committed stage cleanup")
            _remove_attested_capture_tree(
                adapter,
                workspace,
                stage_dir,
                staged_state,
                phase="capture_recover_stage_cleanup",
                parent_sync_phase="capture_recover_stage_cleanup_parent",
                journal_path=journal_path,
                journal_state=journal_state,
                protected_path=profile_dir,
                protected_state=installed_staged_state,
            )
            if adapter.capture_state(profile_dir) != installed_staged_state:
                raise SwitchError(
                    f"Committed capture destination changed during recovery cleanup: "
                    f"{profile_dir}"
                )
        require_journal("committed journal cleanup")
        committed_recovery_prejournal_mismatches = _capture_vector_mismatches(
            adapter,
            (
                (profile_dir, installed_staged_state),
                (stage_dir, missing_stage_state),
                (previous_dir, missing_previous_state),
                (journal_path, journal_state),
            ),
        )
        if committed_recovery_prejournal_mismatches:
            raise _capture_vector_error(
                "Committed capture recovery retained an invalid pre-journal vector",
                committed_recovery_prejournal_mismatches,
            )
        _remove_capture_journal(
            adapter,
            workspace,
            journal_path,
            journal_state,
            phase="capture_recover_journal_cleanup",
            parent_sync_phase="capture_recover_journal_cleanup_parent",
        )
        committed_recovery_terminal_mismatches = _capture_vector_mismatches(
            adapter,
            (
                (profile_dir, installed_staged_state),
                (stage_dir, missing_stage_state),
                (previous_dir, missing_previous_state),
                (journal_path, missing_journal_state),
            ),
        )
        if committed_recovery_terminal_mismatches:
            try:
                _retain_capture_journal(
                    adapter,
                    workspace,
                    journal_path,
                    journal_data,
                    journal_state,
                    replace_changed=True,
                )
            except Exception as evidence_error:
                raise SwitchError(
                    "Committed capture recovery terminal vector is invalid and "
                    f"canonical journal repair failed: {evidence_error}"
                ) from evidence_error
            raise _capture_vector_error(
                "Committed capture recovery retained an invalid terminal vector",
                committed_recovery_terminal_mismatches,
            )
        return

    restored_previous = False
    if previous_state != missing_previous_state:
        if (
            before_state.get("kind") != "directory"
            or previous_state != previous_before_state
        ):
            raise SwitchError(
                f"Prepared capture previous directory changed: {previous_dir}"
            )
        if destination_state != missing_destination_state:
            if destination_state != installed_staged_state:
                raise SwitchError(
                    f"Prepared capture destination is ambiguous: {profile_dir}"
                )
            if stage_state != missing_stage_state:
                raise SwitchError(
                    f"Prepared capture has both destination and stage: {profile}"
                )
            workspace.revalidate(
                "capture_recovery_before_destination_to_stage"
            )
            require_journal("prepared destination recovery rename")
            if (
                adapter.capture_state(profile_dir) != installed_staged_state
                or adapter.capture_state(stage_dir) != missing_stage_state
            ):
                raise SwitchError(
                    f"Prepared capture destination changed before recovery: "
                    f"{profile_dir}"
                )
            adapter.rename(
                profile_dir,
                stage_dir,
                phase="capture_recover_destination_to_stage",
            )
            _sync_capture_parent(
                adapter,
                workspace,
                phase="capture_recover_destination_to_stage_parent",
            )
        workspace.revalidate("capture_recovery_before_previous_to_destination")
        require_journal("prepared previous recovery rename")
        if (
            adapter.capture_state(previous_dir) != previous_before_state
            or adapter.capture_state(profile_dir) != missing_destination_state
        ):
            raise SwitchError(
                f"Prepared capture previous directory changed before recovery: "
                f"{previous_dir}"
            )
        adapter.rename(
            previous_dir,
            profile_dir,
            phase="capture_recover_previous_to_destination",
        )
        _sync_capture_parent(
            adapter,
            workspace,
            phase="capture_recover_previous_to_destination_parent",
        )
        restored_previous = True
        if adapter.capture_state(profile_dir) != before_state:
            raise SwitchError(
                f"Prepared capture restored destination changed: {profile_dir}"
            )
        require_journal("prepared destination restoration")
    elif before_state.get("kind") == "missing":
        if destination_state != missing_destination_state:
            if destination_state != installed_staged_state:
                raise SwitchError(
                    f"Prepared capture destination is ambiguous: {profile_dir}"
                )
            if stage_state != missing_stage_state:
                raise SwitchError(
                    f"Prepared capture has both destination and stage: {profile}"
                )
            workspace.revalidate(
                "capture_recovery_before_destination_to_stage"
            )
            require_journal("prepared destination recovery rename")
            if (
                adapter.capture_state(profile_dir) != installed_staged_state
                or adapter.capture_state(stage_dir) != missing_stage_state
            ):
                raise SwitchError(
                    f"Prepared capture destination changed before recovery: "
                    f"{profile_dir}"
                )
            adapter.rename(
                profile_dir,
                stage_dir,
                phase="capture_recover_destination_to_stage",
            )
            _sync_capture_parent(
                adapter,
                workspace,
                phase="capture_recover_destination_to_stage_parent",
            )
            if adapter.capture_state(stage_dir) != staged_state:
                raise SwitchError(
                    f"Prepared capture stage changed after recovery rename: "
                    f"{stage_dir}"
                )
        elif stage_state != staged_state:
            raise SwitchError(f"Prepared capture stage changed: {stage_dir}")
    else:
        if destination_state != before_state:
            raise SwitchError(
                f"Prepared capture destination is ambiguous: {profile_dir}"
            )
        if stage_state != staged_state:
            raise SwitchError(f"Prepared capture stage changed: {stage_dir}")

    if adapter.capture_state(profile_dir) != before_state:
        raise SwitchError(f"Capture recovery did not restore: {profile_dir}")
    if restored_previous and adapter.capture_state(stage_dir) != staged_state:
        raise SwitchError(
            f"Prepared capture recovery retained changed stage: {stage_dir}"
        )
    if _capture_artifact_exists(adapter, stage_dir):
        require_journal("prepared stage cleanup")
        _remove_attested_capture_tree(
            adapter,
            workspace,
            stage_dir,
            staged_state,
            phase="capture_recover_stage_cleanup",
            parent_sync_phase="capture_recover_stage_cleanup_parent",
            journal_path=journal_path,
            journal_state=journal_state,
            protected_path=profile_dir,
            protected_state=before_state,
        )
    if _capture_artifact_exists(adapter, previous_dir):
        raise SwitchError(f"Capture recovery left previous directory: {previous_dir}")
    require_journal("prepared journal cleanup")
    prepared_recovery_prejournal_mismatches = _capture_vector_mismatches(
        adapter,
        (
            (profile_dir, before_state),
            (stage_dir, missing_stage_state),
            (previous_dir, missing_previous_state),
            (journal_path, journal_state),
        ),
    )
    if prepared_recovery_prejournal_mismatches:
        raise _capture_vector_error(
            "Prepared capture recovery retained an invalid pre-journal vector",
            prepared_recovery_prejournal_mismatches,
        )
    _remove_capture_journal(
        adapter,
        workspace,
        journal_path,
        journal_state,
        phase="capture_recover_journal_cleanup",
        parent_sync_phase="capture_recover_journal_cleanup_parent",
    )
    prepared_recovery_terminal_mismatches = _capture_vector_mismatches(
        adapter,
        (
            (profile_dir, before_state),
            (stage_dir, missing_stage_state),
            (previous_dir, missing_previous_state),
            (journal_path, missing_journal_state),
        ),
    )
    if prepared_recovery_terminal_mismatches:
        try:
            _retain_capture_journal(
                adapter,
                workspace,
                journal_path,
                journal_data,
                journal_state,
                replace_changed=True,
            )
        except Exception as evidence_error:
            raise SwitchError(
                "Prepared capture recovery terminal vector is invalid and canonical "
                f"journal repair failed: {evidence_error}"
            ) from evidence_error
        raise _capture_vector_error(
            "Prepared capture recovery retained an invalid terminal vector",
            prepared_recovery_terminal_mismatches,
        )


def _execute_capture(
    store: Store,
    request: TransactionRequest,
    *,
    dry_run: bool,
    adapter: FilesystemAdapter,
    workspace: _PinnedCaptureParent | None,
) -> TransactionReceipt:
    from codex_switch_store import validate_profile_name

    validate_profile_name(request.profile)
    source_home = _capture_path_option(request, "source_home")
    codex_bin = _capture_string_option(request, "codex_bin")
    app_cli_path = request.options.get("app_cli_path")
    if not isinstance(app_cli_path, str):
        raise SwitchError("Capture transaction app_cli_path option must be a string")
    allow_missing_auth = _capture_bool_option(request, "allow_missing_auth")
    overwrite = _capture_bool_option(request, "overwrite")

    profile_dir, stage_dir, previous_dir, journal_path = _capture_artifact_paths(
        store, request.profile
    )
    if dry_run:
        if any(
            _capture_artifact_exists(adapter, path)
            for path in (stage_dir, previous_dir, journal_path)
        ):
            raise SwitchError(
                f"Incomplete capture artifacts require recovery for profile "
                f"{request.profile}"
            )
    else:
        if workspace is None:
            raise SwitchError("Capture profiles directory is not pinned")
        workspace.revalidate("capture_before_recovery")
        _recover_capture_journal(store, request.profile, adapter, workspace)

    source_config = source_home / "config.toml"
    source_config_state = adapter.capture_state(source_config)
    if source_config_state.get("kind") != "file":
        raise SwitchError(
            f"Missing {source_config}; cannot capture a switchable profile."
        )
    validate_toml(source_config)
    if adapter.capture_state(source_config) != source_config_state:
        raise SwitchError(f"Capture config source changed after preflight: {source_config}")
    captured_provenance = {}
    if request.profile == "internal":
        from codex_switch_parity import capture_catalog_provenance
        captured_provenance = capture_catalog_provenance(store, source_config)
    source_auth = source_home / "auth.json"
    source_auth_state = adapter.capture_state(source_auth)
    source_auth_present = source_auth_state.get("kind") == "file"
    if source_auth_state.get("kind") not in {"file", "missing"}:
        raise SwitchError(f"Capture auth source is not a file: {source_auth}")
    if not source_auth_present and not allow_missing_auth:
        raise SwitchError(
            f"Missing {source_auth}. Re-run with --allow-missing-auth when this "
            "profile has no auth yet."
        )

    profile_state = adapter.capture_state(profile_dir)
    if profile_state.get("kind") not in {"missing", "directory"}:
        raise SwitchError(f"Profile path is not a directory: {profile_dir}")
    if profile_state.get("kind") == "directory":
        frozen_unmanaged_projection: Mapping[str, object] = MappingProxyType(
            _capture_unmanaged_projection(adapter, profile_dir)
        )
        if adapter.capture_state(profile_dir) != profile_state:
            raise SwitchError(
                f"Capture profile changed while freezing unmanaged artifacts: "
                f"{profile_dir}"
            )
    else:
        frozen_unmanaged_projection = MappingProxyType(
            _unmanaged_capture_projection([])
        )
    if (
        profile_state.get("kind") == "directory"
        and not overwrite
        and adapter.capture_state(profile_dir / "manifest.json").get("kind")
        != "missing"
    ):
        raise SwitchError(
            f"Profile already exists: {request.profile}. Use --overwrite to replace files."
        )

    copied = ["config.toml"]
    if source_auth_present:
        copied.append("auth.json")
    preview_lines = (
        f"Captured profile {request.profile}: {', '.join(copied)}",
    )
    if dry_run:
        return TransactionReceipt(
            operation="capture",
            outcome="dry_run",
            preview_lines=preview_lines,
            backup_id=None,
        )

    if workspace is None:
        raise SwitchError("Capture profiles directory is not pinned")
    if not adapter.capture_workspace_bound(store.profiles_dir):
        raise SwitchError("Capture profiles workspace binding is missing")
    expected_staged_config_state: Mapping[str, object] = MappingProxyType(
        {
            "kind": "file",
            "path": str(stage_dir / "config.toml"),
            "mode": 0o600,
            "size": source_config_state.get("size"),
            "sha256": source_config_state.get("sha256"),
        }
    )
    expected_staged_auth_state: Mapping[str, object] = MappingProxyType(
        (
            {
                "kind": "file",
                "path": str(stage_dir / "auth.json"),
                "mode": 0o600,
                "size": source_auth_state.get("size"),
                "sha256": source_auth_state.get("sha256"),
            }
            if source_auth_present
            else {"kind": "missing", "path": str(stage_dir / "auth.json")}
        )
    )
    workspace.revalidate("capture_before_artifact_check")
    if any(
        _capture_artifact_exists(adapter, path)
        for path in (stage_dir, previous_dir, journal_path)
    ):
        raise SwitchError(
            f"Incomplete capture artifacts require recovery for profile "
            f"{request.profile}"
        )
    workspace.revalidate("capture_after_artifact_check")

    stage_root_identity: tuple[int, int] | None = None
    try:
        workspace.revalidate("capture_before_stage_write")
        try:
            stage_root_identity = adapter.make_directory(
                stage_dir,
                mode=0o700,
                phase="capture_stage_create",
            )
        except FileExistsError as exc:
            raise SwitchError(
                f"Capture stage exists but is not owned by this transaction: "
                f"{stage_dir}"
            ) from exc
        if profile_state.get("kind") == "directory":
            adapter.copy_material(
                profile_dir,
                stage_dir,
                "directory",
                phase="capture_clone",
            )
        if _capture_unmanaged_projection(
            adapter,
            stage_dir,
        ) != dict(frozen_unmanaged_projection):
            raise SwitchError(
                f"Staged capture unmanaged artifacts changed during clone: "
                f"{stage_dir}"
            )
        adapter.chmod(stage_dir, 0o700, phase="capture_stage_mode")

        for managed_name in (*MANAGED_FILES, "manifest.json"):
            adapter.remove_path(
                stage_dir / managed_name,
                phase="capture_managed_remove",
            )

        if adapter.capture_state(source_config) != source_config_state:
            raise SwitchError(
                f"Capture config source changed after preflight: {source_config}"
            )
        adapter.copy_material(
            source_config,
            stage_dir / "config.toml",
            "file",
            phase="capture_config",
        )
        adapter.chmod(
            stage_dir / "config.toml",
            0o600,
            phase="capture_config_mode",
        )
        if adapter.capture_state(stage_dir / "config.toml") != dict(
            expected_staged_config_state
        ):
            raise SwitchError(
                f"Staged capture config does not match source: {source_config}"
            )
        if adapter.capture_state(source_auth) != source_auth_state:
            raise SwitchError(
                f"Capture auth source changed after preflight: {source_auth}"
            )
        if source_auth_present:
            adapter.copy_material(
                source_auth,
                stage_dir / "auth.json",
                "file",
                phase="capture_auth",
            )
            adapter.chmod(
                stage_dir / "auth.json",
                0o600,
                phase="capture_auth_mode",
            )
            if adapter.capture_state(stage_dir / "auth.json") != dict(
                expected_staged_auth_state
            ):
                raise SwitchError(
                    f"Staged capture auth does not match source: {source_auth}"
                )
        else:
            adapter.remove_path(
                stage_dir / "auth.json",
                phase="capture_absent_auth_remove",
            )
            if adapter.capture_state(stage_dir / "auth.json") != dict(
                expected_staged_auth_state
            ):
                raise SwitchError(
                    f"Staged capture auth was not removed: {stage_dir / 'auth.json'}"
                )
        provenance = captured_provenance
        if request.profile == "internal":
            from codex_switch_parity import capture_catalog_provenance
            if capture_catalog_provenance(store, source_config) != provenance:
                raise SwitchError("Catalog provenance changed during capture")
        manifest_data = _canonical_json_object({
            **provenance,
            "name": request.profile,
            "description": f"Captured from {source_home}",
            "codex_bin": codex_bin,
            "app_cli_path": app_cli_path or codex_bin,
            "app_cli_binding": "launchagent",
            "captured_at": now_stamp(),
            "managed_files": list(MANAGED_FILES),
        })
        expected_manifest_state: Mapping[str, object] = MappingProxyType(
            _expected_json_file_state(stage_dir / "manifest.json", manifest_data)
        )
        adapter.write_manifest(
            stage_dir / "manifest.json",
            _canonical_json_object(manifest_data),
            phase="capture_manifest",
        )
        frozen_staged_state = _validate_capture_journal_state(
            adapter.capture_state(stage_dir),
            label="staged",
            expected_path=stage_dir,
            allow_missing=False,
        )
        if frozen_staged_state.get("mode") != 0o700:
            raise SwitchError(
                f"Staged capture root mode does not match transaction: {stage_dir}"
            )
        staged_state: Mapping[str, object] = MappingProxyType(
            dict(frozen_staged_state)
        )
        installed_staged_state: Mapping[str, object] = MappingProxyType(
            _capture_state_at_path(frozen_staged_state, profile_dir)
        )
        previous_profile_state: Mapping[str, object] = MappingProxyType(
            _capture_state_at_path(profile_state, previous_dir)
        )
        missing_previous_state = {"kind": "missing", "path": str(previous_dir)}
        missing_destination_state = {"kind": "missing", "path": str(profile_dir)}
        missing_stage_state = {"kind": "missing", "path": str(stage_dir)}
        missing_journal_state = {"kind": "missing", "path": str(journal_path)}
        if _capture_unmanaged_projection(
            adapter,
            stage_dir,
        ) != dict(frozen_unmanaged_projection):
            raise SwitchError(
                f"Staged capture unmanaged artifacts changed: {stage_dir}"
            )
        if adapter.capture_state(stage_dir / "manifest.json") != dict(
            expected_manifest_state
        ):
            raise SwitchError(
                f"Staged capture manifest does not match transaction: "
                f"{stage_dir / 'manifest.json'}"
            )
        if adapter.capture_state(source_config) != source_config_state:
            raise SwitchError(
                f"Capture config source changed after staging: {source_config}"
            )
        if adapter.capture_state(source_auth) != source_auth_state:
            raise SwitchError(
                f"Capture auth source changed after staging: {source_auth}"
            )
        if adapter.capture_state(stage_dir / "config.toml") != dict(
            expected_staged_config_state
        ):
            raise SwitchError(
                f"Staged capture config changed after managed writes: "
                f"{stage_dir / 'config.toml'}"
            )
        if adapter.capture_state(stage_dir / "auth.json") != dict(
            expected_staged_auth_state
        ):
            raise SwitchError(
                f"Staged capture auth changed after managed writes: "
                f"{stage_dir / 'auth.json'}"
            )
        validate_toml_text(
            adapter.read_text(stage_dir / "config.toml"),
            str(stage_dir / "config.toml"),
        )
        _require_exact_capture_state(
            adapter,
            stage_dir,
            staged_state,
            phase="managed-file validation",
        )
        _sync_capture_stage(adapter, stage_dir)
        _require_exact_capture_state(
            adapter,
            stage_dir,
            staged_state,
            phase="stage fsync",
        )
        workspace.revalidate("capture_after_stage_write")
        _require_exact_capture_state(
            adapter,
            stage_dir,
            staged_state,
            phase="parent revalidation",
        )
    except Exception:
        if stage_root_identity is not None:
            _remove_owned_unjournaled_capture_stage(
                adapter,
                workspace,
                stage_dir,
                stage_root_identity,
            )
        raise

    prepared_journal_data = _canonical_json_object({
        "schema_version": 1,
        "operation": "capture",
        "profile": request.profile,
        "lifecycle": "prepared",
        "destination": str(profile_dir),
        "stage": str(stage_dir),
        "previous": str(previous_dir),
        "before_state": dict(profile_state),
        "staged_state": dict(staged_state),
    })
    prepared_journal_state: Mapping[str, object] = MappingProxyType(
        _expected_json_file_state(journal_path, prepared_journal_data)
    )
    try:
        workspace.revalidate("capture_before_prepare_journal")
        _require_exact_capture_state(
            adapter,
            stage_dir,
            staged_state,
            phase="prepared journal",
        )
        adapter.write_manifest(
            journal_path,
            _canonical_json_object(prepared_journal_data),
            phase="capture_prepare",
        )
        if adapter.capture_state(journal_path) != dict(prepared_journal_state):
            raise SwitchError(
                f"Prepared capture journal does not match transaction: "
                f"{journal_path}"
            )
        adapter.sync_file(journal_path, phase="capture_prepare_journal")
        _sync_capture_parent(
            adapter,
            workspace,
            phase="capture_prepare_parent",
        )
        workspace.revalidate("capture_after_prepare_journal")
        if adapter.capture_state(journal_path) != dict(prepared_journal_state):
            raise SwitchError(
                f"Prepared capture journal changed after fsync: {journal_path}"
            )
    except Exception:
        if not _capture_artifact_exists(adapter, journal_path):
            if stage_root_identity is not None:
                _remove_owned_unjournaled_capture_stage(
                    adapter,
                    workspace,
                    stage_dir,
                    stage_root_identity,
                )
        raise

    committed_journal_state: Mapping[str, object] | None = None
    commit_durable = False
    try:
        workspace.revalidate("capture_before_apply")
        if adapter.capture_state(journal_path) != dict(prepared_journal_state):
            raise SwitchError(f"Prepared capture journal changed: {journal_path}")
        if adapter.capture_state(stage_dir) != dict(staged_state):
            raise SwitchError(f"Capture stage changed before apply: {stage_dir}")
        if adapter.capture_state(profile_dir) != profile_state:
            raise SwitchError(f"Capture destination changed before apply: {profile_dir}")
        if profile_state.get("kind") == "directory":
            workspace.revalidate("capture_before_destination_to_previous")
            if (
                adapter.capture_state(journal_path)
                != dict(prepared_journal_state)
            ):
                raise SwitchError(
                    f"Prepared capture journal changed before destination rename: "
                    f"{journal_path}"
                )
            if adapter.capture_state(stage_dir) != dict(staged_state):
                raise SwitchError(
                    f"Capture stage changed before destination rename: {stage_dir}"
                )
            if (
                adapter.capture_state(profile_dir) != profile_state
                or adapter.capture_state(previous_dir) != missing_previous_state
            ):
                raise SwitchError(
                    f"Capture destination changed before rename: {profile_dir}"
                )
            adapter.rename(
                profile_dir,
                previous_dir,
                phase="capture_destination_to_previous",
            )
            _sync_capture_parent(
                adapter,
                workspace,
                phase="capture_destination_to_previous_parent",
            )
            if adapter.capture_state(previous_dir) != dict(
                previous_profile_state
            ):
                raise SwitchError(
                    f"Capture previous directory does not match destination: "
                    f"{previous_dir}"
                )
        workspace.revalidate("capture_before_stage_to_destination")
        if adapter.capture_state(journal_path) != dict(prepared_journal_state):
            raise SwitchError(
                f"Prepared capture journal changed before stage rename: "
                f"{journal_path}"
            )
        if (
            adapter.capture_state(stage_dir) != dict(staged_state)
            or adapter.capture_state(profile_dir).get("kind") != "missing"
        ):
            raise SwitchError(f"Capture stage changed before rename: {stage_dir}")
        adapter.rename(
            stage_dir,
            profile_dir,
            phase="capture_stage_to_destination",
        )
        _sync_capture_parent(
            adapter,
            workspace,
            phase="capture_stage_to_destination_parent",
        )
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(f"Capture destination does not match stage: {profile_dir}")
        committed_journal_data = _canonical_json_object(
            {**prepared_journal_data, "lifecycle": "committed"}
        )
        committed_journal_state = MappingProxyType(
            _expected_json_file_state(journal_path, committed_journal_data)
        )
        workspace.revalidate("capture_before_finalize_journal")
        if adapter.capture_state(journal_path) != dict(prepared_journal_state):
            raise SwitchError(
                f"Prepared capture journal changed before finalize: {journal_path}"
            )
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(
                f"Capture destination changed before finalize: {profile_dir}"
            )
        adapter.write_manifest(
            journal_path,
            _canonical_json_object(committed_journal_data),
            phase="capture_finalize",
        )
        if adapter.capture_state(journal_path) != dict(committed_journal_state):
            raise SwitchError(
                f"Committed capture journal does not match transaction: "
                f"{journal_path}"
            )
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(
                f"Capture destination changed during finalize: {profile_dir}"
            )
        adapter.sync_file(journal_path, phase="capture_finalize_journal")
        workspace.revalidate("capture_finalize_parent_before_sync")
        adapter.sync_directory(
            workspace.path,
            phase="capture_finalize_parent",
        )
        commit_durable = True
        workspace.revalidate("capture_finalize_parent_after_sync")
        workspace.revalidate("capture_after_finalize_journal")
        if adapter.capture_state(journal_path) != dict(committed_journal_state):
            raise SwitchError(
                f"Committed capture journal changed after fsync: {journal_path}"
            )
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(
                f"Capture destination changed after finalize fsync: {profile_dir}"
            )
    except Exception as apply_error:
        if commit_durable:
            postcommit_errors: list[str] = []
            if committed_journal_state is None:
                postcommit_errors.append(
                    "committed capture journal expectation is missing"
                )
            else:
                try:
                    _retain_capture_journal(
                        adapter,
                        workspace,
                        journal_path,
                        committed_journal_data,
                        committed_journal_state,
                        replace_changed=True,
                    )
                except Exception as evidence_error:
                    postcommit_errors.append(str(evidence_error))
            try:
                destination_healthy = adapter.capture_state(profile_dir) == dict(
                    installed_staged_state
                )
            except Exception as destination_error:
                destination_healthy = False
                postcommit_errors.append(str(destination_error))
            if not destination_healthy:
                postcommit_errors.append(
                    f"committed capture destination changed: {profile_dir}"
                )
            if destination_healthy and not postcommit_errors:
                return TransactionReceipt(
                    operation="capture",
                    outcome="committed",
                    preview_lines=preview_lines,
                    backup_id=None,
                )
            return TransactionReceipt(
                operation="capture",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (
                    f"capture failed after durable commit: {apply_error}",
                    "post-commit evidence check failed: "
                    + "; ".join(postcommit_errors),
                ),
                backup_id=None,
            )
        rollback_errors: list[str] = []
        try:
            if workspace is None:
                raise SwitchError("Capture profiles directory is not pinned")
            workspace.revalidate("capture_before_rollback")
            current_journal_state = adapter.capture_state(journal_path)
            if (
                committed_journal_state is not None
                and current_journal_state == dict(committed_journal_state)
            ):
                try:
                    _retain_capture_journal(
                        adapter,
                        workspace,
                        journal_path,
                        prepared_journal_data,
                        prepared_journal_state,
                        replace_changed=True,
                    )
                except Exception as downgrade_error:
                    raise SwitchError(
                        "Capture rollback did not start because committed journal "
                        f"downgrade failed: {downgrade_error}"
                    ) from downgrade_error
                current_journal_state = adapter.capture_state(journal_path)
            if current_journal_state != dict(prepared_journal_state):
                raise SwitchError(
                    f"Capture rollback requires durable prepared journal: "
                    f"{journal_path}"
                )
            rollback_journal_state = prepared_journal_state
            rollback_journal_data = prepared_journal_data
            previous_state = adapter.capture_state(previous_dir)
            destination_state = adapter.capture_state(profile_dir)
            stage_state = adapter.capture_state(stage_dir)
            if previous_state != missing_previous_state:
                if previous_state != dict(previous_profile_state):
                    raise SwitchError(
                        f"Capture rollback previous directory changed: {previous_dir}"
                    )
                if destination_state != missing_destination_state:
                    if destination_state != dict(installed_staged_state):
                        raise SwitchError(
                            f"Capture rollback destination changed: {profile_dir}"
                        )
                    if stage_state != missing_stage_state:
                        raise SwitchError(
                            f"Capture rollback has both destination and stage: "
                            f"{request.profile}"
                        )
                    workspace.revalidate(
                        "capture_before_rollback_destination_to_stage"
                    )
                    if (
                        adapter.capture_state(profile_dir)
                        != dict(installed_staged_state)
                        or adapter.capture_state(stage_dir) != missing_stage_state
                        or adapter.capture_state(previous_dir)
                        != dict(previous_profile_state)
                        or adapter.capture_state(journal_path)
                        != dict(rollback_journal_state)
                    ):
                        raise SwitchError(
                            f"Capture rollback artifacts changed before move: "
                            f"{profile_dir}"
                        )
                    adapter.rename(
                        profile_dir,
                        stage_dir,
                        phase="capture_rollback_destination_to_stage",
                    )
                    _sync_capture_parent(
                        adapter,
                        workspace,
                        phase="capture_rollback_destination_to_stage_parent",
                    )
                if adapter.capture_state(previous_dir) != dict(
                    previous_profile_state
                ):
                    raise SwitchError(
                        f"Capture rollback previous directory changed: {previous_dir}"
                    )
                workspace.revalidate(
                    "capture_before_rollback_previous_to_destination"
                )
                if (
                    adapter.capture_state(previous_dir)
                    != dict(previous_profile_state)
                    or adapter.capture_state(profile_dir)
                    != missing_destination_state
                    or adapter.capture_state(journal_path)
                    != dict(rollback_journal_state)
                ):
                    raise SwitchError(
                        f"Capture rollback artifacts changed before install: "
                        f"{previous_dir}"
                    )
                adapter.rename(
                    previous_dir,
                    profile_dir,
                    phase="capture_rollback_previous_to_destination",
                )
                _sync_capture_parent(
                    adapter,
                    workspace,
                    phase="capture_rollback_previous_to_destination_parent",
                )
                if adapter.capture_state(profile_dir) != profile_state:
                    raise SwitchError(
                        f"Capture rollback restored destination changed: "
                        f"{profile_dir}"
                    )
                if adapter.capture_state(journal_path) != dict(
                    rollback_journal_state
                ):
                    raise SwitchError(
                        f"Capture rollback journal changed after restoring "
                        f"destination: {journal_path}"
                    )
            elif (
                profile_state.get("kind") == "missing"
                and destination_state != missing_destination_state
            ):
                if destination_state != dict(installed_staged_state):
                    raise SwitchError(
                        f"Capture rollback destination changed: {profile_dir}"
                    )
                if stage_state != missing_stage_state:
                    raise SwitchError(
                        f"Capture rollback has both destination and stage: "
                        f"{request.profile}"
                    )
                workspace.revalidate("capture_before_rollback_destination_to_stage")
                if (
                    adapter.capture_state(profile_dir)
                    != dict(installed_staged_state)
                    or adapter.capture_state(stage_dir) != missing_stage_state
                    or adapter.capture_state(journal_path)
                    != dict(rollback_journal_state)
                ):
                    raise SwitchError(
                        f"Capture rollback artifacts changed before move: "
                        f"{profile_dir}"
                    )
                adapter.rename(
                    profile_dir,
                    stage_dir,
                    phase="capture_rollback_destination_to_stage",
                )
                _sync_capture_parent(
                    adapter,
                    workspace,
                    phase="capture_rollback_destination_to_stage_parent",
                )
                if adapter.capture_state(stage_dir) != dict(staged_state):
                    raise SwitchError(
                        f"Capture rollback stage changed after rename: {stage_dir}"
                    )
            if adapter.capture_state(profile_dir) != profile_state:
                raise SwitchError(
                    f"Capture rollback state mismatch: {profile_dir}"
                )
            rollback_precleanup_mismatches = _capture_vector_mismatches(
                adapter,
                (
                    (profile_dir, profile_state),
                    (previous_dir, missing_previous_state),
                    (journal_path, rollback_journal_state),
                ),
            )
            if rollback_precleanup_mismatches:
                raise _capture_vector_error(
                    "Capture rollback retained an invalid pre-cleanup vector",
                    rollback_precleanup_mismatches,
                )
            current_stage_state = adapter.capture_state(stage_dir)
            if current_stage_state not in (
                missing_stage_state,
                dict(staged_state),
            ):
                raise SwitchError(
                    f"rollback retained changed stage: {stage_dir}"
                )
            if current_stage_state == dict(staged_state):
                workspace.revalidate("capture_before_rollback_stage_cleanup")
                _remove_attested_capture_tree(
                    adapter,
                    workspace,
                    stage_dir,
                    staged_state,
                    phase="capture_rollback_stage_cleanup",
                    parent_sync_phase="capture_rollback_stage_cleanup_parent",
                    journal_path=journal_path,
                    journal_state=rollback_journal_state,
                    protected_path=profile_dir,
                    protected_state=profile_state,
                )
            if adapter.capture_state(profile_dir) != profile_state:
                raise SwitchError(
                    f"Capture rollback destination changed during cleanup: "
                    f"{profile_dir}"
                )
            rollback_prejournal_mismatches = _capture_vector_mismatches(
                adapter,
                (
                    (profile_dir, profile_state),
                    (stage_dir, missing_stage_state),
                    (previous_dir, missing_previous_state),
                    (journal_path, rollback_journal_state),
                ),
            )
            if rollback_prejournal_mismatches:
                raise _capture_vector_error(
                    "Capture rollback retained an invalid pre-journal vector",
                    rollback_prejournal_mismatches,
                )
            _remove_capture_journal(
                adapter,
                workspace,
                journal_path,
                rollback_journal_state,
                phase="capture_rollback_journal_cleanup",
                parent_sync_phase="capture_rollback_journal_cleanup_parent",
            )
            rollback_terminal_mismatches = _capture_vector_mismatches(
                adapter,
                (
                    (profile_dir, profile_state),
                    (stage_dir, missing_stage_state),
                    (previous_dir, missing_previous_state),
                    (journal_path, missing_journal_state),
                ),
            )
            if rollback_terminal_mismatches:
                try:
                    _retain_capture_journal(
                        adapter,
                        workspace,
                        journal_path,
                        rollback_journal_data,
                        rollback_journal_state,
                        replace_changed=True,
                    )
                except Exception as evidence_error:
                    raise SwitchError(
                        "Capture rollback terminal vector is invalid and canonical "
                        f"journal repair failed: {evidence_error}"
                    ) from evidence_error
                raise _capture_vector_error(
                    "Capture rollback retained an invalid terminal vector",
                    rollback_terminal_mismatches,
                )
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            return TransactionReceipt(
                operation="capture",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (
                    f"capture failed: {apply_error}",
                    f"rollback failed: {'; '.join(rollback_errors)}",
                ),
                backup_id=None,
            )
        return TransactionReceipt(
            operation="capture",
            outcome="rolled_back",
            preview_lines=preview_lines
            + (f"capture failed: {apply_error}", "rollback completed"),
            backup_id=None,
        )
    try:
        if committed_journal_state is None:
            raise SwitchError("Committed capture journal expectation is missing")
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(
                f"Committed capture destination changed before cleanup: {profile_dir}"
            )
        if adapter.capture_state(journal_path) != dict(committed_journal_state):
            raise SwitchError(
                f"Committed capture journal changed before cleanup: {journal_path}"
            )
        if _capture_artifact_exists(adapter, previous_dir):
            if adapter.capture_state(previous_dir) != dict(
                previous_profile_state
            ):
                raise SwitchError(
                    f"Committed capture previous directory changed: {previous_dir}"
                )
            if workspace is None:
                raise SwitchError("Capture profiles directory is not pinned")
            workspace.revalidate("capture_before_previous_cleanup")
            _remove_attested_capture_tree(
                adapter,
                workspace,
                previous_dir,
                previous_profile_state,
                phase="capture_previous_cleanup",
                parent_sync_phase="capture_previous_cleanup_parent",
                journal_path=journal_path,
                journal_state=committed_journal_state,
                protected_path=profile_dir,
                protected_state=installed_staged_state,
            )
            if adapter.capture_state(profile_dir) != dict(installed_staged_state):
                raise SwitchError(
                    f"Committed capture destination changed after previous cleanup: "
                    f"{profile_dir}"
                )
            if adapter.capture_state(journal_path) != dict(
                committed_journal_state
            ):
                raise SwitchError(
                    f"Committed capture journal changed after previous cleanup: "
                    f"{journal_path}"
                )
        if workspace is None:
            raise SwitchError("Capture profiles directory is not pinned")
        workspace.revalidate("capture_before_journal_cleanup")
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(
                f"Committed capture destination changed before journal cleanup: "
                f"{profile_dir}"
            )
        if adapter.capture_state(journal_path) != dict(committed_journal_state):
            raise SwitchError(
                f"Committed capture journal changed before journal cleanup: "
                f"{journal_path}"
            )
        committed_prejournal_mismatches = _capture_vector_mismatches(
            adapter,
            (
                (profile_dir, installed_staged_state),
                (stage_dir, missing_stage_state),
                (previous_dir, missing_previous_state),
                (journal_path, committed_journal_state),
            ),
        )
        if committed_prejournal_mismatches:
            raise _capture_vector_error(
                "Committed capture cleanup retained an invalid pre-journal vector",
                committed_prejournal_mismatches,
            )
        _remove_capture_journal(
            adapter,
            workspace,
            journal_path,
            committed_journal_state,
            phase="capture_journal_cleanup",
            parent_sync_phase="capture_journal_cleanup_parent",
        )
        if adapter.capture_state(profile_dir) != dict(installed_staged_state):
            raise SwitchError(
                f"Committed capture destination changed after journal cleanup: "
                f"{profile_dir}"
            )
        residual_artifacts = _capture_vector_mismatches(
            adapter,
            (
                (profile_dir, installed_staged_state),
                (stage_dir, missing_stage_state),
                (previous_dir, missing_previous_state),
                (
                    journal_path,
                    {"kind": "missing", "path": str(journal_path)},
                ),
            ),
        )
        if residual_artifacts:
            raise _capture_vector_error(
                "Committed capture cleanup left an invalid terminal vector",
                residual_artifacts,
            )
    except Exception as cleanup_error:
        evidence_errors: list[str] = []
        try:
            _retain_capture_journal(
                adapter,
                workspace,
                journal_path,
                committed_journal_data,
                committed_journal_state,
                replace_changed=True,
            )
        except Exception as evidence_error:
            evidence_errors.append(str(evidence_error))
        try:
            destination_healthy = adapter.capture_state(profile_dir) == dict(
                installed_staged_state
            )
        except Exception:
            destination_healthy = False
        if not destination_healthy or evidence_errors:
            evidence_detail = (
                f"canonical recovery evidence repair failed: "
                f"{'; '.join(evidence_errors)}"
                if evidence_errors
                else "canonical committed journal retained"
            )
            return TransactionReceipt(
                operation="capture",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (
                    f"capture cleanup failed after commit: {cleanup_error}",
                    evidence_detail,
                ),
                backup_id=None,
            )
        return TransactionReceipt(
            operation="capture",
            outcome="committed",
            preview_lines=preview_lines,
            backup_id=None,
        )

    return TransactionReceipt(
        operation="capture",
        outcome="committed",
        preview_lines=preview_lines,
        backup_id=None,
    )


def _switch_bool_option(
    request: TransactionRequest,
    name: str,
    *,
    default: bool,
) -> bool:
    value = request.options.get(name, default)
    if not isinstance(value, bool):
        raise SwitchError(f"Switch transaction {name} option must be boolean")
    return value


def _switch_optional_string_option(
    request: TransactionRequest,
    name: str,
) -> str | None:
    value = request.options.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SwitchError(f"Switch transaction {name} option must be a string")
    return value


def _switch_filesystem_adapter(request: TransactionRequest) -> FilesystemAdapter:
    value = request.options.get("filesystem_adapter")
    adapter = value if value is not None else FilesystemAdapter()
    required = (
        "before_switch_effect_action",
        "bind_switch_effect",
        "bind_switch_staged_artifact",
        "bound_switch_produced_identity",
        "capture_identity",
        "capture_state",
        "copy_material",
        "durable_unlink",
        "materialize",
        "remove_path",
        "sync_directory",
        "sync_file",
        "sync_tree",
        "unbind_switch_effect",
        "write_bytes",
        "write_manifest",
    )
    if not all(callable(getattr(adapter, name, None)) for name in required):
        raise SwitchError(
            "Switch filesystem_adapter does not implement the required boundary"
        )
    return adapter  # type: ignore[return-value]


def _switch_desktop_binding_adapter(
    store: Store,
    request: TransactionRequest,
) -> object:
    from codex_switch_launch import _DesktopBindingAdapter

    value = request.options.get("desktop_binding_adapter")
    adapter = value if value is not None else _DesktopBindingAdapter(store)
    if not all(
        callable(getattr(adapter, name, None))
        for name in ("observe", "apply", "rollback")
    ):
        raise SwitchError(
            "Switch desktop_binding_adapter does not implement the required boundary"
        )
    return adapter


class _SwitchMutationJournal:
    def __init__(
        self,
        adapter: FilesystemAdapter,
        backup_dir: Path,
        manifest: Mapping[str, object],
        *,
        frozen_inputs: tuple[_FrozenSwitchInput, ...],
        desktop_observation: object | None,
        expected_path_states: Mapping[Path, Mapping[str, object]] | None = None,
        staged_artifacts: Mapping[
            tuple[Path, str],
            Mapping[str, object],
        ]
        | None = None,
    ) -> None:
        self.adapter = adapter
        self.backup_dir = backup_dir
        self.manifest = _canonical_json_object(manifest)
        self.effects: list[dict[str, object]] = []
        self._begun_effect_ids: set[int] = set()
        self._frozen_inputs = tuple(frozen_inputs)
        self._frozen_expected_states: dict[str, dict[str, object]] = {}
        self._frozen_expected_identities: dict[str, dict[str, object]] = {}
        self._frozen_commit_states: dict[str, dict[str, object]] = {}
        for frozen in self._frozen_inputs:
            raw_path = str(frozen.path)
            before_state = _immutable_state_object(frozen.before_state_json)
            before_identity = _immutable_state_object(
                frozen.before_identity_json
            )
            commit_state = _immutable_state_object(frozen.commit_state_json)
            existing_before = self._frozen_expected_states.get(raw_path)
            existing_identity = self._frozen_expected_identities.get(raw_path)
            existing_commit = self._frozen_commit_states.get(raw_path)
            if (
                existing_before is not None
                and _immutable_state_bytes(existing_before)
                != frozen.before_state_json
            ) or (
                existing_identity is not None
                and _immutable_state_bytes(existing_identity)
                != frozen.before_identity_json
            ) or (
                existing_commit is not None
                and _immutable_state_bytes(existing_commit)
                != frozen.commit_state_json
            ):
                raise SwitchError(
                    f"Switch plan has conflicting frozen input states: {raw_path}"
                )
            self._frozen_expected_states[raw_path] = before_state
            self._frozen_expected_identities[raw_path] = before_identity
            self._frozen_commit_states[raw_path] = commit_state
        self._expected_path_states: dict[str, dict[str, object]] = {}
        raw_entries = self.manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise SwitchError(
                f"Switch backup {backup_dir.name} has no predecessor entries"
            )
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise SwitchError(
                    f"Switch backup {backup_dir.name} has an invalid predecessor entry"
                )
            raw_path = raw_entry.get("path")
            before_state = raw_entry.get("before_state")
            if not isinstance(raw_path, str) or not isinstance(before_state, dict):
                raise SwitchError(
                    f"Switch backup {backup_dir.name} has an unattested predecessor"
                )
            if raw_path in self._expected_path_states:
                raise SwitchError(
                    f"Switch backup {backup_dir.name} has duplicate predecessors: "
                    f"{raw_path}"
                )
            self._expected_path_states[raw_path] = dict(before_state)
        for path, state in (expected_path_states or {}).items():
            self._expected_path_states.setdefault(str(path), dict(state))
        self._staged_artifacts = {
            (str(path), phase): dict(artifact)
            for (path, phase), artifact in (staged_artifacts or {}).items()
        }
        observed_desktop: dict[str, object] | None = None
        if desktop_observation is not None:
            gui_env = getattr(desktop_observation, "gui_env", None)
            service_loaded = getattr(desktop_observation, "service_loaded", None)
            if gui_env is not None and not isinstance(gui_env, str):
                raise SwitchError("Desktop observation has an invalid GUI environment")
            if not isinstance(service_loaded, bool):
                raise SwitchError("Desktop observation has an invalid service state")
            observed_desktop = {
                "gui_env": gui_env,
                "service_loaded": service_loaded,
            }
        self.manifest["switch_journal"] = {
            "schema_version": 1,
            "state": "prepared",
            "desktop_before": observed_desktop,
            "frozen_inputs": [
                {
                    "label": frozen.label,
                    "path": str(frozen.path),
                    "before_state": _immutable_state_object(
                        frozen.before_state_json
                    ),
                    "before_identity": _immutable_state_object(
                        frozen.before_identity_json
                    ),
                    "commit_state": _immutable_state_object(
                        frozen.commit_state_json
                    ),
                    "commit_replaces_identity": (
                        frozen.commit_replaces_identity
                    ),
                    "capture_kind": frozen.capture_kind,
                }
                for frozen in frozen_inputs
            ],
            "effects": self.effects,
        }

    def persist(self, *, phase: str) -> None:
        self.adapter.write_manifest(
            self.backup_dir / "backup.json",
            self.manifest,
            phase=phase,
        )

    def prepare(self) -> None:
        self.persist(phase="switch_journal_prepare")

    def validate_frozen_inputs(self, *, phase: str) -> None:
        for frozen in self._frozen_inputs:
            raw_path = str(frozen.path)
            expected = self._frozen_expected_states[raw_path]
            expected_identity = self._frozen_expected_identities[raw_path]
            require_state = (
                frozen.capture_kind != "path"
                or expected.get("kind") != "directory"
            )
            state_matches = True
            if require_state:
                current = _capture_switch_input_state(
                    self.adapter,
                    frozen.path,
                    capture_kind=frozen.capture_kind,
                )
                state_matches = (
                    isinstance(current, dict)
                    and _immutable_state_bytes(current)
                    == _immutable_state_bytes(expected)
                )
            current_identity = self.adapter.capture_identity(frozen.path)
            if (
                not state_matches
                or not isinstance(current_identity, dict)
                or _immutable_state_bytes(current_identity)
                != _immutable_state_bytes(expected_identity)
            ):
                raise SwitchError(
                    f"Required switch input changed {phase}: "
                    f"{frozen.label}: {frozen.path}"
                )

    def begin(
        self,
        *,
        kind: str,
        phase: str,
        path: Path | None = None,
        before_state: Mapping[str, object] | None = None,
        planned_after_state: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self.validate_frozen_inputs(phase=f"before {phase} intent")
        effect: dict[str, object] = {
            "id": len(self.effects),
            "kind": kind,
            "phase": phase,
            "status": "intent",
        }
        if path is not None:
            effect["path"] = str(path)
            effect["route_guard"] = _capture_route_guard(path)
            expected_before_state = self._expected_path_states.get(str(path))
            if expected_before_state is None:
                raise SwitchError(
                    f"Switch effect has no planned predecessor: {path}"
                )
            captured_before_state = self.adapter.capture_state(path)
            if not isinstance(captured_before_state, dict):
                raise SwitchError(
                    f"Switch adapter returned invalid effect state: {path}"
                )
            if not _states_match(captured_before_state, expected_before_state):
                raise SwitchError(
                    f"Switch effect predecessor changed before overwrite: {path}"
                )
            effect["before_state"] = dict(expected_before_state)
            before_identity = self.adapter.capture_identity(path)
            if not isinstance(before_identity, dict):
                raise SwitchError(
                    f"Switch adapter returned invalid effect identity: {path}"
                )
            effect["before_identity"] = before_identity
            staged_artifact = self._staged_artifacts.get((str(path), phase))
            if staged_artifact is not None:
                staged_path = staged_artifact.get("path")
                staged_state = staged_artifact.get("state")
                staged_identity = staged_artifact.get("identity")
                staged_route_guard = staged_artifact.get("route_guard")
                if (
                    not isinstance(staged_path, str)
                    or not isinstance(staged_state, dict)
                    or not isinstance(staged_identity, dict)
                    or not isinstance(staged_route_guard, dict)
                ):
                    raise SwitchError(
                        f"Switch effect staged identity is invalid: {path}"
                    )
                effect["staged_path"] = staged_path
                effect["staged_state"] = dict(staged_state)
                effect["staged_identity"] = dict(staged_identity)
                effect["staged_route_guard"] = dict(staged_route_guard)
        elif before_state is not None:
            effect["before_state"] = dict(before_state)
        if planned_after_state is not None:
            effect["planned_after_state"] = dict(planned_after_state)
        self.effects.append(effect)
        self.persist(phase="switch_journal_intent")
        effect_id = effect["id"]
        if not isinstance(effect_id, int):
            raise SwitchError("Switch journal generated an invalid effect ID")
        self._begun_effect_ids.add(effect_id)
        return effect

    def complete(
        self,
        effect: dict[str, object],
        *,
        observed_after_state: Mapping[str, object] | None = None,
        produced_identity: Mapping[str, object] | None = None,
    ) -> None:
        raw_path = effect.get("path")
        if isinstance(raw_path, str) and raw_path:
            observed_after_state = self.adapter.capture_state(Path(raw_path))
            if not isinstance(observed_after_state, dict):
                raise SwitchError(
                    f"Switch adapter returned invalid applied state: {raw_path}"
                )
            planned_after_state = effect.get("planned_after_state")
            if isinstance(planned_after_state, dict) and not _switch_effect_states_match(
                observed_after_state,
                planned_after_state,
            ):
                raise SwitchError(
                    f"Switch effect produced an unexpected state: {raw_path}"
                )
            effect["observed_after_state"] = observed_after_state
            self._expected_path_states[raw_path] = dict(observed_after_state)
            self._frozen_expected_states[raw_path] = dict(observed_after_state)
            if produced_identity is None:
                produced_identity = self.adapter.capture_identity(Path(raw_path))
            effect["produced_identity"] = dict(produced_identity)
            self._frozen_expected_identities[raw_path] = dict(
                produced_identity
            )
        elif observed_after_state is not None:
            planned_after_state = effect.get("planned_after_state")
            if isinstance(planned_after_state, dict) and not _switch_effect_states_match(
                observed_after_state,
                planned_after_state,
            ):
                raise SwitchError("Desktop effect produced an unexpected state")
            effect["observed_after_state"] = dict(observed_after_state)
        self.validate_frozen_inputs(phase=f"after {effect.get('phase')} action")
        effect["status"] = "applied"
        self.persist(phase="switch_journal_applied")

    def apply_path(
        self,
        path: Path,
        *,
        phase: str,
        action: Callable[[], None],
        planned_after_state: Mapping[str, object] | None = None,
        pre_action_guard: Callable[[], None] | None = None,
        post_action_guard: Callable[[], None] | None = None,
    ) -> None:
        effect = self.begin(
            kind="filesystem",
            phase=phase,
            path=path,
            planned_after_state=planned_after_state,
        )
        route_guard = effect.get("route_guard")
        if not isinstance(route_guard, dict):
            raise SwitchError(f"Switch effect has no route guard: {path}")
        parent_descriptor = _open_guarded_parent(path, route_guard)
        produced_state: dict[str, object] | None = None
        produced_identity: dict[str, object] | None = None
        action_started = False
        self.adapter.bind_switch_effect(path, parent_descriptor)
        try:
            self.adapter.bind_switch_staged_artifact(
                effect if isinstance(effect.get("staged_path"), str) else None
            )
            self.adapter.before_switch_effect_action(path, effect)
            pinned_before_state = _path_state_at(
                parent_descriptor,
                (path.name,),
                path,
            )
            pinned_before_identity = _node_identity_at(
                parent_descriptor,
                path.name,
                path,
            )
            before_state = effect.get("before_state")
            before_identity = effect.get("before_identity")
            if (
                not isinstance(before_state, dict)
                or not _states_match(pinned_before_state, before_state)
                or not isinstance(before_identity, dict)
                or pinned_before_identity != before_identity
            ):
                raise SwitchError(
                    f"Switch effect predecessor identity changed before action: "
                    f"{path}"
                )
            self.validate_frozen_inputs(phase=f"after {phase} intent")
            if pre_action_guard is not None:
                pre_action_guard()
            action_started = True
            action()
            action_produced_identity = (
                self.adapter.bound_switch_produced_identity(path)
            )
            produced_state = _path_state_at(
                parent_descriptor,
                (path.name,),
                path,
            )
            produced_identity = _node_identity_at(
                parent_descriptor,
                path.name,
                path,
            )
            if (
                isinstance(action_produced_identity, dict)
                and produced_identity != action_produced_identity
            ):
                raise SwitchError(
                    f"Switch effect identity changed after action: {path}"
                )
            staged_identity = effect.get("staged_identity")
            if isinstance(staged_identity, dict) and not _node_identities_match(
                produced_identity,
                staged_identity,
            ):
                raise SwitchError(
                    f"Switch effect did not install its persisted stage: {path}"
                )
            if post_action_guard is not None:
                post_action_guard()
            os.fsync(parent_descriptor)
        except BaseException:
            try:
                if action_started:
                    produced_state = _path_state_at(
                        parent_descriptor,
                        (path.name,),
                        path,
                    )
                    produced_identity = _node_identity_at(
                        parent_descriptor,
                        path.name,
                        path,
                    )
                    effect["action_observed_state"] = produced_state
                    effect["produced_identity"] = produced_identity
                    self.persist(phase="switch_journal_action_observed")
            finally:
                self.adapter.unbind_switch_effect()
                os.close(parent_descriptor)
            raise
        self.adapter.unbind_switch_effect()
        os.close(parent_descriptor)
        _validate_route_guard(path, route_guard)
        if produced_state is None or produced_identity is None:
            raise SwitchError(f"Switch effect produced no attested state: {path}")
        current = self.adapter.capture_state(path)
        if not isinstance(current, dict) or not _states_match(current, produced_state):
            raise SwitchError(f"Switch effect path changed after action: {path}")
        self.complete(
            effect,
            observed_after_state=produced_state,
            produced_identity=produced_identity,
        )

    def adopt_finalized_manifest(
        self,
        finalized_manifest: Mapping[str, object],
        finalize_effect: Mapping[str, object],
    ) -> None:
        adopted = _canonical_json_object(finalized_manifest)
        if adopted.get("lifecycle") != "committed":
            raise SwitchError("Switch backup finalize did not commit the manifest")
        journal = adopted.get("switch_journal")
        if not isinstance(journal, dict):
            raise SwitchError("Finalized switch backup lost its mutation journal")
        raw_effects = journal.get("effects")
        effect_id = finalize_effect.get("id")
        if (
            not isinstance(raw_effects, list)
            or type(effect_id) is not int
            or effect_id < 0
            or effect_id >= len(raw_effects)
            or not isinstance(raw_effects[effect_id], dict)
        ):
            raise SwitchError("Finalized switch backup lost its finalize intent")
        adopted_effect = raw_effects[effect_id]
        if (
            adopted_effect.get("kind") != "finalize"
            or adopted_effect.get("phase") != "backup_finalize"
            or adopted_effect.get("status") != "applied"
        ):
            raise SwitchError("Finalized switch backup has an invalid finalize state")
        committed_state = {"lifecycle": "committed"}
        planned_after_state = adopted_effect.get("planned_after_state")
        if not isinstance(planned_after_state, dict) or not _switch_effect_states_match(
            committed_state,
            planned_after_state,
        ):
            raise SwitchError("Finalized switch backup has an invalid commit state")
        if not _switch_effect_states_match(
            adopted_effect.get("observed_after_state", {}),
            committed_state,
        ):
            raise SwitchError("Finalized switch backup has an invalid observed state")
        if journal.get("state") != "committed":
            raise SwitchError("Finalized switch backup has no committed journal")
        self.manifest = adopted
        self.effects = raw_effects

    def begun_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for effect in self.effects:
            effect_id = effect.get("id")
            raw_path = effect.get("path")
            if (
                type(effect_id) is int
                and effect_id in self._begun_effect_ids
                and isinstance(raw_path, str)
                and raw_path
            ):
                paths.append(Path(raw_path))
        return tuple(paths)


def _write_switch_backup_lifecycle(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    manifest: dict[str, object],
    lifecycle: str,
    error: Exception,
    *,
    rollback_error: Exception | None = None,
) -> None:
    updated = _canonical_json_object(manifest)
    updated["lifecycle"] = lifecycle
    updated[f"{lifecycle}_at"] = now_stamp()
    updated["failure"] = str(error)
    journal = updated.get("switch_journal")
    if isinstance(journal, dict):
        terminal_states = {
            "committed": "committed",
            "rolled_back": "recovered",
            "rollback_failed": "rollback_failed",
        }
        terminal_state = terminal_states.get(lifecycle)
        if terminal_state is None:
            raise SwitchError(
                f"Unsupported switch terminal lifecycle: {lifecycle}"
            )
        journal["state"] = terminal_state
        _set_switch_effect_recovery_state(journal, terminal_state)
    if rollback_error is not None:
        updated["rollback_failure"] = str(rollback_error)
    adapter.write_manifest(
        backup_dir / "backup.json",
        updated,
        phase=f"switch_{lifecycle}_finalize",
    )
    adapter.sync_file(
        backup_dir / "backup.json",
        phase=f"switch_{lifecycle}_terminal_manifest",
    )
    adapter.sync_directory(
        backup_dir,
        phase=f"switch_{lifecycle}_terminal_backup",
    )


def _write_switch_failure_record(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    manifest: Mapping[str, object],
    lifecycle: str,
    error: Exception,
    *,
    rollback_error: Exception | None = None,
) -> None:
    entries = manifest.get("entries")
    raw_switch_journal = manifest.get("switch_journal")
    switch_journal = (
        _canonical_json_object(raw_switch_journal)
        if isinstance(raw_switch_journal, dict)
        else {}
    )
    terminal_states = {
        "rolled_back": "recovered",
        "rollback_failed": "rollback_failed",
    }
    terminal_state = terminal_states.get(lifecycle)
    if terminal_state is not None:
        switch_journal["state"] = terminal_state
        _set_switch_effect_recovery_state(switch_journal, terminal_state)
    record: dict[str, object] = {
        "schema_version": 1,
        "record_kind": "switch_terminal_failure_receipt",
        "backup_schema_version": manifest.get("schema_version"),
        "lifecycle": lifecycle,
        "id": backup_dir.name,
        "backup_id": backup_dir.name,
        "operation": "switch",
        "failed_at": now_stamp(),
        "error": str(error),
        "entries": entries if isinstance(entries, list) else [],
        "switch_journal": switch_journal,
        "transaction_id": switch_journal.get("transaction_id"),
        "marker_name": switch_journal.get("marker_name"),
        "prepared_journal_sha256": switch_journal.get(
            "prepared_journal_sha256"
        ),
        "recovery_marker_required": switch_journal.get(
            "recovery_marker_required"
        ),
        "rollback_verified": lifecycle == "rolled_back",
    }
    if rollback_error is not None:
        record["rollback_failure"] = str(rollback_error)
    adapter.write_manifest(
        backup_dir / "failure.json",
        record,
        phase="switch_failure_record",
    )
    adapter.sync_file(
        backup_dir / "failure.json",
        phase="switch_failure_record",
    )
    adapter.sync_directory(
        backup_dir,
        phase="switch_failure_record_parent",
    )


def _read_authoritative_committed_switch(
    store: Store,
    backup_dir: Path,
    pending_marker: _PendingTransactionMarker | None,
) -> tuple[dict[str, object] | None, bool]:
    try:
        raw_manifest = read_json(backup_dir / "backup.json")
    except (OSError, SwitchError):
        return None, False
    claims_committed = (
        isinstance(raw_manifest, dict)
        and raw_manifest.get("operation") == "switch"
        and raw_manifest.get("lifecycle") == "committed"
    )
    if not claims_committed:
        return None, False
    if pending_marker is None:
        return None, True
    try:
        evidence = _validate_pending_transaction_evidence(
            store,
            pending_marker.path,
        )
    except SwitchError:
        return None, True
    if (
        evidence.operation != "switch"
        or evidence.lifecycle != "committed"
        or evidence.backup_dir != backup_dir
    ):
        return None, True
    return dict(evidence.manifest), True


def _rollback_switch_filesystem(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    manifest: Mapping[str, object],
    begun_paths: tuple[Path, ...],
    *,
    include_active: bool,
    active_path: Path,
) -> dict[str, object]:
    raw_manifest = _canonical_json_object(manifest)
    entries = raw_manifest.get("entries")
    if not isinstance(entries, list):
        raise SwitchError(f"Switch backup has no entries: {backup_dir.name}")
    entries_by_path: dict[str, dict[str, object]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise SwitchError(
                f"Switch backup entry is not an object: {backup_dir.name}"
            )
        raw_path = raw_entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise SwitchError(
                f"Switch backup entry lacks rollback path: {backup_dir.name}"
            )
        entries_by_path[raw_path] = raw_entry
    rollback_errors: list[str] = []
    restored: set[str] = set()
    journal = raw_manifest.get("switch_journal")
    raw_effects = journal.get("effects") if isinstance(journal, dict) else []
    effects_by_path: dict[str, list[dict[str, object]]] = {}
    if isinstance(raw_effects, list):
        for raw_effect in raw_effects:
            if not isinstance(raw_effect, dict):
                continue
            raw_effect_path = raw_effect.get("path")
            if isinstance(raw_effect_path, str) and raw_effect_path:
                effects_by_path.setdefault(raw_effect_path, []).append(raw_effect)
    reverse_index = 0
    for begun_path in reversed(begun_paths):
        path_key = str(begun_path)
        is_active = begun_path == active_path
        if is_active is not include_active or path_key in restored:
            continue
        raw_entry = entries_by_path.get(path_key)
        if raw_entry is None:
            continue
        restored.add(path_key)
        raw_path = raw_entry.get("path")
        before_state = raw_entry.get("before_state")
        if not isinstance(raw_path, str) or not isinstance(before_state, dict):
            rollback_errors.append(
                f"Switch backup entry lacks rollback state: {backup_dir.name}"
            )
            continue
        payload = raw_entry.get("payload")
        source = None
        if payload is not None:
            if not isinstance(payload, str):
                rollback_errors.append(
                    f"Switch backup entry has an invalid payload: {backup_dir.name}"
                )
                continue
            source = _contained_payload_path(backup_dir, payload)
        destination = _canonical_destination(Path(raw_path))
        try:
            current = adapter.capture_state(destination)
            if not isinstance(current, dict):
                raise SwitchError(
                    f"Switch rollback current state is invalid: {destination}"
                )
            if _states_match(current, before_state):
                continue
            path_effects = effects_by_path.get(path_key, [])
            effect = path_effects.pop() if path_effects else None
            if not isinstance(effect, dict):
                raise SwitchError(
                    f"Switch rollback has no begun effect evidence: {destination}"
                )
            route_guard = effect.get("route_guard")
            if not isinstance(route_guard, dict):
                raise SwitchError(
                    f"Switch rollback has no route identity: {destination}"
                )
            _validate_route_guard(Path(raw_path), route_guard)
            produced_state = effect.get("observed_after_state")
            if not isinstance(produced_state, dict):
                produced_state = effect.get("action_observed_state")
            if not isinstance(produced_state, dict):
                produced_state = effect.get("planned_after_state")
            if not isinstance(produced_state, dict) or not _states_match(
                current,
                produced_state,
            ):
                raise SwitchError(
                    f"Switch rollback refuses ambiguous external drift: "
                    f"{destination}"
                )
            produced_identity = effect.get("produced_identity")
            if isinstance(produced_identity, dict) and adapter.capture_identity(
                Path(raw_path)
            ) != produced_identity:
                raise SwitchError(
                    f"Switch rollback produced identity changed: {destination}"
                )
            adapter.materialize(
                source,
                destination,
                before_state,
                phase=f"switch_rollback_{reverse_index}",
            )
            durability_phase = f"switch_rollback_{reverse_index}"
            reverse_index += 1
            if not _states_match(adapter.capture_state(destination), before_state):
                raise SwitchError(
                    f"Switch rollback state mismatch: {destination}"
                )
            _make_restore_effect_durable(
                adapter,
                destination,
                before_state,
                phase=durability_phase,
            )
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
    if rollback_errors:
        raise SwitchError("; ".join(rollback_errors))
    return dict(raw_manifest)


def _verify_switch_backup_before_states(
    adapter: FilesystemAdapter,
    backup_dir: Path,
) -> dict[str, object]:
    raw_manifest = read_json(backup_dir / "backup.json")
    if not isinstance(raw_manifest, dict):
        raise SwitchError(f"Switch backup manifest is not an object: {backup_dir.name}")
    entries = raw_manifest.get("entries")
    if not isinstance(entries, list):
        raise SwitchError(f"Switch backup has no entries: {backup_dir.name}")
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise SwitchError(
                f"Switch backup entry is not an object: {backup_dir.name}"
            )
        raw_path = raw_entry.get("path")
        before_state = raw_entry.get("before_state")
        if not isinstance(raw_path, str) or not isinstance(before_state, dict):
            raise SwitchError(
                f"Switch backup entry lacks before-state: {backup_dir.name}"
            )
        path = Path(raw_path).expanduser()
        if not _states_match(adapter.capture_state(path), before_state):
            raise SwitchError(f"Switch target changed after backup: {path}")
    return dict(raw_manifest)


def _prepared_switch_backups(store: Store) -> tuple[tuple[Path, dict[str, object]], ...]:
    if not store.backups_dir.is_dir():
        return tuple()
    pending: list[tuple[Path, dict[str, object]]] = []
    for backup_dir in sorted(store.backups_dir.iterdir(), key=lambda path: path.name):
        if not backup_dir.is_dir():
            continue
        try:
            raw_manifest = read_json(backup_dir / "backup.json")
        except Exception:
            continue
        if not isinstance(raw_manifest, dict):
            continue
        journal = raw_manifest.get("switch_journal")
        if (
            type(raw_manifest.get("schema_version")) is int
            and raw_manifest.get("schema_version") == 2
            and raw_manifest.get("operation") == "switch"
            and raw_manifest.get("lifecycle") == "prepared"
            and isinstance(journal, dict)
            and journal.get("schema_version") == 1
            and journal.get("state") == "prepared"
        ):
            pending.append((backup_dir, _canonical_json_object(raw_manifest)))
    return tuple(pending)


def _switch_recovery_entry_map(
    backup_dir: Path,
    manifest: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise SwitchError(
            f"Prepared switch {backup_dir.name} has no recovery entries"
        )
    entries: dict[str, dict[str, object]] = {}
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} entry {index} is invalid"
            )
        raw_path = raw_entry.get("path")
        before_state = raw_entry.get("before_state")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or not isinstance(before_state, dict)
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} entry {index} is unattested"
            )
        if raw_path in entries:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has duplicate entry paths: "
                f"{raw_path}"
            )
        entries[raw_path] = raw_entry
    return entries


def _switch_created_parent_authority(
    backup_dir: Path,
    entries: Mapping[str, Mapping[str, object]],
) -> set[Path]:
    backups_root = backup_dir.parent.resolve()
    created_parents: set[Path] = set()
    for raw_path, entry in entries.items():
        raw_created_parents = entry.get("created_parent_paths", [])
        if not isinstance(raw_created_parents, list) or not all(
            isinstance(raw_parent, str) and raw_parent
            for raw_parent in raw_created_parents
        ):
            raise SwitchError(
                f"Switch {backup_dir.name} has invalid created-parent evidence: "
                f"{raw_path}"
            )
        before_state = entry.get("before_state")
        if raw_created_parents and (
            not isinstance(before_state, dict)
            or before_state.get("kind") != "missing"
        ):
            raise SwitchError(
                f"Switch {backup_dir.name} has created parents for a "
                f"non-missing entry: {raw_path}"
            )
        destination = _canonical_destination(Path(raw_path))
        expected_parent = destination.parent
        for raw_parent in raw_created_parents:
            lexical_parent = Path(raw_parent)
            parent = _canonical_destination(lexical_parent)
            if (
                parent == parent.parent
                or lexical_parent.resolve() != parent
                or parent == backups_root
                or _is_within(parent, backups_root)
                or parent != expected_parent
                or parent in created_parents
            ):
                raise SwitchError(
                    f"Switch {backup_dir.name} created-parent chain is invalid: "
                    f"{raw_parent}"
                )
            created_parents.add(parent)
            expected_parent = parent.parent
    return created_parents


def _switch_recovery_effects(
    backup_dir: Path,
    manifest: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    journal = manifest.get("switch_journal")
    if not isinstance(journal, dict):
        raise SwitchError(f"Prepared switch {backup_dir.name} has no journal")
    raw_effects = journal.get("effects")
    if not isinstance(raw_effects, list):
        raise SwitchError(f"Prepared switch {backup_dir.name} has no effect list")
    effects: list[dict[str, object]] = []
    for index, raw_effect in enumerate(raw_effects):
        if not isinstance(raw_effect, dict) or raw_effect.get("id") != index:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has an invalid effect order"
            )
        if raw_effect.get("status") not in {"intent", "applied"}:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} effect {index} has an "
                "invalid status"
            )
        if raw_effect.get("kind") not in {"filesystem", "desktop", "finalize"}:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} effect {index} has an "
                "invalid kind"
            )
        effects.append(dict(raw_effect))
    return tuple(effects)


def _preflight_prepared_switch_recovery(
    store: Store,
    backup_dir: Path,
    manifest: Mapping[str, object],
    adapter: FilesystemAdapter,
    entry_map: Mapping[str, dict[str, object]],
    effects: tuple[dict[str, object], ...],
) -> None:
    journal = manifest.get("switch_journal")
    marker_bound = (
        isinstance(journal, dict)
        and journal.get("recovery_marker_required") is True
    )
    allowed_roots, exact_targets = _transaction_destination_authority(store)
    backups_root = store.backups_dir.resolve()
    created_parent_authority = _switch_created_parent_authority(
        backup_dir,
        entry_map,
    )

    def require_allowed(
        raw_path: str,
        *,
        label: str,
        extra_targets: set[Path] | None = None,
    ) -> Path:
        destination = _canonical_destination(Path(raw_path))
        if destination == backups_root or _is_within(destination, backups_root):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} {label} is not approved: "
                f"{raw_path}"
            )
        normally_allowed = destination in exact_targets or any(
            destination == root or _is_within(destination, root)
            for root in allowed_roots
        )
        if not normally_allowed and (
            extra_targets is None or destination not in extra_targets
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} {label} is not approved: "
                f"{raw_path}"
            )
        return destination

    seen_destinations: set[Path] = set()
    for raw_path, entry in entry_map.items():
        destination = require_allowed(raw_path, label="entry path")
        if destination in seen_destinations:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has duplicate destinations: "
                f"{raw_path}"
            )
        seen_destinations.add(destination)
        before_state = _validate_attested_path_state(
            entry.get("before_state"),
            Path(raw_path),
            label=f"Prepared switch {backup_dir.name} entry",
        )
        payload = entry.get("payload")
        if before_state.get("kind") in {"file", "directory"}:
            if not isinstance(payload, str) or not payload:
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} entry is missing payload: "
                    f"{raw_path}"
                )
            payload_path = _contained_payload_path(backup_dir, payload)
            payload_state = adapter.capture_state(payload_path)
            if not isinstance(payload_state, dict) or not _states_match(
                payload_state,
                before_state,
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} payload is missing or "
                    f"corrupt: {payload}"
                )
        elif payload is not None:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} entry has unexpected payload: "
                f"{raw_path}"
            )

    for effect in effects:
        if effect.get("kind") != "filesystem":
            continue
        raw_path = effect.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} filesystem effect has no path"
            )
        effect_destination = require_allowed(
            raw_path,
            label="effect path",
            extra_targets=(
                created_parent_authority
                if effect.get("phase") == "target_home_ensure"
                else None
            ),
        )
        route_guard = effect.get("route_guard")
        if marker_bound and not isinstance(route_guard, dict):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} effect has no route identity: "
                f"{raw_path}"
            )
        if isinstance(route_guard, dict):
            _validate_route_guard(Path(raw_path), route_guard)
        before_state = _validate_attested_path_state(
            effect.get("before_state"),
            Path(raw_path),
            label=f"Prepared switch {backup_dir.name} effect",
        )
        if effect_destination in created_parent_authority and (
            effect.get("phase") != "target_home_ensure"
            or before_state.get("kind") != "missing"
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} created-parent effect is "
                f"invalid: {raw_path}"
            )
        planned_after = effect.get("planned_after_state")
        if isinstance(planned_after, dict) and "kind" in planned_after:
            _validate_attested_path_state(
                planned_after,
                Path(raw_path),
                label=f"Prepared switch {backup_dir.name} planned effect",
            )
        observed_after = effect.get("observed_after_state")
        if isinstance(observed_after, dict) and "kind" in observed_after:
            _validate_attested_path_state(
                observed_after,
                Path(raw_path),
                label=f"Prepared switch {backup_dir.name} observed effect",
            )
        produced_identity = effect.get("produced_identity")
        if produced_identity is not None:
            if (
                not isinstance(produced_identity, dict)
                or produced_identity.get("path") != raw_path
                or produced_identity.get("kind")
                not in {"missing", "file", "directory", "symlink"}
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} effect has an invalid "
                    f"produced identity: {raw_path}"
                )
            if produced_identity.get("kind") != "missing" and (
                type(produced_identity.get("device")) is not int
                or type(produced_identity.get("inode")) is not int
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} effect has an invalid "
                    f"produced inode: {raw_path}"
                )
        staged_path = effect.get("staged_path")
        staged_state = effect.get("staged_state")
        staged_identity = effect.get("staged_identity")
        staged_route_guard = effect.get("staged_route_guard")
        staged_values = (
            staged_path,
            staged_state,
            staged_identity,
            staged_route_guard,
        )
        if any(value is not None for value in staged_values):
            if (
                not isinstance(staged_path, str)
                or not isinstance(staged_state, dict)
                or not isinstance(staged_identity, dict)
                or not isinstance(staged_route_guard, dict)
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has incomplete staged "
                    f"identity for {raw_path}"
                )
            contained_stage = _contained_payload_path(
                backup_dir,
                str(Path(staged_path).relative_to(backup_dir)),
            )
            if contained_stage != Path(staged_path).resolve():
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} staged path is invalid: "
                    f"{staged_path}"
                )
            lexical_stage = Path(staged_path)
            _validate_route_guard(lexical_stage, staged_route_guard)
            if not _states_match(adapter.capture_state(lexical_stage), staged_state):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} staged payload changed: "
                    f"{staged_path}"
                )
            if adapter.capture_identity(lexical_stage) != staged_identity:
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} staged identity changed: "
                    f"{staged_path}"
                )


def _restore_prepared_switch_effect(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    entry_map: Mapping[str, dict[str, object]],
    effect: Mapping[str, object],
    before_state: Mapping[str, object],
    *,
    phase_index: int,
    recovery_source: Path | None = None,
) -> None:
    raw_path = effect.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise SwitchError(
            f"Prepared switch {backup_dir.name} filesystem effect has no path"
        )
    destination = _canonical_destination(Path(raw_path))
    source: Path | None = None
    entry = entry_map.get(raw_path)
    if before_state.get("kind") in {"file", "directory"}:
        if recovery_source is not None:
            source = recovery_source
        else:
            if entry is None:
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has no payload entry for "
                    f"{raw_path}"
                )
            payload = entry.get("payload")
            if not isinstance(payload, str) or not payload:
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has no recovery payload "
                    f"for {raw_path}"
                )
            source = _contained_payload_path(backup_dir, payload)
    adapter.materialize(
        source,
        destination,
        before_state,
        phase=f"switch_recovery_{phase_index}",
    )
    if not _states_match(adapter.capture_state(destination), before_state):
        raise SwitchError(
            f"Prepared switch {backup_dir.name} recovery state mismatch: {raw_path}"
        )
    _make_restore_effect_durable(
        adapter,
        destination,
        before_state,
        phase=f"switch_recovery_{phase_index}",
    )


def _prepared_switch_effect_recovery_source(
    adapter: FilesystemAdapter,
    backup_dir: Path,
    entry_map: Mapping[str, dict[str, object]],
    effects: tuple[dict[str, object], ...],
    effect: Mapping[str, object],
    before_state: Mapping[str, object],
) -> Path | None:
    if before_state.get("kind") not in {"file", "directory"}:
        return None
    raw_path = effect.get("path")
    effect_id = effect.get("id")
    if not isinstance(raw_path, str) or type(effect_id) is not int:
        raise SwitchError(
            f"Prepared switch {backup_dir.name} has an invalid recovery effect"
        )
    entry = entry_map.get(raw_path)
    if isinstance(entry, dict):
        entry_before_state = entry.get("before_state")
        payload = entry.get("payload")
        if (
            isinstance(entry_before_state, dict)
            and _states_match(entry_before_state, before_state)
            and isinstance(payload, str)
            and payload
        ):
            source = _contained_payload_path(backup_dir, payload)
            if _states_match(adapter.capture_state(source), before_state):
                return source
    before_identity = effect.get("before_identity")
    for predecessor in reversed(effects[:effect_id]):
        if predecessor.get("path") != raw_path:
            continue
        predecessor_after = predecessor.get("observed_after_state")
        if not isinstance(predecessor_after, dict):
            predecessor_after = predecessor.get("action_observed_state")
        if not isinstance(predecessor_after, dict):
            predecessor_after = predecessor.get("planned_after_state")
        predecessor_identity = predecessor.get("produced_identity")
        if (
            not isinstance(predecessor_after, dict)
            or not _states_match(predecessor_after, before_state)
            or not isinstance(before_identity, dict)
            or not isinstance(predecessor_identity, dict)
            or not _node_identities_match(
                predecessor_identity,
                before_identity,
            )
        ):
            continue
        staged_path = predecessor.get("staged_path")
        staged_identity = predecessor.get("staged_identity")
        if not isinstance(staged_path, str) or not isinstance(
            staged_identity,
            dict,
        ):
            continue
        source = Path(staged_path)
        if (
            _states_match(adapter.capture_state(source), before_state)
            and _node_identities_match(
                adapter.capture_identity(source),
                staged_identity,
            )
            and _node_identities_match(
                predecessor_identity,
                staged_identity,
            )
        ):
            return source
    raise SwitchError(
        f"Prepared switch {backup_dir.name} has no attested predecessor payload "
        f"for {raw_path}"
    )


def _legacy_noop_desktop_recovery_effect_ids(
    store: Store,
    evidence: _PendingTransactionEvidence,
) -> frozenset[int] | None:
    from codex_switch_home_sync import desktop_global_state_path

    if (
        evidence.operation != "switch"
        or evidence.lifecycle != "rollback_failed"
    ):
        return None
    manifest = evidence.manifest
    journal = evidence.journal
    profile_pair = (
        manifest.get("from_profile"),
        manifest.get("to_profile"),
    )
    if profile_pair == ("internal", "openai-official"):
        target = desktop_global_state_path(store.official_codex_home)
    elif profile_pair == ("openai-official", "internal"):
        target = desktop_global_state_path(store.internal_codex_home)
    else:
        return None
    target_path = str(target)
    if (
        journal.get("state") != "rollback_failed"
        or manifest.get("failure")
        != (
            "Required switch input changed after "
            "desktop_global_state_sync action: Desktop global-state "
            f"target: {target}"
        )
        or manifest.get("rollback_failure")
        != f"Switch rollback refuses ambiguous external drift: {target}"
    ):
        return None

    raw_effects = journal.get("effects")
    if not isinstance(raw_effects, list) or not raw_effects:
        return None
    if any(
        not isinstance(effect, dict)
        or effect.get("id") != index
        or effect.get("recovery_state") != "rollback_failed"
        for index, effect in enumerate(raw_effects)
    ):
        return None
    matching_effects = [
        effect
        for effect in raw_effects
        if isinstance(effect, dict)
        and effect.get("phase") == "desktop_global_state_sync"
    ]
    if len(matching_effects) != 1:
        return None
    effect = matching_effects[0]
    effect_id = effect.get("id")
    if (
        type(effect_id) is not int
        or effect_id != len(raw_effects) - 1
        or effect.get("kind") != "filesystem"
        or effect.get("status") != "intent"
        or effect.get("path") != target_path
        or any(
            other.get("path") == target_path
            for other in raw_effects
            if isinstance(other, dict) and other is not effect
        )
        or any(
            key in effect
            for key in (
                "action_observed_state",
                "staged_identity",
                "staged_path",
                "staged_route_guard",
                "staged_state",
            )
        )
    ):
        return None
    before_state = effect.get("before_state")
    planned_state = effect.get("planned_after_state")
    observed_state = effect.get("observed_after_state")
    before_identity = effect.get("before_identity")
    produced_identity = effect.get("produced_identity")
    if (
        not isinstance(before_state, dict)
        or before_state.get("kind") != "file"
        or before_state.get("path") != target_path
        or not isinstance(planned_state, dict)
        or not isinstance(observed_state, dict)
        or _immutable_state_bytes(before_state)
        != _immutable_state_bytes(planned_state)
        or _immutable_state_bytes(before_state)
        != _immutable_state_bytes(observed_state)
        or not isinstance(before_identity, dict)
        or before_identity.get("kind") != "file"
        or before_identity.get("path") != target_path
        or not isinstance(produced_identity, dict)
        or _immutable_state_bytes(before_identity)
        != _immutable_state_bytes(produced_identity)
        or not isinstance(effect.get("route_guard"), dict)
    ):
        return None

    raw_frozen_inputs = journal.get("frozen_inputs")
    if not isinstance(raw_frozen_inputs, list):
        return None
    matching_frozen = [
        item
        for item in raw_frozen_inputs
        if isinstance(item, dict) and item.get("path") == target_path
    ]
    if len(matching_frozen) != 1:
        return None
    frozen = matching_frozen[0]
    frozen_before = frozen.get("before_state")
    frozen_commit = frozen.get("commit_state")
    frozen_identity = frozen.get("before_identity")
    if (
        frozen.get("label") != "Desktop global-state target"
        or frozen.get("capture_kind") != "path"
        or frozen.get("commit_replaces_identity") is not True
        or not isinstance(frozen_before, dict)
        or not isinstance(frozen_commit, dict)
        or not isinstance(frozen_identity, dict)
        or _immutable_state_bytes(frozen_before)
        != _immutable_state_bytes(before_state)
        or _immutable_state_bytes(frozen_commit)
        != _immutable_state_bytes(before_state)
        or _immutable_state_bytes(frozen_identity)
        != _immutable_state_bytes(before_identity)
    ):
        return None

    try:
        entry_map = _switch_recovery_entry_map(
            evidence.backup_dir,
            manifest,
        )
    except SwitchError:
        return None
    target_entry = entry_map.get(target_path)
    if not isinstance(target_entry, dict):
        return None
    entry_before = target_entry.get("before_state")
    if (
        not isinstance(entry_before, dict)
        or _immutable_state_bytes(entry_before)
        != _immutable_state_bytes(before_state)
    ):
        return None

    noop_effects_by_path: dict[str, set[int]] = {}
    paths_with_real_effects: set[str] = set()
    for candidate in raw_effects:
        if candidate is effect or candidate.get("kind") != "filesystem":
            continue
        candidate_path = candidate.get("path")
        candidate_id = candidate.get("id")
        if not isinstance(candidate_path, str) or type(candidate_id) is not int:
            return None
        candidate_entry = entry_map.get(candidate_path)
        candidate_before = candidate.get("before_state")
        candidate_planned = candidate.get("planned_after_state")
        candidate_observed = candidate.get("observed_after_state")
        candidate_before_identity = candidate.get("before_identity")
        candidate_produced_identity = candidate.get("produced_identity")
        candidate_route_guard = candidate.get("route_guard")
        candidate_action_observed = candidate.get("action_observed_state")
        candidate_staged_path = candidate.get("staged_path")
        candidate_staged_state = candidate.get("staged_state")
        candidate_staged_identity = candidate.get("staged_identity")
        candidate_staged_route_guard = candidate.get("staged_route_guard")
        candidate_entry_before = (
            candidate_entry.get("before_state")
            if isinstance(candidate_entry, dict)
            else None
        )
        exact_noop = (
            candidate.get("status") == "applied"
            and candidate.get("recovery_state") == "rollback_failed"
            and isinstance(candidate_before, dict)
            and isinstance(candidate_planned, dict)
            and isinstance(candidate_observed, dict)
            and _immutable_state_bytes(candidate_before)
            == _immutable_state_bytes(candidate_planned)
            and _immutable_state_bytes(candidate_before)
            == _immutable_state_bytes(candidate_observed)
            and (
                candidate_action_observed is None
                or (
                    isinstance(candidate_action_observed, dict)
                    and _immutable_state_bytes(candidate_before)
                    == _immutable_state_bytes(candidate_action_observed)
                )
            )
            and isinstance(candidate_before_identity, dict)
            and isinstance(candidate_produced_identity, dict)
            and candidate_before_identity.get("path") == candidate_path
            and candidate_produced_identity.get("path") == candidate_path
            and candidate_before_identity.get("kind")
            == candidate_before.get("kind")
            and candidate_produced_identity.get("kind")
            == candidate_before.get("kind")
            and isinstance(candidate_route_guard, dict)
            and isinstance(candidate_entry_before, dict)
            and _immutable_state_bytes(candidate_entry_before)
            == _immutable_state_bytes(candidate_before)
            and (
                candidate_staged_path is None
                and candidate_staged_state is None
                and candidate_staged_identity is None
                and candidate_staged_route_guard is None
                or (
                    isinstance(candidate_staged_path, str)
                    and isinstance(candidate_staged_state, dict)
                    and _immutable_state_bytes(candidate_staged_state)
                    == _immutable_state_bytes(candidate_before)
                    and isinstance(candidate_staged_identity, dict)
                    and isinstance(candidate_staged_route_guard, dict)
                )
            )
        )
        if exact_noop:
            noop_effects_by_path.setdefault(candidate_path, set()).add(
                candidate_id
            )
        else:
            paths_with_real_effects.add(candidate_path)

    ignored_effect_ids = {effect_id}
    for candidate_path, candidate_ids in noop_effects_by_path.items():
        if candidate_path not in paths_with_real_effects:
            ignored_effect_ids.update(candidate_ids)
    return frozenset(ignored_effect_ids)


def _recover_prepared_switch(
    store: Store,
    backup_dir: Path,
    manifest: dict[str, object],
    adapter: FilesystemAdapter,
    desktop_adapter: object | None,
    *,
    ignored_effect_ids: frozenset[int] = frozenset(),
) -> None:
    entry_map = _switch_recovery_entry_map(backup_dir, manifest)
    effects = _switch_recovery_effects(backup_dir, manifest)
    effect_ids = {
        effect_id
        for effect in effects
        if type(effect_id := effect.get("id")) is int
    }
    if not ignored_effect_ids.issubset(effect_ids):
        raise SwitchError(
            f"Prepared switch {backup_dir.name} has invalid ignored effects"
        )
    _preflight_prepared_switch_recovery(
        store,
        backup_dir,
        manifest,
        adapter,
        entry_map,
        effects,
    )
    journal = manifest.get("switch_journal")
    if not isinstance(journal, dict):
        raise SwitchError(f"Prepared switch {backup_dir.name} has no journal")
    marker_bound = journal.get("recovery_marker_required") is True
    raw_desktop_before = journal.get("desktop_before")
    desktop_effects = [
        effect for effect in effects if effect.get("kind") == "desktop"
    ]
    desktop_needs_reconcile = False
    desktop_before_observation: object | None = None
    if desktop_effects:
        if not isinstance(raw_desktop_before, dict):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has no Desktop observation"
            )
        gui_env = raw_desktop_before.get("gui_env")
        service_loaded = raw_desktop_before.get("service_loaded")
        if (gui_env is not None and not isinstance(gui_env, str)) or not isinstance(
            service_loaded,
            bool,
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has an invalid Desktop "
                "observation"
            )
        if desktop_adapter is None:
            raise SwitchError(
                f"Prepared switch {backup_dir.name} requires Desktop recovery"
            )
        observe_desktop = getattr(desktop_adapter, "observe", None)
        reconcile_desktop = getattr(desktop_adapter, "reconcile", None)
        if not callable(observe_desktop) or not callable(reconcile_desktop):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} requires a Desktop recovery "
                "adapter"
            )
        current_observation = observe_desktop(skip_launchctl=False)
        current_desktop_state = {
            "gui_env": getattr(current_observation, "gui_env", None),
            "service_loaded": getattr(
                current_observation,
                "service_loaded",
                None,
            ),
        }
        simulated_desktop_state = current_desktop_state
        for effect in reversed(desktop_effects):
            before_state = effect.get("before_state")
            after_state = effect.get("observed_after_state")
            if not isinstance(after_state, dict):
                after_state = effect.get("planned_after_state")
            if not isinstance(before_state, dict) or not isinstance(
                after_state,
                dict,
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has an unattested Desktop "
                    "effect"
                )
            if _switch_effect_states_match(simulated_desktop_state, before_state):
                simulated_desktop_state = dict(before_state)
                continue
            if not _switch_effect_states_match(simulated_desktop_state, after_state):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has ambiguous Desktop "
                    "state; recovery is required before retry"
                )
            desktop_needs_reconcile = True
            simulated_desktop_state = dict(before_state)
        if not _switch_effect_states_match(
            simulated_desktop_state,
            raw_desktop_before,
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} Desktop recovery chain is "
                "incomplete"
            )
        from codex_switch_launch import _DesktopBindingObservation

        desktop_before_observation = _DesktopBindingObservation(
            gui_env=gui_env,
            service_loaded=service_loaded,
        )
    filesystem_effects = [
        effect
        for effect in effects
        if effect.get("kind") == "filesystem"
        and effect.get("phase") != "target_home_ensure"
        and effect.get("id") not in ignored_effect_ids
    ]
    restore_actions: list[
        tuple[dict[str, object], dict[str, object], Path | None]
    ] = []
    simulated_states: dict[str, dict[str, object]] = {}
    simulated_identities: dict[str, dict[str, object]] = {}
    for effect in reversed(filesystem_effects):
        raw_path = effect.get("path")
        before_state = effect.get("before_state")
        if not isinstance(raw_path, str) or not isinstance(before_state, dict):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has an unattested filesystem "
                "effect"
            )
        current = simulated_states.get(raw_path)
        current_identity = simulated_identities.get(raw_path)
        if current is None:
            captured = adapter.capture_state(Path(raw_path))
            captured_identity = adapter.capture_identity(Path(raw_path))
            if not isinstance(captured, dict):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} recovery state is invalid: "
                    f"{raw_path}"
                )
            current = captured
            current_identity = captured_identity
        planned_after_state = effect.get("observed_after_state")
        if not isinstance(planned_after_state, dict):
            planned_after_state = effect.get("action_observed_state")
        if not isinstance(planned_after_state, dict):
            planned_after_state = effect.get("planned_after_state")
        if _states_match(current, before_state):
            simulated_states[raw_path] = dict(before_state)
            if isinstance(current_identity, dict):
                simulated_identities[raw_path] = dict(current_identity)
            continue
        if not isinstance(planned_after_state, dict) or not _states_match(
            current,
            planned_after_state,
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has ambiguous effect state for "
                f"{raw_path}; recovery is required before retry"
            )
        produced_identity = effect.get("produced_identity")
        if marker_bound:
            if not isinstance(produced_identity, dict):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has no produced identity "
                    f"for {raw_path}"
                )
            if (
                not isinstance(current_identity, dict)
                or current_identity != produced_identity
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} produced identity changed "
                    f"for {raw_path}"
                )
        recovery_source = _prepared_switch_effect_recovery_source(
            adapter,
            backup_dir,
            entry_map,
            effects,
            effect,
            before_state,
        )
        restore_actions.append((effect, dict(before_state), recovery_source))
        simulated_states[raw_path] = dict(before_state)
        before_identity = effect.get("before_identity")
        if isinstance(before_identity, dict):
            simulated_identities[raw_path] = dict(before_identity)
        elif isinstance(current_identity, dict):
            simulated_identities[raw_path] = dict(current_identity)

    active_actions = [
        action
        for action in restore_actions
        if action[0].get("path") == str(store.active_path)
    ]
    ordinary_actions = [
        action
        for action in restore_actions
        if action[0].get("path") != str(store.active_path)
    ]
    ensure_effects = [
        effect
        for effect in reversed(effects)
        if effect.get("kind") == "filesystem"
        and effect.get("phase") == "target_home_ensure"
        and effect.get("id") not in ignored_effect_ids
    ]
    ensure_actions: list[tuple[dict[str, object], dict[str, object]]] = []
    for effect in ensure_effects:
        raw_path = effect.get("path")
        before_state = effect.get("before_state")
        if not isinstance(raw_path, str) or not isinstance(before_state, dict):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has an invalid directory effect"
            )
        current = adapter.capture_state(Path(raw_path))
        if _states_match(current, before_state):
            ensure_actions.append((effect, dict(before_state)))
            continue
        observed_after = effect.get("observed_after_state")
        if not isinstance(observed_after, dict):
            observed_after = effect.get("action_observed_state")
        if (
            not isinstance(observed_after, dict)
            or observed_after.get("kind") != "directory"
            or not isinstance(current, dict)
            or current.get("kind") != "directory"
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has ambiguous directory state "
                f"for {raw_path}; recovery is required before retry"
            )
        produced_identity = effect.get("produced_identity")
        if marker_bound and (
            not isinstance(produced_identity, dict)
            or adapter.capture_identity(Path(raw_path)) != produced_identity
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} directory identity changed: "
                f"{raw_path}"
            )
        ensure_actions.append((effect, dict(before_state)))

    for phase_index, (
        effect,
        before_state,
        recovery_source,
    ) in enumerate(ordinary_actions):
        _restore_prepared_switch_effect(
            adapter,
            backup_dir,
            entry_map,
            effect,
            before_state,
            phase_index=phase_index,
            recovery_source=recovery_source,
        )

    for ensure_index, (effect, before_state) in enumerate(ensure_actions):
        raw_path = effect.get("path")
        if not isinstance(raw_path, str):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has an invalid directory path"
            )
        current = adapter.capture_state(Path(raw_path))
        if _states_match(current, before_state):
            continue
        observed_after = effect.get("observed_after_state")
        if not isinstance(observed_after, dict):
            observed_after = effect.get("action_observed_state")
        if not isinstance(observed_after, dict) or not _states_match(
            current,
            observed_after,
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has ambiguous directory state "
                f"for {raw_path}; recovery is required before retry"
            )
        produced_identity = effect.get("produced_identity")
        if marker_bound and (
            not isinstance(produced_identity, dict)
            or adapter.capture_identity(Path(raw_path)) != produced_identity
        ):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} directory identity changed: "
                f"{raw_path}"
            )
        if before_state.get("kind") == "directory":
            mode = before_state.get("mode")
            if not isinstance(mode, int):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} directory mode is invalid: "
                    f"{raw_path}"
                )
            adapter.chmod(
                Path(raw_path),
                mode,
                phase=f"switch_recovery_directory_{ensure_index}",
            )
            if not _states_match(
                adapter.capture_state(Path(raw_path)),
                before_state,
            ):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} directory recovery state "
                    f"mismatch: {raw_path}"
                )
            _make_restore_effect_durable(
                adapter,
                Path(raw_path),
                before_state,
                phase=f"switch_recovery_directory_{ensure_index}",
            )
        else:
            _restore_prepared_switch_effect(
                adapter,
                backup_dir,
                entry_map,
                effect,
                before_state,
                phase_index=len(ordinary_actions) + ensure_index,
            )

    if desktop_needs_reconcile:
        reconcile_desktop = getattr(desktop_adapter, "reconcile")
        reconcile_desktop(
            desktop_before_observation,
            skip_launchctl=False,
        )

    for active_index, (
        effect,
        before_state,
        recovery_source,
    ) in enumerate(active_actions):
        _restore_prepared_switch_effect(
            adapter,
            backup_dir,
            entry_map,
            effect,
            before_state,
            phase_index=len(ordinary_actions) + len(ensure_effects) + active_index,
            recovery_source=recovery_source,
        )

    recovered = _canonical_json_object(manifest)
    recovered["lifecycle"] = "rolled_back"
    recovered["rolled_back_at"] = now_stamp()
    recovered["recovered_at"] = now_stamp()
    recovered_journal = recovered.get("switch_journal")
    if not isinstance(recovered_journal, dict):
        raise SwitchError(f"Prepared switch {backup_dir.name} has no journal")
    if ignored_effect_ids:
        recovered["recovery_note"] = (
            "legacy Desktop global-state no-op ownership ignored"
        )
        recovered_effects = recovered_journal.get("effects")
        if not isinstance(recovered_effects, list):
            raise SwitchError(
                f"Prepared switch {backup_dir.name} has no recovery effects"
            )
        for effect_id in ignored_effect_ids:
            recovered_effect = recovered_effects[effect_id]
            if not isinstance(recovered_effect, dict):
                raise SwitchError(
                    f"Prepared switch {backup_dir.name} has an invalid ignored "
                    "effect"
                )
            recovered_effect["recovery_action"] = (
                "preserved App-owned state from legacy no-op"
            )
    recovered_journal["state"] = "recovered"
    _set_switch_effect_recovery_state(recovered_journal, "recovered")
    adapter.write_manifest(
        backup_dir / "backup.json",
        recovered,
        phase="switch_recovery_finalize",
    )
    adapter.sync_file(
        backup_dir / "backup.json",
        phase="switch_recovery_terminal_manifest",
    )
    adapter.sync_directory(
        backup_dir,
        phase="switch_recovery_terminal_backup",
    )


def _read_strict_active_record(
    store: Store,
) -> tuple[dict[str, object], str | None, Path | None]:
    from codex_switch_selection import active_profile_selection

    if not store.active_path.exists():
        return {}, None, None
    raw_active = read_json(store.active_path)
    if not isinstance(raw_active, dict):
        raise SwitchError(f"active record must be an object: {store.active_path}")
    active = dict(raw_active)
    profile = active.get("profile")
    if not isinstance(profile, str) or not profile:
        raise SwitchError(
            f"active record has an invalid required profile: {store.active_path}"
        )
    profile = active_profile_selection(active).cli_profile
    raw_home = active.get("codex_home") or active.get("live_codex_home")
    if not isinstance(raw_home, str) or not raw_home:
        raise SwitchError(
            f"active record has an invalid required home: {store.active_path}"
        )
    home = Path(raw_home).expanduser()
    if not home.is_absolute():
        raise SwitchError(
            f"active record home must be absolute: {store.active_path}: {raw_home}"
        )
    return active, profile, home


_SPLIT_STOPPED_APP_REQUIREMENT = (
    "fully quit ChatGPT/Codex App and keep it closed until the switch completes"
)


@dataclass(frozen=True)
class _SplitAppRuntimeObservation:
    running: bool
    compatible: bool


@dataclass(frozen=True)
class _SplitAppEffectPlan:
    action: str

    @property
    def requires_rebind(self) -> bool:
        return self.action == "rebind"


def _default_split_app_runtime_observation(
    store: Store,
    selection: object,
    expected_app_cli: Path,
    desktop_observation: object,
) -> _SplitAppRuntimeObservation:
    del selection
    from codex_switch_io import run_quiet
    from codex_switch_paths import equivalent_paths
    from codex_switch_running_app import (
        app_server_matches_expected_cli,
        is_default_desktop_context,
        running_codex_processes,
    )

    if not is_default_desktop_context(store):
        return _SplitAppRuntimeObservation(running=False, compatible=True)

    code, output = run_quiet(["/bin/ps", "-axo", "pid=,ppid=,args="])
    if code != 0:
        raise OSError("process inventory unavailable")
    processes = tuple(
        process
        for process in running_codex_processes(process_output=output)
        if getattr(process, "kind", "") in {"desktop", "app-server"}
    )
    gui_env = getattr(desktop_observation, "gui_env", None)
    compatible = True
    for process in processes:
        kind = getattr(process, "kind", "")
        if kind == "desktop":
            if getattr(process, "host_kind", "") == "legacy-codex":
                compatible = False
                continue
            process_app_cli = str(getattr(process, "app_cli_env", "") or "")
            observed_app_cli = process_app_cli or str(gui_env or "")
            if not equivalent_paths(observed_app_cli, str(expected_app_cli)):
                compatible = False
        elif not app_server_matches_expected_cli(
            process,
            str(expected_app_cli),
        ):
            compatible = False
    return _SplitAppRuntimeObservation(
        running=bool(processes),
        compatible=compatible,
    )


def _observe_split_app_runtime(
    store: Store,
    request: TransactionRequest,
    selection: object,
    expected_app_cli: Path,
    desktop_observation: object,
) -> _SplitAppRuntimeObservation:
    callback = request.options.get("split_app_is_running")
    if callback is not None:
        if not callable(callback):
            raise SwitchError("Switch split_app_is_running must be callable")
        running = callback(store, selection)
        if type(running) is not bool:
            raise OSError("process inventory returned an invalid result")
        return _SplitAppRuntimeObservation(
            running=running,
            compatible=True,
        )
    return _default_split_app_runtime_observation(
        store,
        selection,
        expected_app_cli,
        desktop_observation,
    )


def _split_static_app_binding_is_compatible(
    store: Store,
    active: Mapping[str, object],
    selection: object,
    expected_app_cli: Path,
    desktop_observation: object,
    launch_agent_payload_bytes: bytes,
) -> bool:
    from codex_switch_launch import launch_agent_payload
    from codex_switch_paths import equivalent_paths
    from codex_switch_selection import active_profile_selection

    try:
        active_selection = active_profile_selection(active)
    except SwitchError:
        return False
    active_app_cli = active.get("app_cli_path")
    gui_env = getattr(desktop_observation, "gui_env", None)
    service_loaded = getattr(desktop_observation, "service_loaded", None)
    return bool(
        getattr(selection, "app_profile", None) == "openai-official"
        and active_selection.app_profile == "openai-official"
        and isinstance(active_app_cli, str)
        and equivalent_paths(active_app_cli, str(expected_app_cli))
        and isinstance(gui_env, str)
        and equivalent_paths(gui_env, str(expected_app_cli))
        and service_loaded is True
        and launch_agent_payload_bytes
        == launch_agent_payload(store.launch_agent_label, expected_app_cli)
    )


def _derive_split_app_effect_plan(
    store: Store,
    request: TransactionRequest,
    selection: object,
    active: Mapping[str, object],
    expected_app_cli: Path,
    desktop_observation: object,
    launch_agent_payload_bytes: bytes,
    *,
    dry_run: bool,
) -> _SplitAppEffectPlan:
    static_compatible = _split_static_app_binding_is_compatible(
        store,
        active,
        selection,
        expected_app_cli,
        desktop_observation,
        launch_agent_payload_bytes,
    )
    if dry_run:
        return _SplitAppEffectPlan(
            action="preserve" if static_compatible else "rebind"
        )
    try:
        runtime = _observe_split_app_runtime(
            store,
            request,
            selection,
            expected_app_cli,
            desktop_observation,
        )
    except Exception as exc:
        raise SwitchError(
            "Unable to prove ChatGPT/Codex App is stopped; "
            f"{_SPLIT_STOPPED_APP_REQUIREMENT}"
        ) from exc
    if static_compatible and runtime.compatible:
        return _SplitAppEffectPlan(action="preserve")
    if runtime.running:
        raise SwitchError(
            "Supported split requires you to "
            f"{_SPLIT_STOPPED_APP_REQUIREMENT}"
        )
    return _SplitAppEffectPlan(action="rebind")


def _execute_switch(
    store: Store,
    request: TransactionRequest,
    *,
    dry_run: bool,
) -> TransactionReceipt:
    from codex_switch_config import (
        build_base_config_text,
        build_profile_v2_config_text,
        config_uses_file_auth,
        merge_missing_non_usage_shared_config_defaults,
        merge_preserved_shared_config_blocks,
    )
    from codex_switch_home_select import resolve_independent_homes
    from codex_switch_home_sync import (
        annotate_canonical_profile_config,
        annotate_plugin_support_snapshot,
        absolute_symlink_target,
        build_internal_home_config,
        build_official_home_config_from_internal,
        build_preserved_shared_config_text_from_text,
        build_profile_seed_config_text,
        desktop_global_state_path,
        desktop_global_state_settings_subset,
        merge_desktop_global_state_settings,
        path_is_within,
        plugin_support_snapshot_name,
        read_json_object_if_valid,
        shareable_symlink_rejection_reason,
        shared_support_entries,
        should_remove_rejected_shareable_target,
        stale_runtime_links,
        sync_desktop_global_state_settings,
    )
    from codex_switch_app_wrapper import (
        managed_profile_app_cli_path,
        should_refresh_profile_app_wrapper,
        write_profile_app_wrapper,
    )
    from codex_switch_launch import launch_agent_payload, validate_executable_path
    from codex_switch_plan import (
        resolve_base_config_path,
        transaction_switch_preview_lines,
    )
    from codex_switch_record import active_record
    from codex_switch_runtime_binding import require_internal_app_readiness
    from codex_switch_selection import requested_profile_selection
    from codex_switch_restore import create_switch_backup, finalize_backup
    from codex_switch_shell import (
        replace_managed_block,
        shell_cli_bootstrap_block,
        shell_cli_bootstrap_path,
    )

    if request.profile not in {"internal", "openai-official"}:
        raise SwitchError(f"Unsupported transactional profile: {request.profile}")
    config_mode = _switch_optional_string_option(request, "config_mode") or "shared"
    shared_config_base = _switch_optional_string_option(
        request,
        "shared_config_base",
    )
    clear_missing_auth = _switch_bool_option(
        request,
        "clear_missing_auth",
        default=False,
    )
    skip_shim = _switch_bool_option(request, "skip_shim", default=False)
    skip_app_cli = _switch_bool_option(request, "skip_app_cli", default=False)
    selection = requested_profile_selection(
        request.profile,
        _switch_optional_string_option(request, "app_profile"),
        skip_app_cli=skip_app_cli,
    )
    app_profile = selection.app_profile
    skip_launchctl = _switch_bool_option(
        request,
        "skip_launchctl",
        default=False,
    )
    progress_callback = request.options.get("progress_callback")
    if progress_callback is not None and not callable(progress_callback):
        raise SwitchError("Switch progress_callback must be callable")

    filesystem_adapter = _switch_filesystem_adapter(request)
    planning_inputs = _SwitchPlanningInputTracker(filesystem_adapter)
    manifest_observations = planning_inputs.begin(
        (
            (store.manifest_path(request.profile), "target manifest"),
            (store.manifest_path("internal"), "internal manifest"),
            (
                store.manifest_path("openai-official"),
                "official manifest",
            ),
        )
    )
    manifest = store.load_manifest(request.profile)
    internal_manifest = store.load_manifest("internal")
    official_manifest = store.load_manifest("openai-official")
    app_manifest = (
        internal_manifest if app_profile == "internal" else official_manifest
    )
    planning_inputs.finish(manifest_observations)
    if app_profile == "internal":
        require_internal_app_readiness(app_manifest)
    canonical_app_cli_path = _switch_optional_string_option(
        request,
        "canonical_app_cli_path",
    )
    raw_codex_bin = (
        canonical_app_cli_path
        if request.profile == "openai-official"
        and canonical_app_cli_path is not None
        else str(manifest.get("codex_bin", ""))
    )
    codex_bin_path = Path(raw_codex_bin).expanduser()
    codex_bin_alias_observations = (
        planning_inputs.begin(((codex_bin_path, "codex_bin alias"),))
        if codex_bin_path.is_absolute()
        else tuple()
    )
    if request.profile == "internal":
        codex_bin = resolve_internal_codex_bin(raw_codex_bin)
    else:
        codex_bin = str(
            validate_executable_path(
                raw_codex_bin,
                "codex_bin",
            )
        )
    canonical_codex_bin_path = Path(codex_bin)
    canonical_codex_bin_observations = (
        planning_inputs.begin(
            ((canonical_codex_bin_path, "canonical codex_bin"),)
        )
        if canonical_codex_bin_path != codex_bin_path
        else tuple()
    )
    planning_inputs.finish(
        (*codex_bin_alias_observations, *canonical_codex_bin_observations)
    )
    raw_manifest_app_cli_path = str(app_manifest.get("app_cli_path", ""))
    raw_manifest_app_cli = Path(raw_manifest_app_cli_path).expanduser()
    expected_internal_wrapper = managed_profile_app_cli_path(store, "internal")
    if canonical_app_cli_path is not None:
        canonical_app_cli = Path(canonical_app_cli_path).expanduser()
        if (
            app_profile == "internal"
            and canonical_app_cli != expected_internal_wrapper
        ):
            raise SwitchError(
                "Canonical internal Desktop CLI must be the managed launcher"
            )
        canonical_app_cli_observations = (
            planning_inputs.begin(((canonical_app_cli, "canonical app_cli_path"),))
            if canonical_app_cli.exists()
            else tuple()
        )
        if app_profile == "internal" and not canonical_app_cli.exists():
            manifest_app_cli_path = str(canonical_app_cli)
        else:
            manifest_app_cli_path = str(
                validate_executable_path(
                    str(canonical_app_cli),
                    "canonical app_cli_path",
                )
            )
        planning_inputs.finish(canonical_app_cli_observations)
    else:
        app_cli_observations = (
            planning_inputs.begin(((raw_manifest_app_cli, "app_cli_path"),))
            if raw_manifest_app_cli.is_absolute()
            else tuple()
        )
        planned_missing_internal_wrapper = (
            app_profile == "internal"
            and config_mode == "shared"
            and not skip_app_cli
            and raw_manifest_app_cli.is_absolute()
            and raw_manifest_app_cli == expected_internal_wrapper
            and not raw_manifest_app_cli.exists()
        )
        if planned_missing_internal_wrapper:
            manifest_app_cli_path = str(raw_manifest_app_cli)
        else:
            manifest_app_cli_path = str(
                validate_executable_path(
                    raw_manifest_app_cli_path,
                    "app_cli_path",
                )
            )
        planning_inputs.finish(app_cli_observations)
    app_cli_path = manifest_app_cli_path
    active_observations = planning_inputs.begin(
        ((store.active_path, "active collision input"),)
    )
    active, active_profile, active_home = _read_strict_active_record(store)
    planning_inputs.finish(active_observations)
    homes = resolve_independent_homes(
        store,
        internal_manifest=internal_manifest,
        official_manifest=official_manifest,
        target_profile=request.profile,
        dry_run=dry_run,
        active_profile=active_profile,
        active_home=active_home,
    )
    if (
        request.profile == "internal"
        and str(internal_manifest.get("codex_bin", "")) != codex_bin
    ):
        updated_internal_manifest = dict(
            homes.manifest_updates.get("internal", internal_manifest)
        )
        updated_internal_manifest["codex_bin"] = codex_bin
        updated_internal_manifest["updated_at"] = now_stamp()
        homes.manifest_updates["internal"] = updated_internal_manifest
    target_binding = (
        homes.internal if request.profile == "internal" else homes.official
    )
    target_home = target_binding.path
    app_binding = homes.internal if app_profile == "internal" else homes.official
    app_home = app_binding.path
    if (
        app_profile == "openai-official"
        and canonical_app_cli_path is not None
    ):
        updated_official_manifest = dict(
            homes.manifest_updates.get(
                "openai-official",
                official_manifest,
            )
        )
        updated_official_manifest["codex_bin"] = manifest_app_cli_path
        updated_official_manifest["app_cli_path"] = manifest_app_cli_path
        updated_official_manifest["runtime_binding"] = "canonical"
        updated_official_manifest.setdefault(
            "app_cli_binding",
            "launchagent",
        )
        updated_official_manifest["updated_at"] = now_stamp()
        homes.manifest_updates["openai-official"] = (
            updated_official_manifest
        )
    desktop_adapter = None
    desktop_observation = None
    app_effect_plan = _SplitAppEffectPlan(action="rebind")
    if selection.is_split and not skip_app_cli:
        launch_agent_observations = planning_inputs.begin(
            ((store.launch_agent_path, "current App LaunchAgent binding"),)
        )
        try:
            current_launch_agent_payload = store.launch_agent_path.read_bytes()
        except OSError:
            current_launch_agent_payload = b""
        desktop_adapter = _switch_desktop_binding_adapter(store, request)
        desktop_observation = desktop_adapter.observe(
            skip_launchctl=skip_launchctl
        )
        app_effect_plan = _derive_split_app_effect_plan(
            store,
            request,
            selection,
            active,
            Path(app_cli_path),
            desktop_observation,
            current_launch_agent_payload,
            dry_run=dry_run,
        )
        planning_inputs.finish(launch_agent_observations)
    app_effect_requires_rebind = (
        not skip_app_cli and app_effect_plan.requires_rebind
    )
    created_target_directories = _missing_parent_paths(
        target_home / ".codex-switch-target"
    )
    target_directories_to_ensure = tuple(reversed(created_target_directories))
    target_directory_before_states = {
        directory: filesystem_adapter.capture_state(directory)
        for directory in target_directories_to_ensure
    }
    target_directory_planned_states = {
        directory: _expected_directory_ensure_state(
            directory,
            target_directory_before_states[directory],
        )
        for directory in target_directories_to_ensure
    }
    profile_dir = store.profile_dir(request.profile)
    config_path = profile_dir / "config.toml"
    auth_path = profile_dir / "auth.json"
    target_config_path = target_home / "config.toml"
    target_profile_config_path = target_home / f"{request.profile}.config.toml"
    target_auth_path = target_home / "auth.json"
    base_config_candidate = (
        store.profile_dir(shared_config_base) / "config.toml"
        if config_mode == "shared" and shared_config_base
        else store.live_codex_home / "config.toml"
    )
    base_resolution_specs: list[tuple[Path, str]] = [
        (base_config_candidate, "base config resolution input")
    ]
    if config_mode == "shared" and shared_config_base:
        base_resolution_specs.append(
            (
                store.manifest_path(shared_config_base),
                "shared base manifest",
            )
        )
    base_resolution_observations = planning_inputs.begin(
        tuple(base_resolution_specs)
    )
    base_config_path = resolve_base_config_path(
        store,
        config_mode,
        shared_config_base,
    )
    planning_inputs.finish(base_resolution_observations)
    plugin_snapshot_paths: tuple[Path, ...] = (
        (
            target_home / plugin_support_snapshot_name(request.profile),
            profile_dir / plugin_support_snapshot_name(request.profile),
        )
        if config_mode == "shared"
        else tuple()
    )
    if config_mode == "shared" and request.profile == "internal":
        composite_builder_paths = (
            target_home / "internal.config.toml",
            homes.official.path / "internal.config.toml",
            target_home / plugin_support_snapshot_name("internal"),
            homes.official.path / plugin_support_snapshot_name("internal"),
            profile_dir / plugin_support_snapshot_name("internal"),
            homes.official.path
            / plugin_support_snapshot_name("openai-official"),
            store.profile_dir("openai-official")
            / plugin_support_snapshot_name("openai-official"),
        )
    elif config_mode == "shared":
        composite_builder_paths = (
            homes.official.path / "openai-official.config.toml",
            homes.internal.path / "openai-official.config.toml",
            homes.official.path
            / plugin_support_snapshot_name("openai-official"),
            homes.internal.path
            / plugin_support_snapshot_name("openai-official"),
            profile_dir / plugin_support_snapshot_name("openai-official"),
            homes.internal.path / plugin_support_snapshot_name("internal"),
            store.profile_dir("internal")
            / plugin_support_snapshot_name("internal"),
        )
    else:
        composite_builder_paths = tuple()
    config_observations = planning_inputs.begin(
        tuple(
            [
                (config_path, "profile config"),
                (auth_path, "profile auth"),
                (base_config_path, "base config"),
                (target_config_path, "target config input"),
                (target_profile_config_path, "target profile config input"),
                (target_auth_path, "target auth input"),
                (
                    homes.internal.path / "config.toml",
                    "internal runtime config input",
                ),
                (
                    homes.official.path / "config.toml",
                    "official runtime config input",
                ),
                *(
                    (path, f"plugin snapshot input {path.name}")
                    for path in plugin_snapshot_paths
                ),
                *(
                    (path, f"composite config input {path.name}")
                    for path in composite_builder_paths
                ),
                *(([(profile_dir / "parity" / "receipt.json", "prepared parity receipt")])
                  if config_mode == "shared" and request.profile == "internal"
                  and internal_manifest.get("parity_receipt_path") else []),
            ]
        )
    )
    if not config_path.exists():
        raise SwitchError(f"Profile is missing config.toml: {config_path}")
    validate_toml(config_path)
    if (
        app_profile == "internal"
        and (config_mode == "shared" or canonical_app_cli_path is not None)
    ):
        app_cli_path = str(managed_profile_app_cli_path(store, app_profile))
    profile_config_text = build_profile_v2_config_text(
        request.profile,
        config_path,
    )
    uses_file_auth = config_uses_file_auth(profile_config_text)
    parity_projection = None
    if config_mode == "shared" and request.profile == "internal":
        if internal_manifest.get("parity_receipt_path"):
            from codex_switch_parity import ConfigInputs, prepare_parity_config_projection

            parity_projection = prepare_parity_config_projection(
                config_inputs=ConfigInputs.capture(
                    profile_config=config_path,
                    source_paths=(config_path, homes.official.path / "config.toml"),
                ),
                overlay_path=profile_dir / "parity" / "model-catalog.json",
            )
            if not parity_projection.healthy or parity_projection.changed_paths:
                raise SwitchError(
                    "Prepared internal configuration changed; run set-bin internal "
                    "with the current backend before switching."
                )
        target_config_text = None
        if parity_projection is not None:
            from codex_switch_verify import validate_prepared_parity_config
            from codex_switch_parity import ParityValidationError

            # A full apply has already materialized the verified runtime.
            # Reuse it only when the current profile/shared/runtime bytes
            # still match the receipt; stale legacy support layers cannot
            # replace that proven generation during activation.
            try:
                prepared_runtime_payload = target_config_path.read_bytes()
                validate_prepared_parity_config(store, parity_projection, prepared_runtime_payload)
                target_config_text = prepared_runtime_payload.decode("utf-8")
            except (OSError, UnicodeDecodeError, ParityValidationError):
                pass
        if target_config_text is None:
            target_config_text = build_internal_home_config(
                homes.official.path,
                request.profile,
                target_config_path,
                config_path,
                config_projection=parity_projection,
            )
        if parity_projection is not None:

            validate_prepared_parity_config(store, parity_projection, target_config_text.encode())
        writes_auth = False
        removes_auth = True
    elif config_mode == "shared":
        merged_official_config = build_official_home_config_from_internal(
            homes.official.path,
            homes.internal.path,
            config_path,
        )
        target_config_text = (
            merged_official_config
            if merged_official_config is not None
            else target_config_path.read_text()
        )
        writes_auth = (
            uses_file_auth and auth_path.exists() and not target_auth_path.exists()
        )
        removes_auth = False
    else:
        target_config_text = build_base_config_text(base_config_path)
        if target_config_path.exists():
            target_config_text = merge_preserved_shared_config_blocks(
                target_config_text,
                target_config_path.read_text(),
            )
        writes_auth = uses_file_auth and auth_path.exists()
        removes_auth = not uses_file_auth or (
            clear_missing_auth and not auth_path.exists()
        )
    frozen_auth_payload = auth_path.read_bytes() if writes_auth else None
    canonical_profile_text: str | None = None
    plugin_snapshot_text: str | None = None
    if config_mode == "shared":
        fallback_text = config_path.read_text() if config_path.exists() else None
        canonical_profile_text = annotate_canonical_profile_config(
            build_profile_seed_config_text(
                request.profile,
                target_config_text,
                f"canonical profile seed for {request.profile}",
                fallback_text=fallback_text,
                fallback_keys={"cli_auth_credentials_store"},
            ),
            request.profile,
        )
        if parity_projection is not None:
            # The receipt binds the exact prepared profile, including shared
            # preferences and feature/catalog projection. Do not reduce it to
            # a profile seed after preparing the runtime from those bytes.
            canonical_profile_text = parity_projection.payload_for(config_path).decode("utf-8")
        plugin_snapshot_text = build_preserved_shared_config_text_from_text(
            target_config_text,
            f"plugin support snapshot for {request.profile}",
        )
        seen_snapshot_paths: set[str] = set()
        for snapshot_path in plugin_snapshot_paths:
            snapshot_key = str(snapshot_path.resolve(strict=False))
            if snapshot_key in seen_snapshot_paths:
                continue
            seen_snapshot_paths.add(snapshot_key)
            if not snapshot_path.exists():
                continue
            try:
                existing_snapshot_text = (
                    build_preserved_shared_config_text_from_text(
                        snapshot_path.read_text(),
                        f"existing plugin support snapshot: {snapshot_path}",
                    )
                )
                plugin_snapshot_text = (
                    merge_missing_non_usage_shared_config_defaults(
                        plugin_snapshot_text,
                        existing_snapshot_text,
                    )
                )
            except SwitchError:
                continue
        plugin_snapshot_text = annotate_plugin_support_snapshot(
            plugin_snapshot_text,
            request.profile,
        )
    planning_inputs.finish(config_observations)

    planned_app_wrapper_payload: bytes | None = None
    planned_app_receipt_path: Path | None = None
    planned_app_receipt_payload: bytes | None = None
    wrapper_input_specs: list[tuple[Path, str]] = [
        (Path(codex_bin), "codex_bin"),
        (Path(app_cli_path), "app_cli_path"),
    ]
    if app_profile == "internal":
        planned_app_receipt_path = capability_receipt_path_for_launcher(
            Path(app_cli_path)
        )
        wrapper_input_specs.append(
            (planned_app_receipt_path, "app capability receipt")
        )
    wrapper_observations = planning_inputs.begin(
        tuple(wrapper_input_specs)
    )
    if app_effect_requires_rebind and should_refresh_profile_app_wrapper(
        store,
        app_profile,
        app_cli_path,
    ):
        planned_manifest = dict(app_manifest)
        planned_manifest["codex_home"] = str(app_home)
        if app_profile == "internal":
            planned_manifest["codex_bin"] = codex_bin
        receipt_artifact = None
        if app_profile == "internal":
            if planned_app_receipt_path is None:
                raise SwitchError("Internal capability receipt path is unavailable")
            raw_receipt_path = app_manifest.get("app_capability_receipt_path")
            receipt_path_matches = (
                isinstance(raw_receipt_path, str)
                and Path(raw_receipt_path).expanduser()
                == planned_app_receipt_path
            )
            receipt_artifact = prepare_capability_receipt_artifact(
                Path(codex_bin),
                receipt_path=(
                    planned_app_receipt_path
                    if receipt_path_matches
                    else None
                ),
                expected_payload_sha256=(
                    str(app_manifest.get("app_capability_receipt_sha256") or "")
                    if receipt_path_matches
                    else ""
                ),
                expected_schema_sha256=(
                    str(app_manifest.get("app_schema_sha256") or "")
                    if receipt_path_matches
                    else ""
                ),
                schema_timeout_seconds=5.0,
                probe_timeout_seconds=5.0,
            )
            planned_app_receipt_payload = receipt_artifact.payload
            planned_manifest["app_capability_receipt_path"] = str(
                planned_app_receipt_path
            )
            planned_manifest["app_capability_receipt_sha256"] = (
                receipt_artifact.payload_sha256
            )
            planned_manifest["app_schema_sha256"] = (
                receipt_artifact.receipt.schema_sha256
            )
        render_store = _SwitchWrapperRenderStore(
            store,
            app_profile,
            planned_manifest,
        )
        with tempfile.TemporaryDirectory(prefix="codex-switch-wrapper-plan-") as temp_dir:
            rendered_path = Path(temp_dir) / "planned-app-wrapper"
            write_profile_app_wrapper(
                store=render_store,  # type: ignore[arg-type]
                name=app_profile,
                app_cli_path=str(rendered_path),
                codex_bin=codex_bin,
                switch_scripts=Path(__file__).resolve().parent,
                capability_receipt_path=planned_app_receipt_path,
                schema_sha256=(
                    receipt_artifact.receipt.schema_sha256
                    if receipt_artifact is not None
                    else ""
                ),
                capability_receipt_sha256=(
                    receipt_artifact.payload_sha256
                    if receipt_artifact is not None
                    else ""
                ),
            )
            validate_executable_path(str(rendered_path), "planned app wrapper")
            planned_app_wrapper_payload = rendered_path.read_bytes()
        if (
            not planned_app_wrapper_payload.startswith(b"#!/usr/bin/env sh\n")
            or codex_bin.encode() not in planned_app_wrapper_payload
        ):
            raise SwitchError("Managed app wrapper renderer produced invalid output")
    planning_inputs.finish(wrapper_observations)

    if (
        app_profile == "internal"
        and (
            config_mode == "shared"
            or canonical_app_cli_path is not None
        )
        and planned_app_wrapper_payload is not None
    ):
        updated_internal_manifest = dict(
            homes.manifest_updates.get("internal", internal_manifest)
        )
        updated_internal_manifest["codex_bin"] = codex_bin
        updated_internal_manifest["app_cli_path"] = app_cli_path
        updated_internal_manifest["app_cli_binding"] = "launchagent"
        updated_internal_manifest["runtime_binding"] = "canonical"
        updated_internal_manifest["app_launcher_sha256"] = hashlib.sha256(
            planned_app_wrapper_payload
        ).hexdigest()
        if (
            planned_app_receipt_path is None
            or planned_app_receipt_payload is None
            or receipt_artifact is None
        ):
            raise SwitchError("Internal capability receipt plan is incomplete")
        updated_internal_manifest["app_capability_receipt_path"] = str(
            planned_app_receipt_path
        )
        updated_internal_manifest["app_capability_receipt_sha256"] = (
            receipt_artifact.payload_sha256
        )
        updated_internal_manifest["app_schema_sha256"] = (
            receipt_artifact.receipt.schema_sha256
        )
        updated_internal_manifest["updated_at"] = now_stamp()
        homes.manifest_updates["internal"] = updated_internal_manifest

    app_cli_binding_path = Path(app_cli_path)
    app_cli_commit_state = (
        _expected_file_state(
            app_cli_binding_path,
            planned_app_wrapper_payload,
            mode=0o755,
        )
        if planned_app_wrapper_payload is not None
        else None
    )
    final_profile_config_text = (
        canonical_profile_text
        if canonical_profile_text is not None
        else profile_config_text
    )

    planned_source_commit_states: dict[Path, dict[str, object]] = {
        target_config_path: _expected_file_state(
            target_config_path,
            target_config_text.encode(),
            mode=0o600,
        ),
        target_profile_config_path: _expected_file_state(
            target_profile_config_path,
            final_profile_config_text.encode(),
            mode=0o600,
        ),
    }
    if app_cli_commit_state is not None:
        planned_source_commit_states[app_cli_binding_path] = (
            app_cli_commit_state
        )
    if (
        planned_app_receipt_path is not None
        and planned_app_receipt_payload is not None
    ):
        planned_source_commit_states[planned_app_receipt_path] = (
            _expected_file_state(
                planned_app_receipt_path,
                planned_app_receipt_payload,
                mode=0o600,
            )
        )
    if app_effect_requires_rebind:
        planned_source_commit_states[store.launch_agent_path] = (
            _expected_file_state(
                store.launch_agent_path,
                launch_agent_payload(
                    store.launch_agent_label,
                    Path(app_cli_path),
                ),
                mode=0o644,
            )
        )
    for name, updated_manifest in homes.manifest_updates.items():
        manifest_path = store.manifest_path(name)
        planned_source_commit_states[manifest_path] = _expected_json_file_state(
            manifest_path,
            updated_manifest,
        )
    if canonical_profile_text is not None:
        planned_source_commit_states[config_path] = _expected_file_state(
            config_path,
            canonical_profile_text.encode(),
            mode=0o600,
        )
    if plugin_snapshot_text is not None:
        for snapshot_path in plugin_snapshot_paths:
            planned_source_commit_states[snapshot_path] = _expected_file_state(
                snapshot_path,
                plugin_snapshot_text.encode(),
                mode=0o600,
            )
    if frozen_auth_payload is not None and auth_path == target_auth_path:
        planned_source_commit_states[auth_path] = _expected_file_state(
            auth_path,
            frozen_auth_payload,
            mode=0o600,
        )
    if writes_auth and frozen_auth_payload is not None:
        planned_source_commit_states[target_auth_path] = _expected_file_state(
            target_auth_path,
            frozen_auth_payload,
            mode=0o600,
        )
    elif removes_auth:
        planned_source_commit_states[target_auth_path] = {
            "kind": "missing",
            "path": str(target_auth_path),
        }

    shared_sources: tuple[Path, ...] = tuple()
    shared_source_states: dict[Path, dict[str, object]] = {}
    shared_file_payloads: dict[Path, tuple[bytes, int]] = {}
    shared_source_home: Path | None = None
    shared_target_home: Path | None = None
    shared_prefer_link = False
    shared_stale_links: tuple[Path, ...] = tuple()
    shared_actions: tuple[str, ...] = tuple()
    shared_planned_states: dict[Path, dict[str, object]] = {}
    desktop_state_sync_enabled = not selection.is_split
    desktop_source: Path | None = None
    desktop_state_target: Path | None = None
    desktop_state_planned: dict[str, object] | None = None
    desktop_state_payload: bytes | None = None
    desktop_state_mode = 0o600
    if config_mode == "shared":
        if request.profile == "internal":
            shared_source_home = homes.official.path
            shared_target_home = homes.internal.path
            shared_prefer_link = True
            internal_shared_actions = [
                f"sync shared state from {shared_source_home} to {shared_target_home}",
            ]
            if desktop_state_sync_enabled:
                internal_shared_actions.append(
                    "merge Desktop global settings state from "
                    f"{shared_source_home} to {shared_target_home}"
                )
            internal_shared_actions.extend(
                (
                    f"write managed internal config: {target_config_path}",
                    f"remove managed internal auth: {target_auth_path}",
                )
            )
            shared_actions = tuple(internal_shared_actions)
        else:
            shared_source_home = homes.internal.path
            shared_target_home = homes.official.path
        shared_source_parent_observations = planning_inputs.begin(
            ((shared_source_home, "shared source entry set"),),
            capture_kind="shared_support_entry_set",
        )
        shared_target_parent_observations = planning_inputs.begin(
            ((shared_target_home, "shared target planning entry set"),),
            capture_kind="shared_support_entry_set",
        )
        if request.profile == "internal":
            shared_stale_links = tuple(
                stale_runtime_links(shared_target_home, shared_source_home)
            )
        shared_sources = tuple(shared_support_entries(shared_source_home))
        if request.profile == "openai-official":
            official_shared_actions = [
                f"sync shared state from {shared_source_home} to {shared_target_home}",
            ]
            if desktop_state_sync_enabled:
                official_shared_actions.append(
                    "merge Desktop global settings state from "
                    f"{shared_source_home} to {shared_target_home}"
                )
            shared_actions = tuple(official_shared_actions)
        desktop_source = desktop_global_state_path(shared_source_home)
        desktop_state_target = desktop_global_state_path(shared_target_home)
        shared_child_observations = planning_inputs.begin(
            tuple(
                [
                    *(
                        (source, f"shared support source {source.name}")
                        for source in shared_sources
                    ),
                    *(
                        (
                            shared_target_home / source.name,
                            f"shared support target {source.name}",
                        )
                        for source in shared_sources
                    ),
                    *(
                        ((desktop_source, "Desktop global-state source"),)
                        if desktop_state_sync_enabled
                        else tuple()
                    ),
                    *(
                        (
                            stale_link,
                            f"stale runtime-link input {stale_link.name}",
                        )
                        for stale_link in shared_stale_links
                    ),
                ]
            )
        )
        state_sources = list(shared_sources)
        if desktop_state_sync_enabled and (
            desktop_source.exists() or desktop_source.is_symlink()
        ):
            state_sources.append(desktop_source)
        shared_source_states = {
            path: filesystem_adapter.capture_state(path) for path in state_sources
        }
        symlink_mode = _default_symlink_mode()
        for source in shared_sources:
            target = shared_target_home / source.name
            source_state = shared_source_states[source]
            target_state = filesystem_adapter.capture_state(target)
            source_kind = source_state.get("kind")
            if source_kind == "symlink":
                if shareable_symlink_rejection_reason(
                    source,
                    shared_source_home,
                    target.parent,
                ):
                    shared_planned_states[target] = (
                        _expected_missing_state(target)
                        if should_remove_rejected_shareable_target(
                            target,
                            shared_source_home,
                            shared_target_home,
                        )
                        else dict(target_state)
                    )
                else:
                    shared_planned_states[target] = {
                        "kind": "symlink",
                        "path": str(target),
                        "mode": symlink_mode,
                        "symlink_target": os.readlink(source),
                    }
            elif shared_prefer_link and source_kind == "directory":
                if path_is_within(source, target.parent):
                    shared_planned_states[target] = (
                        _expected_missing_state(target)
                        if target.is_symlink()
                        and symlink_points_within(target, target.parent)
                        else dict(target_state)
                    )
                elif target.is_symlink() and Path(os.readlink(target)) == source:
                    shared_planned_states[target] = dict(target_state)
                elif target_state.get("kind") in {"missing", "symlink"}:
                    shared_planned_states[target] = {
                        "kind": "symlink",
                        "path": str(target),
                        "mode": symlink_mode,
                        "symlink_target": str(source),
                    }
                else:
                    shared_planned_states[target] = dict(target_state)
            elif source_kind == "file":
                source_mode = source_state.get("mode")
                if not isinstance(source_mode, int):
                    raise SwitchError(
                        f"Shared support source has no file mode: {source}"
                    )
                shared_file_payloads[source] = (
                    source.read_bytes(),
                    source_mode,
                )
                shared_planned_states[target] = _state_at_destination(
                    source_state,
                    target,
                )
            elif source_kind == "directory" and target_state.get("kind") == "missing":
                shared_planned_states[target] = _state_at_destination(
                    source_state,
                    target,
                )
            else:
                shared_planned_states[target] = dict(target_state)
        if desktop_state_sync_enabled:
            desktop_source_data = read_json_object_if_valid(desktop_source)
            desktop_source_subset = desktop_global_state_settings_subset(
                desktop_source_data
            )
        else:
            desktop_source_data = {}
            desktop_source_subset = {}
        if desktop_source_subset and desktop_state_target is not None:
            desktop_target_observations = planning_inputs.begin(
                (
                    (
                        desktop_state_target,
                        "Desktop global-state target",
                    ),
                )
            )
            desktop_state_before = filesystem_adapter.capture_state(
                desktop_state_target
            )
            desktop_target_data = read_json_object_if_valid(
                desktop_state_target
            )
            desktop_merged_data = merge_desktop_global_state_settings(
                desktop_source_data,
                desktop_target_data,
            )
            if desktop_merged_data != desktop_target_data:
                desktop_state_payload = (
                    json.dumps(
                        desktop_merged_data,
                        indent=2,
                        sort_keys=True,
                    ).encode()
                    + b"\n"
                )
                if desktop_state_target.exists() and not desktop_state_target.is_symlink():
                    desktop_state_mode = stat.S_IMODE(
                        desktop_state_target.stat().st_mode
                    )
                elif desktop_source.exists() and not desktop_source.is_symlink():
                    desktop_state_mode = stat.S_IMODE(
                        desktop_source.stat().st_mode
                    )
                desktop_state_planned = _expected_file_state(
                    desktop_state_target,
                    desktop_state_payload,
                    mode=desktop_state_mode,
                )
                planning_inputs.finish(desktop_target_observations)
            else:
                planning_inputs.finish(
                    desktop_target_observations,
                    retain=False,
                )
        planning_inputs.finish(shared_child_observations)
        planning_inputs.finish(shared_source_parent_observations)
        planning_inputs.finish(
            shared_target_parent_observations,
            retain=False,
        )
        for target, planned_state in shared_planned_states.items():
            planned_source_commit_states[target] = planned_state
        if desktop_state_planned is not None:
            if desktop_state_target is None:
                raise SwitchError("Desktop global-state target is unavailable")
            planned_source_commit_states[desktop_state_target] = (
                desktop_state_planned
            )
        for stale_link in shared_stale_links:
            planned_source_commit_states[stale_link] = (
                _expected_missing_state(stale_link)
            )
        backup_paths = [
            store.manifest_path("internal"),
            store.manifest_path("openai-official"),
            target_config_path,
            target_profile_config_path,
            *plugin_snapshot_paths,
            config_path,
            target_auth_path,
            *(
                [desktop_state_target]
                if (
                    desktop_state_planned is not None
                    and desktop_state_target is not None
                )
                else []
            ),
            *(shared_target_home / source.name for source in shared_sources),
            *shared_stale_links,
        ]
    else:
        backup_paths = [
            *(store.manifest_path(name) for name in homes.manifest_updates),
            target_config_path,
            target_profile_config_path,
            target_auth_path,
        ]
        if not skip_shim:
            backup_paths.append(store.bin_dir / "codex")
        if app_effect_requires_rebind:
            if should_refresh_profile_app_wrapper(
                store,
                app_profile,
                app_cli_path,
            ):
                backup_paths.append(Path(app_cli_path))
            backup_paths.append(store.launch_agent_path)
    shell_bootstrap = shell_cli_bootstrap_path() if not skip_shim else None
    shell_bootstrap_payload: bytes | None = None
    shell_bootstrap_mode = 0o644
    if shell_bootstrap is not None:
        shell_observations = planning_inputs.begin(
            ((shell_bootstrap, "shell profile"),)
        )
        current_shell_text = (
            shell_bootstrap.read_text() if shell_bootstrap.exists() else ""
        )
        shell_bootstrap_payload = replace_managed_block(
            current_shell_text,
            shell_cli_bootstrap_block(store),
        ).encode()
        if shell_bootstrap.exists():
            shell_bootstrap_mode = shell_bootstrap.stat().st_mode & 0o777
        planning_inputs.finish(shell_observations)
        planned_source_commit_states[shell_bootstrap] = (
            _expected_file_state(
                shell_bootstrap,
                shell_bootstrap_payload,
                mode=shell_bootstrap_mode,
            )
        )
        backup_paths.append(shell_bootstrap)
    if planned_app_receipt_path is not None and planned_app_receipt_payload is not None:
        backup_paths.append(planned_app_receipt_path)
    planned_shim_payload = (
        render_codex_shim_payload(
            store,
            codex_bin,
            target_home,
            profile_name=request.profile,
            switch_scripts=Path(__file__).resolve().parent,
        )
        if not skip_shim
        else None
    )
    frozen_switch_inputs = planning_inputs.frozen(
        planned_source_commit_states
    )
    if not skip_shim:
        backup_paths.append(store.bin_dir / "codex")
    if app_effect_requires_rebind:
        if should_refresh_profile_app_wrapper(
            store,
            app_profile,
            app_cli_path,
        ):
            backup_paths.append(Path(app_cli_path))
        backup_paths.append(store.launch_agent_path)
    unique_backup_paths: list[Path] = []
    seen_backup_paths: set[str] = set()
    for path in (*backup_paths, store.active_path):
        key = str(path.expanduser())
        if key in seen_backup_paths or path == store.active_path:
            continue
        seen_backup_paths.add(key)
        unique_backup_paths.append(path)
    backup_paths = [*unique_backup_paths, store.active_path]
    if config_mode == "shared":
        preview_actions = list(shared_actions)
        if not skip_shim:
            preview_actions.append(f"update shell shim: {store.bin_dir / 'codex'}")
            if shell_bootstrap is not None:
                preview_actions.append(
                    f"ensure command-line codex PATH bootstrap: {shell_bootstrap}"
                )
        if app_effect_requires_rebind:
            preview_actions.append(
                f"update ChatGPT Desktop binding: {store.launch_agent_path}"
            )
        preview_lines = (
            f"switch to profile {request.profile}",
            f"CLI profile: {request.profile}",
            f"App profile: {app_profile}",
            f"CLI binary: {codex_bin}",
            f"App binary: {app_cli_path}",
            *(
                (f"App action: {app_effect_plan.action}",)
                if selection.is_split
                else tuple()
            ),
            *(
                (
                    "Stopped-App requirement: "
                    f"{_SPLIT_STOPPED_APP_REQUIREMENT}",
                )
                if selection.is_split and app_effect_plan.requires_rebind
                else tuple()
            ),
            "Home plan:",
            f"- internal: {homes.internal.path} ({homes.internal.mode})",
            f"- openai-official: {homes.official.path} ({homes.official.mode})",
            *(f"- update profile home binding: {name}" for name in homes.manifest_updates),
            "Backup plan:",
            *(f"- {path}" for path in backup_paths),
            "Mutation plan:",
            *(f"- {action}" for action in preview_actions),
        )
    else:
        preview_actions = [
            f"write snapshot config: {target_config_path}",
            f"write profile layer: {target_profile_config_path}",
        ]
        if writes_auth:
            preview_actions.append(f"write auth from {auth_path}")
        elif removes_auth:
            preview_actions.append(f"remove auth: {target_auth_path}")
        else:
            preview_actions.append(f"preserve auth: {target_auth_path}")
        if not skip_shim:
            preview_actions.append(f"update shell shim: {store.bin_dir / 'codex'}")
            if shell_bootstrap is not None:
                preview_actions.append(
                    f"ensure command-line codex PATH bootstrap: {shell_bootstrap}"
                )
        if planned_app_wrapper_payload is not None:
            preview_actions.append(f"write managed app wrapper: {app_cli_path}")
        if (
            planned_app_receipt_path is not None
            and planned_app_receipt_payload is not None
        ):
            preview_actions.append(
                f"write app capability receipt: {planned_app_receipt_path}"
            )
        if app_effect_requires_rebind:
            preview_actions.append(
                f"update ChatGPT Desktop binding: {store.launch_agent_path}"
            )
            if skip_launchctl:
                preview_actions.append("skip launchctl apply")
        preview_lines = transaction_switch_preview_lines(
            name=request.profile,
            target_home=target_home,
            backup_paths=backup_paths,
            mutation_actions=preview_actions,
        )
        preview_lines = (
            preview_lines[0],
            f"CLI profile: {request.profile}",
            f"App profile: {app_profile}",
            f"CLI binary: {codex_bin}",
            f"App binary: {app_cli_path}",
            *(
                (f"App action: {app_effect_plan.action}",)
                if selection.is_split
                else tuple()
            ),
            *(
                (
                    "Stopped-App requirement: "
                    f"{_SPLIT_STOPPED_APP_REQUIREMENT}",
                )
                if selection.is_split and app_effect_plan.requires_rebind
                else tuple()
            ),
            *preview_lines[1:],
        )
    if dry_run:
        return TransactionReceipt(
            operation="switch",
            outcome="dry_run",
            preview_lines=preview_lines,
            backup_id=None,
        )

    if not skip_app_cli and desktop_adapter is None:
        desktop_adapter = _switch_desktop_binding_adapter(store, request)
        desktop_observation = desktop_adapter.observe(skip_launchctl=skip_launchctl)

    _require_frozen_switch_inputs(
        filesystem_adapter,
        frozen_switch_inputs,
        phase="before mutation",
        commit=False,
    )
    store.ensure()
    if config_mode == "shared" and progress_callback is not None:
        progress_callback("Creating switch backup...")
    backup_dir = create_switch_backup(
        store=store,
        operation="switch",
        from_profile=active_profile,
        to_profile=request.profile,
        paths=backup_paths,
        filesystem_adapter=filesystem_adapter,
        created_parent_paths=(
            {str(target_config_path): created_target_directories}
            if created_target_directories
            else None
        ),
    )
    shim_path = store.bin_dir / "codex" if not skip_shim else None
    active_launch_agent_path = (
        store.launch_agent_path if not skip_app_cli else None
    )
    launch_agent_path = (
        store.launch_agent_path if app_effect_requires_rebind else None
    )
    active_payload = active_record(
        name=request.profile,
        codex_home=target_home,
        config_mode=config_mode,
        base_config_path=base_config_path,
        backup_dir=backup_dir,
        shim_path=shim_path,
        shell_cli_path=codex_bin,
        app_cli_path=app_cli_path,
        launch_agent_path=active_launch_agent_path,
        home_mode=target_binding.mode,
        shared_sync_source=(
            shared_source_home if config_mode == "shared" else None
        ),
        shared_sync_target=(
            shared_target_home if config_mode == "shared" else None
        ),
        app_profile=app_profile,
    )
    active_commit_state = _expected_json_file_state(
        store.active_path,
        active_payload,
    )
    frozen_switch_inputs = tuple(
        _FrozenSwitchInput(
            label=frozen.label,
            path=frozen.path,
            before_state_json=frozen.before_state_json,
            before_identity_json=frozen.before_identity_json,
            commit_state_json=(
                _immutable_state_bytes(active_commit_state)
                if frozen.path == store.active_path
                else frozen.commit_state_json
            ),
            commit_replaces_identity=(
                True
                if frozen.path == store.active_path
                else frozen.commit_replaces_identity
            ),
            capture_kind=frozen.capture_kind,
        )
        for frozen in frozen_switch_inputs
    )
    try:
        if config_mode == "shared" and progress_callback is not None:
            progress_callback(f"Backup captured: {backup_dir}")
            progress_callback("Applying switch mutations...")
        prepared_switch_manifest = _verify_switch_backup_before_states(
            filesystem_adapter,
            backup_dir,
        )
        _require_frozen_switch_inputs(
            filesystem_adapter,
            frozen_switch_inputs,
            phase="after backup",
            commit=False,
            complete=False,
        )
    except Exception as preapply_error:
        try:
            raw_prepared_manifest = read_json(backup_dir / "backup.json")
            if not isinstance(raw_prepared_manifest, dict):
                raise SwitchError(
                    f"Switch backup manifest is not an object: {backup_dir.name}"
                )
            _write_switch_backup_lifecycle(
                filesystem_adapter,
                backup_dir,
                dict(raw_prepared_manifest),
                "rolled_back",
                preapply_error,
            )
        except Exception as lifecycle_error:
            return TransactionReceipt(
                operation="switch",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (
                    f"switch failed: {preapply_error}",
                    f"rollback failed: {lifecycle_error}",
                ),
                backup_id=backup_dir.name,
            )
        return TransactionReceipt(
            operation="switch",
            outcome="rolled_back",
            preview_lines=preview_lines
            + (f"switch failed: {preapply_error}", "rollback completed"),
            backup_id=backup_dir.name,
        )
    switch_journal: _SwitchMutationJournal | None = None
    pending_marker: _PendingTransactionMarker | None = None
    marker_cleanup_warning: str | None = None
    try:
        staged_payload_specs: dict[tuple[Path, str], tuple[bytes, int]] = {}
        for name, updated_manifest in homes.manifest_updates.items():
            staged_payload_specs[(
                store.manifest_path(name),
                "home_binding_write",
            )] = (
                _canonical_json_bytes(updated_manifest),
                0o600,
            )
        staged_payload_specs[(target_config_path, "config_write")] = (
            target_config_text.encode(),
            0o600,
        )
        if canonical_profile_text is not None:
            staged_payload_specs[(config_path, "canonical_profile_write")] = (
                canonical_profile_text.encode(),
                0o600,
            )
        staged_payload_specs[(
            target_profile_config_path,
            "profile_config_write",
        )] = (
            final_profile_config_text.encode(),
            0o600,
        )
        if plugin_snapshot_text is not None:
            for snapshot_path in plugin_snapshot_paths:
                staged_payload_specs[(snapshot_path, "plugin_snapshot_write")] = (
                    plugin_snapshot_text.encode(),
                    0o600,
                )
        if writes_auth and frozen_auth_payload is not None:
            staged_payload_specs[(target_auth_path, "auth_write")] = (
                frozen_auth_payload,
                0o600,
            )
        if shim_path is not None:
            if planned_shim_payload is None:
                raise SwitchError("Switch shim payload is unavailable")
            staged_payload_specs[(shim_path, "shim_write")] = (
                planned_shim_payload,
                0o755,
            )
        if shell_bootstrap is not None and shell_bootstrap_payload is not None:
            staged_payload_specs[(
                shell_bootstrap,
                "shell_bootstrap_write",
            )] = (
                shell_bootstrap_payload,
                shell_bootstrap_mode,
            )
        if planned_app_wrapper_payload is not None:
            staged_payload_specs[(
                Path(app_cli_path),
                "app_wrapper_write",
            )] = (
                planned_app_wrapper_payload,
                0o755,
            )
        if (
            planned_app_receipt_path is not None
            and planned_app_receipt_payload is not None
        ):
            staged_payload_specs[(
                planned_app_receipt_path,
                "app_capability_receipt_write",
            )] = (
                planned_app_receipt_payload,
                0o600,
            )
        if launch_agent_path is not None:
            staged_payload_specs[(launch_agent_path, "plist_write")] = (
                launch_agent_payload(
                    store.launch_agent_label,
                    Path(app_cli_path),
                ),
                0o644,
            )
        if desktop_state_payload is not None:
            staged_payload_specs[(
                desktop_state_target,
                "desktop_global_state_sync",
            )] = (
                desktop_state_payload,
                desktop_state_mode,
            )
        if shared_target_home is not None:
            for source, (payload, mode) in shared_file_payloads.items():
                staged_payload_specs[(
                    shared_target_home / source.name,
                    "shared_support_sync",
                )] = (payload, mode)
        staged_payload_specs[(store.active_path, "active_write")] = (
            _canonical_json_bytes(active_payload),
            0o600,
        )
        prepared_staged_artifacts = _prepare_switch_staged_payloads(
            filesystem_adapter,
            backup_dir,
            staged_payload_specs,
        )
        _durably_sync_transaction_backup(
            filesystem_adapter,
            store,
            backup_dir,
        )
        staged_artifacts = _freeze_switch_staged_payloads(
            filesystem_adapter,
            backup_dir,
            prepared_staged_artifacts,
        )
        switch_journal = _SwitchMutationJournal(
            filesystem_adapter,
            backup_dir,
            prepared_switch_manifest,
            frozen_inputs=frozen_switch_inputs,
            desktop_observation=(
                desktop_observation
                if app_effect_requires_rebind
                else None
            ),
            expected_path_states={
                directory: target_directory_before_states[directory]
                for directory in target_directories_to_ensure
            },
            staged_artifacts=staged_artifacts,
        )
        raw_switch_journal = switch_journal.manifest.get("switch_journal")
        if not isinstance(raw_switch_journal, dict):
            raise SwitchError("Switch mutation journal is unavailable")
        pending_marker = _bind_pending_transaction(
            store,
            backup_dir,
            "switch",
            raw_switch_journal,
        )
        switch_journal.prepare()
        _publish_pending_transaction(filesystem_adapter, pending_marker)
        for name, updated_manifest in homes.manifest_updates.items():
            manifest_path = store.manifest_path(name)
            switch_journal.apply_path(
                manifest_path,
                phase="home_binding_write",
                planned_after_state=_expected_json_file_state(
                    manifest_path,
                    updated_manifest,
                ),
                action=lambda path=manifest_path, data=updated_manifest: (
                    filesystem_adapter.write_manifest(
                        path,
                        data,
                        phase="home_binding_write",
                    )
                ),
            )
        for target_directory in target_directories_to_ensure:
            target_directory_before = target_directory_before_states[
                target_directory
            ]
            target_directory_mode = (
                int(target_directory_before["mode"])
                if target_directory_before.get("kind") == "directory"
                else 0o700
            )
            switch_journal.apply_path(
                target_directory,
                phase="target_home_ensure",
                planned_after_state=target_directory_planned_states[
                    target_directory
                ],
                action=lambda path=target_directory, mode=target_directory_mode: (
                    filesystem_adapter.ensure_directory(
                        path,
                        mode=mode,
                        phase="target_home_ensure",
                    )
                ),
            )
        if shared_target_home is not None:
            for shared_index, source in enumerate(shared_sources, start=1):
                shared_target = shared_target_home / source.name
                expected_source_state = shared_source_states[source]

                def require_shared_source(
                    *,
                    expected: Mapping[str, object] = expected_source_state,
                    source_path: Path = source,
                    phase: str,
                ) -> None:
                    if not _states_match(
                        filesystem_adapter.capture_state(source_path),
                        expected,
                    ):
                        raise SwitchError(
                            f"Shared support source changed {phase}: {source_path}"
                        )

                shared_file_payload = shared_file_payloads.get(source)
                if shared_file_payload is not None:
                    payload, mode = shared_file_payload
                    shared_action = (
                        lambda target_path=shared_target,
                        frozen_payload=payload,
                        frozen_mode=mode: filesystem_adapter.write_bytes(
                            target_path,
                            frozen_payload,
                            mode=frozen_mode,
                            phase="shared_support_sync",
                        )
                    )
                else:
                    shared_action = (
                        lambda source_path=source,
                        target_path=shared_target: filesystem_adapter.sync_shared_entry(
                            source_path,
                            target_path,
                            prefer_link=shared_prefer_link,
                            phase="shared_support_sync",
                        )
                    )
                if progress_callback is not None:
                    progress_callback(
                        "Applying shared support "
                        f"[{shared_index}/{len(shared_sources)}]: {source.name}"
                    )
                switch_journal.apply_path(
                    shared_target,
                    phase="shared_support_sync",
                    planned_after_state=shared_planned_states[shared_target],
                    action=shared_action,
                    pre_action_guard=lambda guard=require_shared_source: guard(
                        phase="before apply"
                    ),
                    post_action_guard=lambda guard=require_shared_source: guard(
                        phase="during apply"
                    ),
                )
            if desktop_state_payload is not None:
                if desktop_state_planned is None:
                    raise SwitchError("Desktop global-state plan is unavailable")
                switch_journal.apply_path(
                    desktop_state_target,
                    phase="desktop_global_state_sync",
                    planned_after_state=desktop_state_planned,
                    action=lambda: filesystem_adapter.write_bytes(
                        desktop_state_target,
                        desktop_state_payload,
                        mode=desktop_state_mode,
                        phase="desktop_global_state_sync",
                    ),
                )
            for stale_link in shared_stale_links:
                switch_journal.apply_path(
                    stale_link,
                    phase="stale_runtime_link_remove",
                    planned_after_state=_expected_missing_state(stale_link),
                    action=lambda path=stale_link: filesystem_adapter.remove_path(
                        path,
                        phase="stale_runtime_link_remove",
                    ),
                )
        switch_journal.apply_path(
            target_config_path,
            phase="config_write",
            planned_after_state=_expected_file_state(
                target_config_path,
                target_config_text.encode(),
                mode=0o600,
            ),
            action=lambda: filesystem_adapter.write_bytes(
                target_config_path,
                target_config_text.encode(),
                mode=0o600,
                phase="config_write",
            ),
        )
        if config_mode == "shared":
            if canonical_profile_text is None or plugin_snapshot_text is None:
                raise SwitchError("Shared switch plan is incomplete")
            switch_journal.apply_path(
                config_path,
                phase="canonical_profile_write",
                planned_after_state=_expected_file_state(
                    config_path,
                    canonical_profile_text.encode(),
                    mode=0o600,
                ),
                action=lambda: filesystem_adapter.write_bytes(
                    config_path,
                    canonical_profile_text.encode(),
                    mode=0o600,
                    phase="canonical_profile_write",
                ),
            )
        switch_journal.apply_path(
            target_profile_config_path,
            phase="profile_config_write",
            planned_after_state=_expected_file_state(
                target_profile_config_path,
                final_profile_config_text.encode(),
                mode=0o600,
            ),
            action=lambda: filesystem_adapter.write_bytes(
                target_profile_config_path,
                final_profile_config_text.encode(),
                mode=0o600,
                phase="profile_config_write",
            ),
        )
        if config_mode == "shared":
            for snapshot_path in plugin_snapshot_paths:
                switch_journal.apply_path(
                    snapshot_path,
                    phase="plugin_snapshot_write",
                    planned_after_state=_expected_file_state(
                        snapshot_path,
                        plugin_snapshot_text.encode(),
                        mode=0o600,
                    ),
                    action=lambda path=snapshot_path: filesystem_adapter.write_bytes(
                        path,
                        plugin_snapshot_text.encode(),
                        mode=0o600,
                        phase="plugin_snapshot_write",
                    ),
                )
        if writes_auth:
            if frozen_auth_payload is None:
                raise SwitchError("Frozen profile auth payload is unavailable")
            switch_journal.apply_path(
                target_auth_path,
                phase="auth_write",
                planned_after_state=_expected_file_state(
                    target_auth_path,
                    frozen_auth_payload,
                    mode=0o600,
                ),
                action=lambda: filesystem_adapter.write_bytes(
                    target_auth_path,
                    frozen_auth_payload,
                    mode=0o600,
                    phase="auth_write",
                ),
            )
        elif removes_auth and (
            target_auth_path.exists() or target_auth_path.is_symlink()
        ):
            switch_journal.apply_path(
                target_auth_path,
                phase="auth_remove",
                planned_after_state=_expected_missing_state(target_auth_path),
                action=lambda: filesystem_adapter.remove_path(
                    target_auth_path,
                    phase="auth_remove",
                ),
            )
        if not skip_shim:
            if shim_path is None or planned_shim_payload is None:
                raise SwitchError("Switch shim plan is unavailable")
            switch_journal.apply_path(
                shim_path,
                phase="shim_write",
                planned_after_state=_expected_file_state(
                    shim_path,
                    planned_shim_payload,
                    mode=0o755,
                ),
                action=lambda: filesystem_adapter.write_bytes(
                    shim_path,
                    planned_shim_payload,
                    mode=0o755,
                    phase="shim_write",
                ),
            )
            if shell_bootstrap is not None and shell_bootstrap_payload is not None:
                switch_journal.apply_path(
                    shell_bootstrap,
                    phase="shell_bootstrap_write",
                    planned_after_state=_expected_file_state(
                        shell_bootstrap,
                        shell_bootstrap_payload,
                        mode=shell_bootstrap_mode,
                    ),
                    action=lambda: filesystem_adapter.write_bytes(
                        shell_bootstrap,
                        shell_bootstrap_payload,
                        mode=shell_bootstrap_mode,
                        phase="shell_bootstrap_write",
                    ),
                )
        if app_effect_requires_rebind:
            if (
                planned_app_receipt_path is not None
                and planned_app_receipt_payload is not None
            ):
                switch_journal.apply_path(
                    planned_app_receipt_path,
                    phase="app_capability_receipt_write",
                    planned_after_state=_expected_file_state(
                        planned_app_receipt_path,
                        planned_app_receipt_payload,
                        mode=0o600,
                    ),
                    action=lambda: filesystem_adapter.write_bytes(
                        planned_app_receipt_path,
                        planned_app_receipt_payload,
                        mode=0o600,
                        phase="app_capability_receipt_write",
                    ),
                )
            if planned_app_wrapper_payload is not None:
                wrapper_path = Path(app_cli_path)
                switch_journal.apply_path(
                    wrapper_path,
                    phase="app_wrapper_write",
                    planned_after_state=_expected_file_state(
                        wrapper_path,
                        planned_app_wrapper_payload,
                        mode=0o755,
                    ),
                    action=lambda: filesystem_adapter.write_bytes(
                        wrapper_path,
                        planned_app_wrapper_payload,
                        mode=0o755,
                        phase="app_wrapper_write",
                    ),
                )
            if launch_agent_path is None:
                raise SwitchError("Switch launch-agent plan is unavailable")
            switch_journal.apply_path(
                launch_agent_path,
                phase="plist_write",
                planned_after_state=_expected_file_state(
                    launch_agent_path,
                    launch_agent_payload(
                        store.launch_agent_label,
                        Path(app_cli_path),
                    ),
                    mode=0o644,
                ),
                action=lambda: filesystem_adapter.write_bytes(
                    launch_agent_path,
                    launch_agent_payload(
                        store.launch_agent_label,
                        Path(app_cli_path),
                    ),
                    mode=0o644,
                    phase="plist_write",
                ),
            )
            if desktop_adapter is None or desktop_observation is None:
                raise SwitchError("Desktop binding observation is unavailable")
            bind_effect_journal = getattr(
                desktop_adapter,
                "bind_effect_journal",
                None,
            )
            if callable(bind_effect_journal):
                bind_effect_journal(switch_journal)
            desktop_adapter.apply(
                Path(app_cli_path),
                desktop_observation,
                skip_launchctl=skip_launchctl,
            )
        switch_journal.validate_frozen_inputs(phase="before active record")
        switch_journal.apply_path(
            store.active_path,
            phase="active_write",
            planned_after_state=_expected_json_file_state(
                store.active_path,
                active_payload,
            ),
            action=lambda: filesystem_adapter.write_manifest(
                store.active_path,
                active_payload,
                phase="active_write",
            ),
        )
        finalize_effect = switch_journal.begin(
            kind="finalize",
            phase="backup_finalize",
            before_state={"lifecycle": "prepared"},
            planned_after_state={"lifecycle": "committed"},
        )
        _require_frozen_switch_inputs(
            filesystem_adapter,
            frozen_switch_inputs,
            phase="at commit",
            commit=True,
        )
        finalize_effect_id = finalize_effect.get("id")
        if type(finalize_effect_id) is not int:
            raise SwitchError("Switch finalize effect has an invalid ID")
        finalized_manifest = finalize_backup(
            backup_dir,
            filesystem_adapter=filesystem_adapter,
            terminal_journal_effect_id=finalize_effect_id,
        )
        switch_journal.adopt_finalized_manifest(
            finalized_manifest,
            finalize_effect,
        )
        authoritative_commit, _terminal_commit_claimed = (
            _read_authoritative_committed_switch(
                store,
                backup_dir,
                pending_marker,
            )
        )
        if authoritative_commit is None:
            invalid_terminal_guidance = (
                "switch terminal commit evidence is invalid; pending recovery "
                "marker retained for manual recovery",
            )
            return TransactionReceipt(
                operation="switch",
                outcome="rollback_failed",
                preview_lines=preview_lines + invalid_terminal_guidance,
                backup_id=backup_dir.name,
                guidance_lines=invalid_terminal_guidance,
            )
        marker_cleanup_warning = _retire_pending_marker(
            filesystem_adapter,
            pending_marker,
            outcome="committed",
        )
    except Exception as apply_error:
        authoritative_commit, terminal_commit_claimed = (
            _read_authoritative_committed_switch(
                store,
                backup_dir,
                pending_marker,
            )
        )
        if authoritative_commit is not None:
            cleanup_lines: tuple[str, ...] = tuple()
            if pending_marker is not None:
                cleanup_warning = _retire_pending_marker(
                    filesystem_adapter,
                    pending_marker,
                    outcome="committed",
                )
                if cleanup_warning is not None:
                    cleanup_lines = (cleanup_warning,)
            commit_guidance = (
                "switch committed; terminal write reported an error: "
                f"{apply_error}",
            ) + cleanup_lines
            return TransactionReceipt(
                operation="switch",
                outcome="committed",
                preview_lines=preview_lines + commit_guidance,
                backup_id=backup_dir.name,
                guidance_lines=commit_guidance,
            )
        if terminal_commit_claimed:
            invalid_terminal_guidance = (
                "switch terminal commit evidence is invalid; pending recovery "
                "marker retained for manual recovery",
            )
            return TransactionReceipt(
                operation="switch",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (f"switch failed: {apply_error}",)
                + invalid_terminal_guidance,
                backup_id=backup_dir.name,
                guidance_lines=invalid_terminal_guidance,
            )
        rollback_failures: list[Exception] = []
        rollback_manifest = (
            dict(switch_journal.manifest)
            if switch_journal is not None
            else dict(prepared_switch_manifest)
        )
        begun_paths = (
            switch_journal.begun_paths()
            if switch_journal is not None
            else tuple()
        )
        try:
            rollback_manifest = _rollback_switch_filesystem(
                filesystem_adapter,
                backup_dir,
                rollback_manifest,
                begun_paths,
                include_active=False,
                active_path=store.active_path,
            )
        except Exception as filesystem_rollback_error:
            rollback_failures.append(filesystem_rollback_error)
        raw_cleanup_journal = rollback_manifest.get("switch_journal")
        raw_cleanup_effects = (
            raw_cleanup_journal.get("effects")
            if isinstance(raw_cleanup_journal, dict)
            else None
        )
        cleanup_effects_by_path = {
            raw_effect.get("path"): raw_effect
            for raw_effect in raw_cleanup_effects
            if isinstance(raw_effect, dict)
            and raw_effect.get("kind") == "filesystem"
            and raw_effect.get("phase") == "target_home_ensure"
            and isinstance(raw_effect.get("path"), str)
        } if isinstance(raw_cleanup_effects, list) else {}
        for cleanup_index, directory in enumerate(created_target_directories):
            try:
                cleanup_effect = cleanup_effects_by_path.get(str(directory))
                if not isinstance(cleanup_effect, dict):
                    raise SwitchError(
                        f"Switch rollback has no target-directory evidence: "
                        f"{directory}"
                    )
                produced_identity = cleanup_effect.get("produced_identity")
                route_guard = cleanup_effect.get("route_guard")
                current_identity = filesystem_adapter.capture_identity(directory)
                if current_identity.get("kind") == "missing":
                    continue
                if (
                    not isinstance(produced_identity, dict)
                    or current_identity != produced_identity
                ):
                    raise SwitchError(
                        f"Switch rollback target-directory identity changed: "
                        f"{directory}"
                    )
                if not isinstance(route_guard, dict):
                    raise SwitchError(
                        f"Switch rollback target-directory route is missing: "
                        f"{directory}"
                    )
                cleanup_parent_descriptor = _open_guarded_parent(
                    directory,
                    route_guard,
                )
                filesystem_adapter.bind_restore_parent_cleanup(
                    directory,
                    cleanup_parent_descriptor,
                    produced_identity,
                )
                try:
                    filesystem_adapter.remove_empty_dir(
                        directory,
                        phase=f"switch_rollback_target_dir_{cleanup_index}",
                    )
                finally:
                    filesystem_adapter.unbind_restore_parent_cleanup()
            except Exception as directory_cleanup_error:
                rollback_failures.append(directory_cleanup_error)
        if (
            app_effect_requires_rebind
            and desktop_adapter is not None
            and desktop_observation is not None
        ):
            try:
                desktop_adapter.rollback(
                    desktop_observation,
                    skip_launchctl=skip_launchctl,
                )
            except Exception as desktop_rollback_error:
                rollback_failures.append(desktop_rollback_error)
        try:
            rollback_manifest = _rollback_switch_filesystem(
                filesystem_adapter,
                backup_dir,
                rollback_manifest,
                begun_paths,
                include_active=True,
                active_path=store.active_path,
            )
        except Exception as active_rollback_error:
            rollback_failures.append(active_rollback_error)
        if rollback_failures:
            rollback_error = SwitchError(
                "; ".join(str(error) for error in rollback_failures)
            )
            try:
                _write_switch_backup_lifecycle(
                    filesystem_adapter,
                    backup_dir,
                    rollback_manifest,
                    "rollback_failed",
                    apply_error,
                    rollback_error=rollback_error,
                )
            except Exception as lifecycle_error:
                try:
                    _write_switch_failure_record(
                        filesystem_adapter,
                        backup_dir,
                        rollback_manifest,
                        "rollback_failed",
                        apply_error,
                        rollback_error=rollback_error,
                    )
                except Exception as fallback_error:
                    rollback_error = SwitchError(
                        f"{rollback_error}; lifecycle: {lifecycle_error}; "
                        f"failure record: {fallback_error}"
                    )
            return TransactionReceipt(
                operation="switch",
                outcome="rollback_failed",
                preview_lines=preview_lines
                + (
                    f"switch failed: {apply_error}",
                    f"rollback failed: {rollback_error}",
                ),
                backup_id=backup_dir.name,
            )
        try:
            _write_switch_backup_lifecycle(
                filesystem_adapter,
                backup_dir,
                rollback_manifest,
                "rolled_back",
                apply_error,
            )
        except Exception as lifecycle_error:
            try:
                _write_switch_failure_record(
                    filesystem_adapter,
                    backup_dir,
                    rollback_manifest,
                    "rolled_back",
                    apply_error,
                    rollback_error=lifecycle_error,
                )
            except Exception as fallback_error:
                return TransactionReceipt(
                    operation="switch",
                    outcome="rollback_failed",
                    preview_lines=preview_lines
                    + (
                        f"switch failed: {apply_error}",
                        f"rollback evidence failed: {lifecycle_error}; "
                        f"failure record: {fallback_error}",
                    ),
                    backup_id=backup_dir.name,
                )
            return TransactionReceipt(
                operation="switch",
                outcome="rolled_back",
                preview_lines=preview_lines
                + (
                    f"switch failed: {apply_error}",
                    "rollback completed; independent failure record retained",
                ),
                backup_id=backup_dir.name,
            )
        cleanup_lines = tuple()
        if pending_marker is not None:
            cleanup_warning = _retire_pending_marker(
                filesystem_adapter,
                pending_marker,
                outcome="rolled_back",
            )
            if cleanup_warning is not None:
                cleanup_lines = (cleanup_warning,)
        return TransactionReceipt(
            operation="switch",
            outcome="rolled_back",
            preview_lines=preview_lines
            + (f"switch failed: {apply_error}", "rollback completed")
            + cleanup_lines,
            backup_id=backup_dir.name,
        )
    completion_lines = (
        (marker_cleanup_warning,) if marker_cleanup_warning is not None else tuple()
    )
    return TransactionReceipt(
        operation="switch",
        outcome="committed",
        preview_lines=preview_lines + completion_lines,
        backup_id=backup_dir.name,
        guidance_lines=completion_lines,
    )


def _execute_transaction(
    store: Store,
    request: TransactionRequest,
    *,
    dry_run: bool = False,
    locked_store: LockedStoreMutation | None = None,
) -> TransactionReceipt:
    lock_context = (
        locked_store._reuse_context(store)
        if locked_store is not None
        else _StoreLock(store.root)
    )
    capture_adapter: FilesystemAdapter | None = None
    if request.operation == "capture":
        capture_adapter = _capture_filesystem_adapter(request)
    if request.operation == "capture" and not dry_run:
        try:
            store.root.lstat()
        except FileNotFoundError:
            if capture_adapter is None:
                raise SwitchError("Capture filesystem adapter is unavailable")
            capture_adapter.create_store_root(store.root)
    with lock_context:
        runtime_rebind_marker = _runtime_rebind_marker_path(store)
        if _runtime_rebind_marker_present(runtime_rebind_marker):
            if dry_run:
                return TransactionReceipt(
                    operation=request.operation,
                    outcome="dry_run",
                    preview_lines=(
                        "pending runtime rebind recovery required before retry",
                        "dry-run made no recovery or destination changes",
                    ),
                    backup_id=None,
                )
            _recover_runtime_binding_rebind(store)
        classification = _classify_store_recovery(store)
        pending_transaction = classification.pending_transaction
        pending_capture_profiles = classification.pending_capture_profiles
        unmarked_transactions = classification.unmarked_transactions
        if unmarked_transactions:
            unmarked = unmarked_transactions[0]
            if unmarked.lifecycle == "rollback_failed":
                raise SwitchError(
                    "Unmarked transaction rollback failed and requires manual "
                    f"recovery: {unmarked.backup_dir.name}"
                )
            if unmarked.marker_required and unmarked.effects_begun:
                raise SwitchError(
                    "Transaction requires a missing recovery marker and cannot "
                    f"be resumed automatically: {unmarked.backup_dir.name}"
                )
            if not unmarked.marker_required and unmarked.operation == "restore":
                raise SwitchError(
                    "Pre-marker restore requires manual recovery before any new "
                    f"mutation: {unmarked.backup_dir.name}"
                )
            if dry_run:
                recovery_label = (
                    "effect-free transaction closure"
                    if unmarked.marker_required
                    else "legacy switch recovery"
                )
                return TransactionReceipt(
                    operation=request.operation,
                    outcome="dry_run",
                    preview_lines=(
                        f"pending {recovery_label} required before retry: "
                        f"{unmarked.backup_dir.name}",
                        "dry-run made no recovery or destination changes",
                    ),
                    backup_id=unmarked.backup_dir.name,
                )
            if request.operation == "switch":
                legacy_recovery_adapter = _switch_filesystem_adapter(request)
            elif request.operation == "capture":
                if capture_adapter is None:
                    raise SwitchError("Capture filesystem adapter is unavailable")
                legacy_recovery_adapter = capture_adapter
            elif request.operation == "restore":
                legacy_recovery_adapter = _filesystem_adapter(request)
            else:
                raise SwitchError(
                    f"Unsupported transaction operation: {request.operation}"
                )
            if unmarked.marker_required:
                _close_effect_free_unmarked_transaction(
                    store,
                    unmarked,
                    legacy_recovery_adapter,
                )
            else:
                legacy_desktop_adapter = None
                if unmarked.journal.get("desktop_before") is not None:
                    legacy_desktop_adapter = _switch_desktop_binding_adapter(
                        store,
                        request,
                    )
                _recover_prepared_switch(
                    store,
                    unmarked.backup_dir,
                    dict(unmarked.manifest),
                    legacy_recovery_adapter,
                    legacy_desktop_adapter,
                )
        if pending_capture_profiles:
            pending_capture_profile = pending_capture_profiles[0]
            if (
                request.operation != "capture"
                or request.profile != pending_capture_profile
            ):
                raise SwitchError(
                    "Pending capture recovery blocks new mutations for profile "
                    f"{pending_capture_profile}"
                )
            if dry_run:
                return TransactionReceipt(
                    operation="capture",
                    outcome="dry_run",
                    preview_lines=(
                        "pending capture recovery required before retry: "
                        f"{pending_capture_profile}",
                        "dry-run made no recovery or destination changes",
                    ),
                    backup_id=None,
                )
        if pending_transaction is not None:
            if pending_transaction.lifecycle == "rollback_failed":
                ignored_effect_ids = (
                    _legacy_noop_desktop_recovery_effect_ids(
                        store,
                        pending_transaction,
                    )
                )
                if ignored_effect_ids is None:
                    raise SwitchError(
                        "Pending transaction rollback failed and requires manual "
                        f"recovery: {pending_transaction.backup_dir.name}"
                    )
                if dry_run:
                    return TransactionReceipt(
                        operation=request.operation,
                        outcome="dry_run",
                        preview_lines=(
                            "pending legacy Desktop no-op recovery required "
                            "before retry: "
                            f"{pending_transaction.backup_dir.name}",
                            "dry-run made no recovery or destination changes",
                        ),
                        backup_id=pending_transaction.backup_dir.name,
                    )
                recovery_adapter = _switch_filesystem_adapter(request)
                recovery_desktop_adapter = None
                if pending_transaction.journal.get("desktop_before") is not None:
                    recovery_desktop_adapter = _switch_desktop_binding_adapter(
                        store,
                        request,
                    )
                _recover_prepared_switch(
                    store,
                    pending_transaction.backup_dir,
                    dict(pending_transaction.manifest),
                    recovery_adapter,
                    recovery_desktop_adapter,
                    ignored_effect_ids=ignored_effect_ids,
                )
                cleanup_warning = _retire_pending_marker(
                    recovery_adapter,
                    pending_transaction.marker,
                    outcome="rolled_back",
                )
                if cleanup_warning is not None:
                    raise SwitchError(cleanup_warning)
            elif pending_transaction.lifecycle == "prepared":
                if dry_run:
                    pending_label = (
                        "switch"
                        if pending_transaction.operation == "switch"
                        else "restore"
                    )
                    return TransactionReceipt(
                        operation=request.operation,
                        outcome="dry_run",
                        preview_lines=(
                            f"pending {pending_label} recovery required before retry: "
                            f"{pending_transaction.backup_dir.name}",
                            "dry-run made no recovery or destination changes",
                        ),
                        backup_id=pending_transaction.backup_dir.name,
                    )
                if pending_transaction.operation == "switch":
                    recovery_adapter = _switch_filesystem_adapter(request)
                    recovery_desktop_adapter = None
                    if pending_transaction.journal.get("desktop_before") is not None:
                        recovery_desktop_adapter = _switch_desktop_binding_adapter(
                            store,
                            request,
                        )
                    _recover_prepared_switch(
                        store,
                        pending_transaction.backup_dir,
                        dict(pending_transaction.manifest),
                        recovery_adapter,
                        recovery_desktop_adapter,
                    )
                else:
                    recovery_adapter = _filesystem_adapter(request)
                    _recover_prepared_restore(
                        store,
                        pending_transaction.backup_dir,
                        pending_transaction.manifest,
                        recovery_adapter,
                    )
                cleanup_warning = _retire_pending_marker(
                    recovery_adapter,
                    pending_transaction.marker,
                    outcome="rolled_back",
                )
                if cleanup_warning is not None:
                    raise SwitchError(cleanup_warning)
            elif not dry_run:
                if request.operation == "switch":
                    pending_adapter = _switch_filesystem_adapter(request)
                elif request.operation == "capture":
                    if capture_adapter is None:
                        raise SwitchError(
                            "Capture filesystem adapter is unavailable"
                        )
                    pending_adapter = capture_adapter
                elif request.operation == "restore":
                    pending_adapter = _filesystem_adapter(request)
                else:
                    raise SwitchError(
                        f"Unsupported transaction operation: {request.operation}"
                    )
                cleanup_warning = _retire_pending_marker(
                    pending_adapter,
                    pending_transaction.marker,
                    outcome=pending_transaction.lifecycle,
                )
                if cleanup_warning is not None:
                    raise SwitchError(cleanup_warning)
        if request.operation == "switch":
            return _execute_switch(store, request, dry_run=dry_run)
        if request.operation == "capture":
            if capture_adapter is None:
                raise SwitchError("Capture filesystem adapter is unavailable")
            if dry_run:
                try:
                    store.profiles_dir.lstat()
                except FileNotFoundError:
                    return _execute_capture(
                        store,
                        request,
                        dry_run=True,
                        adapter=capture_adapter,
                        workspace=None,
                    )
                with _PinnedCaptureParent(
                    store.profiles_dir,
                    capture_adapter,
                ) as workspace:
                    return _execute_capture(
                        store,
                        request,
                        dry_run=True,
                        adapter=capture_adapter,
                        workspace=workspace,
                    )
            _ensure_capture_store_layout(store)
            with _PinnedCaptureParent(store.profiles_dir, capture_adapter) as workspace:
                return _execute_capture(
                    store,
                    request,
                    dry_run=False,
                    adapter=capture_adapter,
                    workspace=workspace,
                )
        if request.operation != "restore":
            raise SwitchError(f"Unsupported transaction operation: {request.operation}")
        backup_id = request.options.get("backup_id")
        if not isinstance(backup_id, str) or not backup_id:
            raise SwitchError("Restore transaction requires a backup_id")
        historical_backup_dir = _historical_backup_dir(store, backup_id)
        manifest = read_restore_manifest(historical_backup_dir)
        if manifest.lifecycle != "committed":
            raise SwitchError(
                f"Backup {backup_id} is not committed: {manifest.lifecycle}"
            )
        force = request.options.get("force", False)
        if not isinstance(force, bool):
            raise SwitchError("Restore transaction force option must be boolean")
        adapter = _filesystem_adapter(request)
        destinations, parent_cleanup_plans = _preflight_restore_targets(
            store,
            manifest,
        )
        _preflight_manifest_states(manifest)
        _preflight_payload_paths(historical_backup_dir, manifest)
        initial_states, initial_identities = _preflight_current_states(
            manifest,
            destinations,
            force=force,
            adapter=adapter,
        )
        target_route_guards = tuple(
            _capture_restore_route_guard(
                Path(entry.path).expanduser(),
                destination,
            )
            for entry, destination in zip(manifest.entries, destinations)
        )
        preview_lines = (
            f"restore backup {backup_id}",
            *(f"- restore {destination}" for destination in destinations),
        )
        if dry_run:
            return TransactionReceipt(
                operation="restore",
                outcome="dry_run",
                preview_lines=preview_lines,
                backup_id=None,
            )
        return _execute_restore_apply(
            store,
            manifest,
            historical_backup_dir,
            preview_lines,
            destinations,
            parent_cleanup_plans,
            adapter,
            initial_states,
            initial_identities,
            target_route_guards,
        )


def execute_transaction(
    store: Store,
    request: TransactionRequest,
    *,
    dry_run: bool = False,
) -> TransactionReceipt:
    return _execute_transaction(store, request, dry_run=dry_run)
