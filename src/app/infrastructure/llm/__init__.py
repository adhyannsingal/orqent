"""LLM provider + agent-runner adapters.

Phase (mock providers): ``mock_provider.py`` implements the ``LLMProvider`` port
with deterministic fake output — no API keys, no network. Execution phase:
``langchain_agent_runner.py`` implements the ``AgentRunner`` port. This module
is the ONLY place permitted to import ``langchain`` or a vendor SDK.
"""
