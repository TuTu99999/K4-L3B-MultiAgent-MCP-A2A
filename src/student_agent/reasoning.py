from __future__ import annotations

import copy
import math
from typing import Any

from .contracts import Contracts

EXPECTED_RESPONSIBILITY = {
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "valid_split_payment": "customer",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "refund_pending": "payment_provider",
    "refund_failed": "payment_provider",
    "unsupported_claim": "customer",
    "insufficient_evidence": "unknown",
}


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result


def evidence_refs(state: dict[str, Any]) -> list[str]:
    return [
        record["evidence_ref"]
        for record in state.get("evidence", {}).values()
        if isinstance(record, dict) and isinstance(record.get("evidence_ref"), str)
    ]


def _claim_ids(case: dict[str, Any]) -> list[str]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    return [
        claim["claim_id"]
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
    ]


def _claim_topics_by_id(case: dict[str, Any]) -> dict[str, str]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    return {
        claim["claim_id"]: claim["topic"]
        for claim in claims
        if isinstance(claim, dict)
        and isinstance(claim.get("claim_id"), str)
        and isinstance(claim.get("topic"), str)
    }


def _relevant_claim_refs(claim_id: str, state: dict[str, Any]) -> list[str]:
    topic = _claim_topics_by_id(state["case"]).get(claim_id, "")
    if topic.startswith("late_delivery"):
        tools = {
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_policy",
        }
    elif topic in {"valid_split_payment", "payment_mismatch", "duplicate_charge"}:
        tools = {"get_order", "get_order_payments", "get_payment_timeline", "get_policy"}
    elif topic in {"refund_pending", "refund_failed", "requested_full_refund"}:
        tools = {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        }
    elif topic in {"canceled_order_paid", "unavailable_order_paid"}:
        tools = {
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_refund_timeline",
            "get_policy",
        }
    else:
        tools = {record.get("tool_name") for record in state.get("evidence", {}).values()}
    return [
        record["evidence_ref"]
        for record in state.get("evidence", {}).values()
        if record.get("tool_name") in tools and isinstance(record.get("evidence_ref"), str)
    ]


def fallback_output(state: dict[str, Any]) -> dict[str, Any]:
    case = state["case"]
    resolution = state.get("entity_resolution", {})
    resolved = _unique_strings(resolution.get("resolved_order_ids", []))
    rejected = _unique_strings(resolution.get("rejected_candidates", []))
    refs = evidence_refs(state)
    customer_id = case.get("customer_unique_id_hint")
    if not isinstance(customer_id, str):
        customer_id = None
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": resolved,
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": claim_id,
                "verdict": "insufficient_evidence",
                "confidence": 0.0,
                "evidence_refs": refs,
            }
            for claim_id in _claim_ids(case)
        ],
        "entity_resolution": {
            "status": resolution.get("status", "not_found"),
            "resolved_order_ids": resolved,
            "rejected_candidates": rejected,
            "confidence": float(resolution.get("confidence", 0.0)),
        },
        "customer_context": {
            "customer_unique_id": customer_id,
            "related_order_ids": [],
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [
                {"party_type": "unknown", "party_id": None},
            ],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["manual_investigation"],
    }


def normalize_draft(draft: Any, state: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(draft) if isinstance(draft, dict) else {}
    case = state["case"]
    resolution = state.get("entity_resolution", {})
    actual_refs = evidence_refs(state)

    value["schema_version"] = "day09-l3b-output-v2"
    value["case_id"] = case["case_id"]
    submitted_refs = _unique_strings(value.get("evidence_refs", []))
    value["evidence_refs"] = [ref for ref in submitted_refs if ref in actual_refs]
    if not value["evidence_refs"] and actual_refs:
        value["evidence_refs"] = actual_refs
    value["entity_resolution"] = {
        "status": resolution.get("status", "not_found"),
        "resolved_order_ids": _unique_strings(resolution.get("resolved_order_ids", [])),
        "rejected_candidates": _unique_strings(resolution.get("rejected_candidates", [])),
        "confidence": float(resolution.get("confidence", 0.0)),
    }

    entities = value.get("affected_entities")
    if isinstance(entities, dict):
        for field in (
            "order_ids",
            "item_ids",
            "seller_ids",
            "payment_references",
            "shipment_ids",
        ):
            entities[field] = _unique_strings(entities.get(field, []))
        for order_id in value["entity_resolution"]["resolved_order_ids"]:
            if order_id not in entities["order_ids"]:
                entities["order_ids"].append(order_id)

    claims = value.get("claim_assessments")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict):
                filtered_refs = [
                    ref
                    for ref in _unique_strings(claim.get("evidence_refs", []))
                    if ref in actual_refs
                ]
                if not filtered_refs and isinstance(claim.get("claim_id"), str):
                    filtered_refs = _relevant_claim_refs(claim["claim_id"], state)
                claim["evidence_refs"] = filtered_refs

    causes = value.get("root_cause_analysis", {}).get("ranked_causes")
    if isinstance(causes, list):
        for rank, cause in enumerate(causes, 1):
            if isinstance(cause, dict):
                cause["rank"] = rank

    conflicts = value.get("data_conflicts")
    if isinstance(conflicts, list):
        for conflict in conflicts:
            if isinstance(conflict, dict):
                conflict["sources"] = _unique_strings(conflict.get("sources", []))

    value["resolution_actions"] = _unique_strings(value.get("resolution_actions", []))
    return value


def verify_output(value: dict[str, Any], state: dict[str, Any], contracts: Contracts) -> list[str]:
    errors: list[str] = []
    try:
        contracts.validate_output(value, f"draft/{state['case']['case_id']}.json")
    except ValueError as exc:
        errors.append(f"schema:{exc}")

    actual_refs = set(evidence_refs(state))
    submitted_refs = value.get("evidence_refs", [])
    if isinstance(submitted_refs, list) and not set(submitted_refs).issubset(actual_refs):
        errors.append("provenance:output contains an unknown evidence_ref")

    expected_claims = set(_claim_ids(state["case"]))
    submitted_claims = value.get("claim_assessments", [])
    if isinstance(submitted_claims, list):
        submitted_ids = {
            claim.get("claim_id") for claim in submitted_claims if isinstance(claim, dict)
        }
        if submitted_ids != expected_claims:
            errors.append("claims:every input claim must have exactly one assessment")
        missing_refs = any(
            not claim.get("evidence_refs")
            for claim in submitted_claims
            if isinstance(claim, dict)
        )
        if actual_refs and missing_refs:
            errors.append("claims:every claim assessment requires evidence")

        topics = _claim_topics_by_id(state["case"])
        primary_issue = value.get("assessment", {}).get("primary_issue")
        secondary_issues = set(value.get("assessment", {}).get("secondary_issues", []))
        shipment_verdict = value.get("shipment_analysis", {}).get("verdict")
        payment_verdict = value.get("payment_analysis", {}).get("verdict")
        evidence_supported = {
            "late_delivery_logistics": shipment_verdict
            in {"logistics_delay", "lost", "returned"},
            "late_delivery_seller": shipment_verdict == "seller_delay",
            "payment_mismatch": payment_verdict == "capture_mismatch",
            "duplicate_charge": payment_verdict == "duplicate_capture",
            "refund_pending": payment_verdict == "refund_pending",
            "refund_failed": payment_verdict == "refund_failed",
        }
        for claim in submitted_claims:
            if not isinstance(claim, dict):
                continue
            topic = topics.get(claim.get("claim_id", ""))
            verdict = claim.get("verdict")
            if (
                topic == primary_issue
                and primary_issue != "insufficient_evidence"
                and verdict != "supported"
            ):
                errors.append("claims:primary issue claim must be supported")
            if evidence_supported.get(topic, False) and verdict != "supported":
                errors.append(f"claims:{topic} conflicts with domain analysis")
            if (
                verdict in {"supported", "partially_supported"}
                and topic != primary_issue
                and topic not in secondary_issues
            ):
                errors.append(f"claims:{topic} must appear in secondary_issues")

    resolution = value.get("entity_resolution", {})
    if isinstance(resolution, dict):
        status = resolution.get("status")
        resolved = resolution.get("resolved_order_ids", [])
        if status == "resolved" and len(resolved) != 1:
            errors.append("entity:resolved status requires exactly one order")
        if status == "not_found" and resolved:
            errors.append("entity:not_found cannot contain resolved orders")

    financial = value.get("financial_resolution", {})
    if isinstance(financial, dict):
        total = financial.get("recommended_refund_brl")
        lines = financial.get("refund_lines", [])
        if isinstance(total, (int, float)) and isinstance(lines, list):
            line_total = sum(
                line.get("amount_brl", 0)
                for line in lines
                if isinstance(line, dict) and isinstance(line.get("amount_brl"), (int, float))
            )
            if not math.isclose(float(total), float(line_total), abs_tol=0.01):
                errors.append("financial:recommended refund must equal refund line total")
            status = value.get("assessment", {}).get("case_status")
            if status == "no_action" and float(total) > 0:
                errors.append("consistency:no_action cannot recommend a positive refund")

    payment = value.get("payment_analysis", {})
    if isinstance(payment, dict):
        captured = payment.get("captured_total_brl")
        refunded = payment.get("refunded_total_brl")
        refundable = payment.get("refundable_total_brl")
        if all(isinstance(item, (int, float)) for item in (captured, refunded, refundable)):
            expected_refundable = max(float(captured) - float(refunded), 0.0)
            if not math.isclose(float(refundable), expected_refundable, abs_tol=0.01):
                errors.append(
                    "financial:refundable total must equal captured minus refunded"
                )

    entities = value.get("affected_entities", {})
    shipment = value.get("shipment_analysis", {})
    if isinstance(entities, dict) and isinstance(shipment, dict):
        sellers = set(entities.get("seller_ids", []))
        late_sellers = set(shipment.get("late_seller_ids", []))
        if not late_sellers.issubset(sellers):
            errors.append("consistency:late seller must appear in affected seller_ids")

    assessment = value.get("assessment", {})
    root_cause = value.get("root_cause_analysis", {})
    if isinstance(assessment, dict) and isinstance(root_cause, dict):
        issue = assessment.get("primary_issue")
        expected_party = EXPECTED_RESPONSIBILITY.get(issue)
        parties = root_cause.get("responsible_parties", [])
        party_types = {
            party.get("party_type")
            for party in parties
            if isinstance(party, dict) and isinstance(party.get("party_type"), str)
        }
        if expected_party is not None and expected_party not in party_types:
            errors.append(
                f"consistency:{issue} requires responsible party {expected_party}"
            )
    return errors
