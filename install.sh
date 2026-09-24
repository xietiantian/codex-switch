#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]-}"
SCRIPT_DIR=""
SCRIPT_LOCAL_MODULES=0
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" && ! -L "$SCRIPT_SOURCE" ]]; then
  if SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_SOURCE")" && pwd)"; then
    SCRIPT_LOCAL_MODULES=1
  else
    echo "codex-switch install: could not resolve script directory" >&2
    exit 1
  fi
fi
REPO_TARBALL_BASE="https://github.com/cYz26/codex-switch/releases"
REPO_SOURCE_BASE="https://github.com/cYz26/codex-switch/archive"
INSTALL_DIR="${CODEX_SWITCH_INSTALL_DIR:-$HOME/.local/bin}"
LIB_DIR="${CODEX_SWITCH_LIB_DIR:-$HOME/.local/share/codex-switch}"
PYTHON_BIN="${CODEX_SWITCH_PYTHON:-python3}"
TRUSTED_SCRIPTS_DIR="${CODEX_SWITCH_TRUSTED_SCRIPTS_DIR:-}"
TRUSTED_BUNDLE_SHA256="9af53d9bcfff8f4c195c1d4ae204fcba9f27dd96543f0efe8c9e19cf60c80c0f"
TRUSTED_PROMOTION_SHA256="800df7f8c69393759976ceb11731660c8be77a213eee5b50643eea13cc5e2b91"
INSTALL_VERSION="${CODEX_SWITCH_VERSION:-}"
SOURCE_DIR="${CODEX_SWITCH_SOURCE_DIR:-}"
TARBALL_URL="${CODEX_SWITCH_TARBALL_URL:-}"
SOURCE_TARBALL_URL="${CODEX_SWITCH_SOURCE_TARBALL_URL:-}"
DRY_RUN="${CODEX_SWITCH_DRY_RUN:-0}"
PROXY_URL="${CODEX_SWITCH_GITHUB_PROXY:-}"

if [[ -z "$TARBALL_URL" ]]; then
  if [[ -n "$INSTALL_VERSION" ]]; then
    TARBALL_URL="$REPO_TARBALL_BASE/download/$INSTALL_VERSION/codex-switch.tar.gz"
  else
    TARBALL_URL="$REPO_TARBALL_BASE/latest/download/codex-switch.tar.gz"
  fi
fi

run_cmd() {
  if [[ "$DRY_RUN" == 1 || "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

is_dry_run() {
  [[ "$DRY_RUN" == 1 || "$DRY_RUN" == "true" ]]
}

copy_source() {
  local src="$1"
  local dest="$2"
  if [[ ! -x "$src/scripts/codex-switch" ]]; then
    echo "Invalid codex-switch source directory: $src" >&2
    return 1
  fi
  if ! rm -rf "$dest"; then
    return 1
  fi
  if ! mkdir -p "$(dirname "$dest")"; then
    return 1
  fi
  if ! cp -pR "$src" "$dest"; then
    return 1
  fi
}

source_tarball_url() {
  if [[ -n "$SOURCE_TARBALL_URL" ]]; then
    printf '%s\n' "$SOURCE_TARBALL_URL"
  elif [[ -n "$INSTALL_VERSION" ]]; then
    printf '%s/refs/tags/%s.tar.gz\n' "$REPO_SOURCE_BASE" "$INSTALL_VERSION"
  else
    printf '%s/refs/heads/main.tar.gz\n' "$REPO_SOURCE_BASE"
  fi
}

validate_downloaded_source() {
  local src="$1"
  local path

  for path in README.md SKILL.md VERSION run.sh; do
    if [[ ! -f "$src/$path" || -L "$src/$path" ]]; then
      echo "Downloaded source archive is missing required file: $path" >&2
      return 1
    fi
  done
  for path in agents docs evals scripts; do
    if [[ ! -d "$src/$path" || -L "$src/$path" ]]; then
      echo "Downloaded source archive is missing required directory: $path" >&2
      return 1
    fi
  done
  for path in run.sh scripts/codex-switch scripts/package-release.sh; do
    if [[ ! -f "$src/$path" || -L "$src/$path" || ! -x "$src/$path" ]]; then
      echo "Downloaded source archive is missing required executable: $path" >&2
      return 1
    fi
  done
}

copy_downloaded_source() {
  local src="$1"
  local dest="$2"
  local staged="${dest}.source.$$"
  local path

  if ! rm -rf "$staged"; then
    return 1
  fi
  if ! validate_downloaded_source "$src"; then
    return 1
  fi
  if ! mkdir -p "$(dirname "$dest")" "$staged"; then
    if ! rm -rf "$staged"; then
      echo "codex-switch install: could not clean source staging: $staged" >&2
    fi
    return 1
  fi
  for path in README.md SKILL.md VERSION run.sh agents docs evals scripts; do
    if ! cp -pR "$src/$path" "$staged/"; then
      if ! rm -rf "$staged"; then
        echo "codex-switch install: could not clean source staging: $staged" >&2
      fi
      return 1
    fi
  done
  if ! rm -rf "$staged/scripts/__pycache__"; then
    if ! rm -rf "$staged"; then
      echo "codex-switch install: could not clean source staging: $staged" >&2
    fi
    return 1
  fi
  if ! rm -rf "$dest" || ! mv "$staged" "$dest"; then
    if ! rm -rf "$staged"; then
      echo "codex-switch install: could not clean source staging: $staged" >&2
    fi
    return 1
  fi
}

resolve_archive_source() {
  local tmp="$1"
  local candidate="" entry
  local count=0

  if [[ -d "$tmp/codex-switch" && ! -L "$tmp/codex-switch" ]]; then
    printf '%s\n' "$tmp/codex-switch"
    return 0
  fi
  for entry in "$tmp"/*; do
    if [[ ! -e "$entry" && ! -L "$entry" ]]; then
      continue
    fi
    if [[ -d "$entry" && ! -L "$entry" ]]; then
      candidate="$entry"
      count=$((count + 1))
    fi
  done
  if [[ "$count" -ne 1 ]]; then
    echo "Downloaded archive must contain exactly one source directory" >&2
    return 1
  fi
  printf '%s\n' "$candidate"
}

install_archive() {
  local url="$1"
  local dest="$2"
  local mode="$3"
  local tmp src tar_options
  if ! tmp="$(mktemp -d)"; then
    echo "codex-switch install: could not create archive staging directory" >&2
    return 1
  fi
  tar_options="-xpzf"

  if [[ -n "$PROXY_URL" ]]; then
    export https_proxy="${https_proxy:-$PROXY_URL}"
    export http_proxy="${http_proxy:-$PROXY_URL}"
  fi

  if is_dry_run; then
    printf '[DRY-RUN] curl -fsSL "%s" -o "%s/codex-switch.tar.gz"\n' "$url" "$tmp"
    printf '[DRY-RUN] tar -xpzf "%s/codex-switch.tar.gz" -C "%s"\n' "$tmp" "$tmp"
    printf '[DRY-RUN] install extracted codex-switch to "%s"\n' "$dest"
    if ! rm -rf "$tmp"; then
      return 1
    fi
    return
  fi

  local status
  if curl -fsSL "$url" -o "$tmp/codex-switch.tar.gz"; then
    :
  else
    status=$?
    if ! rm -rf "$tmp"; then
      echo "codex-switch install: could not clean archive staging: $tmp" >&2
    fi
    return "$status"
  fi
  if tar "$tar_options" "$tmp/codex-switch.tar.gz" -C "$tmp"; then
    :
  else
    status=$?
    if ! rm -rf "$tmp"; then
      echo "codex-switch install: could not clean archive staging: $tmp" >&2
    fi
    return "$status"
  fi
  if src="$(resolve_archive_source "$tmp")"; then
    :
  else
    status=$?
    if ! rm -rf "$tmp"; then
      echo "codex-switch install: could not clean archive staging: $tmp" >&2
    fi
    return "$status"
  fi
  if [[ -z "$src" || ! -x "$src/scripts/codex-switch" ]]; then
    echo "Downloaded archive does not contain scripts/codex-switch" >&2
    if ! rm -rf "$tmp"; then
      echo "codex-switch install: could not clean archive staging: $tmp" >&2
    fi
    return 1
  fi
  if [[ "$mode" == "source" ]]; then
    if ! copy_downloaded_source "$src" "$dest"; then
      if ! rm -rf "$tmp"; then
        echo "codex-switch install: could not clean archive staging: $tmp" >&2
      fi
      return 1
    fi
  else
    if ! copy_source "$src" "$dest"; then
      if ! rm -rf "$tmp"; then
        echo "codex-switch install: could not clean archive staging: $tmp" >&2
      fi
      return 1
    fi
  fi
  if ! rm -rf "$tmp"; then
    return 1
  fi
}

download_source() {
  local dest="$1"
  local fallback_url status

  if install_archive "$TARBALL_URL" "$dest" "bundle"; then
    return
  fi

  if fallback_url="$(source_tarball_url)"; then
    :
  else
    status=$?
    return "$status"
  fi
  if [[ -n "$fallback_url" ]]; then
    echo "codex-switch install: release bundle unavailable; trying source archive fallback" >&2
    if install_archive "$fallback_url" "$dest" "source"; then
      return
    fi
  fi

  echo "codex-switch install: failed to download release bundle or source archive" >&2
  return 1
}

create_workdir() {
  local workdir
  if ! mkdir -p "$LIB_DIR"; then
    echo "codex-switch install: could not create library directory: $LIB_DIR" >&2
    return 1
  fi
  if ! workdir="$(mktemp -d "$LIB_DIR/.install-candidate.XXXXXX")"; then
    echo "codex-switch install: could not create candidate staging directory" >&2
    return 1
  fi
  printf '%s\n' "$workdir"
}

cleanup_workdir() {
  local workdir="$1"
  if ! rm -rf "$workdir"; then
    echo "codex-switch install: could not clean candidate staging: $workdir" >&2
    return 1
  fi
}

scripts_dir_ready() {
  local scripts_dir="$1"
  local module

  for module in codex_switch_release_bundle.py codex_switch_promotion.py; do
    if [[
      ! -f "$scripts_dir/$module"
      || -L "$scripts_dir/$module"
    ]]; then
      return 1
    fi
  done
}

file_sha256() {
  local path="$1"
  "$PYTHON_BIN" -I -B -c \
    'import hashlib, pathlib, sys
path = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())' \
    "$path"
}

scripts_dir_hash_bound() {
  local scripts_dir="$1"
  local bundle_hash promotion_hash

  if ! scripts_dir_ready "$scripts_dir"; then
    return 1
  fi
  if bundle_hash="$(file_sha256 "$scripts_dir/codex_switch_release_bundle.py")"; then
    :
  else
    return 1
  fi
  if promotion_hash="$(file_sha256 "$scripts_dir/codex_switch_promotion.py")"; then
    :
  else
    return 1
  fi
  [[
    "$bundle_hash" == "$TRUSTED_BUNDLE_SHA256"
    && "$promotion_hash" == "$TRUSTED_PROMOTION_SHA256"
  ]]
}

materialize_hash_bound_scripts() {
  local source_dir="$1"
  local destination_dir="$2"
  local module

  if ! scripts_dir_hash_bound "$source_dir"; then
    return 1
  fi
  if ! rm -rf "$destination_dir"; then
    return 1
  fi
  if ! mkdir -p "$destination_dir"; then
    return 1
  fi
  for module in codex_switch_release_bundle.py codex_switch_promotion.py; do
    if ! cp -p "$source_dir/$module" "$destination_dir/$module"; then
      if ! rm -rf "$destination_dir"; then
        echo "codex-switch install: could not clean trusted module staging" >&2
      fi
      return 1
    fi
  done
  if ! scripts_dir_hash_bound "$destination_dir"; then
    if ! rm -rf "$destination_dir"; then
      echo "codex-switch install: could not clean trusted module staging" >&2
    fi
    return 1
  fi
  printf '%s\n' "$destination_dir"
}

resolve_scripts_dir() {
  local source_root="$1"
  local workdir="$2"
  local candidate materialized

  if [[ -n "$TRUSTED_SCRIPTS_DIR" ]]; then
    if scripts_dir_ready "$TRUSTED_SCRIPTS_DIR"; then
      printf '%s\n' "$TRUSTED_SCRIPTS_DIR"
      return 0
    fi
    echo "codex-switch install: explicit trusted scripts are invalid" >&2
    return 1
  fi
  candidate="$SCRIPT_DIR/scripts"
  if [[
    "$SCRIPT_LOCAL_MODULES" == 1
    && -n "$SCRIPT_DIR"
  ]] && scripts_dir_ready "$candidate"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  for candidate in "$LIB_DIR/current/scripts" "$source_root/scripts"; do
    if scripts_dir_hash_bound "$candidate"; then
      if materialized="$(materialize_hash_bound_scripts "$candidate" "$workdir/trusted-scripts")"; then
        printf '%s\n' "$materialized"
        return 0
      fi
      echo "codex-switch install: could not materialize trusted promotion modules" >&2
      return 1
    fi
  done
  echo "codex-switch install: promotion modules are unavailable" >&2
  return 1
}

canonical_candidate() {
  local source_root="$1"
  local workdir="$2"
  local scripts_dir="$3"
  local output_root="$workdir/bundle"

  if [[
    -f "$source_root/bundle-manifest.json"
    && ! -L "$source_root/bundle-manifest.json"
  ]]; then
    printf '%s\n' "$source_root"
    return 0
  fi
  if ! "$PYTHON_BIN" -B "$scripts_dir/codex_switch_release_bundle.py" \
    --repo-root "$source_root" \
    --output-root "$output_root" >/dev/null; then
    return 1
  fi
  printf '%s\n' "$output_root/codex-switch"
}

promote_candidate() {
  local candidate_root="$1"
  local scripts_dir="$2"
  local expected_version="${INSTALL_VERSION#v}"
  local -a command=(
    "$PYTHON_BIN"
    -B
    "$scripts_dir/codex_switch_promotion.py"
    --candidate-root "$candidate_root"
    --layout-root "$LIB_DIR"
  )

  if [[ -n "$expected_version" ]]; then
    command+=(--expected-version "$expected_version")
  fi
  "${command[@]}" >/dev/null
}

main() {
  local workdir source_root scripts_dir candidate_root status cleanup_status
  local target_lib="$LIB_DIR/current"
  local target_bin="$INSTALL_DIR/codex-switch"

  if is_dry_run; then
    printf '[DRY-RUN] stage and validate candidate in "%s"\n' "$LIB_DIR"
    printf '[DRY-RUN] promote immutable release and retain rollback\n'
    run_cmd mkdir -p "$INSTALL_DIR"
    run_cmd ln -sfn "$target_lib/scripts/codex-switch" "$target_bin"
    echo "Installed codex-switch: $target_bin -> $target_lib/scripts/codex-switch"
    echo "Run: codex-switch status"
    return
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "codex-switch install: Python is unavailable: $PYTHON_BIN" >&2
    return 1
  fi
  if ! workdir="$(create_workdir)"; then
    return 1
  fi
  source_root="$workdir/source"
  status=0
  if [[ -n "$SOURCE_DIR" ]]; then
    if copy_source "$SOURCE_DIR" "$source_root"; then
      :
    else
      status=$?
    fi
  else
    if download_source "$source_root"; then
      :
    else
      status=$?
    fi
  fi

  if [[ "$status" -eq 0 ]]; then
    if scripts_dir="$(resolve_scripts_dir "$source_root" "$workdir")"; then
      :
    else
      status=$?
    fi
  fi
  if [[ "$status" -eq 0 ]]; then
    if candidate_root="$(canonical_candidate "$source_root" "$workdir" "$scripts_dir")"; then
      :
    else
      status=$?
    fi
  fi
  if [[ "$status" -eq 0 ]]; then
    if promote_candidate "$candidate_root" "$scripts_dir"; then
      :
    else
      status=$?
    fi
  fi
  cleanup_status=0
  if cleanup_workdir "$workdir"; then
    :
  else
    cleanup_status=$?
  fi
  if [[ "$status" -ne 0 ]]; then
    return "$status"
  fi
  if [[ "$cleanup_status" -ne 0 ]]; then
    return "$cleanup_status"
  fi
  if ! mkdir -p "$INSTALL_DIR"; then
    return 1
  fi
  if ! ln -sfn "$target_lib/scripts/codex-switch" "$target_bin"; then
    return 1
  fi
  echo "Installed codex-switch: $target_bin -> $target_lib/scripts/codex-switch"
  echo "Run: codex-switch status"
}

main "$@"
