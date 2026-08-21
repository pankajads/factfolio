"""llm_config.py — llm.yaml provider selection.

Covers: the in-memory claude_local default when llm.yaml doesn't exist yet,
validating "exactly one provider enabled", the implemented-vs-placeholder
distinction (claude_local/anthropic_api actually run; chatgpt_api/codex
fail fast), anthropic_api's API-key precondition, and render_llm_yaml's
round-trip through yaml.safe_load.
"""

from __future__ import annotations

import pytest
import yaml

from mybroker import config, llm_config


@pytest.fixture(autouse=True)
def _clear_cache():
    """load_llm_config() is lru_cache'd — never let one test's LLM_FILE
    monkeypatch leak into the next, independent of monkeypatch's own undo.
    Same reasoning as test_data.py's tickers-cache teardown."""
    llm_config.load_llm_config.cache_clear()
    yield
    llm_config.load_llm_config.cache_clear()


def _write(tmp_path, monkeypatch, text: str):
    """Point config.LLM_FILE at a scratch file with `text` for this test."""
    llm_file = tmp_path / "llm.yaml"
    llm_file.write_text(text)
    monkeypatch.setattr(config, "LLM_FILE", llm_file)
    llm_config.load_llm_config.cache_clear()
    return llm_file


class TestDefault:
    def test_no_file_defaults_to_claude_local(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LLM_FILE", tmp_path / "does-not-exist.yaml")
        assert llm_config.active_provider() == "claude_local"

    def test_ensure_supported_provider_passes_for_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LLM_FILE", tmp_path / "does-not-exist.yaml")
        assert llm_config.ensure_supported_provider() == "claude_local"


class TestValidation:
    def test_zero_enabled_raises(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, "providers:\n  claude_local:\n    enabled: false\n")
        with pytest.raises(llm_config.UnsupportedProviderError, match="No provider is enabled"):
            llm_config.active_provider()

    def test_multiple_enabled_raises(self, tmp_path, monkeypatch):
        _write(
            tmp_path, monkeypatch,
            "providers:\n"
            "  claude_local:\n    enabled: true\n"
            "  anthropic_api:\n    enabled: true\n",
        )
        with pytest.raises(llm_config.UnsupportedProviderError, match="Multiple providers"):
            llm_config.active_provider()


class TestEnsureSupportedProvider:
    def test_chatgpt_api_is_not_implemented_yet(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, "providers:\n  chatgpt_api:\n    enabled: true\n")
        with pytest.raises(llm_config.UnsupportedProviderError, match="isn.t implemented yet"):
            llm_config.ensure_supported_provider()

    def test_codex_is_not_implemented_yet(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, "providers:\n  codex:\n    enabled: true\n")
        with pytest.raises(llm_config.UnsupportedProviderError, match="isn.t implemented yet"):
            llm_config.ensure_supported_provider()

    def test_anthropic_api_requires_its_env_var(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _write(
            tmp_path, monkeypatch,
            "providers:\n  anthropic_api:\n    enabled: true\n    api_key_env: ANTHROPIC_API_KEY\n",
        )
        with pytest.raises(llm_config.UnsupportedProviderError, match="ANTHROPIC_API_KEY"):
            llm_config.ensure_supported_provider()

    def test_anthropic_api_passes_once_the_env_var_is_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _write(
            tmp_path, monkeypatch,
            "providers:\n  anthropic_api:\n    enabled: true\n    api_key_env: ANTHROPIC_API_KEY\n",
        )
        assert llm_config.ensure_supported_provider() == "anthropic_api"

    def test_respects_a_custom_api_key_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        _write(
            tmp_path, monkeypatch,
            "providers:\n  anthropic_api:\n    enabled: true\n    api_key_env: MY_KEY\n",
        )
        with pytest.raises(llm_config.UnsupportedProviderError, match="MY_KEY"):
            llm_config.ensure_supported_provider()


class TestRenderLlmYaml:
    @pytest.mark.parametrize("provider", llm_config.PROVIDERS)
    def test_flags_the_chosen_provider_and_leaves_every_other_intact(self, provider):
        """Picking one provider must only ever flip `enabled:` — every
        provider stays present with its description (and api_key_env,
        where it has one) untouched, just disabled."""
        data = yaml.safe_load(llm_config.render_llm_yaml(provider))
        providers = data["providers"]
        assert set(providers) == set(llm_config.PROVIDERS)
        for name, entry in providers.items():
            assert entry["enabled"] is (name == provider)
            assert entry["description"]  # never stripped, whichever one is chosen
        for name in ("anthropic_api", "chatgpt_api", "codex"):
            assert providers[name]["api_key_env"]

    def test_rejects_an_unknown_provider(self):
        with pytest.raises(ValueError):
            llm_config.render_llm_yaml("gemini")


class TestBundledDefault:
    def test_matches_render_llm_yaml_claude_local(self):
        """DEFAULT_LLM_FILE (committed, shown to anyone browsing the repo)
        must be byte-identical to what render_llm_yaml("claude_local")
        produces — one template, no second copy to drift out of sync."""
        assert config.DEFAULT_LLM_FILE.read_text() == llm_config.render_llm_yaml("claude_local")

    def test_is_valid_and_claude_local_only(self):
        data = yaml.safe_load(config.DEFAULT_LLM_FILE.read_text())
        providers = data["providers"]
        assert providers["claude_local"]["enabled"] is True
        for name in ("anthropic_api", "chatgpt_api", "codex"):
            assert providers[name]["enabled"] is False


class TestDescribeActiveProvider:
    def test_claude_local(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LLM_FILE", tmp_path / "does-not-exist.yaml")
        assert "claude_local" in llm_config.describe_active_provider()

    def test_invalid_config_is_reported_not_raised(self, tmp_path, monkeypatch):
        _write(tmp_path, monkeypatch, "providers:\n  claude_local:\n    enabled: false\n")
        # A status line, not a gate — must never itself raise, even for an
        # invalid config (ensure_supported_provider is the gate).
        assert "invalid" in llm_config.describe_active_provider()
