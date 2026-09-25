from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypedDict
from weakref import WeakKeyDictionary

from .mcp_gateway import EvidenceGateway
from .reasoning import fallback_output, normalize_draft, verify_output
from .rule_engine import generate_rule_draft
from .trace import TraceWriter

REQUIRED_TOOLS = {
    "get_customer_history",
    "get_order",
    "get_order_items",
    "get_order_payments",
    "get_payment_timeline",
    "get_policy",
    "get_product_context",
    "get_refund_timeline",
    "get_shipment_summary",
}
MAX_VERIFICATION_ATTEMPTS = 2
MAX_MCP_ATTEMPTS = 2
SYNTHETIC_CANDIDATE_PATTERN = re.compile(r"^candidate-\d+$", re.IGNORECASE)
PRODUCT_TOPICS = {"unavailable_order_paid"}
SHIPMENT_TOPICS = {
    "canceled_order_paid",
    "late_delivery_logistics",
    "late_delivery_seller",
    "unsupported_claim",
}
REFUND_TIMELINE_TOPICS = {
    "payment_mismatch",
    "refund_failed",
    "refund_pending",
    "valid_split_payment",
}
_TOOL_CACHE: WeakKeyDictionary[EvidenceGateway, tuple[str, ...]] = WeakKeyDictionary()


class WorkflowState(TypedDict, total=False):
    case: dict[str, Any]
    available_tools: list[str]
    plan: list[str]
    hypotheses: list[str]
    evidence: dict[str, dict[str, Any]]
    tool_failures: list[str]
    entity_resolution: dict[str, Any]
    order_id: str | None
    draft: dict[str, Any]
    verification_errors: list[str]
    verification_attempt: int


def _cache_key(tool_name: str, arguments: dict[str, str]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return f"{tool_name}:{encoded}"


def _exception_tree(error: BaseException) -> list[BaseException]:
    nested = getattr(error, "exceptions", None)
    if not nested:
        return [error]
    result: list[BaseException] = []
    for child in nested:
        result.extend(_exception_tree(child))
    return result


def _is_transient(error: BaseException) -> bool:
    markers = (
        "timeout",
        "connecterror",
        "connectionerror",
        "readerror",
        "writeerror",
        "pooltimeout",
        "remoteprotocolerror",
    )
    return any(
        any(marker in type(item).__name__.lower() for marker in markers)
        for item in _exception_tree(error)
    )


def _is_plausible_order_id(value: str) -> bool:
    """Reject synthetic decoys before they consume the MCP call budget."""
    return bool(value) and not SYNTHETIC_CANDIDATE_PATTERN.fullmatch(value)


def _find_key_values(value: Any, key: str) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for name, child in value.items():
            if name == key and isinstance(child, str) and child not in result:
                result.append(child)
            for item in _find_key_values(child, key):
                if item not in result:
                    result.append(item)
    elif isinstance(value, list):
        for child in value:
            for item in _find_key_values(child, key):
                if item not in result:
                    result.append(item)
    return result


def _claim_topics(case: dict[str, Any]) -> list[str]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    return [
        claim["topic"]
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    ]


def _has_capture_event(record: dict[str, Any] | None) -> bool:
    """Return whether a payment timeline can authoritatively supply the captured total."""
    if not record:
        return False

    def contains_valid_capture(value: Any) -> bool:
        if isinstance(value, dict):
            event_type = str(value.get("event_type", "")).lower()
            status = str(value.get("status", "")).lower()
            is_capture = any(marker in event_type for marker in ("captur", "charge", "paid"))
            is_valid = not any(
                marker in status for marker in ("fail", "declin", "cancel", "revers")
            )
            if is_capture and is_valid:
                return True
            return any(contains_valid_capture(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_valid_capture(child) for child in value)
        return False

    return contains_valid_capture(record.get("data"))


def _handoff(
    trace: TraceWriter,
    case_id: str,
    actor: str,
    target: str,
    decision_code: str,
) -> None:
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target=target,
        decision_code=decision_code,
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=target,
        decision_code=decision_code,
    )


async def _collect_evidence(
    *,
    state: WorkflowState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> dict[str, Any] | None:
    evidence = state.setdefault("evidence", {})
    failures = state.setdefault("tool_failures", [])
    key = _cache_key(tool_name, arguments)
    if key in evidence:
        return evidence[key]
    if tool_name not in state.get("available_tools", []):
        failure = f"{tool_name}:not_discovered"
        if failure not in failures:
            failures.append(failure)
        return None

    error: Exception | None = None
    result: dict[str, Any] | None = None
    for attempt in range(MAX_MCP_ATTEMPTS):
        try:
            result = await gateway.call(
                tool_name,
                case_id=state["case"]["case_id"],
                **arguments,
            )
            break
        except Exception as exc:
            error = exc
            if attempt == MAX_MCP_ATTEMPTS - 1 or not _is_transient(exc):
                break
            await asyncio.sleep(0.5 * (2**attempt))
    if result is None:
        label = type(error).__name__ if error is not None else "unknown_error"
        detail = str(error).replace("\n", " ")[:160] if error is not None else ""
        failure = f"{tool_name}:{label}:{detail}"
        if failure not in failures:
            failures.append(failure)
        return None

    record = {
        "tool_name": tool_name,
        "arguments": arguments,
        "evidence_ref": result["evidence_ref"],
        "result_hash": result["result_hash"],
        "domain": result["domain"],
        "data": result["data"],
        "warnings": result.get("warnings", []),
    }
    evidence[key] = record
    trace.emit(
        case_id=state["case"]["case_id"],
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[result["evidence_ref"]],
    )
    return record


async def _collect_batch(
    *,
    state: WorkflowState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    calls: list[tuple[str, dict[str, str]]],
) -> dict[str, dict[str, Any] | None]:
    if not calls:
        return {}
    results = await asyncio.gather(
        *(
            _collect_evidence(
                state=state,
                gateway=gateway,
                trace=trace,
                actor=actor,
                tool_name=tool_name,
                arguments=arguments,
            )
            for tool_name, arguments in calls
        )
    )
    return {
        _cache_key(tool_name, arguments): result
        for (tool_name, arguments), result in zip(calls, results, strict=True)
    }


async def _entity_agent(
    state: WorkflowState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    case = state["case"]
    request = case.get("customer_request", {})
    claimed = request.get("claimed_order_id") if isinstance(request, dict) else None
    candidates = [item for item in case.get("candidate_order_ids", []) if isinstance(item, str)]
    if isinstance(claimed, str) and claimed and claimed not in candidates:
        candidates.insert(0, claimed)
    candidates = list(dict.fromkeys(candidates))
    query_candidates = [candidate for candidate in candidates if _is_plausible_order_id(candidate)]
    customer_id = case.get("customer_unique_id_hint")
    calls = [("get_order", {"order_id": candidate}) for candidate in query_candidates]
    if isinstance(customer_id, str) and customer_id:
        calls.append(("get_customer_history", {"customer_unique_id": customer_id}))
    records = await _collect_batch(
        state=state,
        gateway=gateway,
        trace=trace,
        actor="entity-agent",
        calls=calls,
    )

    history_record = (
        records.get(_cache_key("get_customer_history", {"customer_unique_id": customer_id}))
        if isinstance(customer_id, str) and customer_id
        else None
    )
    history_ids = _find_key_values(history_record.get("data"), "order_id") if history_record else []
    resolved: list[str] = []
    for candidate in query_candidates:
        record = records.get(_cache_key("get_order", {"order_id": candidate}))
        order_ids = _find_key_values(record.get("data"), "order_id") if record else []
        if candidate in order_ids or candidate in history_ids:
            resolved.append(candidate)
    resolved = list(dict.fromkeys(resolved))
    rejected = [candidate for candidate in candidates if candidate not in resolved]
    if len(resolved) == 1:
        status, confidence, order_id = "resolved", 0.99, resolved[0]
    elif resolved:
        status, confidence, order_id = "ambiguous", 0.4, None
    else:
        status, confidence, order_id = "not_found", 0.0, None
    state["entity_resolution"] = {
        "status": status,
        "resolved_order_ids": resolved,
        "rejected_candidates": rejected,
        "confidence": confidence,
    }
    state["order_id"] = order_id
    order_records = [
        record
        for record in state.get("evidence", {}).values()
        if record.get("tool_name") == "get_order"
    ]
    if not order_records:
        failures = "; ".join(state.get("tool_failures", [])) or "unknown MCP failure"
        raise RuntimeError(f"MCP order evidence unavailable for {case['case_id']}: {failures}")
    _handoff(
        trace,
        case["case_id"],
        "entity-agent",
        "order-product-agent",
        "ENTITY_RESOLUTION_COMPLETE",
    )


async def _order_product_agent(
    state: WorkflowState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    order_id = state.get("order_id")
    if order_id:
        calls = [("get_order_items", {"order_id": order_id})]
        if PRODUCT_TOPICS.intersection(_claim_topics(state["case"])):
            calls.append(("get_product_context", {"order_id": order_id}))
        await _collect_batch(
            state=state,
            gateway=gateway,
            trace=trace,
            actor="order-product-agent",
            calls=calls,
        )
    _handoff(
        trace,
        state["case"]["case_id"],
        "order-product-agent",
        "shipment-agent",
        "ORDER_CONTEXT_READY",
    )


async def _shipment_agent(
    state: WorkflowState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    order_id = state.get("order_id")
    if order_id and SHIPMENT_TOPICS.intersection(_claim_topics(state["case"])):
        await _collect_evidence(
            state=state,
            gateway=gateway,
            trace=trace,
            actor="shipment-agent",
            tool_name="get_shipment_summary",
            arguments={"order_id": order_id},
        )
    _handoff(
        trace,
        state["case"]["case_id"],
        "shipment-agent",
        "payment-refund-agent",
        "SHIPMENT_ANALYSIS_READY",
    )


async def _payment_refund_agent(
    state: WorkflowState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    order_id = state.get("order_id")
    if order_id:
        timeline_arguments = {"order_id": order_id}
        calls = [("get_payment_timeline", timeline_arguments)]
        if REFUND_TIMELINE_TOPICS.intersection(_claim_topics(state["case"])):
            calls.append(("get_refund_timeline", {"order_id": order_id}))
        records = await _collect_batch(
            state=state,
            gateway=gateway,
            trace=trace,
            actor="payment-refund-agent",
            calls=calls,
        )
        timeline = records.get(_cache_key("get_payment_timeline", timeline_arguments))
        if not _has_capture_event(timeline):
            await _collect_evidence(
                state=state,
                gateway=gateway,
                trace=trace,
                actor="payment-refund-agent",
                tool_name="get_order_payments",
                arguments={"order_id": order_id},
            )
    _handoff(
        trace,
        state["case"]["case_id"],
        "payment-refund-agent",
        "policy-agent",
        "FINANCIAL_RECONCILIATION_READY",
    )


async def _policy_agent(
    state: WorkflowState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    case = state["case"]
    policy_version = case.get("policy_version")
    record = None
    if isinstance(policy_version, str) and policy_version:
        record = await _collect_evidence(
            state=state,
            gateway=gateway,
            trace=trace,
            actor="policy-agent",
            tool_name="get_policy",
            arguments={"policy_version": policy_version},
        )
    if record is None:
        failures = "; ".join(state.get("tool_failures", [])) or "policy evidence missing"
        raise RuntimeError(f"MCP policy evidence unavailable for {case['case_id']}: {failures}")
    trace.emit(
        case_id=case["case_id"],
        event_type="policy_decided",
        actor="policy-agent",
        decision_code="POLICY_EVIDENCE_APPLIED" if record else "POLICY_EVIDENCE_MISSING",
        evidence_refs=[record["evidence_ref"]] if record else None,
    )
    _handoff(
        trace,
        case["case_id"],
        "policy-agent",
        "verifier-agent",
        "RULE_DRAFT_READY",
    )


async def _rule_and_verify(state: WorkflowState, trace: TraceWriter) -> dict[str, Any]:
    output: dict[str, Any] = {}
    errors: list[str] = []
    final_attempt = 0
    for attempt in range(1, MAX_VERIFICATION_ATTEMPTS + 1):
        final_attempt = attempt
        if attempt == 1:
            draft = await generate_rule_draft(dict(state))
        else:
            draft = fallback_output(dict(state))
        output = normalize_draft(draft, dict(state))
        errors = verify_output(output, dict(state), trace.contracts)
        state["draft"] = output
        state["verification_errors"] = errors
        state["verification_attempt"] = attempt
        if not errors:
            break
        if attempt < MAX_VERIFICATION_ATTEMPTS:
            _handoff(
                trace,
                state["case"]["case_id"],
                "verifier-agent",
                "rule-engine",
                "REVISE_RULE_OUTPUT",
            )
    if errors:
        raise RuntimeError(f"deterministic rule output is invalid: {errors}")
    trace.emit(
        case_id=state["case"]["case_id"],
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="PASS",
        evidence_refs=output.get("evidence_refs", [])[:20] or None,
        attributes={"attempt": final_attempt, "error_count": 0},
    )
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a bounded, rule-based and evidence-first investigation for one case."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    available_tools = await _discovered_tools(gateway)
    missing = sorted(REQUIRED_TOOLS - set(available_tools))
    if missing:
        raise RuntimeError(f"MCP Gateway is missing required tools: {missing}")

    topics = _claim_topics(case)
    state: WorkflowState = {
        "case": case,
        "available_tools": available_tools,
        "plan": [
            "resolve_entity",
            "inspect_order_product",
            "inspect_shipment",
            "reconcile_payment_refund",
            "apply_policy",
            "rule_synthesis_and_verification",
        ],
        "hypotheses": [f"claim:{topic}" for topic in topics],
        "evidence": {},
        "tool_failures": [],
        "verification_errors": [],
        "verification_attempt": 0,
    }
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_ENTITY",
        attributes={"candidate_count": len(case.get("candidate_order_ids", []))},
    )
    await _entity_agent(state, gateway, trace)
    await _order_product_agent(state, gateway, trace)
    await _shipment_agent(state, gateway, trace)
    await _payment_refund_agent(state, gateway, trace)
    await _policy_agent(state, gateway, trace)
    return await _rule_and_verify(state, trace)


async def _discovered_tools(gateway: EvidenceGateway) -> list[str]:
    cached = _TOOL_CACHE.get(gateway)
    if cached is None:
        cached = tuple(await gateway.list_tools())
        _TOOL_CACHE[gateway] = cached
    return list(cached)
