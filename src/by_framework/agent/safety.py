"""Durable tool approval policies and structured terminal states."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .model import ToolCall
from .runtime.store import RunEvent
from .tools import ToolSpec, validate_tool_call


class TerminalStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    APPROVAL_REJECTED = "approval_rejected"


class ApprovalMode(str, Enum):
    NEVER = "never"
    RISK_BASED = "risk_based"
    ALWAYS = "always"


class ApprovalAction(str, Enum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    tool_call: ToolCall
    risk: str
    tool_spec: ToolSpec

    def interrupt(self):
        from .graph import Interrupt

        return Interrupt(
            self.approval_id,
            f"Approve tool {self.tool_call.name}?",
            "approval_decision",
        )


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    action: ApprovalAction
    arguments: dict[str, Any] | None = None
    reason: str = ""


class ApprovalRejectedError(RuntimeError):
    """A durable approval explicitly rejected the requested side effect."""


class ApprovalController:
    """Evaluate policy and normalize approve/edit/reject decisions."""

    def __init__(self, mode: ApprovalMode = ApprovalMode.RISK_BASED):
        self.mode = mode

    def request(
        self, approval_id: str, call: ToolCall, spec: ToolSpec
    ) -> ApprovalRequest | None:
        risky = spec.side_effect not in ("none", "read")
        if self.mode == ApprovalMode.NEVER:
            return None
        if self.mode == ApprovalMode.RISK_BASED and not risky:
            return None
        validate_tool_call(call, spec)
        return ApprovalRequest(approval_id, call, spec.side_effect, spec)

    def resolve(self, request: ApprovalRequest, decision: ApprovalDecision) -> ToolCall:
        if decision.approval_id != request.approval_id:
            raise ValueError("approval decision targets a different request")
        if decision.action == ApprovalAction.REJECT:
            raise ApprovalRejectedError(decision.reason or "tool approval rejected")
        if decision.action == ApprovalAction.EDIT:
            if decision.arguments is None:
                raise ValueError("edited approval requires replacement arguments")
            edited_call = ToolCall(
                request.tool_call.id,
                request.tool_call.name,
                dict(decision.arguments),
            )
            validate_tool_call(edited_call, request.tool_spec)
            return edited_call
        return request.tool_call

    @staticmethod
    def requested_event(request: ApprovalRequest) -> RunEvent:
        return RunEvent(
            "ApprovalRequested",
            {
                "approval_id": request.approval_id,
                "tool_call_id": request.tool_call.id,
                "tool_name": request.tool_call.name,
                "arguments": request.tool_call.arguments,
                "risk": request.risk,
            },
        )

    @staticmethod
    def resolved_event(decision: ApprovalDecision) -> RunEvent:
        return RunEvent(
            "ApprovalResolved",
            {
                "approval_id": decision.approval_id,
                "action": decision.action.value,
                "arguments": decision.arguments,
                "reason": decision.reason,
            },
        )
