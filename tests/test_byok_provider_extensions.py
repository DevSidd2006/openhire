"""Provider construction coverage for the additional BYOK providers.

These tests deliberately construct adapters only; they never claim that a
live credential is valid or make provider network requests.
"""
from providers.llm import build_provider


def test_build_provider_openrouter_uses_its_compatible_endpoint():
    provider = build_provider("openrouter", "test-secret", "openai/gpt-4o-mini")
    assert provider.model == "openai/gpt-4o-mini"
    assert str(provider.client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"


def test_build_provider_anthropic_constructs_without_exposing_or_validating_key():
    provider = build_provider("anthropic", "test-secret", "claude-3-5-sonnet-latest")
    assert provider.model == "claude-3-5-sonnet-latest"
