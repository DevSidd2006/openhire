from cryptography.fernet import Fernet
import pytest

from providers.base import LLMPermanentError
from repositories.memory import InMemoryLLMCredentialRepository
from services.llm_credential_service import LLMCredentialService


@pytest.fixture(autouse=True)
def mock_provider_health(monkeypatch):
    async def healthy(provider, api_key, model):
        return {"provider": provider, "model": model, "connected": True, "success": True,
                "status": "connected", "auth_status": "valid", "last_tested_at": "2026-11-02T00:00:00+00:00",
                "last_successful_test_at": "2026-11-02T00:00:00+00:00", "message": "mock provider accessible"}
    monkeypatch.setattr("services.llm_credential_service.check_provider", healthy)


class StubProvider:
    def __init__(self, key, fail=False):
        self.key, self.fail = key, fail
        self.model = "stub-model"

    async def generate(self, prompt, **kwargs):
        if self.fail:
            raise LLMPermanentError("invalid test credential")
        return "accepted"

    async def generate_structured(self, prompt, schema, **kwargs):
        if self.fail:
            raise LLMPermanentError("invalid test credential")
        return {"ok": True}


def make_service():
    repo = InMemoryLLMCredentialRepository()
    service = LLMCredentialService(credential_repository=repo, encryption_key=Fernet.generate_key().decode())
    return repo, service


@pytest.mark.asyncio
async def test_multiple_same_provider_keys_and_metadata_edits_are_isolated(monkeypatch):
    repo, service = make_service()
    monkeypatch.setattr("services.llm_credential_service.build_provider", lambda provider, api_key, model=None: StubProvider(api_key))
    first = await service.save(user_id="r1", credential_id="key-1", name="Primary", provider="openai", api_key="secret-1")
    second = await service.save(user_id="r1", credential_id="key-2", name="Backup", provider="openai", api_key="secret-2")
    await service.save(user_id="r2", credential_id="key-3", name="Other user", provider="openai", api_key="secret-3")
    updated = await service.update_metadata("r1", "key-2", name="Spare", model="gpt-4o-mini", name_provided=True, model_provided=True)
    assert updated.name == "Spare" and updated.model == "gpt-4o-mini"
    assert [r.credential_id for r in await service.list("r1")] == [first.credential_id, second.credential_id]
    assert await service.get_by_id("r1", "key-3") is None
    await service.delete_key("r1", "key-1")
    assert [r.credential_id for r in await service.list("r1")] == ["key-2"]
    assert len(await service.list("r2")) == 1


@pytest.mark.asyncio
async def test_platform_mode_is_per_user_and_default_mode_resolves_default_credential(monkeypatch):
    repo, service = make_service()
    assert (await service.get_settings("new-user"))["mode"] == "platform"
    built = []
    monkeypatch.setattr("services.llm_credential_service.build_provider", lambda provider, api_key, model=None: built.append(api_key) or StubProvider(api_key))
    a = await service.save(user_id="r1", credential_id="a", name="A", provider="openai", api_key="key-a")
    await service.save(user_id="r1", credential_id="b", name="B", provider="gemini", api_key="key-b")
    await service.save(user_id="r2", credential_id="c", name="C", provider="openai", api_key="key-c")
    await service.save_settings("r1", {"mode":"platform","default_credential_id":"a","fallback_to_other_keys":True,"fallback_to_platform":True,"model":None})
    platform = StubProvider("platform")
    monkeypatch.setattr("services.llm_credential_service.get_llm_provider", lambda: platform)
    platform_mode = await service.resolve_provider_for_user("r1")
    assert await platform_mode.generate("uses platform") == "accepted"
    assert (await service.get_usage("r1"))[0]["credential_id"] == "platform"
    await service.save_settings("r1", {"mode":"byok","default_credential_id":"b","fallback_to_other_keys":False,"fallback_to_platform":False,"model":None})
    await service.save_settings("r2", {"mode":"byok","provider":"openai","default_credential_id":"c","fallback_to_other_keys":False,"fallback_to_platform":False,"model":None})
    provider = await service.resolve_provider_for_user("r1")
    assert await provider.generate("hello") == "accepted"
    assert built[-1] == "key-b"
    other = await service.resolve_provider_for_user("r2")
    assert await other.generate("hello") == "accepted"
    assert built[-1] == "key-c"
    assert a.credential_id == "a"


@pytest.mark.asyncio
async def test_fallback_order_and_usage_are_recorded_by_user_and_key(monkeypatch):
    repo, service = make_service()
    calls = {}
    def factory(provider, api_key, model=None):
        calls[api_key] = calls.get(api_key, 0) + 1
        return StubProvider(api_key, fail=api_key == "bad" and calls[api_key] > 0)
    monkeypatch.setattr(
        "services.llm_credential_service.build_provider",
        factory,
    )
    await service.save(user_id="r1", credential_id="bad-id", name="Bad", provider="openai", api_key="bad")
    await service.save(user_id="r1", credential_id="good-id", name="Good", provider="openai", api_key="good")
    await service.save_settings("r1", {"mode":"byok","default_credential_id":"bad-id","fallback_to_other_keys":True,"fallback_to_platform":False,"model":None})
    provider = await service.resolve_provider_for_user("r1")
    assert await provider.generate("prompt text") == "accepted"
    usage = {r["credential_id"]: r for r in await service.get_usage("r1")}
    assert usage["bad-id"]["requests"] == usage["bad-id"]["failures"] == 1
    assert usage["good-id"]["requests"] == usage["good-id"]["successes"] == 1
    assert usage["good-id"]["input_tokens"] > 0 and usage["good-id"]["output_tokens"] > 0
    assert await service.get_usage("r2") == []


@pytest.mark.asyncio
async def test_failure_can_fall_back_to_platform_provider(monkeypatch):
    repo, service = make_service()
    calls = {"bad": 0}
    def factory(provider, api_key, model=None):
        calls[api_key] = calls.get(api_key, 0) + 1
        return StubProvider(api_key, fail=calls[api_key] > 0)
    monkeypatch.setattr("services.llm_credential_service.build_provider", factory)
    platform = StubProvider("platform")
    monkeypatch.setattr("services.llm_credential_service.get_llm_provider", lambda: platform)
    await service.save(user_id="r1", credential_id="bad", name="Bad", provider="openai", api_key="bad")
    await service.save_settings("r1", {"mode":"byok","default_credential_id":"bad","fallback_to_other_keys":False,"fallback_to_platform":True,"model":None})
    provider = await service.resolve_provider_for_user("r1")
    assert await provider.generate("prompt") == "accepted"
    usage = await service.get_usage("r1")
    assert usage[0]["failures"] == 1


@pytest.mark.asyncio
async def test_preferred_provider_credential_and_model_are_resolved(monkeypatch):
    _repo, service = make_service()
    built = []
    monkeypatch.setattr(
        "services.llm_credential_service.build_provider",
        lambda provider, api_key, model=None: built.append((provider, api_key, model)) or StubProvider(api_key),
    )
    await service.save(user_id="r1", credential_id="openai-1", name="OpenAI primary", provider="openai", api_key="openai-key")
    await service.save(user_id="r1", credential_id="gemini-1", name="Gemini", provider="gemini", api_key="gemini-key")
    await service.save_settings("r1", {
        "mode": "byok", "provider": "gemini", "default_credential_id": "gemini-1",
        "fallback_to_other_keys": False, "fallback_to_platform": False, "model": "gemini-test-model",
    })
    provider = await service.resolve_provider_for_user("r1")
    await provider.generate("use the chosen model")
    assert built[-1] == ("gemini", "gemini-key", "gemini-test-model")
