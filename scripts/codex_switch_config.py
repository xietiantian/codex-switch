from __future__ import annotations

from pathlib import Path

from codex_switch_config_document import ConfigDocument
from codex_switch_constants import PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE, SwitchError
from codex_switch_toml_edit import (
    append_toml_block,
    top_level_assignment,
    upsert_top_level_assignment,
)
from codex_switch_toml_scan import (
    extract_toml_table_block,
    toml_table_name,
)
from codex_switch_toml_validate import validate_toml_text

PROFILE_TABLE_PREFIXES = (
    "model_providers.",
    "profiles.",
)
PRESERVED_SHARED_TABLES = (
    "skills.config",
)
PRESERVED_SHARED_TABLE_PREFIXES = (
    "marketplaces.",
    "plugins.",
    "hooks.state.",
)
PLUGIN_SKILL_USAGE_TABLES = (
    "plugins",
    "skills.config",
)
PLUGIN_SKILL_USAGE_TABLE_PREFIXES = (
    "plugins.",
    "skills.config.",
)


def is_profile_specific_table(table_name: str) -> bool:
    return table_name == "model_providers" or table_name == "profiles" or any(
        table_name.startswith(prefix) for prefix in PROFILE_TABLE_PREFIXES
    )


def is_profile_specific_path(path: tuple[str, ...]) -> bool:
    return bool(path) and path[0] in {"model_providers", "profiles"}


def remove_top_level_assignment(text: str, key: str) -> str:
    kept: list[str] = []
    in_top_level = True
    for line in text.splitlines():
        if toml_table_name(line):
            in_top_level = False
        stripped = line.strip()
        if in_top_level and (stripped.startswith(f"{key} ") or stripped.startswith(f"{key}=")):
            continue
        kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def remove_legacy_profile_tables(text: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        table = toml_table_name(line)
        if table:
            skipping = table == "profiles" or table.startswith("profiles.")
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def remove_toml_table_block(text: str, table_name: str) -> str:
    kept: list[str] = []
    skipping = False
    nested_prefix = f"{table_name}."
    for line in text.splitlines():
        table = toml_table_name(line)
        if table:
            skipping = table == table_name or table.startswith(nested_prefix)
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def is_preserved_shared_table(table_name: str) -> bool:
    return table_name in PRESERVED_SHARED_TABLES or any(
        table_name.startswith(prefix) for prefix in PRESERVED_SHARED_TABLE_PREFIXES
    )


def is_preserved_shared_path(path: tuple[str, ...]) -> bool:
    return (
        path[:2] == ("skills", "config")
        or bool(path)
        and path[0] in {"marketplaces", "plugins"}
        or path[:2] == ("hooks", "state")
    )


def is_plugin_skill_usage_table(table_name: str) -> bool:
    return table_name in PLUGIN_SKILL_USAGE_TABLES or any(
        table_name.startswith(prefix)
        for prefix in PLUGIN_SKILL_USAGE_TABLE_PREFIXES
    )


def is_plugin_skill_usage_path(path: tuple[str, ...]) -> bool:
    return bool(path) and (
        path[0] == "plugins" or path[:2] == ("skills", "config")
    )


def is_preserved_shared_support_table(table_name: str) -> bool:
    return is_preserved_shared_table(table_name) and not is_plugin_skill_usage_table(
        table_name
    )


def is_preserved_shared_support_path(path: tuple[str, ...]) -> bool:
    return is_preserved_shared_path(path) and not is_plugin_skill_usage_path(path)


def recovery_text_or_raise(result, label: str) -> str:
    if result.diagnostics:
        codes = ", ".join(
            sorted({diagnostic.code for diagnostic in result.diagnostics})
        )
        raise SwitchError(f"{label}: Config Document diagnostics: {codes}")
    return result.text


def merge_preserved_shared_config_blocks(updated: str, preserve_source_text: str) -> str:
    current = ConfigDocument.parse(updated, "preserved shared config destination")
    source = ConfigDocument.parse(
        preserve_source_text,
        "preserved shared config source",
    ).select(
        include_top_level=lambda _path: False,
        include_table=lambda path, _is_array: is_preserved_shared_path(path),
        label="selected preserved shared config source",
    )
    without_preserved = current.select(
        include_top_level=lambda _path: True,
        include_table=lambda path, _is_array: not is_preserved_shared_path(path),
        label="preserved shared config destination without shared tables",
    )
    result = without_preserved.recover_missing_from(
        source,
        protected_paths=frozenset(),
    )
    return recovery_text_or_raise(result, "preserved shared config")


def merge_toml_table_overlay(updated: str, overlay_text: str, predicate, label: str) -> str:
    overlay = ConfigDocument.parse(overlay_text, f"{label} source").select(
        include_top_level=lambda _path: False,
        include_table=lambda path, _is_array: predicate(".".join(path)),
        label=f"{label} selected source",
    )
    if not overlay.tables:
        return updated
    current = ConfigDocument.parse(updated, f"{label} destination")
    overlaid = current.replace_values_from(overlay)
    return overlaid.recover_missing_from(
        overlay,
        protected_paths=frozenset(),
    ).text


def merge_preserved_shared_config_overlay(updated: str, overlay_text: str) -> str:
    return merge_toml_table_overlay(
        updated,
        overlay_text,
        is_preserved_shared_table,
        "preserved shared config overlay",
    )


def merge_preserved_shared_support_overlay(updated: str, overlay_text: str) -> str:
    return merge_toml_table_overlay(
        updated,
        overlay_text,
        is_preserved_shared_support_table,
        "preserved shared support overlay",
    )


def build_preserved_shared_config_text_from_text(text: str, label: str) -> str:
    return ConfigDocument.parse(text, label).select(
        include_top_level=lambda _path: False,
        include_table=lambda path, _is_array: is_preserved_shared_path(path),
        label=f"{label} selected preserved shared config",
    ).text


def replace_plugin_skill_usage_state(
    updated: str,
    authoritative_text: str,
    label: str,
) -> str:
    destination = ConfigDocument.parse(updated, f"{label} destination").select(
        include_top_level=lambda _path: True,
        include_table=lambda path, _is_array: not is_plugin_skill_usage_path(path),
        label=f"{label} destination without usage state",
    )
    authoritative = ConfigDocument.parse(
        authoritative_text,
        f"{label} authoritative source",
    ).select(
        include_top_level=lambda _path: False,
        include_table=lambda path, _is_array: is_plugin_skill_usage_path(path),
        label=f"{label} selected authoritative usage state",
    )
    result = destination.recover_missing_from(
        authoritative,
        protected_paths=frozenset(),
    )
    return recovery_text_or_raise(result, label)


def merge_shared_config_overlay(updated: str, overlay_text: str) -> str:
    overlay_shared = build_base_config_text_from_text(overlay_text, "shared config overlay")
    current = ConfigDocument.parse(updated, "shared config overlay destination")
    overlay = ConfigDocument.parse(overlay_shared, "shared config overlay source")
    overlaid = current.replace_values_from(overlay)
    return overlaid.recover_missing_from(
        overlay,
        protected_paths=frozenset(),
    ).text


def merge_missing_shared_config_defaults(updated: str, defaults_text: str) -> str:
    defaults_shared = build_base_config_text_from_text(
        defaults_text,
        "missing shared config defaults",
    )
    current = ConfigDocument.parse(updated, "missing shared config destination")
    defaults = ConfigDocument.parse(
        defaults_shared,
        "missing shared config defaults source",
    )
    return current.recover_missing_from(
        defaults,
        protected_paths=frozenset(),
    ).text


def merge_missing_non_usage_shared_config_defaults(
    updated: str,
    defaults_text: str,
) -> str:
    defaults_without_usage = ConfigDocument.parse(
        defaults_text,
        "non-usage shared config defaults",
    ).select(
        include_top_level=lambda _path: True,
        include_table=lambda path, _is_array: not is_plugin_skill_usage_path(path),
        label="non-usage shared config defaults without usage state",
    )
    return merge_missing_shared_config_defaults(updated, defaults_without_usage.text)


def profile_table_assignments(profile_block: str) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    for line in profile_block.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            assignments.append((key, stripped))
    return assignments


def target_profile_assignments(
    profile_text: str,
    profile_name: str,
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for key in PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE:
        assignment = top_level_assignment(profile_text, key)
        if assignment:
            assignments[key] = assignment

    profile_block = extract_toml_table_block(profile_text, f"profiles.{profile_name}")
    for key, assignment in profile_table_assignments(profile_block):
        if key in PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE:
            assignments[key] = assignment
    return assignments


def string_assignment_value(assignment: str) -> str:
    value = assignment.split("=", 1)[1].strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def append_target_model_provider_table(
    updated: str,
    profile_text: str,
    assignments: dict[str, str],
) -> str:
    provider_assignment = assignments.get("model_provider")
    if not provider_assignment:
        return updated
    provider = string_assignment_value(provider_assignment)
    if not provider:
        return updated
    table_name = f"model_providers.{provider}"
    provider_block = extract_toml_table_block(profile_text, table_name)
    if not provider_block:
        return updated
    updated = remove_toml_table_block(updated, table_name)
    return append_toml_block(updated, provider_block)


def build_profile_v2_config_text(profile_name: str, profile_config_path: Path) -> str:
    profile_text = profile_config_path.read_text()
    updated = remove_top_level_assignment(profile_text, "profile")
    updated = remove_legacy_profile_tables(updated)

    assignments = target_profile_assignments(profile_text, profile_name)
    for key, assignment in assignments.items():
        updated = upsert_top_level_assignment(updated, key, assignment)
    updated = append_target_model_provider_table(updated, profile_text, assignments)

    validate_toml_text(updated, str(profile_config_path))
    return updated


def append_model_provider_from_sources(
    updated: str,
    assignments: dict[str, str],
    sources: list[str],
) -> str:
    provider_assignment = assignments.get("model_provider")
    if not provider_assignment:
        return updated
    provider = string_assignment_value(provider_assignment)
    if not provider:
        return updated
    table_name = f"model_providers.{provider}"
    for source in sources:
        provider_block = extract_toml_table_block(source, table_name)
        if provider_block:
            updated = remove_toml_table_block(updated, table_name)
            return append_toml_block(updated, provider_block)
    return updated


def build_profile_seed_config_text(
    profile_name: str,
    profile_text: str,
    label: str,
    fallback_text: str | None = None,
    fallback_keys: set[str] | None = None,
) -> str:
    sources = [profile_text]
    if fallback_text is not None:
        sources.append(fallback_text)

    assignments = target_profile_assignments(profile_text, profile_name)
    if fallback_text is not None:
        fallback_assignments = target_profile_assignments(fallback_text, profile_name)
        for key, assignment in fallback_assignments.items():
            if fallback_keys is None or key in fallback_keys:
                assignments.setdefault(key, assignment)

    updated = ""
    for key in PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE:
        assignment = assignments.get(key)
        if assignment:
            updated = upsert_top_level_assignment(updated, key, assignment)
    updated = append_model_provider_from_sources(updated, assignments, sources)
    validate_toml_text(updated, label)
    return updated


def build_base_config_text_from_text(text: str, label: str) -> str:
    excluded_top_level = {"profile", *PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE}
    return ConfigDocument.parse(text, label).select(
        include_top_level=lambda path: not (
            len(path) == 1 and path[0] in excluded_top_level
        ),
        include_table=lambda path, _is_array: not is_profile_specific_path(path),
        label=f"{label} shared base",
    ).text


def build_base_config_text(base_config_path: Path) -> str:
    return build_base_config_text_from_text(
        base_config_path.read_text(),
        str(base_config_path),
    )


def config_uses_file_auth(config_text: str) -> bool:
    assignment = top_level_assignment(config_text, "cli_auth_credentials_store")
    if not assignment:
        return False
    value = assignment.split("=", 1)[1].strip().strip('"').strip("'").lower()
    return value == "file"


def config_requires_openai_auth(config_text: str) -> bool:
    """Match the provider credential contract used by staged binding apply."""
    data = ConfigDocument.parse(config_text, "runtime authentication config").data
    providers = data.get("model_providers", {})
    provider = providers.get(data.get("model_provider"), {}) if isinstance(providers, dict) else {}
    return isinstance(provider, dict) and provider.get("requires_openai_auth") is True


def build_shared_config_text(
    base_config_path: Path,
    profile_name: str,
    profile_config_path: Path,
) -> str:
    profile_text = profile_config_path.read_text()
    updated = base_config_path.read_text()
    updated = remove_top_level_assignment(updated, "profile")
    updated = remove_legacy_profile_tables(updated)

    for key in PROFILE_TOP_LEVEL_KEYS_FROM_PROFILE:
        updated = remove_top_level_assignment(updated, key)

    assignments = target_profile_assignments(profile_text, profile_name)
    for key, assignment in assignments.items():
        updated = upsert_top_level_assignment(updated, key, assignment)
    updated = append_target_model_provider_table(updated, profile_text, assignments)

    validate_toml_text(updated, str(base_config_path))
    return updated
