#!/usr/bin/env bash
# Usage: source /path/to/fletcherlake-scheduler/sourceme-client.sh
# Prepare the client without changing the caller's directory or Python environment.

if [ -n "${BASH_VERSION:-}" ]; then
  if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    printf 'Use: source %s\n' "$0" >&2
    exit 1
  fi
  _fl_client_source=${BASH_SOURCE[0]}
elif [ -n "${ZSH_VERSION:-}" ]; then
  case ":${ZSH_EVAL_CONTEXT:-}:" in
    *:file:*) _fl_client_source=${(%):-%x} ;;
    *) printf 'Use: source %s\n' "$0" >&2; exit 1 ;;
  esac
else
  printf 'Source sourceme-client.sh from Bash or zsh.\n' >&2
  return 1 2>/dev/null || exit 1
fi

_fl_client_setup() {
  local root launcher completion_code
  if ! command -v pixi >/dev/null 2>&1; then
    printf 'Pixi is required. Install it from https://pixi.sh, then source this script again.\n' >&2
    return 1
  fi
  root=$(CDPATH='' cd -- "$(dirname -- "$_fl_client_source")" && pwd -P) || return 1
  if ! pixi install --locked --manifest-path "$root/pixi.toml"; then
    return 1
  fi
  if [ ! -x "$root/.pixi/envs/default/bin/fl-client" ]; then
    printf 'The Pixi environment is missing fl-client: %s\n' "$root" >&2
    return 1
  fi
  launcher="$root/bin"
  case ":${PATH:-}:" in
    *":$launcher:"*) ;;
    *) export PATH="$launcher${PATH:+:$PATH}" ;;
  esac
  if [ -n "${ZSH_VERSION:-}" ]; then
    if ! typeset -f compdef >/dev/null 2>&1; then
      autoload -Uz compinit
      # Initialize only in memory: no .zcompdump, startup edits or interactive prompts.
      compinit -D -i || return 1
    fi
    completion_code=$(_FL_CLIENT_COMPLETE=source_zsh "$launcher/fl-client") || return 1
  elif [ "${BASH_VERSINFO[0]}" -gt 4 ] || {
    [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 4 ];
  }; then
    completion_code=$(_FL_CLIENT_COMPLETE=source_bash "$launcher/fl-client") || return 1
  else
    printf 'fl-client is ready. Completion requires Bash 4.4+ or zsh.\n'
    return 0
  fi
  eval "$completion_code" || return 1
  printf 'fl-client is ready with session-only completion. Run: fl-client --help\n'
}

if _fl_client_setup; then
  unset _fl_client_source
  unset -f _fl_client_setup
else
  unset _fl_client_source
  unset -f _fl_client_setup
  return 1
fi
