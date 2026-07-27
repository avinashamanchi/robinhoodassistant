#!/usr/bin/env bash
# Shell helpers shared by start.sh and stop.sh. This file is sourced only.

configure_process_identity() {
  PROCESS_PROJECT="$1"
  PROCESS_PY="$2"
  PROCESS_PID_FILE="$3"
  PROCESS_LOG_DIR="$(dirname "$PROCESS_PID_FILE")"
  PROCESS_EXPECTED_ARGV="$PROCESS_PY -m trading_assistant.ops.serve"
  PROCESS_METADATA_PID=""
  PROCESS_METADATA_START=""
  PROCESS_METADATA_CWD=""
  PROCESS_METADATA_ARGV=""
}

_trim_process_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

_process_start_identity() {
  local pid="$1"
  local value
  value="$(ps -ww -p "$pid" -o lstart= 2>/dev/null)" || return 1
  value="$(_trim_process_value "$value")"
  [[ -n "$value" ]] || return 1
  awk '{$1=$1; print}' <<< "$value"
}

_process_command_identity() {
  local pid="$1"
  local value
  value="$(ps -ww -p "$pid" -o command= 2>/dev/null)" || return 1
  _trim_process_value "$value"
}

_process_cwd_identity() {
  local pid="$1"
  local value
  value="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ {sub(/^n/, ""); print; exit}')" || return 1
  [[ -n "$value" ]] || return 1
  printf '%s\n' "$value"
}

managed_process_matches() {
  local pid="$1"
  local expected_start="$2"
  local command
  local cwd
  local actual_start
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  [[ -n "$expected_start" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  command="$(_process_command_identity "$pid")" || return 1
  [[ "$command" == "$PROCESS_EXPECTED_ARGV" ]] || return 1
  cwd="$(_process_cwd_identity "$pid")" || return 1
  [[ "$cwd" == "$PROCESS_PROJECT" ]] || return 1
  actual_start="$(_process_start_identity "$pid")" || return 1
  [[ "$actual_start" == "$expected_start" ]]
}

write_process_metadata() {
  local pid="$1"
  local start
  local temporary
  start="$(_process_start_identity "$pid")" || return 1
  managed_process_matches "$pid" "$start" || return 1
  temporary="$(mktemp "$PROCESS_LOG_DIR/.app.pid.XXXXXX")" || return 1
  chmod 600 "$temporary"
  {
    printf 'version=1\n'
    printf 'pid=%s\n' "$pid"
    printf 'start=%s\n' "$start"
    printf 'cwd=%s\n' "$PROCESS_PROJECT"
    printf 'argv=%s\n' "$PROCESS_EXPECTED_ARGV"
  } > "$temporary"
  mv -f "$temporary" "$PROCESS_PID_FILE"
}

load_process_metadata() {
  local line
  local value
  local version_seen=0
  local pid_seen=0
  local start_seen=0
  local cwd_seen=0
  local argv_seen=0
  [[ -f "$PROCESS_PID_FILE" && ! -L "$PROCESS_PID_FILE" ]] || return 1
  PROCESS_METADATA_PID=""
  PROCESS_METADATA_START=""
  PROCESS_METADATA_CWD=""
  PROCESS_METADATA_ARGV=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      version=*)
        [[ "$version_seen" == 0 && "$line" == "version=1" ]] || return 1
        version_seen=1
        ;;
      pid=*)
        [[ "$pid_seen" == 0 ]] || return 1
        value="${line#pid=}"
        [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 1
        PROCESS_METADATA_PID="$value"
        pid_seen=1
        ;;
      start=*)
        [[ "$start_seen" == 0 ]] || return 1
        value="${line#start=}"
        [[ -n "$value" ]] || return 1
        PROCESS_METADATA_START="$value"
        start_seen=1
        ;;
      cwd=*)
        [[ "$cwd_seen" == 0 ]] || return 1
        value="${line#cwd=}"
        [[ "$value" == "$PROCESS_PROJECT" ]] || return 1
        PROCESS_METADATA_CWD="$value"
        cwd_seen=1
        ;;
      argv=*)
        [[ "$argv_seen" == 0 ]] || return 1
        value="${line#argv=}"
        [[ "$value" == "$PROCESS_EXPECTED_ARGV" ]] || return 1
        PROCESS_METADATA_ARGV="$value"
        argv_seen=1
        ;;
      *)
        return 1
        ;;
    esac
  done < "$PROCESS_PID_FILE"
  [[ "$version_seen" == 1 && "$pid_seen" == 1 && "$start_seen" == 1 && "$cwd_seen" == 1 && "$argv_seen" == 1 ]]
}

managed_process_matches_metadata() {
  [[ "$PROCESS_METADATA_CWD" == "$PROCESS_PROJECT" ]] || return 1
  [[ "$PROCESS_METADATA_ARGV" == "$PROCESS_EXPECTED_ARGV" ]] || return 1
  managed_process_matches "$PROCESS_METADATA_PID" "$PROCESS_METADATA_START"
}

signal_managed_process() {
  local signal="$1"
  load_process_metadata || return 1
  managed_process_matches_metadata || return 1
  kill "-$signal" "$PROCESS_METADATA_PID"
}
