from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_switch_constants import SwitchError
from codex_switch_io import now_stamp
from codex_switch_paths import equivalent_paths
from codex_switch_store import Store


@dataclass
class ProfileHome:
    path: Path
    mode: str
    source: str


@dataclass
class IndependentHomes:
    internal: ProfileHome
    official: ProfileHome
    manifest_updates: dict[str, dict[str, Any]]


def path_from_manifest(manifest: dict[str, Any]) -> Path | None:
    raw = manifest.get("codex_home")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def manifest_home_confirmed(manifest: dict[str, Any]) -> bool:
    return manifest.get("home_selection_confirmed") is True


def path_matches(left: Path, right: Path) -> bool:
    return equivalent_paths(str(left), str(right))


def home_mode(store: Store, profile: str, home: Path) -> str:
    if path_matches(home, store.managed_home(profile)):
        return "managed"
    if profile == "openai-official" and path_matches(home, store.official_codex_home):
        return "official"
    return "adopted"


def profile_home_binding(
    store: Store,
    profile: str,
    manifest: dict[str, Any],
) -> ProfileHome:
    if profile == "internal" and store.internal_codex_home is not None:
        home = store.internal_codex_home
        return ProfileHome(home, home_mode(store, profile, home), "explicit")

    manifest_home = path_from_manifest(manifest)
    if manifest_home is not None:
        source = "manifest" if manifest_home_confirmed(manifest) else "manifest_unconfirmed"
        return ProfileHome(manifest_home, home_mode(store, profile, manifest_home), source)

    if profile == "openai-official":
        home = store.official_codex_home
        return ProfileHome(home, home_mode(store, profile, home), store.official_codex_home_source)

    home = store.managed_home(profile)
    return ProfileHome(home, home_mode(store, profile, home), "default")


def prompt_enabled(dry_run: bool) -> bool:
    if dry_run:
        return False
    return sys.stdin.isatty() or os.environ.get("CODEX_SWITCH_FORCE_HOME_PROMPT") == "1"


def dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    for path in paths:
        if not any(path_matches(path, existing) for existing in deduped):
            deduped.append(path)
    return deduped


def other_profile(profile: str) -> str:
    return "openai-official" if profile == "internal" else "internal"


def recommended_alternate_home(store: Store, profile: str, forbidden: Path | None) -> Path:
    candidates = [
        store.managed_home(profile),
        store.official_codex_home,
        store.managed_home(other_profile(profile)),
    ]
    for candidate in candidates:
        if forbidden is None or not path_matches(candidate, forbidden):
            return candidate
    return store.managed_home(profile)


def semantic_recommended_home(store: Store, profile: str) -> Path:
    if profile == "openai-official":
        return store.official_codex_home
    return store.managed_home(profile)


def prompt_profile_home(
    store: Store,
    profile: str,
    current: ProfileHome,
    other_home: Path,
    *,
    forbidden: Path | None = None,
) -> ProfileHome:
    recommended = semantic_recommended_home(store, profile)
    if forbidden is not None and path_matches(recommended, forbidden):
        recommended = recommended_alternate_home(store, profile, forbidden)
    choices = dedupe_paths(
        [
            recommended,
            current.path,
            other_home,
            store.managed_home(profile),
            store.official_codex_home,
        ]
    )
    print(f"Select Codex home for {profile}:")
    for index, path in enumerate(choices, start=1):
        suffix = " (Recommended)" if index == 1 else ""
        print(f"  {index}. {path}{suffix}")
    custom_index = len(choices) + 1
    print(f"  {custom_index}. Custom path")

    while True:
        raw = input("Choice [1]: ").strip()
        if not raw:
            selected = choices[0] if choices else recommended
        else:
            try:
                choice = int(raw)
            except ValueError:
                print("Enter a number from the list.")
                continue
            if 1 <= choice <= len(choices):
                selected = choices[choice - 1]
            elif choice == custom_index:
                custom = input("Custom Codex home: ").strip()
                if not custom:
                    print("Custom path cannot be empty.")
                    continue
                selected = Path(os.path.expandvars(custom)).expanduser()
            else:
                print("Enter a number from the list.")
                continue
        if forbidden is not None and path_matches(selected, forbidden):
            print("Choose a different directory; the other profile already uses that home.")
            continue
        break

    return ProfileHome(selected, home_mode(store, profile, selected), "prompt")


def prompt_order(target_profile: str) -> tuple[str, str]:
    if target_profile == "openai-official":
        return "openai-official", "internal"
    return "internal", "openai-official"


def home_selection_confirmed_value(binding: ProfileHome) -> bool:
    return binding.source in {"explicit", "official_arg", "prompt", "manifest"}


def active_home_for_target(
    active_profile: str | None,
    active_home: Path | None,
    target_profile: str,
) -> Path | None:
    if active_profile is None or active_home is None or active_profile == target_profile:
        return None
    return active_home


def maybe_prompt_homes(
    store: Store,
    internal: ProfileHome,
    official: ProfileHome,
    *,
    target_profile: str,
    dry_run: bool,
    active_profile: str | None = None,
    active_home: Path | None = None,
) -> tuple[ProfileHome, ProfileHome]:
    if not prompt_enabled(dry_run):
        return internal, official
    homes = {"internal": internal, "openai-official": official}
    target_forbidden = active_home_for_target(active_profile, active_home, target_profile)
    for profile in prompt_order(target_profile):
        current = homes[profile]
        forbidden = target_forbidden if profile == target_profile else None
        target_matches_active = (
            forbidden is not None and path_matches(current.path, forbidden)
        )
        if profile == "internal":
            should_prompt = current.source in {"default", "manifest_unconfirmed"}
        else:
            should_prompt = current.source in {"default", "legacy_arg", "manifest_unconfirmed"}
        should_prompt = should_prompt or target_matches_active
        if should_prompt:
            other = homes[other_profile(profile)]
            if target_matches_active and active_profile:
                print(
                    f"{active_profile} currently uses {forbidden}; "
                    f"choose a different Codex home for {profile}."
                )
            homes[profile] = prompt_profile_home(
                store,
                profile,
                current,
                other.path,
                forbidden=forbidden,
            )
    internal = homes["internal"]
    official = homes["openai-official"]
    return internal, official


def resolve_active_home_conflict(
    store: Store,
    internal: ProfileHome,
    official: ProfileHome,
    *,
    target_profile: str,
    dry_run: bool,
    active_profile: str | None,
    active_home: Path | None,
) -> tuple[ProfileHome, ProfileHome]:
    forbidden = active_home_for_target(active_profile, active_home, target_profile)
    if forbidden is None:
        return internal, official

    homes = {"internal": internal, "openai-official": official}
    target = homes[target_profile]
    if not path_matches(target.path, forbidden):
        return internal, official

    if prompt_enabled(dry_run):
        print(
            f"{active_profile} currently uses {forbidden}; "
            f"choose a different Codex home for {target_profile}."
        )
        homes[target_profile] = prompt_profile_home(
            store,
            target_profile,
            target,
            homes[other_profile(target_profile)].path,
            forbidden=forbidden,
        )
        return homes["internal"], homes["openai-official"]

    raise SwitchError(
        f"Refusing to switch from {active_profile} to {target_profile} with the same "
        f"Codex home: {forbidden}. Choose a different directory for {target_profile}."
    )


def resolve_collision(
    store: Store,
    internal: ProfileHome,
    official: ProfileHome,
    *,
    target_profile: str,
    dry_run: bool,
) -> tuple[ProfileHome, ProfileHome]:
    if not path_matches(internal.path, official.path):
        return internal, official

    if prompt_enabled(dry_run):
        homes = {"internal": internal, "openai-official": official}
        profile_to_change = other_profile(target_profile)
        target_home = homes.get(target_profile, internal).path
        if profile_to_change not in homes:
            profile_to_change = "openai-official"
        print(
            f"{target_profile} already uses {target_home}; "
            f"choose a different Codex home for {profile_to_change}."
        )
        fixed = prompt_profile_home(
            store,
            profile_to_change,
            homes[profile_to_change],
            target_home,
            forbidden=target_home,
        )
        homes[profile_to_change] = fixed
        internal = homes["internal"]
        official = homes["openai-official"]
        if not path_matches(internal.path, official.path):
            return internal, official

    if (
        internal.source in {"explicit", "manifest", "prompt"}
        and official.source not in {"official_arg", "prompt"}
    ):
        home = store.managed_home("openai-official")
        official = ProfileHome(home, home_mode(store, "openai-official", home), "auto-managed")
    elif official.source == "official_arg" and internal.source not in {"explicit", "manifest", "prompt"}:
        home = store.managed_home("internal")
        internal = ProfileHome(home, home_mode(store, "internal", home), "auto-managed")

    if path_matches(internal.path, official.path):
        raise SwitchError(
            "Refusing to use the same Codex home for internal and openai-official. "
            "Choose a different directory for one profile."
        )
    return internal, official


def manifest_needs_home_update(
    manifest: dict[str, Any],
    profile: str,
    binding: ProfileHome,
) -> bool:
    if binding.source == "default":
        return False
    raw_home = manifest.get("codex_home")
    raw_mode = manifest.get("home_mode")
    raw_confirmed = manifest.get("home_selection_confirmed")
    expected_confirmed = home_selection_confirmed_value(binding)
    return (
        raw_home != str(binding.path)
        or raw_mode != binding.mode
        or raw_confirmed is not expected_confirmed
    )


def build_manifest_update(
    manifest: dict[str, Any],
    profile: str,
    binding: ProfileHome,
) -> dict[str, Any]:
    updated = dict(manifest)
    updated.setdefault("name", profile)
    updated["codex_home"] = str(binding.path)
    updated["home_mode"] = binding.mode
    updated["home_selection_confirmed"] = home_selection_confirmed_value(binding)
    updated["updated_at"] = now_stamp()
    return updated


def resolve_independent_homes(
    store: Store,
    internal_manifest: dict[str, Any],
    official_manifest: dict[str, Any],
    *,
    target_profile: str,
    dry_run: bool,
    active_profile: str | None = None,
    active_home: Path | None = None,
) -> IndependentHomes:
    internal = profile_home_binding(store, "internal", internal_manifest)
    official = profile_home_binding(store, "openai-official", official_manifest)
    internal, official = maybe_prompt_homes(
        store,
        internal,
        official,
        target_profile=target_profile,
        dry_run=dry_run,
        active_profile=active_profile,
        active_home=active_home,
    )
    internal, official = resolve_active_home_conflict(
        store,
        internal,
        official,
        target_profile=target_profile,
        dry_run=dry_run,
        active_profile=active_profile,
        active_home=active_home,
    )
    internal, official = resolve_collision(
        store,
        internal,
        official,
        target_profile=target_profile,
        dry_run=dry_run,
    )

    updates: dict[str, dict[str, Any]] = {}
    if manifest_needs_home_update(internal_manifest, "internal", internal):
        updates["internal"] = build_manifest_update(internal_manifest, "internal", internal)
    if manifest_needs_home_update(official_manifest, "openai-official", official):
        updates["openai-official"] = build_manifest_update(
            official_manifest,
            "openai-official",
            official,
        )
    return IndependentHomes(internal=internal, official=official, manifest_updates=updates)


def resolve_runtime_homes(store: Store) -> IndependentHomes:
    """Resolve the same independent homes for noninteractive preparation/readers."""
    manifests = {}
    for profile in ("internal", "openai-official"):
        path = store.manifest_path(profile)
        try:
            path.lstat()
        except FileNotFoundError:
            manifests[profile] = {}
        else:
            manifests[profile] = store.load_manifest(profile)
    return resolve_independent_homes(
        store, manifests["internal"], manifests["openai-official"],
        target_profile="internal", dry_run=True,
    )
