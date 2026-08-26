"""
P8B.1: evaluation-framework provider-injection tests.

Proves the evaluation framework (evaluation/adapters.py,
evaluation/runner.py) can run the SAME golden cases through the SAME
adapters against either the default ScriptedLLMProvider or a real injected
LLMProvider (GeminiProvider), without any adapter creating a second, hidden
provider, without touching agent business logic, and without ever printing
an API key.

No real Gemini API call anywhere in this file - GeminiProvider construction
here only builds a local google.genai.Client object (no network), and every
call that would hit the network is either not exercised or is on a fake
provider standing in for a real one.
"""
import copy
import json

import pytest

from evaluation import adapters as adapters_module
from evaluation.adapters import ADAPTERS
from evaluation.models import EvaluationCase
from evaluation.runner import build_provider, load_cases, run_all, run_case
from providers.base import LLMProvider
from providers.llm.gemini import GeminiProvider
from tests.fakes import ScriptedLLMProvider, SpyLLMProvider


def _jd_case() -> EvaluationCase:
    real = next(c for c in load_cases(["jd_analyzer"]) if c.case_id == "jd_normal_rich")
    return real


def _tech_case() -> EvaluationCase:
    return next(c for c in load_cases(["technical_evaluator"]) if c.case_id == "tech_strong_answer_grounded")


# ---------------------------------------------------------------------------
# A/B: mock (default and explicit) uses ScriptedLLMProvider
# ---------------------------------------------------------------------------

class TestMockProviderSelection:
    def test_a_default_provider_choice_is_mock(self):
        """No --provider flag -> argparse default is "mock" -> build_provider
        returns None, which is what every adapter interprets as "build your
        own ScriptedLLMProvider", exactly as before P8B.1."""
        from evaluation.runner import _parse_args
        args = _parse_args([])
        assert args.provider == "mock"

    def test_b_explicit_mock_returns_none_sentinel(self, capsys):
        result = build_provider("mock")
        assert result is None
        captured = capsys.readouterr()
        assert "Provider: mock" in captured.out

    @pytest.mark.asyncio
    async def test_running_a_case_with_provider_none_uses_scripted_provider(self, monkeypatch):
        constructed = []
        real_init = ScriptedLLMProvider.__init__

        def _spy_init(self, *a, **kw):
            constructed.append(True)
            return real_init(self, *a, **kw)

        monkeypatch.setattr(ScriptedLLMProvider, "__init__", _spy_init)
        result = await run_case(_jd_case(), provider=None)
        assert constructed  # ScriptedLLMProvider WAS constructed
        assert result.passed


# ---------------------------------------------------------------------------
# C: explicit gemini constructs GeminiProvider (construction only, no network)
# ---------------------------------------------------------------------------

class TestGeminiProviderSelection:
    def test_c_gemini_provider_constructed_with_configured_model(self, monkeypatch, capsys):
        import evaluation.runner as runner_module
        monkeypatch.setattr("config.settings.GEMINI_API_KEY", "fake-key-not-real")
        monkeypatch.setattr("config.settings.GEMINI_MODEL", "gemini-3.6-flash")

        result = build_provider("gemini")

        assert isinstance(result, GeminiProvider)
        assert result.model == "gemini-3.6-flash"
        captured = capsys.readouterr()
        assert "Provider: gemini" in captured.out
        assert "Model: gemini-3.6-flash" in captured.out
        assert "fake-key-not-real" not in captured.out


# ---------------------------------------------------------------------------
# D/E: adapters use the injected provider directly, never a hidden one
# ---------------------------------------------------------------------------

class TestAdaptersUseInjectedProvider:
    @pytest.mark.asyncio
    async def test_d_agent_receives_the_exact_injected_provider(self):
        """Wrap a real (scripted) provider in a Spy so we can prove the
        SAME object reached the agent's generate_structured() call - not a
        fresh one the adapter built itself."""
        script = [json.dumps({"title": "Injected Title", "competencies": [{"name": "Python", "weight": 1.0}]})]
        injected = SpyLLMProvider(ScriptedLLMProvider(script=script))

        ctx = await ADAPTERS["jd_analyzer"](_jd_case(), injected)

        assert len(injected.generate_structured_calls) == 1
        assert ctx["output"].title == "Injected Title"

    @pytest.mark.asyncio
    async def test_e_no_adapter_builds_a_hidden_scripted_provider_when_injected(self, monkeypatch):
        # Build the real (scripted) fake FIRST, before patching __init__ to
        # explode - it stands in for "a real provider", already constructed,
        # exactly as evaluation/runner.py would hand a real GeminiProvider
        # to every adapter call for the whole run.
        real = ScriptedLLMProvider(script=[json.dumps({
            "technical_score": 7.0,
            "competency_scores": {"Python": {"score": 7.0, "confidence": 0.8, "evidence_question_number": 1, "explanation": "x"}},
        })])
        injected = SpyLLMProvider(real)

        def _boom(self, *a, **kw):
            raise AssertionError("ScriptedLLMProvider must not be constructed when a provider is injected")
        monkeypatch.setattr(ScriptedLLMProvider, "__init__", _boom)

        result = await ADAPTERS["technical_evaluator"](_tech_case(), injected)
        assert result["output"] is not None

    @pytest.mark.asyncio
    async def test_e_interviewer_adaptive_sequence_never_builds_hidden_provider(self, monkeypatch):
        """The multi-call, two-scenario adapter is the one most likely to
        accidentally construct its own provider per scenario - verify it
        doesn't when a provider is injected."""
        def _boom(self, *a, **kw):
            raise AssertionError("ScriptedLLMProvider must not be constructed when a provider is injected")
        monkeypatch.setattr(ScriptedLLMProvider, "__init__", _boom)

        # A tiny fake provider standing in for a real one - always returns a
        # valid, schema-shaped response regardless of prompt content. Each
        # question is made unique via a monotonic counter (never id(prompt),
        # which is not a reliable uniqueness source for equal/interned
        # strings) so the real duplicate-question check never misfires.
        class _AlwaysAskNewFake(LLMProvider):
            def __init__(self):
                self._counter = 0

            async def generate(self, prompt, **kwargs):
                raise NotImplementedError

            async def generate_structured(self, prompt, schema, **kwargs):
                if "question_text" in json.dumps(schema):
                    self._counter += 1
                    return {"question_text": f"Fake question number {self._counter}.", "question_type": "initial",
                            "difficulty": "medium", "reason": "x", "expected_duration_seconds": 60}
                return {"score": 9.0, "confidence": 0.9, "evidence_status": "supported",
                        "is_vague": False, "missing_detail": None, "explanation": "x"}

        case = next(c for c in load_cases(["interviewer_adaptive_sequence"])
                    if c.case_id == "interviewer_adaptive_termination_sufficient_evidence")
        ctx = await ADAPTERS["interviewer_adaptive_sequence"](case, _AlwaysAskNewFake())
        assert ctx is not None


# ---------------------------------------------------------------------------
# F: provider selection never mutates the golden cases themselves
# ---------------------------------------------------------------------------

class TestProviderSelectionDoesNotModifyCases:
    @pytest.mark.asyncio
    async def test_f_case_input_unchanged_after_running_with_injected_provider(self):
        case = _jd_case()
        before = copy.deepcopy(case.input)

        injected = ScriptedLLMProvider(script=[json.dumps({"title": "X", "competencies": [{"name": "Python", "weight": 1.0}]})])
        await ADAPTERS["jd_analyzer"](case, injected)

        assert case.input == before

    def test_f_load_cases_identical_regardless_of_provider_choice(self):
        cases_a = load_cases(["jd_analyzer"])
        cases_b = load_cases(["jd_analyzer"])
        assert [c.model_dump() for c in cases_a] == [c.model_dump() for c in cases_b]


# ---------------------------------------------------------------------------
# G: mock mode makes zero attempts to construct a real provider
# ---------------------------------------------------------------------------

class TestMockModeNoNetwork:
    @pytest.mark.asyncio
    async def test_g_mock_run_never_constructs_gemini_provider(self, monkeypatch):
        def _boom(self, *a, **kw):
            raise AssertionError("GeminiProvider must never be constructed during a mock-mode run")

        monkeypatch.setattr(GeminiProvider, "__init__", _boom)
        results = await run_all(["jd_analyzer"], provider=None)
        assert all(r is not None for r in results)


# ---------------------------------------------------------------------------
# H/I: missing credentials -> explicit error, key never leaked
# ---------------------------------------------------------------------------

class TestGeminiCredentialSafety:
    def test_h_missing_api_key_raises_explicit_system_exit(self, monkeypatch):
        monkeypatch.setattr("config.settings.GEMINI_API_KEY", "")
        with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
            build_provider("gemini")

    def test_i_missing_key_error_never_contains_a_key_value(self, monkeypatch):
        monkeypatch.setattr("config.settings.GEMINI_API_KEY", "")
        with pytest.raises(SystemExit) as exc_info:
            build_provider("gemini")
        message = str(exc_info.value)
        assert "GEMINI_API_KEY" in message  # names the variable
        assert "=" not in message  # never shows a key=value pair

    def test_i_successful_gemini_selection_never_prints_key(self, monkeypatch, capsys):
        secret = "SUPER_SECRET_NOT_REAL_VALUE"
        monkeypatch.setattr("config.settings.GEMINI_API_KEY", secret)
        monkeypatch.setattr("config.settings.GEMINI_MODEL", "gemini-3.6-flash")

        build_provider("gemini")

        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

    def test_i_reports_never_receive_the_api_key(self, tmp_path, monkeypatch):
        """write_report only ever records the provider NAME string
        ("mock"/"gemini"), never any credential."""
        from evaluation.runner import write_report
        from evaluation.models import EvaluationResult, Verdict

        monkeypatch.setattr("evaluation.runner.REPORTS_DIR", tmp_path)
        results = [EvaluationResult(case_id="x", agent="jd_analyzer", verdict=Verdict.PASS)]
        path = write_report(results, provider_label="gemini")
        content = path.read_text(encoding="utf-8")
        assert "GEMINI_API_KEY" not in content
        assert "AQ." not in content  # this project's known test key's distinctive prefix


# ---------------------------------------------------------------------------
# J: OpenAI provider remains fully unaffected
# ---------------------------------------------------------------------------

class TestOpenAIProviderUnaffected:
    def test_j_openai_provider_still_imports_and_classifies_normally(self):
        from providers.llm.openai import OpenAIProvider
        import openai as openai_sdk
        import httpx

        provider = OpenAIProvider.__new__(OpenAIProvider)
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(429, request=req)
        err = openai_sdk.RateLimitError("rate limited", response=resp, body=None)

        from providers.base import LLMTransientError
        classified = provider._classify(err)
        assert isinstance(classified, LLMTransientError)

    def test_j_build_provider_never_touches_openai_module(self, monkeypatch):
        """Neither the mock nor the gemini path in build_provider() should
        import or reference OpenAIProvider at all."""
        import providers.llm.openai as openai_module

        def _boom(*a, **kw):
            raise AssertionError("build_provider must never construct OpenAIProvider")

        monkeypatch.setattr(openai_module.OpenAIProvider, "__init__", _boom)
        assert build_provider("mock") is None
        monkeypatch.setattr("config.settings.GEMINI_API_KEY", "fake-key-not-real")
        assert isinstance(build_provider("gemini"), GeminiProvider)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
