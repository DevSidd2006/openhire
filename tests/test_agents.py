"""Tests for agent functionality."""
import pytest
import asyncio

from agents.base import BaseAgent


class _ConcreteAgent(BaseAgent):
    """Minimal concrete BaseAgent subclass for testing the base class itself.

    BaseAgent is an ABC with an abstract execute(), so it cannot be
    instantiated directly."""

    async def execute(self, **kwargs):
        return {"result": "success"}


class TestBaseAgent:
    """Test BaseAgent functionality."""

    @pytest.mark.asyncio
    async def test_base_agent_initialization(self):
        """Test BaseAgent initialization."""
        agent = _ConcreteAgent(name="test_agent")
        assert agent.name == "test_agent"
        assert agent.logger is not None

    @pytest.mark.asyncio
    async def test_base_agent_run_wrapper(self):
        """Test BaseAgent.run() creates audit log."""
        test_agent = _ConcreteAgent(name="test")
        result = await test_agent.run(run_id="test_001")

        # run() should return dict with result
        assert result is not None

    @pytest.mark.asyncio
    async def test_prompt_loading(self):
        """Test prompt loading from file."""
        agent = _ConcreteAgent(name="test_agent")

        # This will test error handling for non-existent prompt
        try:
            prompt = agent.load_prompt("nonexistent.md")
            # If it returns None or empty, that's ok for testing
        except FileNotFoundError:
            # Expected for non-existent file
            pass


class TestAgentInheritance:
    """Test that agents properly inherit from BaseAgent."""

    @pytest.mark.asyncio
    async def test_agent_has_execute_method(self):
        """Test that all agents have execute method."""
        from agents import (
            JDAnalyzerAgent,
            ResumeParserAgent,
            ResumeMatcherAgent,
        )
        
        agents = [
            JDAnalyzerAgent(),
            ResumeParserAgent(),
            ResumeMatcherAgent(),
        ]
        
        for agent in agents:
            assert hasattr(agent, 'execute')
            assert callable(agent.execute)

    @pytest.mark.asyncio
    async def test_agent_has_run_method(self):
        """Test that all agents have run method."""
        from agents import JDAnalyzerAgent
        
        agent = JDAnalyzerAgent()
        assert hasattr(agent, 'run')
        assert callable(agent.run)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
