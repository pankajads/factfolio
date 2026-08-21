"""LLM provider selection — `llm.yaml`.

Every agent in this project (report orchestrator, chat REPL, ticker/schema
resolvers — see agents/) runs on `claude_agent_sdk`, which spawns the local
`claude` CLI. Historically the only "choice" was implicit: `ANTHROPIC_API_KEY`
wins if set in the environment, otherwise it falls back to a local `claude
login` session — nothing recorded that choice anywhere a user could see or
edit it.

`llm.yaml` makes that choice explicit: exactly one provider is `enabled:
true` at a time. Same two-file pattern as tickers.yaml: a generic, committed
`data/llm.yaml` (DEFAULT_LLM_FILE — dummy/example data, safe to browse in
the repo and shows anyone reading it exactly how to configure this) seeds
your project's own `llm.yaml` at `factfolio init`, which is yours to
hand-edit afterward and never overwritten again.

Only two providers actually run anything today:
  - claude_local   — the existing zero-config default (local `claude login`).
  - anthropic_api  — the same claude_agent_sdk session, but requires its
                      api_key_env (default ANTHROPIC_API_KEY) to be set.

`chatgpt_api` and `codex` are scaffolding for future providers — selecting
either is a valid, recorded choice, but `ensure_supported_provider()` fails
fast with a clear error rather than silently doing nothing. Actually running
the agent graph on either would mean a different SDK and a different
tool-calling loop end to end, not a config flag — out of scope here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

PROVIDERS = ("claude_local", "anthropic_api", "chatgpt_api", "codex")

# The only providers that can actually run an agent session today — see the
# module docstring. Selecting anything else is a real, recorded choice in
# llm.yaml; it just isn't wired up to execute yet.
IMPLEMENTED_PROVIDERS = frozenset({"claude_local", "anthropic_api"})

# Default env var each provider's API key is read from — overridable per
# provider via llm.yaml's own `api_key_env:` field.
_DEFAULT_API_KEY_ENV = {
    "anthropic_api": "ANTHROPIC_API_KEY",
    "chatgpt_api": "OPENAI_API_KEY",
    "codex": "OPENAI_API_KEY",
}


class UnsupportedProviderError(RuntimeError):
    """llm.yaml selects a provider that can't run (not implemented yet, or
    missing its required API key), or its own config is invalid (zero or
    more than one provider enabled). Always a short, actionable message —
    errors.py's friendly_message() surfaces it as-is, no rewriting."""


def render_llm_yaml(enabled_provider: str = "claude_local") -> str:
    """The full, commented llm.yaml text with `enabled_provider` flagged
    `enabled: true` and every other provider `false`.

    Hand-written, not `yaml.safe_load` + `yaml.dump` — a round trip through
    PyYAML's dumper would silently drop every comment below, the same
    reason cli.py's `_render_policy_template` builds its output as an
    f-string rather than serialising a dict.
    """
    if enabled_provider not in PROVIDERS:
        raise ValueError(
            f"{enabled_provider!r} is not a known provider — one of {PROVIDERS}."
        )

    def flag(name: str) -> str:
        return "true" if name == enabled_provider else "false"

    return f"""\
# ─────────────────────────────────────────────────────────────────────────────
# LLM provider selection
#
# Exactly ONE provider below must be `enabled: true` — factfolio uses it to
# run every agent (report, chat, ticker/schema resolvers).
#
# TO SWITCH PROVIDERS BY HAND: set the one you want to `enabled: true` and
# every other provider's `enabled: false` (exactly one true at a time), save,
# then just re-run `factfolio` — no `init`, no restart, nothing else needed.
#
# claude_local and anthropic_api both run today, via the same underlying
# claude_agent_sdk — they differ only in auth: claude_local uses your local
# `claude login` session, anthropic_api uses an explicit API key.
#
# chatgpt_api and codex are placeholders for future providers. Enabling one
# is a valid choice, but factfolio doesn't talk to OpenAI yet — it fails
# fast with a clear error instead of silently doing nothing.
# ─────────────────────────────────────────────────────────────────────────────
providers:
  claude_local:
    enabled: {flag("claude_local")}
    description: "Local Claude Code CLI session (`claude login`). Zero-config default."

  anthropic_api:
    enabled: {flag("anthropic_api")}
    description: "Anthropic API via an explicit key, through the same claude_agent_sdk."
    api_key_env: ANTHROPIC_API_KEY

  chatgpt_api:
    enabled: {flag("chatgpt_api")}
    description: "OpenAI ChatGPT API. NOT YET IMPLEMENTED — placeholder for a future release."
    api_key_env: OPENAI_API_KEY

  codex:
    enabled: {flag("codex")}
    description: "OpenAI Codex CLI. NOT YET IMPLEMENTED — placeholder for a future release."
    api_key_env: OPENAI_API_KEY
"""


@lru_cache(maxsize=1)
def load_llm_config() -> dict[str, Any]:
    """Load and cache llm.yaml: your project's own (seeded by `factfolio
    init`, yours to edit) if it exists, the bundled generic default
    (DEFAULT_LLM_FILE, committed to the repo — claude_local enabled)
    otherwise — e.g. before your first `init`, or if llm.yaml was deleted.
    Mirrors config.load_tickers()'s own fallback behaviour exactly."""
    from mybroker import config

    path = config.LLM_FILE if config.LLM_FILE.exists() else config.DEFAULT_LLM_FILE
    with path.open() as fh:
        return yaml.safe_load(fh)


def active_provider() -> str:
    """The single enabled provider's name.

    Raises UnsupportedProviderError if llm.yaml has zero or more than one
    provider enabled — exactly one must be true.
    """
    from mybroker import config

    providers = load_llm_config().get("providers", {})
    enabled = [name for name in PROVIDERS if providers.get(name, {}).get("enabled")]

    if len(enabled) == 1:
        return enabled[0]

    where = config.LLM_FILE if config.LLM_FILE.exists() else config.DEFAULT_LLM_FILE
    if not enabled:
        raise UnsupportedProviderError(
            f"No provider is enabled in {where} — set exactly one of "
            f"{PROVIDERS} to `enabled: true`."
        )
    raise UnsupportedProviderError(
        f"Multiple providers are enabled in {where} ({', '.join(enabled)}) — "
        f"exactly one must be `enabled: true`."
    )


def ensure_supported_provider() -> str:
    """active_provider(), plus a fast, friendly failure for a provider that
    isn't wired up to actually run anything yet, or is missing its required
    API key. Call this before opening any claude_agent_sdk session — every
    agents/*.py options builder does."""
    import os

    provider = active_provider()

    if provider not in IMPLEMENTED_PROVIDERS:
        raise UnsupportedProviderError(
            f"llm.yaml selects '{provider}', which isn't implemented yet — "
            f"only {', '.join(sorted(IMPLEMENTED_PROVIDERS))} actually run "
            f"today. Edit llm.yaml and enable one of those instead."
        )

    if provider == "anthropic_api":
        env_var = _api_key_env("anthropic_api")
        if not os.environ.get(env_var):
            raise UnsupportedProviderError(
                f"llm.yaml selects 'anthropic_api' but ${env_var} is not "
                f"set. Export it, or switch llm.yaml back to claude_local."
            )

    return provider


def describe_active_provider() -> str:
    """Plain-text one-liner naming the active provider and its auth source
    — the single source of truth cli.py and chat.py both format for their
    own pre-run banner (rich markup / raw ANSI respectively)."""
    import os

    try:
        provider = active_provider()
    except UnsupportedProviderError as exc:
        return f"provider: invalid ({exc})"

    if provider == "claude_local":
        return "provider: claude_local · auth: local `claude login` session"

    if provider == "anthropic_api":
        env_var = _api_key_env("anthropic_api")
        state = "set" if os.environ.get(env_var) else "NOT SET"
        return f"provider: anthropic_api · auth: ${env_var} ({state})"

    return f"provider: {provider} · NOT IMPLEMENTED YET"


def _api_key_env(provider: str) -> str:
    """The env var `provider`'s API key is read from — llm.yaml's own
    `api_key_env:` if it set one, else the built-in default."""
    entry = load_llm_config().get("providers", {}).get(provider, {})
    return entry.get("api_key_env") or _DEFAULT_API_KEY_ENV[provider]
