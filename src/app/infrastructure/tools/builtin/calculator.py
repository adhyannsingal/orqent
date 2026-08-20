"""``calculator`` — arithmetic a model can ask for (Phase 10, M6).

**Chosen to prove the mechanism, not to be useful.** M6's claim is that a model
can be shown a capability, request it, have its arguments checked, have it run,
and reason over the result — all without the engine, the queue, or the worker
learning that tools exist. Proving that needs a tool with four properties, and
arithmetic has all four:

- **Deterministic.** ``137 * 29`` is ``3973`` in every run, so an assertion about
  the answer is an assertion about the plumbing rather than about a model's mood.
- **Verifiable from outside.** A wrong answer is obviously wrong. A summarisation
  tool would have made every test a judgement call.
- **Genuinely beyond the model.** Not because models cannot multiply, but because
  the test asserts the *executor ran*, not that the digits matched — and a tool
  whose result the model could have guessed makes that assertion the only honest
  one available.
- **``PURE``.** No network, no filesystem, no database, no tenant, no credential,
  and nothing to clean up. Repeating it is free, which is what makes it safe
  under at-least-once execution (ADR-024) and what lets M6 enforce a PURE-only
  registry instead of building idempotency machinery it does not need yet.

Everything a real tool will eventually need — egress policy, connections,
approvals, per-tenant scoping — is deliberately absent, because none of it is
required to demonstrate the contract, and each is a milestone of its own.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.nodes.descriptor import SideEffect
from app.domain.tools.contract import Tool, ToolDefinition, ToolError

NAME = "calculator"


class CalculatorArguments(BaseModel):
    """What the model must supply.

    ``extra="forbid"``, the catalogue-wide rule, and here it does real work: a
    model that invents an extra field is a model that has misunderstood the tool,
    and accepting the call while ignoring the field would run *something* — just
    not what was asked for.
    """

    model_config = ConfigDict(extra="forbid")

    a: float = Field(description="The left-hand operand.")
    b: float = Field(description="The right-hand operand.")
    operation: Literal["add", "subtract", "multiply", "divide"] = Field(
        description="Which arithmetic operation to perform."
    )
    """A closed set rather than a free string.

    The model is shown the four permitted values and the schema refuses anything
    else, so "evaluate this expression" — the version of this tool that would
    need a parser, and with it an entire injection surface — is not
    representable. ADR-022's refusal to execute what the catalogue did not ship,
    applied one level down.
    """


class Calculator(Tool):
    """Adds, subtracts, multiplies, and divides. Nothing else."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=NAME,
            description=(
                "Perform exact arithmetic on two numbers. "
                "Use this for any calculation rather than computing it yourself."
            ),
            parameters=CalculatorArguments,
            side_effect=SideEffect.PURE,
        )

    async def execute(self, arguments: BaseModel) -> object:
        if not isinstance(arguments, CalculatorArguments):  # pragma: no cover - executor guarantees
            raise TypeError(f"Expected {CalculatorArguments.__name__}")

        if arguments.operation == "add":
            return arguments.a + arguments.b
        if arguments.operation == "subtract":
            return arguments.a - arguments.b
        if arguments.operation == "multiply":
            return arguments.a * arguments.b

        if arguments.b == 0:
            # A `ToolError`, not `inf` and not a fabricated string. The model
            # asked for something arithmetic does not define, and the honest
            # answer is a refusal it can react to — a silently wrong number
            # would be reasoned over as if it were right.
            raise ToolError("Division by zero is undefined.", retryable=False)
        return arguments.a / arguments.b


TOOL = Calculator()
