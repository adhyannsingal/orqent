"""Execution engine core — a reentrant scheduler over persisted state.

Pure domain (ADR-014): standard library only, and no node type. It holds no run
state between ticks; everything is re-derived from what the service layer has
already committed (ADR-019).

Phase 6 populates this package milestone by milestone. ``state.py`` (M1) declares
the run and node-execution state machines; the scheduler and its pure snapshot
boundary follow. Queues, workers, retries, timeouts, and cancellation are *not*
here — they belong to later phases and are deliberately absent rather than
stubbed.
"""
