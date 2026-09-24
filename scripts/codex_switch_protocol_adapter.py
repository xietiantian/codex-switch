from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence, Union

from codex_switch_constants import SwitchError


JsonPath = tuple[str, ...]
JsonRpcId = Union[str, int]


@contextmanager
def defer_probe_cleanup_signals() -> Iterator[None]:
    """Finish bounded cleanup before delivering a cancellation signal.

    A second signal must not replace an exception already being unwound. For
    ordinary cleanup, deliver the first deferred signal to its original handler
    after cleanup, so cancellation is never lost in a successful return path.
    Python only dispatches signal handlers on the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    unwinding = sys.exc_info()[0] is not None
    pending: list[tuple[int, object]] = []
    previous = {}

    def defer(signum: int, frame: object) -> None:
        if not pending:
            pending.append((signum, frame))

    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.signal(signum, defer)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if pending and not unwinding and sys.exc_info()[0] is None:
            signum, frame = pending[0]
            handler = previous[signum]
            if callable(handler):
                handler(signum, frame)
            elif handler == signal.SIG_DFL:
                os.kill(os.getpid(), signum)


@contextmanager
def managed_probe_process(
    process: subprocess.Popen,
    *,
    threads: Sequence[threading.Thread] = (),
    terminate: Callable[[subprocess.Popen], object] | None = None,
) -> Iterator[None]:
    """Own a probe session through every exit, including BaseException exits."""
    try:
        yield
    finally:
        with defer_probe_cleanup_signals():
            (terminate or _terminate_probe_process)(process)
            deadline = time.monotonic() + 0.5
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
            _close_probe_streams(process)


CONFIG_WRITE_METHODS = frozenset(
    {"config/value/write", "config/batchWrite"}
)
REMOTE_MARKETPLACE_KIND = "created-by-me-remote"
CONFIG_WRITE_PROBE_INITIALIZE_ID = "__codex_switch_config_probe_initialize__"
CONFIG_WRITE_PROBE_WRITE_ID = "__codex_switch_config_probe_write__"
CONFIG_WRITE_PROBE_VERIFY_ID = "__codex_switch_config_probe_verify__"
CONFIG_WRITE_PROBE_KEY_PATH = "features.codex_switch_config_write_probe"
CONFIG_WRITE_PROBE_SEED = """[features]
codex_switch_config_write_probe = false

[mcp_servers.codex-switch-probe]
command = "/usr/bin/false"
args = ["--codex-switch-probe"]

[marketplaces.codex-switch-probe]
source_type = "local"
source = "/codex-switch-probe/marketplace"

[plugins."codex-switch-probe@local"]
enabled = false

[[skills.config]]
path = "/codex-switch-probe/SKILL.md"
enabled = false
"""
CONFIG_WRITE_PROBE_EXPECTED = CONFIG_WRITE_PROBE_SEED.replace(
    "codex_switch_config_write_probe = false",
    "codex_switch_config_write_probe = true",
    1,
)


def capability_receipt_path_for_launcher(launcher_path: Path) -> Path:
    return launcher_path.with_name(f"{launcher_path.name}.capabilities.json")


REQUEST_MODEL_PATHS: Mapping[str, tuple[JsonPath, ...]] = {
    "thread/start": (("params", "model"),),
    "turn/start": (("params", "model"),),
    "realtime/start": (("params", "model"),),
}

RESPONSE_MODEL_PATHS: Mapping[str, tuple[JsonPath, ...]] = {
    "config/read": (("result", "config", "model"),),
    "thread/load": (
        ("result", "conversation", "model"),
        ("result", "conversation", "latestModel"),
        ("result", "conversation", "previousTurnModel"),
        ("result", "conversation", "settings", "model"),
    ),
    "thread/start": (("result", "thread", "model"),),
    "thread/read": (("result", "thread", "model"),),
    "turn/start": (("result", "turn", "model"),),
    "realtime/start": (("result", "model"),),
}

NOTIFICATION_MODEL_PATHS: Mapping[str, tuple[JsonPath, ...]] = {
    "thread/started": (("params", "thread", "model"),),
    "thread/updated": (("params", "thread", "model"),),
    "turn/started": (("params", "turn", "model"),),
    "turn/completed": (("params", "turn", "model"),),
    "item/started": (("params", "item", "model"),),
    "item/completed": (("params", "item", "model"),),
}


@dataclass(frozen=True)
class ProtocolAdapterRule:
    rule_id: str
    direction: str
    method: str
    paths: tuple[JsonPath, ...]
    variants: tuple[str, ...]
    capability_predicate: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.rule_id, str)
            or not self.rule_id
            or self.rule_id != self.rule_id.strip()
            or self.direction
            not in {
                "client_request",
                "server_notification",
                "server_response",
            }
            or not isinstance(self.method, str)
            or not self.method
            or self.method != self.method.strip()
            or not isinstance(self.capability_predicate, str)
            or not self.capability_predicate
            or self.capability_predicate
            != self.capability_predicate.strip()
        ):
            raise SwitchError("Protocol Adapter rule metadata is invalid")
        paths = tuple(
            sorted(
                (
                    tuple(path)
                    for path in self.paths
                )
            )
        )
        if (
            not paths
            or any(
                not path
                or any(
                    not isinstance(component, str) or not component
                    for component in path
                )
                for path in paths
            )
        ):
            raise SwitchError("Protocol Adapter rule paths are invalid")
        variants = tuple(sorted(set(self.variants)))
        if (
            not variants
            or len(variants) != len(self.variants)
            or any(
                not isinstance(variant, str)
                or not variant
                or variant != variant.strip()
                for variant in variants
            )
        ):
            raise SwitchError("Protocol Adapter rule variants are invalid")
        object.__setattr__(self, "paths", paths)
        object.__setattr__(self, "variants", variants)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "capability_predicate": self.capability_predicate,
            "direction": self.direction,
            "method": self.method,
            "paths": [list(path) for path in self.paths],
            "rule_id": self.rule_id,
            "variants": list(self.variants),
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.canonical_payload(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


THREAD_RESUME_HISTORY_RULE_ID = (
    "client_request.thread_resume_history_portability"
)
THREAD_RESUME_HISTORY_RULE = ProtocolAdapterRule(
    rule_id=THREAD_RESUME_HISTORY_RULE_ID,
    direction="client_request",
    method="thread/resume",
    paths=(("params", "history"),),
    variants=(
        "drop_nonportable_opaque_reasoning",
        "remove_top_level_item_id",
    ),
    capability_predicate="always",
)


def _adapter_rule_token(method: str) -> str:
    return method.replace("/", "_").replace("-", "_")


def _model_path_rules(
    *,
    direction: str,
    table: Mapping[str, tuple[JsonPath, ...]],
) -> tuple[ProtocolAdapterRule, ...]:
    return tuple(
        ProtocolAdapterRule(
            rule_id=(
                f"{direction}.model_alias.{_adapter_rule_token(method)}"
            ),
            direction=direction,
            method=method,
            paths=paths,
            variants=("replace_exact_model_alias",),
            capability_predicate=(
                "desktop_model_differs_from_actual_model"
            ),
        )
        for method, paths in sorted(table.items())
    )


def _config_write_rule(method: str) -> ProtocolAdapterRule:
    if method == "config/value/write":
        paths = (
            ("params", "keyPath"),
            ("params", "value"),
        )
        variants = (
            "key_path_equals_model",
            "replace_exact_model_alias",
        )
        rule_id = "client_request.config_value_write_model_alias"
    elif method == "config/batchWrite":
        paths = (
            ("params", "edits", "*", "keyPath"),
            ("params", "edits", "*", "value"),
        )
        variants = (
            "edit_key_path_equals_model",
            "replace_exact_model_alias",
        )
        rule_id = "client_request.config_batch_write_model_alias"
    else:
        raise SwitchError(
            "Protocol Adapter config-write rule method is unsupported"
        )
    return ProtocolAdapterRule(
        rule_id=rule_id,
        direction="client_request",
        method=method,
        paths=paths,
        variants=variants,
        capability_predicate="desktop_model_differs_from_actual_model",
    )


def protocol_adapter_rule_manifest() -> tuple[ProtocolAdapterRule, ...]:
    rules = (
        *_model_path_rules(
            direction="client_request",
            table=REQUEST_MODEL_PATHS,
        ),
        *_model_path_rules(
            direction="server_response",
            table=RESPONSE_MODEL_PATHS,
        ),
        *_model_path_rules(
            direction="server_notification",
            table=NOTIFICATION_MODEL_PATHS,
        ),
        *(
            _config_write_rule(method)
            for method in sorted(CONFIG_WRITE_METHODS)
        ),
        THREAD_RESUME_HISTORY_RULE,
        ProtocolAdapterRule(
            rule_id="client_request.dynamic_tools_legacy",
            direction="client_request",
            method="thread/start",
            paths=(("params", "dynamicTools"),),
            variants=("flatten_namespace_tools",),
            capability_predicate="canonical_dynamic_tools=false",
        ),
        ProtocolAdapterRule(
            rule_id="client_request.remote_marketplace_kind_filter",
            direction="client_request",
            method="plugin/list",
            paths=(("params", "marketplaceKinds"),),
            variants=(
                f"remove_marketplace_kind:{REMOTE_MARKETPLACE_KIND}",
            ),
            capability_predicate="remote_marketplace_kind=false",
        ),
        ProtocolAdapterRule(
            rule_id="server_response.model_list_alias",
            direction="server_response",
            method="model/list",
            paths=(
                ("result", "data", "*", "id"),
                ("result", "data", "*", "model"),
                ("result", "models", "*", "id"),
                ("result", "models", "*", "model"),
            ),
            variants=("replace_exact_model_alias",),
            capability_predicate=(
                "desktop_model_differs_from_actual_model"
            ),
        ),
    )
    return tuple(
        sorted(
            rules,
            key=lambda rule: (
                rule.direction,
                rule.method,
                rule.rule_id,
            ),
        )
    )


def protocol_adapter_rule_set_digest() -> str:
    payload = {
        "rules": [
            dict(rule.canonical_payload())
            for rule in protocol_adapter_rule_manifest()
        ],
        "schema_version": 1,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _tri_state(value: object, *, label: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise SwitchError(f"Protocol capability {label} must be true, false, or null")


@dataclass(frozen=True)
class BackendCapabilities:
    canonical_dynamic_tools: bool | None
    remote_marketplace_kind: bool | None
    versioned_config_write_preserves_unrelated: bool | None

    def __post_init__(self) -> None:
        _tri_state(
            self.canonical_dynamic_tools,
            label="canonical_dynamic_tools",
        )
        _tri_state(
            self.remote_marketplace_kind,
            label="remote_marketplace_kind",
        )
        _tri_state(
            self.versioned_config_write_preserves_unrelated,
            label="versioned_config_write_preserves_unrelated",
        )

    def to_dict(self) -> dict[str, bool | None]:
        return {
            "canonical_dynamic_tools": self.canonical_dynamic_tools,
            "remote_marketplace_kind": self.remote_marketplace_kind,
            "versioned_config_write_preserves_unrelated": (
                self.versioned_config_write_preserves_unrelated
            ),
        }

    @classmethod
    def from_dict(cls, value: object) -> "BackendCapabilities":
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "canonical_dynamic_tools",
                "remote_marketplace_kind",
                "versioned_config_write_preserves_unrelated",
            }
        ):
            raise SwitchError("Protocol capabilities must be an object")
        return cls(
            canonical_dynamic_tools=_tri_state(
                value.get("canonical_dynamic_tools"),
                label="canonical_dynamic_tools",
            ),
            remote_marketplace_kind=_tri_state(
                value.get("remote_marketplace_kind"),
                label="remote_marketplace_kind",
            ),
            versioned_config_write_preserves_unrelated=_tri_state(
                value.get("versioned_config_write_preserves_unrelated"),
                label="versioned_config_write_preserves_unrelated",
            ),
        )


@dataclass(frozen=True)
class AdaptResult:
    message: dict[str, object]
    changed: bool


def json_rpc_id(message: Mapping[str, object]) -> JsonRpcId | None:
    value = message.get("id")
    if type(value) is int or isinstance(value, str):
        return value
    return None


class PendingRequestTracker:
    def __init__(self) -> None:
        self._pending: dict[JsonRpcId, str] = {}
        self._lock = threading.Lock()

    def observe_client(self, message: Mapping[str, object]) -> None:
        request_id = json_rpc_id(message)
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            return
        with self._lock:
            self._pending[request_id] = method

    def consume_backend_response(
        self,
        message: Mapping[str, object],
    ) -> str | None:
        if isinstance(message.get("method"), str):
            return None
        if "result" not in message and "error" not in message:
            return None
        request_id = json_rpc_id(message)
        if request_id is None:
            return None
        with self._lock:
            return self._pending.pop(request_id, None)


def _replace_path_value(
    root: object,
    path: Sequence[str],
    *,
    old: str,
    new: str,
) -> bool:
    if not path:
        return False
    current = root
    for component in path[:-1]:
        if not isinstance(current, dict):
            return False
        current = current.get(component)
    if not isinstance(current, dict):
        return False
    key = path[-1]
    if current.get(key) != old:
        return False
    current[key] = new
    return True


def _replace_exact_paths(
    message: dict[str, object],
    paths: Sequence[JsonPath],
    *,
    old: str,
    new: str,
) -> AdaptResult:
    if old == new:
        return AdaptResult(message, False)
    candidate = copy.deepcopy(message)
    changed = False
    for path in paths:
        changed = (
            _replace_path_value(candidate, path, old=old, new=new)
            or changed
        )
    return AdaptResult(candidate if changed else message, changed)


def _older_backend_function_tool(
    tool: Mapping[str, object],
    *,
    namespace: str,
) -> dict[str, object]:
    result: dict[str, object] = {"namespace": namespace}
    for key in ("type", "name", "description", "inputSchema", "deferLoading"):
        if key in tool:
            result[key] = copy.deepcopy(tool[key])
    return result


def _legacy_dynamic_tools(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    result: list[object] = []
    changed = False
    for entry in value:
        if not isinstance(entry, dict) or entry.get("type") != "namespace":
            result.append(copy.deepcopy(entry))
            continue
        namespace = entry.get("name")
        tools = entry.get("tools")
        if not isinstance(namespace, str) or not isinstance(tools, list):
            result.append(copy.deepcopy(entry))
            continue
        changed = True
        for tool in tools:
            if isinstance(tool, dict):
                result.append(
                    _older_backend_function_tool(tool, namespace=namespace)
                )
            else:
                result.append(copy.deepcopy(tool))
    return result if changed else None


def _adapt_config_write_model(
    message: dict[str, object],
    *,
    old: str,
    new: str,
) -> AdaptResult:
    if old == new:
        return AdaptResult(message, False)
    params = message.get("params")
    if not isinstance(params, dict):
        return AdaptResult(message, False)
    method = message.get("method")
    if method == "config/value/write":
        if params.get("keyPath") != "model" or params.get("value") != old:
            return AdaptResult(message, False)
        candidate = copy.deepcopy(message)
        candidate_params = candidate["params"]
        assert isinstance(candidate_params, dict)
        candidate_params["value"] = new
        return AdaptResult(candidate, True)
    if method != "config/batchWrite":
        return AdaptResult(message, False)
    edits = params.get("edits")
    if not isinstance(edits, list):
        return AdaptResult(message, False)
    indexes = [
        index
        for index, edit in enumerate(edits)
        if isinstance(edit, dict)
        and edit.get("keyPath") == "model"
        and edit.get("value") == old
    ]
    if not indexes:
        return AdaptResult(message, False)
    candidate = copy.deepcopy(message)
    candidate_params = candidate["params"]
    assert isinstance(candidate_params, dict)
    candidate_edits = candidate_params["edits"]
    assert isinstance(candidate_edits, list)
    for index in indexes:
        edit = candidate_edits[index]
        assert isinstance(edit, dict)
        edit["value"] = new
    return AdaptResult(candidate, True)


def _adapt_thread_resume_history(
    message: dict[str, object],
) -> AdaptResult:
    rule = THREAD_RESUME_HISTORY_RULE
    if message.get("method") != rule.method:
        return AdaptResult(message, False)
    params_key, history_key = rule.paths[0]
    params = message.get(params_key)
    if not isinstance(params, dict):
        return AdaptResult(message, False)
    history = params.get(history_key)
    if not isinstance(history, list):
        return AdaptResult(message, False)
    id_indexes = [
        index
        for index, item in enumerate(history)
        if isinstance(item, dict) and "id" in item
    ]
    drop_indexes = [
        index
        for index, item in enumerate(history)
        if isinstance(item, dict)
        and item.get("type") == "reasoning"
        and not item.get("encrypted_content")
        and not item.get("content")
        and not item.get("summary")
    ]
    if not id_indexes and not drop_indexes:
        return AdaptResult(message, False)
    candidate = copy.deepcopy(message)
    candidate_params = candidate[params_key]
    assert isinstance(candidate_params, dict)
    candidate_history = candidate_params[history_key]
    assert isinstance(candidate_history, list)
    for index in id_indexes:
        item = candidate_history[index]
        assert isinstance(item, dict)
        del item["id"]
    if drop_indexes:
        drop_index_set = set(drop_indexes)
        candidate_params[history_key] = [
            item
            for index, item in enumerate(candidate_history)
            if index not in drop_index_set
        ]
    return AdaptResult(candidate, True)


def _adapt_dynamic_tools(
    message: dict[str, object],
    capabilities: BackendCapabilities,
) -> AdaptResult:
    if (
        capabilities.canonical_dynamic_tools is not False
        or message.get("method") != "thread/start"
    ):
        return AdaptResult(message, False)
    params = message.get("params")
    if not isinstance(params, dict):
        return AdaptResult(message, False)
    flattened = _legacy_dynamic_tools(params.get("dynamicTools"))
    if flattened is None:
        return AdaptResult(message, False)
    candidate = copy.deepcopy(message)
    candidate_params = candidate["params"]
    assert isinstance(candidate_params, dict)
    candidate_params["dynamicTools"] = flattened
    return AdaptResult(candidate, True)


def _adapt_marketplace_kinds(
    message: dict[str, object],
    capabilities: BackendCapabilities,
) -> AdaptResult:
    if (
        capabilities.remote_marketplace_kind is not False
        or message.get("method") != "plugin/list"
    ):
        return AdaptResult(message, False)
    params = message.get("params")
    if not isinstance(params, dict):
        return AdaptResult(message, False)
    kinds = params.get("marketplaceKinds")
    if not isinstance(kinds, list) or REMOTE_MARKETPLACE_KIND not in kinds:
        return AdaptResult(message, False)
    candidate = copy.deepcopy(message)
    candidate_params = candidate["params"]
    assert isinstance(candidate_params, dict)
    candidate_params["marketplaceKinds"] = [
        kind for kind in kinds if kind != REMOTE_MARKETPLACE_KIND
    ]
    return AdaptResult(candidate, True)


@dataclass(frozen=True)
class ProtocolAdapter:
    actual_model: str
    desktop_model: str
    capabilities: BackendCapabilities

    def client_request(self, message: dict[str, object]) -> AdaptResult:
        current = message
        changed = False
        method = current.get("method")
        if isinstance(method, str):
            history_result = _adapt_thread_resume_history(current)
            current = history_result.message
            changed = changed or history_result.changed
            model_result = _replace_exact_paths(
                current,
                REQUEST_MODEL_PATHS.get(method, ()),
                old=self.desktop_model,
                new=self.actual_model,
            )
            current = model_result.message
            changed = changed or model_result.changed
            write_result = _adapt_config_write_model(
                current,
                old=self.desktop_model,
                new=self.actual_model,
            )
            current = write_result.message
            changed = changed or write_result.changed
            dynamic_result = _adapt_dynamic_tools(current, self.capabilities)
            current = dynamic_result.message
            changed = changed or dynamic_result.changed
            marketplace_result = _adapt_marketplace_kinds(
                current,
                self.capabilities,
            )
            current = marketplace_result.message
            changed = changed or marketplace_result.changed
        return AdaptResult(current if changed else message, changed)

    def server_message(
        self,
        message: dict[str, object],
        *,
        pending_method: str | None,
    ) -> AdaptResult:
        if "error" in message:
            return AdaptResult(message, False)
        method = message.get("method")
        if isinstance(method, str):
            paths = NOTIFICATION_MODEL_PATHS.get(method, ())
        elif pending_method == "model/list":
            return self._model_list_response(message)
        else:
            paths = RESPONSE_MODEL_PATHS.get(pending_method or "", ())
        return _replace_exact_paths(
            message,
            paths,
            old=self.actual_model,
            new=self.desktop_model,
        )

    def _model_list_response(self, message: dict[str, object]) -> AdaptResult:
        if self.actual_model == self.desktop_model:
            return AdaptResult(message, False)
        result = message.get("result")
        if not isinstance(result, dict):
            return AdaptResult(message, False)
        targets: list[tuple[str, int, str]] = []
        for collection_key in ("data", "models"):
            entries = result.get(collection_key)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                for key in ("id", "model"):
                    if entry.get(key) == self.actual_model:
                        targets.append((collection_key, index, key))
        if not targets:
            return AdaptResult(message, False)
        candidate = copy.deepcopy(message)
        candidate_result = candidate["result"]
        assert isinstance(candidate_result, dict)
        for collection_key, index, key in targets:
            candidate_entries = candidate_result[collection_key]
            assert isinstance(candidate_entries, list)
            entry = candidate_entries[index]
            assert isinstance(entry, dict)
            entry[key] = self.desktop_model
        return AdaptResult(candidate, True)


def _schema_defs(schema: Mapping[str, object]) -> Mapping[str, object]:
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        return defs
    definitions = schema.get("definitions")
    if isinstance(definitions, dict):
        return definitions
    return {}


def extract_schema_capabilities(
    schema: Mapping[str, object],
) -> BackendCapabilities:
    raw_documents = schema.get("schema_documents")
    documents = (
        tuple(
            document
            for document in raw_documents.values()
            if isinstance(document, dict)
        )
        if isinstance(raw_documents, dict)
        else (schema,)
    )
    dynamic_evidence: list[bool] = []
    marketplace_evidence: list[bool] = []
    for document in documents:
        definitions = _schema_defs(document)
        thread_params = definitions.get("ThreadStartParams")
        if isinstance(thread_params, dict):
            properties = thread_params.get("properties")
            if isinstance(properties, dict):
                dynamic_evidence.append("dynamicTools" in properties)

        for definition_name in (
            "PluginMarketplaceKind",
            "PluginListMarketplaceKind",
        ):
            marketplace_definition = definitions.get(definition_name)
            if isinstance(marketplace_definition, dict):
                enum = marketplace_definition.get("enum")
                if isinstance(enum, list) and all(
                    isinstance(item, str) for item in enum
                ):
                    marketplace_evidence.append(
                        REMOTE_MARKETPLACE_KIND in enum
                    )

    dynamic_tools = (
        dynamic_evidence[0]
        if dynamic_evidence
        and all(item == dynamic_evidence[0] for item in dynamic_evidence)
        else None
    )
    remote_marketplace = (
        marketplace_evidence[0]
        if marketplace_evidence
        and all(item == marketplace_evidence[0] for item in marketplace_evidence)
        else None
    )

    return BackendCapabilities(
        canonical_dynamic_tools=dynamic_tools,
        remote_marketplace_kind=remote_marketplace,
        versioned_config_write_preserves_unrelated=None,
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SwitchError(f"Capability backend cannot be inspected: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise SwitchError(f"Capability backend must be a regular file: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SwitchError(f"Capability backend cannot be opened safely: {path}") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise SwitchError(f"Capability backend identity changed: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        try:
            after = path.lstat()
        except OSError as exc:
            raise SwitchError(f"Capability backend identity changed: {path}") from exc
        if after.st_dev != opened.st_dev or after.st_ino != opened.st_ino:
            raise SwitchError(f"Capability backend identity changed: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _generated_schema_payload(schema_root: Path) -> bytes:
    documents: dict[str, object] = {}
    for path in sorted(schema_root.rglob("*.json")):
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise SwitchError("Generated app-server schema JSON is invalid") from exc
        if not isinstance(document, dict):
            raise SwitchError("Generated app-server schema must be an object")
        documents[path.relative_to(schema_root).as_posix()] = document
    if not documents:
        raise SwitchError("Generated app-server schema directory is empty")
    return json.dumps(
        {"schema_documents": documents},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def generate_app_server_schema(
    backend_path: Path,
    *,
    timeout_seconds: float,
) -> bytes:
    if timeout_seconds <= 0:
        raise SwitchError("App-server schema generation timeout must be positive")
    with tempfile.TemporaryDirectory(
        prefix="codex-switch-schema-probe-"
    ) as temp_dir:
        schema_root = Path(temp_dir).resolve()
        environment = os.environ.copy()
        environment.update({
            "HOME": str(schema_root), "CODEX_HOME": str(schema_root),
            "XDG_CONFIG_HOME": str(schema_root / ".config"),
        })
        try:
            process = subprocess.Popen(
                [
                    str(backend_path),
                    "app-server",
                    "generate-json-schema",
                    "--out",
                    str(schema_root),
                    "--experimental",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=schema_root,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise SwitchError("App-server schema generation failed") from exc
        with managed_probe_process(process):
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise SwitchError("App-server schema generation failed") from exc
            if process.returncode != 0:
                raise SwitchError("App-server schema generation failed")
            return _generated_schema_payload(schema_root)


def _write_probe_message(
    process: subprocess.Popen[str],
    message: Mapping[str, object],
) -> bool:
    if process.stdin is None:
        return False
    try:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def _read_probe_lines(
    stream: object,
    output: queue.Queue[str],
) -> None:
    if stream is None:
        return
    for line in stream:  # type: ignore[union-attr]
        try:
            output.put_nowait(line)
        except queue.Full:
            continue


def _discard_probe_output(stream: object) -> None:
    if stream is None:
        return
    for _line in stream:  # type: ignore[union-attr]
        pass


def _wait_for_probe_response(
    process: subprocess.Popen[str],
    output: queue.Queue[str],
    request_id: str,
    *,
    deadline: float,
) -> dict[str, object] | None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = output.get(timeout=min(remaining, 0.05))
        except queue.Empty:
            if process.poll() is not None:
                return None
            continue
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    process_group: int,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.02)
    return not _process_group_exists(process_group)


def _terminate_probe_process(process: subprocess.Popen[object]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None and not _process_group_exists(process.pid):
        return
    if process.poll() is None:
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    if not _wait_for_process_group_exit(
        process.pid,
        timeout_seconds=0.5,
    ):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        _wait_for_process_group_exit(
            process.pid,
            timeout_seconds=0.5,
        )
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _close_probe_streams(process: subprocess.Popen[object]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _probe_config_preserves_unrelated(config_path: Path) -> bool:
    try:
        current = config_path.read_text()
    except OSError:
        return False
    try:
        import tomllib
    except ImportError:
        return current == CONFIG_WRITE_PROBE_EXPECTED
    try:
        return tomllib.loads(current) == tomllib.loads(
            CONFIG_WRITE_PROBE_EXPECTED
        )
    except tomllib.TOMLDecodeError:
        return False


def _validated_probe_write_response(
    response: dict[str, object] | None,
    config_path: Path,
) -> tuple[bool | None, str | None]:
    if response is None or "error" in response:
        return None, None
    result = response.get("result")
    if not isinstance(result, dict):
        return None, None
    file_path = result.get("filePath")
    status = result.get("status")
    version = result.get("version")
    if (
        not isinstance(file_path, str)
        or not isinstance(status, str)
        or not isinstance(version, str)
        or not version
    ):
        return None, None
    if file_path != str(config_path.resolve()) or status != "ok":
        return False, None
    return True, version


def probe_config_write_capability(
    backend_path: Path,
    *,
    timeout_seconds: float,
) -> bool | None:
    if timeout_seconds <= 0:
        return None
    with tempfile.TemporaryDirectory(prefix="codex-switch-config-probe-") as temp_dir:
        home = Path(temp_dir).resolve()
        config_path = home / "config.toml"
        config_path.write_text(CONFIG_WRITE_PROBE_SEED)
        config_path.chmod(0o600)
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home), "CODEX_HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
        })
        try:
            process = subprocess.Popen(
                [
                    str(backend_path),
                    "app-server",
                    "--analytics-default-enabled",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                cwd=home,
                start_new_session=True,
            )
        except OSError:
            return None
        threads: list[threading.Thread] = []
        with managed_probe_process(process, threads=threads):
            output: queue.Queue[str] = queue.Queue(maxsize=64)
            stdout_thread = threading.Thread(
                target=_read_probe_lines,
                args=(process.stdout, output),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_discard_probe_output,
                args=(process.stderr,),
                daemon=True,
            )
            threads.extend((stdout_thread, stderr_thread))
            stdout_thread.start()
            stderr_thread.start()
            deadline = time.monotonic() + timeout_seconds
            if not _write_probe_message(
                process,
                {
                    "id": CONFIG_WRITE_PROBE_INITIALIZE_ID,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "codex-switch-config-write-probe",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
            ):
                return None
            initialize_response = _wait_for_probe_response(
                process,
                output,
                CONFIG_WRITE_PROBE_INITIALIZE_ID,
                deadline=deadline,
            )
            if (
                initialize_response is None
                or "error" in initialize_response
                or not isinstance(initialize_response.get("result"), dict)
            ):
                return None
            if not _write_probe_message(process, {"method": "initialized"}):
                return None
            if not _write_probe_message(
                process,
                {
                    "id": CONFIG_WRITE_PROBE_WRITE_ID,
                    "method": "config/value/write",
                    "params": {
                        "filePath": str(config_path.resolve()),
                        "keyPath": CONFIG_WRITE_PROBE_KEY_PATH,
                        "value": True,
                        "mergeStrategy": "replace",
                        "expectedVersion": None,
                    },
                },
            ):
                return None
            write_response = _wait_for_probe_response(
                process,
                output,
                CONFIG_WRITE_PROBE_WRITE_ID,
                deadline=deadline,
            )
            first_outcome, first_version = _validated_probe_write_response(
                write_response,
                config_path,
            )
            if first_outcome is not True:
                return first_outcome
            assert first_version is not None
            if not _write_probe_message(
                process,
                {
                    "id": CONFIG_WRITE_PROBE_VERIFY_ID,
                    "method": "config/value/write",
                    "params": {
                        "filePath": str(config_path.resolve()),
                        "keyPath": CONFIG_WRITE_PROBE_KEY_PATH,
                        "value": True,
                        "mergeStrategy": "replace",
                        "expectedVersion": first_version,
                    },
                },
            ):
                return None
            verify_response = _wait_for_probe_response(
                process,
                output,
                CONFIG_WRITE_PROBE_VERIFY_ID,
                deadline=deadline,
            )
            verify_outcome, _verify_version = _validated_probe_write_response(
                verify_response,
                config_path,
            )
            if verify_outcome is not True:
                return verify_outcome
            return _probe_config_preserves_unrelated(config_path)


@dataclass(frozen=True)
class CapabilityReceipt:
    backend_sha256: str
    schema_sha256: str
    capabilities: BackendCapabilities
    schema_version: int = 2

    @classmethod
    def from_schema(
        cls,
        backend_path: Path,
        schema_payload: bytes,
    ) -> "CapabilityReceipt":
        try:
            parsed = json.loads(schema_payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SwitchError("Generated app-server schema JSON is invalid") from exc
        if not isinstance(parsed, dict):
            raise SwitchError("Generated app-server schema must be an object")
        return cls(
            backend_sha256=_file_sha256(backend_path),
            schema_sha256=_sha256(schema_payload),
            capabilities=extract_schema_capabilities(parsed),
        )

    @classmethod
    def from_backend_probe(
        cls,
        backend_path: Path,
        schema_payload: bytes,
        *,
        timeout_seconds: float,
        expected_backend_sha256: str | None = None,
    ) -> "CapabilityReceipt":
        before_digest = _file_sha256(backend_path)
        if (
            expected_backend_sha256 is not None
            and before_digest != expected_backend_sha256
        ):
            raise SwitchError(
                "Capability backend changed during capability receipt generation"
            )
        schema_receipt = cls.from_schema(backend_path, schema_payload)
        if schema_receipt.backend_sha256 != before_digest:
            raise SwitchError(
                "Capability backend changed during capability receipt generation"
            )
        probe_result = probe_config_write_capability(
            backend_path,
            timeout_seconds=timeout_seconds,
        )
        if _file_sha256(backend_path) != before_digest:
            raise SwitchError(
                "Capability backend changed during capability receipt generation"
            )
        capabilities = schema_receipt.capabilities
        return cls(
            backend_sha256=schema_receipt.backend_sha256,
            schema_sha256=schema_receipt.schema_sha256,
            capabilities=BackendCapabilities(
                canonical_dynamic_tools=capabilities.canonical_dynamic_tools,
                remote_marketplace_kind=capabilities.remote_marketplace_kind,
                versioned_config_write_preserves_unrelated=probe_result,
            ),
            schema_version=schema_receipt.schema_version,
        )

    @classmethod
    def from_backend(
        cls,
        backend_path: Path,
        *,
        schema_timeout_seconds: float,
        probe_timeout_seconds: float,
    ) -> "CapabilityReceipt":
        initial_backend_sha256 = _file_sha256(backend_path)
        schema_payload = generate_app_server_schema(
            backend_path,
            timeout_seconds=schema_timeout_seconds,
        )
        if _file_sha256(backend_path) != initial_backend_sha256:
            raise SwitchError(
                "Capability backend changed during capability receipt generation"
            )
        return cls.from_backend_probe(
            backend_path,
            schema_payload,
            timeout_seconds=probe_timeout_seconds,
            expected_backend_sha256=initial_backend_sha256,
        )

    def matches(self, backend_path: Path, schema_payload: bytes) -> bool:
        try:
            return (
                self.backend_sha256 == _file_sha256(backend_path)
                and self.schema_sha256 == _sha256(schema_payload)
            )
        except (OSError, SwitchError):
            return False

    def matches_backend_and_schema_digest(
        self,
        backend_path: Path,
        schema_sha256: str,
    ) -> bool:
        if (
            len(schema_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in schema_sha256
            )
        ):
            return False
        try:
            return (
                self.backend_sha256 == _file_sha256(backend_path)
                and self.schema_sha256 == schema_sha256
            )
        except (OSError, SwitchError):
            return False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "backend_sha256": self.backend_sha256,
            "schema_sha256": self.schema_sha256,
            "capabilities": self.capabilities.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "CapabilityReceipt":
        if (
            not isinstance(value, dict)
            or value.get("schema_version") not in {1, 2}
            or set(value)
            != {
                "schema_version",
                "backend_sha256",
                "schema_sha256",
                "capabilities",
            }
        ):
            raise SwitchError("Protocol capability receipt schema is invalid")
        backend_digest = value.get("backend_sha256")
        schema_digest = value.get("schema_sha256")
        for label, digest in (
            ("backend", backend_digest),
            ("schema", schema_digest),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise SwitchError(
                    f"Protocol capability receipt {label} digest is invalid"
                )
        return cls(
            backend_sha256=backend_digest,
            schema_sha256=schema_digest,
            capabilities=BackendCapabilities.from_dict(value.get("capabilities")),
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True)
class CapabilityReceiptArtifact:
    receipt: CapabilityReceipt
    payload: bytes
    payload_sha256: str
    reused: bool


def _valid_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_capability_receipt_payload(path: Path) -> bytes | None:
    try:
        before = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 65536:
                return None
            chunks.append(chunk)
        try:
            after = path.lstat()
        except OSError:
            return None
        if after.st_dev != opened.st_dev or after.st_ino != opened.st_ino:
            return None
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _capability_receipt_payload(receipt: CapabilityReceipt) -> bytes:
    return (
        json.dumps(receipt.to_dict(), indent=2, sort_keys=True).encode()
        + b"\n"
    )


def load_capability_receipt_artifact(
    backend_path: Path,
    *,
    receipt_path: Path,
    expected_payload_sha256: str,
    expected_schema_sha256: str,
) -> CapabilityReceiptArtifact | None:
    if (
        not _valid_sha256(expected_payload_sha256)
        or not _valid_sha256(expected_schema_sha256)
    ):
        return None
    payload = _read_capability_receipt_payload(receipt_path)
    if (
        payload is None
        or _sha256(payload) != expected_payload_sha256
    ):
        return None
    try:
        receipt = CapabilityReceipt.from_dict(json.loads(payload))
    except (json.JSONDecodeError, UnicodeDecodeError, SwitchError):
        return None
    if not receipt.matches_backend_and_schema_digest(
        backend_path,
        expected_schema_sha256,
    ):
        return None
    return CapabilityReceiptArtifact(
        receipt=receipt,
        payload=payload,
        payload_sha256=expected_payload_sha256,
        reused=True,
    )


def _unknown_capability_receipt(
    backend_path: Path,
    backend_sha256: str,
) -> CapabilityReceipt:
    if _file_sha256(backend_path) != backend_sha256:
        raise SwitchError(
            "Capability backend changed during capability receipt generation"
        )
    schema_marker = (
        "codex-switch-schema-unavailable-v1:"
        f"{backend_sha256}"
    ).encode()
    return CapabilityReceipt(
        backend_sha256=backend_sha256,
        schema_sha256=_sha256(schema_marker),
        capabilities=BackendCapabilities(None, None, None),
        schema_version=2,
    )


def prepare_capability_receipt_artifact(
    backend_path: Path,
    *,
    receipt_path: Path | None = None,
    expected_payload_sha256: str = "",
    expected_schema_sha256: str = "",
    schema_timeout_seconds: float,
    probe_timeout_seconds: float,
) -> CapabilityReceiptArtifact:
    if receipt_path is not None:
        existing = load_capability_receipt_artifact(
            backend_path,
            receipt_path=receipt_path,
            expected_payload_sha256=expected_payload_sha256,
            expected_schema_sha256=expected_schema_sha256,
        )
        if existing is not None:
            return existing

    backend_sha256 = _file_sha256(backend_path)
    try:
        receipt = CapabilityReceipt.from_backend(
            backend_path,
            schema_timeout_seconds=schema_timeout_seconds,
            probe_timeout_seconds=probe_timeout_seconds,
        )
    except SwitchError as exc:
        if (
            "backend changed during capability receipt generation"
            in str(exc).lower()
            or _file_sha256(backend_path) != backend_sha256
        ):
            raise
        receipt = _unknown_capability_receipt(
            backend_path,
            backend_sha256,
        )
    payload = _capability_receipt_payload(receipt)
    return CapabilityReceiptArtifact(
        receipt=receipt,
        payload=payload,
        payload_sha256=_sha256(payload),
        reused=False,
    )
