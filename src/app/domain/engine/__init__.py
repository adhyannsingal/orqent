"""Execution engine core — framework-free sequential runner.

Added in the execution phase. Owns lifecycle, state transitions, step
persistence, retries, timeouts, and cancellation. Depends only on ports
(``AgentRunner``, ``TaskQueue``, repositories via a unit of work) and never
imports FastAPI, LangChain, or a vendor SDK.
"""
