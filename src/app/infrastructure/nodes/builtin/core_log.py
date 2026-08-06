"""``core.log@1`` — write a value to the application log.

The only built-in with no outputs, so it is what proves a terminal node is
legal. Its ``Text`` input is the other half of the type-incompatibility case:
``trigger.manual`` emits ``Json``, which cannot connect here.
"""

from __future__ import annotations

from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner

log = structlog.get_logger(__name__)


class LogLevel(StrEnum):
    """Severity to emit at."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogConfig(BaseModel):
    """How loudly to log."""

    model_config = ConfigDict(extra="forbid")

    level: LogLevel = LogLevel.INFO


DESCRIPTOR = NodeDescriptor(
    node_type="core.log",
    version=1,
    category=NodeCategory.OUTPUT,
    config_model=LogConfig,
    display=NodeDisplay(
        label="Log",
        description="Writes the incoming text to the application log.",
        icon="file-text",
    ),
    inputs=(InputHandle(name="main", type=handles.TEXT),),
    # No outputs: a terminal node. Nothing may be connected downstream.
    side_effect=SideEffect.IDEMPOTENT,
)


class LogRunner(NodeRunner):
    """Emits one log line and completes."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        config = context.config
        if not isinstance(config, LogConfig):  # pragma: no cover - engine guarantees this
            raise TypeError(f"Expected {LogConfig.__name__}, got {type(config).__name__}")

        # Writing to a log is repeatable without visible harm, hence IDEMPOTENT
        # rather than AT_LEAST_ONCE: a duplicate line is noise, not a duplicated
        # side effect in the world.
        log.log(_LEVELS[config.level], "workflow_log_node", value=context.inputs["main"])
        return Completed()


# structlog's `log()` takes a stdlib numeric level; mapping here keeps the
# node's configuration vocabulary independent of the logging library.
_LEVELS = {
    LogLevel.DEBUG: 10,
    LogLevel.INFO: 20,
    LogLevel.WARNING: 30,
    LogLevel.ERROR: 40,
}

RUNNER = LogRunner()
