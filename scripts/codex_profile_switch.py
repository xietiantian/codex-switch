#!/usr/bin/env python3
"""Manage local Codex profiles; switch active CLI/app configuration."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Application imports require tomllib too, so reject unsupported interpreters
# before importing them or touching profile state.
if sys.version_info < (3, 11):
    print(
        "codex-profile-switch: Python 3.11+ with tomllib is required "
        "before any profile state mutation",
        file=sys.stderr,
    )
    raise SystemExit(2)

from codex_switch_core import (
    CONFIG_MODE_SHARED,
    CONFIG_MODE_SNAPSHOT,
    DEFAULT_LAUNCH_AGENT_LABEL,
    SwitchError,
    expand_path,
)
from codex_switch_bindings import (
    cmd_login,
    cmd_promote_internal_update,
    cmd_set_app_bin,
    cmd_set_bin,
    cmd_shim_env,
)
from codex_switch_capture import cmd_capture
from codex_switch_doctor import cmd_doctor
from codex_switch_lifecycle import cmd_init
from codex_switch_list import cmd_list
from codex_switch_plugins import cmd_repair_plugins
from codex_switch_status import cmd_status
from codex_switch_restore import cmd_restore
from codex_switch_shared_configuration import cmd_sync_shared
from codex_switch_switching import (
    cmd_switch,
)
from codex_switch_verify import cmd_verify


class OfficialHomeAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | list[str] | None,
        option_string: str | None = None,
    ) -> None:
        raw = values[0] if isinstance(values, list) else values
        default_official = Path.home() / ".codex"
        setattr(namespace, self.dest, expand_path(raw, default_official))
        source = "official_arg" if option_string == "--official-codex-home" else "legacy_arg"
        setattr(namespace, "official_codex_home_source", source)


class InternalHomeAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | list[str] | None,
        option_string: str | None = None,
    ) -> None:
        raw = values[0] if isinstance(values, list) else values
        default_internal = Path.home() / ".codex-switch" / "homes" / "internal"
        setattr(namespace, self.dest, expand_path(raw, default_internal))
        setattr(namespace, "internal_codex_home_source", "explicit")


def add_home_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--official-codex-home",
        dest="official_codex_home",
        action=OfficialHomeAction,
        default=argparse.SUPPRESS,
        help="Official Codex home. Default: ~/.codex.",
    )
    parser.add_argument(
        "--live-codex-home",
        dest="official_codex_home",
        action=OfficialHomeAction,
        default=argparse.SUPPRESS,
        help="Legacy alias for --official-codex-home.",
    )
    parser.add_argument(
        "--internal-codex-home",
        dest="internal_codex_home",
        action=InternalHomeAction,
        default=argparse.SUPPRESS,
        help="Internal Codex home. Default: ~/.codex-switch/homes/internal.",
    )


def add_global_arguments(parser: argparse.ArgumentParser) -> None:
    default_store = Path.home() / ".codex-switch"
    default_official = Path.home() / ".codex"
    default_launch_agent = (
        Path.home() / "Library" / "LaunchAgents" / f"{DEFAULT_LAUNCH_AGENT_LABEL}.plist"
    )
    parser.set_defaults(
        official_codex_home_source="default",
        internal_codex_home=None,
        internal_codex_home_source="default",
    )
    parser.add_argument(
        "--store-dir",
        type=lambda value: expand_path(value, default_store),
        default=expand_path(os.environ.get("CODEX_SWITCH_HOME"), default_store),
        help="Profile store directory. Default: ~/.codex-switch; CODEX_SWITCH_HOME accepted.",
    )
    parser.set_defaults(official_codex_home=default_official.expanduser())
    add_home_arguments(parser)
    parser.add_argument(
        "--launch-agent-path",
        type=lambda value: expand_path(value, default_launch_agent),
        default=default_launch_agent.expanduser(),
        help="LaunchAgent plist used to persist Codex Desktop CODEX_CLI_PATH.",
    )
    parser.add_argument(
        "--launch-agent-label",
        default=DEFAULT_LAUNCH_AGENT_LABEL,
        help=f"LaunchAgent label. Default: {DEFAULT_LAUNCH_AGENT_LABEL}.",
    )


def add_init_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    init = sub.add_parser("init", help="Initialize store plus OpenAI official profile.")
    init.add_argument("--codex-bin", help="OpenAI official profile Codex binary path.")
    init.add_argument(
        "--app-cli-path",
        help="OpenAI official profile Desktop CODEX_CLI_PATH. Defaults to the bundled ChatGPT.app Codex binary.",
    )
    init.add_argument("--capture-current", help="Also capture the current live ~/.codex profile.")
    init.add_argument(
        "--overwrite-capture",
        action="store_true",
        help="Overwrite an existing captured current profile.",
    )
    init.set_defaults(func=cmd_init)


def add_capture_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    default_live = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    capture = sub.add_parser("capture", help="Capture an existing CODEX_HOME as a profile.")
    capture.add_argument("name")
    capture.add_argument(
        "--from-codex-home",
        type=lambda value: expand_path(value, Path.home() / ".codex"),
        default=default_live.expanduser(),
    )
    capture.add_argument("--codex-bin", help="Profile Codex binary path.")
    capture.add_argument("--app-cli-path", help="Profile Desktop CODEX_CLI_PATH.")
    capture.add_argument("--allow-missing-auth", action="store_true")
    capture.add_argument("--overwrite", action="store_true")
    capture.set_defaults(func=cmd_capture)


def add_switch_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    switch = sub.add_parser(
        "switch",
        help="Switch live ~/.codex, codex shim, Desktop CODEX_CLI_PATH.",
    )
    switch.add_argument("name")
    add_home_arguments(switch)
    switch.add_argument("--dry-run", action="store_true")
    switch.add_argument("--clear-missing-auth", action="store_true")
    switch.add_argument(
        "--config-mode",
        choices=(CONFIG_MODE_SHARED, CONFIG_MODE_SNAPSHOT),
        default=CONFIG_MODE_SHARED,
        help=(
            "How to update config.toml. shared preserves one live config, only "
            "updates the top-level profile, ensures target [profiles.*] "
            "section exists. snapshot copies the target profile config.toml. "
            "Default: shared."
        ),
    )
    switch.add_argument(
        "--shared-config-base",
        help=(
            "With --config-mode shared, build the live shared config from this stored "
            "profile's config.toml before setting the target profile. Useful when "
            "migrating back from an older snapshot switch."
        ),
    )
    switch.add_argument("--skip-shim", action="store_true")
    switch.add_argument("--skip-app-cli", action="store_true")
    switch.add_argument(
        "--app-profile",
        help=(
            "Select a distinct ChatGPT Desktop profile. The supported split is "
            "internal CLI with official App profile."
        ),
    )
    switch.add_argument(
        "--skip-launchctl",
        action="store_true",
        help="Write the LaunchAgent but do not call launchctl. Useful in isolated tests.",
    )
    switch.set_defaults(func=cmd_switch)


def add_simple_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    list_cmd = sub.add_parser("list", help="List profiles.")
    list_cmd.set_defaults(func=cmd_list)

    status = sub.add_parser("status", help="Show active profile plus CLI resolution.")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="Validate profile store health.")
    doctor.set_defaults(func=cmd_doctor)

    repair_plugins = sub.add_parser(
        "repair-plugins",
        help=(
            "Refresh plugin catalogs, install missing available plugins, and "
            "refresh confirmed stale local caches in a profile CODEX_HOME."
        ),
    )
    repair_plugins.add_argument("name")
    repair_plugins.add_argument("--dry-run", action="store_true")
    repair_plugins.add_argument(
        "--disable-unavailable",
        action="store_true",
        help=(
            "After refreshing the available plugin catalog, disable missing "
            "enabled plugin selectors that are not available for install."
        ),
    )
    repair_plugins.set_defaults(func=cmd_repair_plugins)

    sync_shared = sub.add_parser(
        "sync-shared",
        help=(
            "Preview or apply Official App Plugin and Skill state to the "
            "internal CLI profile."
        ),
    )
    sync_shared.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the reconciliation plan without changing config, cache, "
            "or state."
        ),
    )
    sync_shared.set_defaults(func=cmd_sync_shared)

    verify = sub.add_parser("verify", help="Verify an active target profile switch.")
    verify.add_argument("name")
    verify.add_argument(
        "--repair",
        choices=("none", "safe"),
        default="none",
        help="Run bounded safe repairs before verification. Default: none.",
    )
    verify.add_argument(
        "--runtime-smoke",
        action="store_true",
        help="Run target codex --version and plugin list smoke with profile CODEX_HOME.",
    )
    verify.add_argument(
        "--app-server-smoke",
        action="store_true",
        help=(
            "Run a Desktop-like app-server startup smoke with profile "
            "CODEX_HOME."
        ),
    )
    verify.add_argument(
        "--exec-smoke",
        help="Run explicit `codex exec --json <prompt>` smoke with profile CODEX_HOME.",
    )
    verify.add_argument(
        "--responses-tool-smoke",
        action="store_true",
        help=(
            "Run a deterministic Responses API tool-call follow-up smoke with "
            "profile CODEX_HOME."
        ),
    )
    verify.add_argument(
        "--report",
        action="store_true",
        help="Write a JSON verification report under the profile store.",
    )
    verify.set_defaults(func=cmd_verify)

    from codex_switch_verify import cmd_verify_app_launch
    launch_verify = sub.add_parser(
        "verify-app-launch",
        help="Check the actual App launcher/home handshake; changed settings may trigger compatibility preparation.",
    )
    launch_verify.add_argument("name", nargs="?", default="internal")
    launch_verify.set_defaults(func=cmd_verify_app_launch)


    shim_env = sub.add_parser("shim-env", help="Print shell export used by the codex shim.")
    shim_env.set_defaults(func=cmd_shim_env)


def add_restore_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    restore = sub.add_parser("restore", help="Restore a codex-switch backup.")
    restore.add_argument("backup_id")
    mode = restore.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    restore.add_argument("--force", action="store_true")
    restore.set_defaults(func=cmd_restore)


def add_login_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    login = sub.add_parser("login", help="Run codex login inside a stored profile.")
    login.add_argument("name")
    login.add_argument("--codex-bin", help="Override the profile codex binary.")
    login.add_argument("--with-api-key", action="store_true")
    login.set_defaults(func=cmd_login)


def add_set_bin_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    set_bin = sub.add_parser("set-bin", help="Bind a stored profile to a codex binary.")
    set_bin.add_argument("name")
    set_bin.add_argument("codex_bin")
    set_bin.add_argument(
        "--preserve-app-cli",
        action="store_true",
        help=(
            "Only update the shell/profile codex_bin; leave the ChatGPT Desktop "
            "app_cli_path unchanged."
        ),
    )
    set_bin.set_defaults(func=cmd_set_bin)


def add_set_app_bin_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    set_app_bin = sub.add_parser(
        "set-app-bin",
        help="Bind ChatGPT Desktop CODEX_CLI_PATH to a stored profile.",
    )
    set_app_bin.add_argument("name")
    set_app_bin.add_argument("app_cli_path")
    set_app_bin.set_defaults(func=cmd_set_app_bin)


def add_internal_update_promotion_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    promote = sub.add_parser(
        "promote-internal-update",
        help=argparse.SUPPRESS,
    )
    promote.add_argument("--bound-bin", required=True)
    promote.add_argument("--candidate-bin", required=True)
    promote.add_argument("--backup-bin", required=True)
    promote.add_argument("--target-version", required=True)
    promote.add_argument("--cli-only", action="store_true", help=argparse.SUPPRESS)
    promote.set_defaults(func=cmd_promote_internal_update)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_global_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    add_init_parser(sub)
    add_capture_parser(sub)
    add_switch_parser(sub)
    add_simple_parsers(sub)
    add_restore_parser(sub)
    add_login_parser(sub)
    add_set_bin_parser(sub)
    add_set_app_bin_parser(sub)
    add_internal_update_promotion_parser(sub)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except SwitchError as exc:
        print(f"codex-profile-switch: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
