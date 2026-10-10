from __future__ import annotations

from codex_switch_constants import APP_CLI_ENV, SwitchError
from codex_switch_runtime_binding import discover_desktop_hosts
from codex_switch_paths import equivalent_paths, profile_app_cli_path
from codex_switch_running_app import (
    attestation_problem_messages,
    collect_store_runtime_observation,
    print_running_desktop_status,
)
from codex_switch_runtime_binding import (
    RuntimeBinding,
    RuntimeBindingError,
    RuntimeObservation,
    manifest_uses_canonical_binding,
    resolve_store_runtime_binding,
)
from codex_switch_status_shell import print_codex_version
from codex_switch_store import Store


def print_app_codex_status(
    store: Store,
    active_profile: str | None,
    *,
    runtime_binding: RuntimeBinding | None = None,
    runtime_observation: RuntimeObservation | None = None,
) -> None:
    binding = runtime_binding
    if binding is None and active_profile in {"internal", "openai-official", "official"}:
        try:
            manifest = store.load_manifest(active_profile)
            if not manifest_uses_canonical_binding(active_profile, manifest):
                raise RuntimeBindingError(
                    "binding.official.explicit_compatibility",
                    "Explicit official App CLI compatibility is not a canonical ChatGPT binding.",
                )
            binding = resolve_store_runtime_binding(
                store,
                active_profile,
                manifest=manifest,
            )
        except (SwitchError, RuntimeBindingError) as exc:
            if getattr(exc, "code", "") != "binding.official.explicit_compatibility":
                print(f"Runtime binding: <unavailable: {exc}>")
    observation = runtime_observation
    if observation is None:
        observation = collect_store_runtime_observation(store, binding)
    app_env = observation.gui_app_cli
    active_app_cli = ""
    print(f"GUI {APP_CLI_ENV}: {app_env or '<unset>'}")
    if binding is not None:
        active_app_cli = str(binding.desktop_cli)
        print(f"Expected Desktop CLI: {active_app_cli}")
    elif active_profile and app_env:
        try:
            active_app_cli = profile_app_cli_path(store.load_manifest(active_profile))
            if active_app_cli and not equivalent_paths(app_env, active_app_cli):
                print(f"GUI {APP_CLI_ENV} expected profile path: {active_app_cli}")
        except SwitchError:
            pass
    print(f"LaunchAgent: {store.launch_agent_path if store.launch_agent_path.exists() else '<missing>'}")
    launch_agent_cli = observation.launch_agent_cli
    if launch_agent_cli:
        print(f"LaunchAgent {APP_CLI_ENV}: {launch_agent_cli}")
    desktop_host = (
        binding.desktop_host
        if binding is not None and binding.desktop_host is not None
        else discover_desktop_hosts().current
    )
    if desktop_host is not None:
        bundled_cli = desktop_host.bundled_cli
        print(f"Bundled app codex: {bundled_cli}")
        print_codex_version("Bundled app codex version", str(bundled_cli))
    if binding is not None:
        for problem in attestation_problem_messages(binding, observation):
            print(f"Runtime binding finding: {problem}")
    print_running_desktop_status(
        store,
        active_app_cli,
        runtime_observation=observation,
    )
