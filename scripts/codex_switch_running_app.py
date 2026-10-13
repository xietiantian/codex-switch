from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from codex_switch_constants import APP_CLI_ENV, DEFAULT_LAUNCH_AGENT_LABEL
from codex_switch_io import run_quiet
from codex_switch_paths import equivalent_paths
from codex_switch_runtime_binding import (
    DesktopInventory,
    RuntimeBinding,
    RuntimeObservation,
    attest_runtime_binding,
    discover_desktop_hosts,
)
from codex_switch_store import Store


APP_PROXY_MARKER = "codex_switch_app_proxy.py"
LISTEN_ARG = "--listen"
PRIMARY_APP_SERVER_ARG = "--analytics-default-enabled"
GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-a",
        "--ask-for-approval",
        "-C",
        "--cd",
        "-c",
        "--config",
        "--disable",
        "--enable",
        "-i",
        "--image",
        "--local-provider",
        "-m",
        "--model",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "--add-dir",
    }
)
GLOBAL_FLAG_OPTIONS = frozenset(
    {
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto",
        "--no-alt-screen",
        "--oss",
        "--search",
    }
)


@dataclass(frozen=True)
class RunningCodexProcess:
    pid: int
    kind: str
    command_path: str
    app_cli_env: str
    ppid: int = 0
    parent_command: str = ""
    host_kind: str = ""


def is_default_desktop_context(store: Store) -> bool:
    default_live = Path.home() / ".codex"
    default_agent = (
        Path.home() / "Library" / "LaunchAgents" / f"{DEFAULT_LAUNCH_AGENT_LABEL}.plist"
    )
    return (
        store.live_codex_home.expanduser() == default_live
        and store.launch_agent_path.expanduser() == default_agent
    )


def parse_ps_processes(output: str) -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        processes.append((int(parts[0]), parts[1]))
    return processes


def parse_ps_process_tree(output: str) -> list[tuple[int, int, str]]:
    processes: list[tuple[int, int, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        processes.append((int(parts[0]), int(parts[1]), parts[2]))
    return processes


def parse_env_app_cli_path(output: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(APP_CLI_ENV)}=([^\s]+)", output)
    return match.group(1) if match else ""


def _codex_subcommand(args: Sequence[str]) -> str | None:
    """Return the command, empty for the TUI, or None for unrecognized syntax."""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return ""
        if token in GLOBAL_FLAG_OPTIONS:
            index += 1
            continue
        if token in GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            option = token.split("=", 1)[0]
            if option in GLOBAL_OPTIONS_WITH_VALUE:
                index += 1
                continue
        if token.startswith("-c") and token != "-c":
            index += 1
            continue
        return None if token.startswith("-") else token
    return ""


def argv_invokes_app_server(args: Sequence[str]) -> bool:
    return _codex_subcommand(args) == "app-server"


def app_server_command_path(args: str) -> str:
    try:
        tokens = shlex.split(args)
    except ValueError:
        return ""
    if len(tokens) < 2:
        return ""
    command = tokens[0]
    command_name = Path(command).name
    if command_name != "codex" and not command_name.startswith("codex-"):
        return ""
    return command if argv_invokes_app_server(tokens[1:]) else ""


def process_app_cli_env(pid: int) -> str:
    code, output = run_quiet(["/bin/ps", "eww", "-p", str(pid)])
    if code != 0:
        return ""
    return parse_env_app_cli_path(output)


def _first_command_token(args: str) -> str:
    try:
        tokens = shlex.split(args)
    except ValueError:
        return ""
    return tokens[0] if tokens else ""


def _desktop_host_for_command(
    args: str,
    inventory: DesktopInventory,
) -> tuple[str, str]:
    command = _first_command_token(args)
    if not command:
        return ("", "")
    hosts = []
    if inventory.current is not None:
        hosts.append(inventory.current)
    hosts.extend(inventory.legacy)
    for host in hosts:
        if equivalent_paths(command, str(host.main_executable)):
            return (str(host.main_executable), host.kind)
    return ("", "")


def _invokes_app_proxy(tokens: Sequence[str]) -> bool | None:
    """Recognize the script position; unknown Python syntax remains ambiguous."""
    if Path(tokens[0]).name == APP_PROXY_MARKER:
        return True
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(tokens[0]).name.lower()):
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        if token.startswith(("-c", "-m")):
            return None
        if token in {"-W", "-X"}:
            if index + 1 >= len(tokens):
                return None
            index += 2
        elif token.startswith(("-W", "-X")) or re.fullmatch(r"-[bBdEiIOPqRsSuvVx]+", token):
            index += 1
        else:
            return None
    if index == len(tokens):
        return None
    return Path(tokens[index]).name == APP_PROXY_MARKER


def _has_independent_cli_ancestry(
    pid: int,
    processes: Mapping[int, tuple[int, str]],
    inventory: DesktopInventory,
) -> bool:
    """Exclude only proven CLI children; ambiguous or Desktop chains stay visible."""
    seen: set[int] = set()
    cli_ancestor = False
    while pid > 1:
        if pid in seen or pid not in processes:
            return False
        seen.add(pid)
        ppid, args = processes[pid]
        try:
            tokens = shlex.split(args)
        except ValueError:
            return False
        if not tokens:
            return False
        if _desktop_host_for_command(args, inventory)[0] or _invokes_app_proxy(tokens) is not False:
            return False
        command_name = Path(tokens[0]).name
        if command_name == "codex":
            subcommand = _codex_subcommand(tokens[1:])
            if subcommand is None:
                return False
            if subcommand != "app-server":
                # Positional text also starts the TUI: codex [OPTIONS] [PROMPT].
                cli_ancestor = True
        elif command_name.startswith("codex-"):
            return False
        pid = ppid
    return cli_ancestor


def running_codex_processes(
    inventory: DesktopInventory | None = None,
    process_output: str | None = None,
    env_reader: Callable[[int], str] | None = None,
) -> list[RunningCodexProcess]:
    if inventory is None:
        inventory = discover_desktop_hosts()
    if process_output is None:
        code, output = run_quiet(["/bin/ps", "-axo", "pid=,ppid=,args="])
        if code != 0:
            return []
    else:
        output = process_output
    read_env = env_reader or process_app_cli_env

    observations: list[RunningCodexProcess] = []
    processes = parse_ps_process_tree(output)
    command_by_pid = {pid: args for pid, _ppid, args in processes}
    ancestry = {pid: (ppid, args) for pid, ppid, args in processes}
    for pid, ppid, args in processes:
        desktop_path, host_kind = _desktop_host_for_command(args, inventory)
        if desktop_path:
            observations.append(
                RunningCodexProcess(
                    pid=pid,
                    kind="desktop",
                    command_path=desktop_path,
                    app_cli_env=read_env(pid),
                    ppid=ppid,
                    parent_command=command_by_pid.get(ppid, ""),
                    host_kind=host_kind,
                )
            )
            continue

        command_path = app_server_command_path(args)
        if not command_path:
            continue
        try:
            parsed_args = shlex.split(args)
        except ValueError:
            continue
        if LISTEN_ARG in parsed_args and PRIMARY_APP_SERVER_ARG not in parsed_args:
            continue
        if _has_independent_cli_ancestry(ppid, ancestry, inventory):
            continue
        observations.append(
            RunningCodexProcess(
                pid=pid,
                kind="app-server",
                command_path=command_path,
                app_cli_env=read_env(pid),
                ppid=ppid,
                parent_command=command_by_pid.get(ppid, ""),
            )
        )
    return observations


def managed_launcher_fingerprint(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(payload).hexdigest()


def collect_runtime_observation(
    *,
    inventory: DesktopInventory | None = None,
    process_output: str | None = None,
    env_reader: Callable[[int], str] | None = None,
    gui_app_cli: str = "",
    launch_agent_cli: str = "",
    managed_launcher: Path | None = None,
) -> RuntimeObservation:
    observed_inventory = inventory or discover_desktop_hosts()
    return RuntimeObservation(
        processes=tuple(
            running_codex_processes(
                inventory=observed_inventory,
                process_output=process_output,
                env_reader=env_reader,
            )
        ),
        gui_app_cli=gui_app_cli,
        launch_agent_cli=launch_agent_cli,
        managed_launcher_fingerprint=(
            managed_launcher_fingerprint(managed_launcher)
            if managed_launcher is not None
            else ""
        ),
    )


def collect_store_runtime_observation(
    store: Store,
    binding: RuntimeBinding | None = None,
) -> RuntimeObservation:
    from codex_switch_launch import read_launch_agent_cli_path
    from codex_switch_paths import detect_current_app_cli_path

    default_context = is_default_desktop_context(store)
    managed_launcher = (
        binding.desktop_cli
        if binding is not None and binding.requires_proxy
        else None
    )
    return collect_runtime_observation(
        process_output=None if default_context else "",
        gui_app_cli=detect_current_app_cli_path() if default_context else "",
        launch_agent_cli=read_launch_agent_cli_path(store.launch_agent_path),
        managed_launcher=managed_launcher,
    )


def attestation_problem_messages(
    binding: RuntimeBinding,
    observation: RuntimeObservation,
) -> list[str]:
    return [
        f"{finding.code}: {finding.message}"
        for finding in attest_runtime_binding(binding, observation).findings
        if finding.severity in {"error", "warning"}
    ]


def app_server_matches_expected_cli(process: RunningCodexProcess, expected_app_cli: str) -> bool:
    if equivalent_paths(process.command_path, expected_app_cli):
        return True
    parent_command = getattr(process, "parent_command", "")
    observed_app_cli = getattr(process, "app_cli_env", "")
    return (
        bool(observed_app_cli)
        and equivalent_paths(observed_app_cli, expected_app_cli)
        and APP_PROXY_MARKER in parent_command
    )


def running_desktop_problems(
    store: Store,
    active_profile: str,
    expected_app_cli: str,
    observations: list[RunningCodexProcess] | None = None,
    enforce_default_context: bool = True,
    runtime_observation: RuntimeObservation | None = None,
) -> list[str]:
    if not expected_app_cli:
        return []
    if enforce_default_context and not is_default_desktop_context(store):
        return []

    problems: list[str] = []
    if runtime_observation is not None:
        selected_observations = runtime_observation.processes
    elif observations is not None:
        selected_observations = observations
    else:
        selected_observations = running_codex_processes()
    for process in selected_observations:
        if process.kind == "desktop":
            observed = process.app_cli_env
            if observed and not equivalent_paths(observed, expected_app_cli):
                problems.append(
                    f"running ChatGPT pid {process.pid} has {APP_CLI_ENV}="
                    f"{observed}, but active profile {active_profile} expects "
                    f"{expected_app_cli}; quit ChatGPT completely and reopen it"
                )
        elif process.kind == "app-server":
            if not app_server_matches_expected_cli(process, expected_app_cli):
                problems.append(
                    f"running Codex app-server pid {process.pid} uses "
                    f"{process.command_path}, but active profile {active_profile} "
                    f"expects {expected_app_cli}; quit ChatGPT completely "
                    "and reopen it"
                )
    return problems


def print_running_desktop_status(
    store: Store,
    expected_app_cli: str,
    runtime_observation: RuntimeObservation | None = None,
    enforce_default_context: bool = True,
) -> None:
    if enforce_default_context and not is_default_desktop_context(store):
        return
    observations = (
        runtime_observation.processes
        if runtime_observation is not None
        else running_codex_processes()
    )
    for process in observations:
        if process.kind == "desktop":
            line = (
                f"Running ChatGPT pid {process.pid} {APP_CLI_ENV}: "
                f"{process.app_cli_env or '<unset>'}"
            )
            if (
                expected_app_cli
                and process.app_cli_env
                and not equivalent_paths(process.app_cli_env, expected_app_cli)
            ):
                line += f" (expected {expected_app_cli})"
            print(line)
        elif process.kind == "app-server":
            line = f"Running Codex app-server pid {process.pid}: {process.command_path}"
            if expected_app_cli and app_server_matches_expected_cli(
                process, expected_app_cli
            ) and not equivalent_paths(process.command_path, expected_app_cli):
                ppid = getattr(process, "ppid", 0)
                line += f" (via app proxy pid {ppid})"
            elif expected_app_cli and not equivalent_paths(
                process.command_path, expected_app_cli
            ):
                line += f" (expected {expected_app_cli})"
            print(line)
