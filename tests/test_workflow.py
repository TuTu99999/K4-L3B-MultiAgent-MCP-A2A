from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent import workflow
from student_agent.contracts import Contracts
from student_agent.submission import _validate_lifecycle
from student_agent.trace import TraceWriter


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


class FakeGateway:
    def __init__(self) -> None:
        self.sequence = 0

    async def list_tools(self) -> list[str]:
        return sorted(workflow.REQUIRED_TOOLS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.sequence += 1
        if tool_name == "get_order":
            data: Any = {"order_id": arguments["order_id"], "order_status": "delivered"}
            domain = "order"
        elif tool_name == "get_customer_history":
            data = {"orders": [{"order_id": "ORDER_1"}]}
            domain = "customer"
        elif tool_name == "get_policy":
            data = {"rules": {}}
            domain = "policy"
        else:
            data = {"order_id": "ORDER_1"}
            domain = tool_name.removeprefix("get_")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_workflow_reference_{self.sequence:04d}",
            "result_hash": f"sha256:{self.sequence:064x}",
            "domain": domain,
            "data": data,
        }


def test_workflow_emits_valid_a2a_lifecycle_and_output(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts())
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["ORDER_1"],
        "customer_unique_id_hint": "CUSTOMER_1",
        "policy_version": "EC_POLICY_V2",
        "customer_request": {
            "claimed_order_id": "ORDER_1",
            "claims": [{"claim_id": "CLAIM_1", "topic": "unsupported_claim"}],
        },
    }
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(workflow.solve_case(case, FakeGateway(), trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")

    contracts().validate_output(output, "workflow-output")
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    root = Path(__file__).resolve().parents[1]
    _validate_lifecycle(root, {case["case_id"]: output}, {case["case_id"]: events})
    event_types = {event["event_type"] for event in events}
    assert {"task_assigned", "handoff", "policy_decided", "verification_completed"} <= event_types
    assert {event["actor"] for event in events} >= {
        "coordinator",
        "entity-agent",
        "order-product-agent",
        "payment-refund-agent",
        "shipment-agent",
        "policy-agent",
        "verifier-agent",
    }


def test_generic_mcp_execution_error_is_retryable() -> None:
    error = RuntimeError("MCP tool failed: Error executing tool get_order")
    assert workflow._is_transient(error)
    assert not workflow._is_transient(RuntimeError("MCP tool failed: forbidden"))


class FailingEvidenceGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def list_tools(self) -> list[str]:
        return sorted(workflow.REQUIRED_TOOLS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")


def test_workflow_fails_fast_without_order_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(workflow.asyncio, "sleep", no_wait)
    gateway = FailingEvidenceGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    case = {
        "case_id": "CASE_001",
        "candidate_order_ids": ["ORDER_1"],
        "policy_version": "EC_POLICY_V2",
    }

    with pytest.raises(RuntimeError, match="MCP order evidence unavailable"):
        asyncio.run(workflow.solve_case(case, gateway, trace))
    assert gateway.calls == workflow.MAX_MCP_ATTEMPTS
