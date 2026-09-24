from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import os
import shutil
import sys
from pathlib import Path

from codex_switch_config import (
    build_base_config_text_from_text,
    build_profile_seed_config_text,
    build_preserved_shared_config_text_from_text,
    is_profile_specific_table,
    merge_missing_non_usage_shared_config_defaults,
    merge_preserved_shared_support_overlay,
    merge_shared_config_overlay,
    replace_plugin_skill_usage_state,
    string_assignment_value,
)
from codex_switch_constants import PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE, SwitchError
from codex_switch_io import atomic_write, ensure_private_dir
from codex_switch_parity import ConfigProjection
from codex_switch_toml_edit import top_level_assignment
from codex_switch_toml_scan import toml_table_name
from codex_switch_toml_validate import validate_toml_text


RUNTIME_STATE_NAMES = {
    "sessions",
    "session_index.jsonl",
    "history.jsonl",
    "archived_sessions",
    "log",
    "tmp",
    ".tmp",
    "ipc",
    "mcp-oauth-locks",
    "process_manager",
    "node_repl",
    "shell_snapshots",
    "browser",
    "ambient-suggestions",
}
NON_SHAREABLE_HOME_ENTRY_NAMES = {
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
    ".credentials.json",
    "agent-kb",
    "automations",
    "cache",
    "chrome-native-hosts-v2.json",
    "chrome-native-hosts.json",
    "computer-use",
    "installation_id",
    "model-catalogs",
    "models_cache.json",
    "plugins",
    "secrets",
    "sqlite",
    "update-backups",
    "vendor_imports",
    "version.json",
}
SHARED_SUPPORT_HOME_ENTRY_NAMES_V1 = frozenset(
    {
        "AGENTS.md",
        "prompts",
        "rules",
        "skills",
    }
)
MANAGED_COMMENT_PREFIX = "# codex-switch:"
DESKTOP_GLOBAL_STATE_NAME = ".codex-global-state.json"
DESKTOP_GLOBAL_STATE_TOP_LEVEL_SETTING_KEYS = {
    "appshotHotkey",
    "active-workspace-roots",
    "codex-mobile-has-connected-device",
    "computer-use-bundled-plugin-auto-install-disabled",
    "electron-chrome-extension-sync-managed-plugin-ids",
    "electron-internal-update-cdn-enabled",
    "electron-main-window-bounds",
    "electron-openai-mcp-form-elicitations-enabled",
    "electron-saved-workspace-roots",
    "primary-runtime-update-jitter-ms",
    "project-order",
    "use-copilot-auth-if-available",
}
DESKTOP_GLOBAL_STATE_ATOM_KEY = "electron-persisted-atom-state"
DESKTOP_GLOBAL_STATE_ATOM_SETTING_KEYS = {
    "agent-mode-by-host-id",
    "app-shell:bottom-panel-height",
    "browser-sidebar-bottom-reserve-collapsed-v1",
    "browser-sidebar-comment-mode-coachmark-dismissed",
    "codex-mobile-sidebar-nav-item-clicked-v1",
    "codexCloudAccess",
    "composer-auto-context-enabled",
    "composer-permission-mode-visibility",
    "diff-filter",
    "editorDiffViewMode",
    "environment",
    "fast-mode-personalized-estimate",
    "has-accepted-appshot-intro",
    "has-seen-ambient-suggestions-connected-apps-consent",
    "has-seen-codex-mobile-announcement",
    "has-seen-fast-mode-announcement",
    "has-seen-fast-mode-home-banner",
    "has-seen-multi-agent-composer-banner",
    "has-seen-work-plugins-announcement",
    "has-user-changed-service-tier",
    "last_completed_onboarding",
    "local-conversation-status-section-visible",
    "preferred-non-full-access-agent-mode-by-host-id",
    "rate-limit-reset-home-announcement-dismissal-by-account-id",
    "seen-model-upgrade-list",
    "sidebar-collapsed-groups",
    "sidebar-collapsed-sections-v1",
    "sidebar-organize-mode-v1",
    "sidebar-width",
    "skip-full-access-confirm",
    "thread-sort-key",
    "thread-summary-panel-section-expanded-progress",
    "thread-summary-panel-section-expanded-tool-sources",
}
DESKTOP_GLOBAL_STATE_ATOM_SETTING_PREFIXES = (
    "app-shell:right-panel-width:v2:",
    "electron:onboarding-",
    "sidebar-project-expanded-v1-codex:",
)
DESKTOP_GLOBAL_STATE_ATOM_RUNTIME_KEYS = {
    "composer-prompt-drafts-v1",
    "heartbeat-thread-permissions-by-id",
    "prompt-history",
    "unread-thread-ids-by-host-v1",
}
DESKTOP_GLOBAL_STATE_ATOM_RUNTIME_PREFIXES = (
    "remote-thread-summaries:",
    "thread-client-id-v1:",
)


@dataclass(frozen=True)
class PlannedHomeWrite:
    path: Path
    payload: bytes
    mode: int


def is_runtime_state_name(name: str) -> bool:
    return (
        name in RUNTIME_STATE_NAMES
        or name.endswith(".sqlite")
        or name.endswith(".sqlite-shm")
        or name.endswith(".sqlite-wal")
        or ".sqlite.corrupt." in name
        or ".sqlite-shm.corrupt." in name
        or ".sqlite-wal.corrupt." in name
    )


def is_profile_state_name(name: str) -> bool:
    return name == "config.toml" or name == "auth.json" or name.endswith(".config.toml")


def is_non_shareable_home_entry_name(name: str) -> bool:
    return name in NON_SHAREABLE_HOME_ENTRY_NAMES


def is_shareable_home_entry(path: Path) -> bool:
    return path.name in SHARED_SUPPORT_HOME_ENTRY_NAMES_V1


def is_desktop_global_state_atom_setting_key(key: str) -> bool:
    if key in DESKTOP_GLOBAL_STATE_ATOM_RUNTIME_KEYS:
        return False
    if any(key.startswith(prefix) for prefix in DESKTOP_GLOBAL_STATE_ATOM_RUNTIME_PREFIXES):
        return False
    return (
        key in DESKTOP_GLOBAL_STATE_ATOM_SETTING_KEYS
        or any(
            key.startswith(prefix)
            for prefix in DESKTOP_GLOBAL_STATE_ATOM_SETTING_PREFIXES
        )
    )


def desktop_global_state_settings_subset(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return {}

    subset: dict[str, object] = {}
    for key in DESKTOP_GLOBAL_STATE_TOP_LEVEL_SETTING_KEYS:
        if key in data:
            subset[key] = deepcopy(data[key])

    atom = data.get(DESKTOP_GLOBAL_STATE_ATOM_KEY)
    if isinstance(atom, dict):
        atom_subset = {
            key: deepcopy(value)
            for key, value in atom.items()
            if isinstance(key, str)
            and is_desktop_global_state_atom_setting_key(key)
        }
        if atom_subset:
            subset[DESKTOP_GLOBAL_STATE_ATOM_KEY] = atom_subset

    return subset


def merge_desktop_global_state_settings(
    source: object,
    target: object,
) -> dict[str, object]:
    merged = deepcopy(target) if isinstance(target, dict) else {}
    settings = desktop_global_state_settings_subset(source)

    for key, value in settings.items():
        if key != DESKTOP_GLOBAL_STATE_ATOM_KEY:
            merged[key] = deepcopy(value)
            continue

        target_atom = merged.get(DESKTOP_GLOBAL_STATE_ATOM_KEY)
        if not isinstance(target_atom, dict):
            target_atom = {}
        else:
            target_atom = deepcopy(target_atom)
        source_atom = value if isinstance(value, dict) else {}
        for atom_key, atom_value in source_atom.items():
            target_atom[atom_key] = deepcopy(atom_value)
        if target_atom:
            merged[DESKTOP_GLOBAL_STATE_ATOM_KEY] = target_atom

    return merged


def read_json_object_if_valid(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def desktop_global_state_path(home: Path) -> Path:
    return home / DESKTOP_GLOBAL_STATE_NAME


def sync_desktop_global_state_settings(source_home: Path, target_home: Path) -> Path | None:
    source_path = desktop_global_state_path(source_home)
    if not source_path.exists() and not source_path.is_symlink():
        return None

    source_data = read_json_object_if_valid(source_path)
    if not desktop_global_state_settings_subset(source_data):
        return None

    target_path = desktop_global_state_path(target_home)
    target_data = read_json_object_if_valid(target_path)
    merged = merge_desktop_global_state_settings(source_data, target_data)
    if merged == target_data:
        return None

    mode = 0o600
    if target_path.exists() and not target_path.is_symlink():
        mode = target_path.stat().st_mode & 0o777
    elif source_path.exists() and not source_path.is_symlink():
        mode = source_path.stat().st_mode & 0o777
    atomic_write(
        target_path,
        (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode(),
        mode=mode,
    )
    return target_path


def shared_support_entries(source_home: Path) -> list[Path]:
    if not source_home.exists():
        return []
    return sorted(
        (path for path in source_home.iterdir() if is_shareable_home_entry(path)),
        key=lambda path: path.name,
    )


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def absolute_symlink_target(path: Path) -> Path:
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent / target
    return Path(os.path.abspath(target))


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(parent)))
        return True
    except ValueError:
        return False


def symlink_points_within(path: Path, parent: Path) -> bool:
    return path.is_symlink() and path_is_within(absolute_symlink_target(path), parent)


def symlink_points_to_itself(path: Path) -> bool:
    return path.is_symlink() and absolute_symlink_target(path) == Path(os.path.abspath(path))


def shareable_symlink_rejection_reason(
    path: Path,
    source_home: Path,
    target_home: Path,
) -> str | None:
    if not path.is_symlink():
        return None
    raw_target = Path(os.readlink(path))
    if not raw_target.is_absolute():
        return "relative"
    absolute_target = absolute_symlink_target(path)
    if absolute_target == Path(os.path.abspath(path)):
        return "self-referential"
    try:
        resolved_target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return "dangling"
    if path_is_within(resolved_target, source_home.resolve(strict=False)):
        return "source-home"
    if path_is_within(resolved_target, target_home.resolve(strict=False)):
        return "target-home"
    return None


def should_remove_rejected_shareable_target(
    target: Path,
    source_home: Path,
    target_home: Path,
) -> bool:
    return (
        target.is_symlink()
        and shareable_symlink_rejection_reason(
            target,
            source_home,
            target_home,
        )
        is not None
    )


def unsafe_copytree_symlinks(target_home: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for name in names:
            path = Path(directory) / name
            if symlink_points_to_itself(path) or symlink_points_within(path, target_home):
                skipped.add(name)
        return skipped

    return ignore


def copy_or_link_shared_entry(source: Path, target: Path, prefer_link: bool) -> None:
    if source.is_symlink():
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
                remove_path(target)
            return
        link_target = os.readlink(source)
        if target.is_symlink() and os.readlink(target) == link_target:
            return
        if target.exists() or target.is_symlink():
            remove_path(target)
        ensure_private_dir(target.parent)
        target.symlink_to(link_target)
        return

    if prefer_link and source.is_dir():
        if path_is_within(source, target.parent):
            if symlink_points_within(target, target.parent):
                remove_path(target)
            return
        if target.is_symlink() and Path(os.readlink(target)) == source:
            return
        if target.is_symlink():
            remove_path(target)
        if not target.exists() and not target.is_symlink():
            ensure_private_dir(target.parent)
            target.symlink_to(source, target_is_directory=True)
        return

    if source.is_file():
        atomic_write(target, source.read_bytes(), mode=source.stat().st_mode & 0o777)
        return

    if source.is_dir() and not target.exists() and not target.is_symlink():
        ensure_private_dir(target.parent)
        shutil.copytree(
            source,
            target,
            symlinks=True,
            ignore=unsafe_copytree_symlinks(target.parent),
        )


def shared_support_targets(source_home: Path, target_home: Path) -> list[Path]:
    return [target_home / entry.name for entry in shared_support_entries(source_home)]


def sync_shared_support(source_home: Path, target_home: Path, prefer_link: bool) -> list[Path]:
    mutated: list[Path] = []
    for source in shared_support_entries(source_home):
        target = target_home / source.name
        before_exists = target.exists() or target.is_symlink()
        copy_or_link_shared_entry(source, target, prefer_link=prefer_link)
        if before_exists or target.exists() or target.is_symlink():
            mutated.append(target)
    return mutated


def stale_runtime_links(home: Path, source_home: Path) -> list[Path]:
    del source_home
    if not home.exists():
        return []
    return sorted(
        (
            path
            for path in home.iterdir()
            if path.is_symlink()
            and (
                is_runtime_state_name(path.name)
                or is_non_shareable_home_entry_name(path.name)
            )
        ),
        key=lambda path: path.name,
    )


def remove_stale_runtime_links(home: Path, source_home: Path) -> list[Path]:
    removed: list[Path] = []
    for path in stale_runtime_links(home, source_home):
        path.unlink()
        removed.append(path)
    return removed


def strip_managed_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(MANAGED_COMMENT_PREFIX):
            while lines and not lines[-1].strip():
                lines.pop()
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def top_level_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def append_section_header(lines: list[str], section: str) -> None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{MANAGED_COMMENT_PREFIX} {section} settings")


def annotate_config_sections(text: str) -> str:
    annotated: list[str] = []
    current_section: str | None = None
    in_top_level = True

    for line in text.strip().splitlines():
        table = toml_table_name(line)
        if table:
            in_top_level = False
            section = "profile-specific" if is_profile_specific_table(table) else "shared"
        elif in_top_level:
            key = top_level_key(line)
            if key is None:
                section = current_section
            else:
                section = (
                    "profile-specific"
                    if key in PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE
                    else "shared"
                )
        else:
            section = current_section

        if section != current_section and section in {"profile-specific", "shared"}:
            append_section_header(annotated, section)
            current_section = section
        annotated.append(line)

    return "\n".join(annotated).strip() + "\n"


def annotate_runtime_config(
    text: str,
    profile_name: str,
    shared_source: Path,
    profile_source: str,
) -> str:
    body = strip_managed_comments(text)
    body = annotate_config_sections(body)
    annotated = (
        f"{MANAGED_COMMENT_PREFIX} managed runtime config for profile {profile_name}\n"
        f"{MANAGED_COMMENT_PREFIX} shared settings are merged from {shared_source}\n"
        f"{MANAGED_COMMENT_PREFIX} profile-specific settings are preserved from {profile_source}\n"
        "\n"
        f"{body}"
    )
    validate_toml_text(annotated, f"annotated runtime config for {profile_name}")
    return annotated


def annotate_canonical_profile_config(text: str, profile_name: str) -> str:
    body = annotate_config_sections(strip_managed_comments(text))
    annotated = (
        f"{MANAGED_COMMENT_PREFIX} canonical fallback config for profile {profile_name}\n"
        f"{MANAGED_COMMENT_PREFIX} profile-specific settings only; shared settings stay in active homes\n"
        "\n"
        f"{body}"
    )
    validate_toml_text(annotated, f"canonical profile config for {profile_name}")
    return annotated


def annotate_plugin_support_snapshot(text: str, profile_name: str) -> str:
    body = strip_managed_comments(text)
    annotated = (
        f"{MANAGED_COMMENT_PREFIX} profile-local plugin support snapshot for {profile_name}\n"
        f"{MANAGED_COMMENT_PREFIX} contains marketplace, plugin, skill, and hook trust settings\n"
    )
    if body.strip():
        annotated += f"\n{body}"
    validate_toml_text(annotated, f"plugin support snapshot for {profile_name}")
    return annotated


def managed_runtime_profile_name(text: str) -> str | None:
    prefix = f"{MANAGED_COMMENT_PREFIX} managed runtime config for profile "
    for line in text.splitlines()[:5]:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def read_valid_config(path: Path, label: str) -> str:
    text = path.read_text()
    validate_toml_text(text, label)
    return text


def catalog_models(data) -> list[dict]:
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict)]


def model_entry_matches(model: dict, model_slug: str) -> bool:
    for key in ("slug", "id", "name", "model"):
        if model.get(key) == model_slug:
            return True
    return False


def supported_reasoning_efforts_from_catalog(seed_text: str) -> set[str] | None:
    model_assignment = top_level_assignment(seed_text, "model")
    catalog_assignment = top_level_assignment(seed_text, "model_catalog_json")
    if not model_assignment or not catalog_assignment:
        return None

    model_slug = string_assignment_value(model_assignment)
    catalog_path = Path(string_assignment_value(catalog_assignment)).expanduser()
    if not model_slug or not catalog_path.is_absolute() or not catalog_path.exists():
        return None

    try:
        data = json.loads(catalog_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    for model in catalog_models(data):
        if not model_entry_matches(model, model_slug):
            continue
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list):
            return None
        efforts: set[str] = set()
        for level in levels:
            if isinstance(level, str):
                efforts.add(level)
            elif isinstance(level, dict) and isinstance(level.get("effort"), str):
                efforts.add(level["effort"])
        return efforts or None
    return None


def validate_profile_seed_reasoning_effort(seed_text: str, label: str) -> None:
    effort_assignment = top_level_assignment(seed_text, "model_reasoning_effort")
    if not effort_assignment:
        return
    supported_efforts = supported_reasoning_efforts_from_catalog(seed_text)
    if supported_efforts is None:
        return
    effort = string_assignment_value(effort_assignment)
    if effort not in supported_efforts:
        supported = ", ".join(sorted(supported_efforts))
        raise SwitchError(
            f"Unsupported model_reasoning_effort {effort!r} in {label}; "
            f"supported values from model_catalog_json are: {supported}"
        )


def merge_shared_with_profile_seed(
    shared_source_config: Path,
    profile_name: str,
    target_runtime_config: Path,
    canonical_config: Path,
    profile_layer_configs: list[Path] | None = None,
    profile_shared_layer_configs: list[Path] | None = None,
    shared_source_text_override: str | None = None,
    profile_seed_text_override: str | None = None,
) -> str:
    shared_source_text = shared_source_text_override or ""
    if shared_source_text_override is not None:
        validate_toml_text(
            shared_source_text_override,
            f"shared source config: {shared_source_config}",
        )
    elif shared_source_config.exists():
        shared_source_text = read_valid_config(
            shared_source_config,
            f"shared source config: {shared_source_config}",
        )
    shared_text = (
        build_base_config_text_from_text(shared_source_text, str(shared_source_config))
        if shared_source_text
        else ""
    )
    if profile_seed_text_override is not None:
        validate_toml_text(
            profile_seed_text_override,
            f"projected profile config: {canonical_config}",
        )
        shared_text = merge_shared_config_overlay(
            shared_text,
            profile_seed_text_override,
        )
    errors: list[str] = []
    target_runtime_text: str | None = None
    if profile_seed_text_override is None and target_runtime_config.exists():
        try:
            target_runtime_text = read_valid_config(
                target_runtime_config,
                f"last runtime config: {target_runtime_config}",
            )
            shared_text = merge_missing_non_usage_shared_config_defaults(
                shared_text,
                target_runtime_text,
            )
        except SwitchError as exc:
            errors.append(str(exc))

    for path in unique_paths(profile_shared_layer_configs or []):
        if not path.exists():
            continue
        try:
            shared_layer_text = read_valid_config(
                path,
                f"profile plugin support snapshot: {path}",
            )
            shared_text = merge_preserved_shared_defaults_from_text(
                shared_text,
                shared_layer_text,
                f"profile plugin support snapshot: {path}",
            )
        except SwitchError as exc:
            errors.append(str(exc))

    profile_layers = unique_paths(profile_layer_configs or [])
    profile_layer_texts: dict[Path, str] = {}
    for path in profile_layers:
        if not path.exists():
            continue
        try:
            profile_layer_text = read_valid_config(path, f"profile layer shared config: {path}")
            profile_layer_texts[path] = profile_layer_text
            shared_text = merge_preserved_shared_support_overlay(
                shared_text,
                profile_layer_text,
            )
        except SwitchError as exc:
            errors.append(str(exc))

    candidates: list[tuple[Path, str, str | None]] = []
    if profile_seed_text_override is not None:
        candidates.append(
            (
                canonical_config,
                "projected canonical config",
                profile_seed_text_override,
            )
        )
    else:
        if target_runtime_text is not None:
            try:
                if not should_skip_managed_runtime_seed(
                    profile_name,
                    target_runtime_text,
                    profile_layer_texts,
                    shared_source_text,
                ):
                    candidates.append(
                        (
                            target_runtime_config,
                            "last runtime config",
                            target_runtime_text,
                        )
                    )
            except SwitchError as exc:
                errors.append(str(exc))
        for path in profile_layers:
            if path.exists():
                candidates.append((path, "profile layer config", None))
        candidates.append((canonical_config, "fallback canonical config", None))

    for path, label, preloaded_text in candidates:
        try:
            profile_text = preloaded_text
            if profile_text is None:
                profile_text = read_valid_config(path, f"{label}: {path}")
            seed_text = build_profile_seed_config_text(
                profile_name,
                profile_text,
                f"profile seed from {label}: {path}",
            )
            validate_profile_seed_reasoning_effort(seed_text, f"{label}: {path}")
            merged = merge_shared_config_overlay(seed_text, shared_text)
            validate_toml_text(merged, f"runtime config for {profile_name}")
            profile_source = "fallback canonical config" if label.startswith("fallback") else label
            return annotate_runtime_config(
                merged,
                profile_name=profile_name,
                shared_source=shared_source_config,
                profile_source=profile_source,
            )
        except SwitchError as exc:
            errors.append(str(exc))

    raise SwitchError(
        f"Unable to build runtime config for {profile_name}; "
        + "; ".join(errors)
    )


def merge_preserved_shared_defaults_from_text(
    shared_text: str,
    defaults_text: str,
    label: str,
) -> str:
    preserved_text = build_preserved_shared_config_text_from_text(defaults_text, label)
    if not preserved_text.strip():
        return shared_text
    return merge_missing_non_usage_shared_config_defaults(
        shared_text,
        preserved_text,
    )


def should_skip_managed_runtime_seed(
    profile_name: str,
    runtime_text: str,
    profile_layer_texts: dict[Path, str],
    shared_source_text: str = "",
) -> bool:
    if profile_name != "openai-official":
        return False
    if not profile_layer_texts:
        return False
    if not top_level_assignment(runtime_text, "model_provider"):
        return False
    if any(
        top_level_assignment(profile_layer_text, "model_provider")
        for profile_layer_text in profile_layer_texts.values()
    ):
        return False
    if any(
        top_level_assignment(profile_layer_text, "model")
        for profile_layer_text in profile_layer_texts.values()
    ):
        return True
    if managed_runtime_profile_name(runtime_text) == profile_name:
        return True
    return profile_seed_matches_shared_source(profile_name, runtime_text, shared_source_text)


def profile_seed_matches_shared_source(
    profile_name: str,
    runtime_text: str,
    shared_source_text: str,
) -> bool:
    if not shared_source_text:
        return False
    try:
        runtime_seed = build_profile_seed_config_text(
            profile_name,
            runtime_text,
            "runtime profile seed comparison",
        )
        shared_seed = build_profile_seed_config_text(
            profile_name,
            shared_source_text,
            "shared source profile seed comparison",
        )
    except SwitchError:
        return False
    return strip_managed_comments(runtime_seed) == strip_managed_comments(shared_seed)


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = Path(os.path.abspath(path))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def build_internal_home_config(
    official_home: Path,
    profile_name: str,
    target_runtime_config: Path,
    canonical_config: Path,
    *,
    shared_source_text_override: str | None = None,
    config_projection: ConfigProjection | None = None,
) -> str:
    profile_seed_text_override: str | None = None
    private_profile_seed = False
    if config_projection is not None:
        if not isinstance(config_projection, ConfigProjection):
            raise SwitchError("Internal home config projection is invalid.")
        if profile_name != "internal":
            raise SwitchError(
                "Parity config projection is supported only for internal."
            )
        if not config_projection.healthy:
            raise SwitchError("Parity config projection is unhealthy.")
        if shared_source_text_override is not None:
            raise SwitchError(
                "Parity config projection cannot be combined with a shared "
                "source text override."
            )

        canonical_profile = Path(os.path.abspath(canonical_config))
        if config_projection.config_inputs.profile_config != canonical_profile:
            raise SwitchError(
                "Parity config projection does not match the internal profile."
            )
        private_profile_seed = (
            config_projection.config_inputs.profile_source != canonical_profile
        )
        shared_source_config = official_home / "config.toml"
        try:
            profile_seed_text_override = config_projection.payload_for(
                canonical_profile
            ).decode("utf-8")
            shared_source_text_override = config_projection.payload_for(
                shared_source_config
            ).decode("utf-8")
        except (KeyError, UnicodeDecodeError) as exc:
            raise SwitchError(
                "Parity config projection does not cover canonical home inputs."
            ) from exc

    return merge_shared_with_profile_seed(
        official_home / "config.toml",
        profile_name,
        target_runtime_config,
        canonical_config,
        # A captured final seed replaces the saved profile input for this
        # preparation only. Legacy profile layers must not restore removed
        # settings. Ordinary runtime/profile fallback keeps its existing order.
        profile_layer_configs=[] if private_profile_seed else [
            target_runtime_config.parent / f"{profile_name}.config.toml",
            official_home / f"{profile_name}.config.toml",
        ],
        profile_shared_layer_configs=[] if private_profile_seed else [
            target_runtime_config.parent / plugin_support_snapshot_name(profile_name),
            official_home / plugin_support_snapshot_name(profile_name),
            canonical_config.parent / plugin_support_snapshot_name(profile_name),
            official_home / plugin_support_snapshot_name("openai-official"),
            canonical_config.parent.parent
            / "openai-official"
            / plugin_support_snapshot_name("openai-official"),
        ],
        shared_source_text_override=shared_source_text_override,
        profile_seed_text_override=profile_seed_text_override,
    )


def build_official_home_config_from_internal(
    official_home: Path,
    internal_home: Path,
    canonical_config: Path,
) -> str | None:
    official_config = official_home / "config.toml"
    internal_config = internal_home / "config.toml"
    if not internal_config.exists():
        return None
    return merge_shared_with_profile_seed(
        internal_config,
        "openai-official",
        official_config,
        canonical_config,
        profile_layer_configs=[
            official_home / "openai-official.config.toml",
            internal_home / "openai-official.config.toml",
        ],
        profile_shared_layer_configs=[
            official_home / plugin_support_snapshot_name("openai-official"),
            internal_home / plugin_support_snapshot_name("openai-official"),
            canonical_config.parent / plugin_support_snapshot_name("openai-official"),
            internal_home / plugin_support_snapshot_name("internal"),
            canonical_config.parent.parent
            / "internal"
            / plugin_support_snapshot_name("internal"),
        ],
    )


def refresh_profile_canonical_config(
    profile_name: str,
    runtime_config_path: Path,
    canonical_config_path: Path,
) -> str:
    runtime_text = runtime_config_path.read_text()
    fallback_text = canonical_config_path.read_text() if canonical_config_path.exists() else None
    seed_text = build_profile_seed_config_text(
        profile_name,
        runtime_text,
        f"canonical profile seed for {profile_name}",
        fallback_text=fallback_text,
        fallback_keys={"cli_auth_credentials_store"},
    )
    seed_text = annotate_canonical_profile_config(seed_text, profile_name)
    atomic_write(canonical_config_path, seed_text.encode(), mode=0o600)
    return seed_text


def plugin_support_snapshot_name(profile_name: str) -> str:
    return f"{profile_name}.plugin-support.config.toml"


def refresh_profile_plugin_support_snapshot(
    profile_name: str,
    runtime_config_path: Path,
    snapshot_paths: list[Path],
) -> str:
    runtime_text = read_valid_config(
        runtime_config_path,
        f"runtime config for plugin support snapshot: {runtime_config_path}",
    )
    snapshot_text = build_profile_plugin_support_snapshot_text(
        profile_name,
        runtime_text,
        snapshot_paths,
    )
    for path in unique_paths(snapshot_paths):
        atomic_write(path, snapshot_text.encode(), mode=0o600)
    return snapshot_text


def build_profile_plugin_support_snapshot_text(
    profile_name: str,
    runtime_text: str,
    snapshot_paths: list[Path],
) -> str:
    validate_toml_text(
        runtime_text,
        f"runtime config for plugin support snapshot: {profile_name}",
    )
    snapshot_text = build_preserved_shared_config_text_from_text(
        runtime_text,
        f"plugin support snapshot for {profile_name}",
    )
    for path in unique_paths(snapshot_paths):
        if not path.exists():
            continue
        try:
            existing_text = read_valid_config(
                path,
                f"existing plugin support snapshot: {path}",
            )
            existing_snapshot_text = build_preserved_shared_config_text_from_text(
                existing_text,
                f"existing plugin support snapshot: {path}",
            )
            snapshot_text = merge_missing_non_usage_shared_config_defaults(
                snapshot_text,
                existing_snapshot_text,
            )
        except SwitchError:
            continue
    return annotate_plugin_support_snapshot(snapshot_text, profile_name)


def _planned_desktop_state_write(
    source_path: Path,
    source_data: dict[str, object],
    target_path: Path,
    target_data: dict[str, object],
) -> PlannedHomeWrite | None:
    if not desktop_global_state_settings_subset(source_data):
        return None
    merged = merge_desktop_global_state_settings(source_data, target_data)
    if merged == target_data:
        return None
    mode = 0o600
    if target_path.exists() and not target_path.is_symlink():
        mode = target_path.stat().st_mode & 0o777
    elif source_path.exists() and not source_path.is_symlink():
        mode = source_path.stat().st_mode & 0o777
    return PlannedHomeWrite(
        path=target_path,
        payload=(json.dumps(merged, indent=2, sort_keys=True) + "\n").encode(),
        mode=mode,
    )


def _plan_bidirectional_desktop_state_writes(
    live_home: Path,
    app_home: Path,
) -> tuple[PlannedHomeWrite, ...]:
    live_path = desktop_global_state_path(live_home)
    app_path = desktop_global_state_path(app_home)
    live_data = read_json_object_if_valid(live_path)
    app_data = read_json_object_if_valid(app_path)
    writes: list[PlannedHomeWrite] = []

    live_write = _planned_desktop_state_write(
        app_path,
        app_data,
        live_path,
        live_data,
    )
    if live_write is not None:
        writes.append(live_write)
        live_data = json.loads(live_write.payload)

    app_write = _planned_desktop_state_write(
        live_path,
        live_data,
        app_path,
        app_data,
    )
    if app_write is not None:
        writes.append(app_write)
    return tuple(writes)


def sync_profile_app_home_for_launch(
    live_home: Path,
    app_home: Path,
    profile_config: Path,
    profile_name: str,
) -> tuple[Path, ...]:
    live_config = live_home / "config.toml"
    app_config = app_home / "config.toml"
    read_valid_config(
        profile_config,
        f"launcher profile config: {profile_config}",
    )
    live_text = read_valid_config(
        live_config,
        f"launcher shared config: {live_config}",
    )
    app_text: str | None = None
    updated_live_text = live_text
    if app_config.exists():
        app_text = read_valid_config(
            app_config,
            f"launcher runtime config: {app_config}",
        )
        updated_live_text = replace_plugin_skill_usage_state(
            live_text,
            app_text,
            "internal Desktop plugin and skill usage sync",
        )
        updated_live_text = merge_shared_config_overlay(
            updated_live_text,
            app_text,
        )
        validate_toml_text(
            updated_live_text,
            f"launcher shared config: {live_config}",
        )

    target_config_text = build_internal_home_config(
        live_home,
        profile_name,
        app_config,
        profile_config,
        shared_source_text_override=updated_live_text,
    )
    if (
        profile_name == "internal"
        and app_text is not None
        and strip_managed_comments(target_config_text)
        == strip_managed_comments(app_text)
    ):
        target_config_text = app_text
    snapshot_paths = [
        app_home / plugin_support_snapshot_name(profile_name),
        profile_config.parent / plugin_support_snapshot_name(profile_name),
    ]
    snapshot_text = build_profile_plugin_support_snapshot_text(
        profile_name,
        target_config_text,
        snapshot_paths,
    )
    desktop_writes = _plan_bidirectional_desktop_state_writes(
        live_home,
        app_home,
    )
    isolated_links = tuple(stale_runtime_links(app_home, live_home))
    shared_sources = tuple(shared_support_entries(live_home))

    ensure_private_dir(app_home)
    mutated: list[Path] = []
    for path in isolated_links:
        if path.is_symlink():
            path.unlink()
            mutated.append(path)
    for source in shared_sources:
        target = app_home / source.name
        before_target = os.readlink(target) if target.is_symlink() else None
        before_exists = target.exists() or target.is_symlink()
        copy_or_link_shared_entry(source, target, prefer_link=True)
        after_target = os.readlink(target) if target.is_symlink() else None
        if (
            before_exists != (target.exists() or target.is_symlink())
            or before_target != after_target
        ):
            mutated.append(target)
    for write in desktop_writes:
        atomic_write(write.path, write.payload, mode=write.mode)
        mutated.append(write.path)
    if app_text is not None:
        atomic_write(live_config, updated_live_text.encode(), mode=0o600)
        mutated.append(live_config)
    atomic_write(app_config, target_config_text.encode(), mode=0o600)
    mutated.append(app_config)
    for snapshot_path in unique_paths(snapshot_paths):
        atomic_write(snapshot_path, snapshot_text.encode(), mode=0o600)
        mutated.append(snapshot_path)
    auth_path = app_home / "auth.json"
    if auth_path.exists() or auth_path.is_symlink():
        remove_path(auth_path)
        mutated.append(auth_path)
    return tuple(mutated)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=Path(argv[0]).name)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-launch")
    prepare.add_argument("--live-home", required=True)
    prepare.add_argument("--app-home", required=True)
    prepare.add_argument("--profile-config", required=True)
    prepare.add_argument("--profile-name", required=True)
    args = parser.parse_args(argv[1:])
    try:
        if args.command == "prepare-launch":
            sync_profile_app_home_for_launch(
                Path(args.live_home).expanduser(),
                Path(args.app_home).expanduser(),
                Path(args.profile_config).expanduser(),
                str(args.profile_name),
            )
            return 0
    except (OSError, SwitchError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
