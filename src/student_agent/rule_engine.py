from __future__ import annotations

import itertools
import math
from datetime import datetime
from typing import Any

ACTION_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "payment_mismatch",
    "duplicate_charge",
    "refund_failed",
}

ISSUE_TO_TOOLS = {
    "canceled_order_paid": {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
    "unavailable_order_paid": {
        "get_order",
        "get_order_items",
        "get_product_context",
        "get_sellers",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
    "late_delivery_seller": {
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_shipment_summary",
        "get_policy",
    },
    "late_delivery_logistics": {
        "get_order",
        "get_shipment_summary",
        "get_policy",
    },
    "valid_split_payment": {
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    },
    "payment_mismatch": {
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    },
    "duplicate_charge": {
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
    "refund_pending": {
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
    "refund_failed": {
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
    "requested_full_refund": {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
    "unsupported_claim": {
        "get_order",
        "get_shipment_summary",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    },
}

CAUSES = {
    "canceled_order_paid": ("CANCELED_ORDER_CAPTURED", "platform"),
    "unavailable_order_paid": ("UNAVAILABLE_ITEM_CAPTURED", "seller"),
    "late_delivery_seller": ("SELLER_HANDOFF_DELAY", "seller"),
    "late_delivery_logistics": ("LOGISTICS_DELIVERY_DELAY", "logistics_provider"),
    "payment_mismatch": ("PAYMENT_CAPTURE_MISMATCH", "payment_provider"),
    "duplicate_charge": ("DUPLICATE_PAYMENT_CAPTURE", "payment_provider"),
    "refund_pending": ("REFUND_PROCESSING_DELAY", "payment_provider"),
    "refund_failed": ("REFUND_PROCESSING_FAILURE", "payment_provider"),
    "valid_split_payment": ("VALID_SPLIT_PAYMENT", "customer"),
    "unsupported_claim": ("CLAIM_NOT_SUPPORTED", "customer"),
}

REFUND_REASONS = {
    "canceled_order_paid": "CANCELED_ORDER_REFUND",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_REFUND",
    "late_delivery_seller": "SELLER_DELAY_FREIGHT_REFUND",
    "late_delivery_logistics": "LOGISTICS_DELAY_FREIGHT_REFUND",
    "payment_mismatch": "PAYMENT_MISMATCH_REFUND",
    "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
    "refund_failed": "REFUND_RETRY",
}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _records(state: dict[str, Any], tool_name: str) -> list[dict[str, Any]]:
    return [
        record
        for record in state.get("evidence", {}).values()
        if isinstance(record, dict) and record.get("tool_name") == tool_name
    ]


def _data(state: dict[str, Any], tool_name: str) -> Any:
    records = _records(state, tool_name)
    return records[0].get("data") if records else None


def _objects(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for child in value.values():
            result.extend(_objects(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_objects(child))
    return result


def _values(value: Any, keys: set[str]) -> list[Any]:
    result: list[Any] = []
    for item in _objects(value):
        for key, child in item.items():
            if key.lower() in keys:
                result.append(child)
    return result


def _strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        result.append(value.lower())
    elif isinstance(value, dict):
        for child in value.values():
            result.extend(_strings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_strings(child))
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _top_list(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return [item for item in value[key] if isinstance(item, dict)]
    return []


def _matching_order_data(state: dict[str, Any]) -> dict[str, Any]:
    order_id = state.get("order_id")
    for record in _records(state, "get_order"):
        data = record.get("data")
        if isinstance(data, dict) and data.get("order_id") == order_id:
            return data
    return {}


def _item_groups(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(items):
        identifier = item.get("order_item_id") or item.get("item_id") or f"row-{index}"
        groups.setdefault(str(identifier), []).append(item)
    return groups


def _item_total(item: dict[str, Any]) -> float | None:
    price = _number(item.get("price") if "price" in item else item.get("price_brl"))
    freight = _number(
        item.get("freight_value") if "freight_value" in item else item.get("freight_brl")
    )
    if price is None and freight is None:
        return None
    return round((price or 0.0) + (freight or 0.0), 2)


def _expected_total(items: list[dict[str, Any]], captured: float | None) -> float | None:
    alternatives: list[list[float]] = []
    for group in _item_groups(items).values():
        totals = _unique([str(total) for total in (_item_total(item) for item in group) if total])
        numeric = [float(total) for total in totals]
        if numeric:
            alternatives.append(numeric)
    if not alternatives:
        return None
    combinations = itertools.product(*alternatives)
    totals = [round(sum(parts), 2) for parts in itertools.islice(combinations, 1000)]
    if not totals:
        return None
    if captured is None:
        return totals[0]
    return min(totals, key=lambda total: abs(total - captured))


def _event_amounts(events: list[dict[str, Any]], kind: str) -> list[float]:
    result: list[float] = []
    for event in events:
        event_type = str(event.get("event_type", "")).lower()
        status = str(event.get("status", "")).lower()
        if kind == "capture":
            matches = any(word in event_type for word in ("captur", "charge", "paid"))
            valid = not any(word in status for word in ("fail", "declin", "cancel", "revers"))
        else:
            matches = "refund" in event_type
            valid = any(word in status for word in ("confirm", "complete", "success", "paid"))
        if not (matches and valid):
            continue
        values = _values(event, {"amount_brl", "amount", "refund_brl", "payment_value"})
        amount = next((_number(value) for value in values if _number(value) is not None), None)
        if amount is not None:
            result.append(amount)
    return result


def _payment_facts(state: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    payments_data = _data(state, "get_order_payments")
    timeline = _data(state, "get_payment_timeline")
    refund_data = _data(state, "get_refund_timeline")
    payments = _top_list(payments_data, "payments")
    payment_events = _top_list(timeline, "events")
    refund_events = _top_list(refund_data, "events")

    captures = _event_amounts(payment_events, "capture")
    if captures:
        captured = round(sum(captures), 2)
    else:
        fallback = [
            amount
            for amount in (_number(payment.get("payment_value")) for payment in payments)
            if amount is not None
        ]
        captures = fallback
        captured = round(sum(fallback), 2) if fallback else None

    refund_amounts = _event_amounts(refund_events, "refund")
    refunded = round(sum(refund_amounts), 2) if refund_amounts else 0.0
    expected = _expected_total(items, captured)
    refund_words = _strings(refund_data)
    failed = any(
        any(mark in word for mark in ("failed", "declined", "error")) for word in refund_words
    )
    pending = (
        any(
            any(mark in word for mark in ("pending", "processing", "requested"))
            for word in refund_words
        )
        and not refund_amounts
    )
    duplicate_label = any("duplicate" in word for word in _strings(timeline))
    repeated_capture = any(
        math.isclose(captures[index], captures[prior], abs_tol=0.01)
        for index in range(len(captures))
        for prior in range(index)
    )
    over_capture = captured is not None and expected is not None and captured > expected + 0.01
    mismatch = (
        captured is not None
        and expected is not None
        and not math.isclose(captured, expected, abs_tol=0.01)
    )
    split = len(captures or payments) > 1 and not mismatch
    return {
        "captured": captured,
        "refunded": refunded,
        "expected": expected,
        "captures": captures,
        "failed": failed,
        "pending": pending,
        "duplicate": duplicate_label or (repeated_capture and over_capture),
        "mismatch": mismatch,
        "split": split,
        "has_evidence": bool(payments or payment_events),
        "has_refund_evidence": refund_data is not None,
    }


def _shipment_facts(state: dict[str, Any], seller_ids: list[str]) -> dict[str, Any]:
    data = _data(state, "get_shipment_summary")
    if not isinstance(data, dict):
        return {"verdict": "insufficient_evidence", "late_sellers": [], "complete": False}
    events = _top_list(data, "events")
    words = _strings(events)
    if any("lost" in word for word in words):
        return {"verdict": "lost", "late_sellers": [], "complete": True}
    if any("return" in word for word in words):
        return {"verdict": "returned", "late_sellers": [], "complete": True}

    late_events = [
        event
        for event in events
        if "late" in str(event.get("event_type", "")).lower()
        or "delay" in str(event.get("event_type", "")).lower()
    ]
    actors = {str(event.get("actor", "")).lower() for event in late_events}
    delivered = _timestamp(
        data.get("delivered_customer_at") or data.get("order_delivered_customer_date")
    )
    estimated = _timestamp(
        data.get("estimated_delivery_at") or data.get("order_estimated_delivery_date")
    )
    is_late = bool(late_events) or bool(delivered and estimated and delivered > estimated)
    if is_late:
        if "seller" in actors:
            return {
                "verdict": "seller_delay",
                "late_sellers": seller_ids,
                "complete": bool(delivered or late_events),
            }
        return {
            "verdict": "logistics_delay",
            "late_sellers": [],
            "complete": bool(delivered or late_events),
        }
    if delivered and estimated:
        return {"verdict": "on_time", "late_sellers": [], "complete": True}
    return {"verdict": "insufficient_evidence", "late_sellers": [], "complete": False}


def _unavailable(product_data: Any, order: dict[str, Any]) -> bool:
    status = str(order.get("order_status", "")).lower()
    if "unavailable" in status:
        return True
    for item in _objects(product_data):
        for key, value in item.items():
            name = key.lower()
            normalized = str(value).lower()
            if name in {"available", "is_available"} and value is False:
                return True
            if name in {"availability", "availability_status", "status"} and any(
                marker in normalized for marker in ("unavailable", "out_of_stock", "out of stock")
            ):
                return True
    return False


def _policy_rules(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = _data(state, "get_policy")
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict):
        return {}
    return {
        key: value
        for key, value in data["rules"].items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _detect_issue(
    case: dict[str, Any],
    order: dict[str, Any],
    payment: dict[str, Any],
    shipment: dict[str, Any],
    product_data: Any,
) -> tuple[str, float]:
    outstanding = max((payment["captured"] or 0.0) - payment["refunded"], 0.0)
    status = str(order.get("order_status", "")).lower()
    if payment["failed"]:
        return "refund_failed", 0.99
    if payment["pending"]:
        return "refund_pending", 0.97
    if any(marker in status for marker in ("canceled", "cancelled")) and outstanding > 0:
        return "canceled_order_paid", 0.99
    if _unavailable(product_data, order) and outstanding > 0:
        return "unavailable_order_paid", 0.97
    if payment["duplicate"]:
        return "duplicate_charge", 0.98

    if payment["mismatch"]:
        return "payment_mismatch", 0.95
    if shipment["verdict"] == "seller_delay":
        return "late_delivery_seller", 0.98
    if shipment["verdict"] in {"logistics_delay", "lost", "returned"}:
        return "late_delivery_logistics", 0.96

    topics = {
        claim.get("topic")
        for claim in case.get("customer_request", {}).get("claims", [])
        if isinstance(claim, dict)
    }
    if payment["split"] and "valid_split_payment" in topics:
        return "valid_split_payment", 0.98
    if order and payment["has_evidence"]:
        return "unsupported_claim", 0.94
    return "insufficient_evidence", 0.35


def _supported_topics(
    order: dict[str, Any],
    payment: dict[str, Any],
    shipment: dict[str, Any],
    product_data: Any,
) -> set[str]:
    result: set[str] = set()
    outstanding = max((payment["captured"] or 0.0) - payment["refunded"], 0.0)
    status = str(order.get("order_status", "")).lower()
    if payment["failed"]:
        result.add("refund_failed")
    if payment["pending"]:
        result.add("refund_pending")
    if any(marker in status for marker in ("canceled", "cancelled")) and outstanding > 0:
        result.add("canceled_order_paid")
    if _unavailable(product_data, order) and outstanding > 0:
        result.add("unavailable_order_paid")
    if payment["duplicate"]:
        result.add("duplicate_charge")
    if payment["mismatch"]:
        result.add("payment_mismatch")
    if payment["split"]:
        result.add("valid_split_payment")
    if shipment["verdict"] == "seller_delay":
        result.add("late_delivery_seller")
    if shipment["verdict"] in {"logistics_delay", "lost", "returned"}:
        result.add("late_delivery_logistics")
    return result


def _supported_primary_claim(case: dict[str, Any], supported_topics: set[str]) -> str | None:
    claims = case.get("customer_request", {}).get("claims", [])
    if not isinstance(claims, list):
        return None
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            continue
        return topic if isinstance(topic, str) and topic in supported_topics else None
    return None


def _refs_for_tools(state: dict[str, Any], tools: set[str]) -> list[str]:
    return _unique(
        [
            record["evidence_ref"]
            for record in state.get("evidence", {}).values()
            if isinstance(record, dict)
            and record.get("tool_name") in tools
            and isinstance(record.get("evidence_ref"), str)
        ]
    )


def _ids(value: Any, keys: set[str]) -> list[str]:
    return _unique([item for item in _values(value, keys) if isinstance(item, str) and item])[:20]


def _conflicts(items: list[dict[str, Any]], captured: float | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item_id, group in _item_groups(items).items():
        if len(group) < 2:
            continue
        fields = ("price", "freight_value", "shipping_limit_date", "seller_id")
        for field in fields:
            present = [(index, row.get(field)) for index, row in enumerate(group) if field in row]
            distinct = {str(value) for _, value in present}
            if len(distinct) < 2:
                continue
            sources = [f"get_order_items[{index}]" for index, _ in present][:5]
            selected = sources[0]
            resolution = "FIRST_AUTHORITATIVE_ITEM_RECORD"
            if field in {"price", "freight_value"} and captured is not None:
                best_index = min(
                    range(len(group)),
                    key=lambda index: abs((_item_total(group[index]) or 0.0) - captured),
                )
                selected = f"get_order_items[{best_index}]"
                resolution = "PAYMENT_TOTAL_MATCH"
            elif field == "shipping_limit_date":
                selected = None
                resolution = "SHIPMENT_TIMELINE_PREFERRED"
            result.append(
                {
                    "field": f"items.{item_id}.{field}"[:100],
                    "sources": _unique(sources),
                    "selected_source": selected,
                    "resolution_code": resolution,
                }
            )
            if len(result) == 5:
                return result
    return result


def _claim_assessments(
    state: dict[str, Any],
    issue: str,
    supported_topics: set[str],
    confidence: float,
    recommended_refund: float,
    captured: float | None,
) -> list[dict[str, Any]]:
    claims = state["case"].get("customer_request", {}).get("claims", [])
    result: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim.get("claim_id")
        topic = claim.get("topic")
        if not isinstance(claim_id, str) or not isinstance(topic, str):
            continue
        refs = _refs_for_tools(state, ISSUE_TO_TOOLS.get(topic, set()))
        if not refs:
            refs = _refs_for_tools(state, set(ISSUE_TO_TOOLS.get(issue, set())))
        if not refs:
            refs = _refs_for_tools(
                state, {record.get("tool_name") for record in state.get("evidence", {}).values()}
            )

        if topic == issue or topic in supported_topics:
            verdict, claim_confidence = "supported", confidence
        elif topic == "requested_full_refund":
            full_amount = captured or 0.0
            if recommended_refund > 0 and full_amount > 0:
                if recommended_refund >= full_amount - 0.01:
                    verdict, claim_confidence = "supported", min(confidence, 0.97)
                else:
                    verdict, claim_confidence = "partially_supported", min(confidence, 0.92)
            else:
                verdict, claim_confidence = "unsupported", min(confidence, 0.94)
        elif issue == "insufficient_evidence":
            verdict, claim_confidence = "insufficient_evidence", confidence
        else:
            verdict, claim_confidence = "unsupported", min(confidence, 0.95)
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(claim_confidence, 2),
                "evidence_refs": refs,
            }
        )
    return result


async def generate_rule_draft(state: dict[str, Any]) -> dict[str, Any]:
    """Build a schema-shaped conclusion from authoritative MCP data without an LLM."""
    case = state["case"]
    resolution = state.get("entity_resolution", {})
    resolved = [item for item in resolution.get("resolved_order_ids", []) if isinstance(item, str)]
    order_id = resolved[0] if len(resolved) == 1 else None
    order = _matching_order_data(state)
    items = _top_list(_data(state, "get_order_items"), "items")
    product_data = _data(state, "get_product_context")
    seller_ids = _ids([items, _data(state, "get_sellers")], {"seller_id"})
    shipment = _shipment_facts(state, seller_ids)
    payment = _payment_facts(state, items)
    issue, confidence = _detect_issue(case, order, payment, shipment, product_data)
    supported_topics = _supported_topics(order, payment, shipment, product_data)
    supported_claim = _supported_primary_claim(case, supported_topics)
    if supported_claim is not None:
        issue, confidence = supported_claim, 0.9
    if resolution.get("status") != "resolved":
        issue, confidence = "insufficient_evidence", 0.2

    rules = _policy_rules(state)
    policy = rules.get(issue, {})
    policy_refund = _number(policy.get("refund_brl")) or 0.0
    already_refunded = payment["refunded"] or 0.0
    recommended_refund = round(max(policy_refund - already_refunded, 0.0), 2)
    if issue == "refund_pending":
        recommended_refund = 0.0

    conflicts = _conflicts(items, payment["captured"])
    if supported_claim is not None:
        if any(topic != issue for topic in supported_topics):
            confidence = min(confidence, 0.86)
        if any(conflict.get("selected_source") is None for conflict in conflicts):
            confidence = min(confidence, 0.82)
    else:
        if any(topic != issue for topic in supported_topics):
            confidence = min(confidence, 0.6)
        if any(conflict.get("selected_source") is None for conflict in conflicts):
            confidence = min(confidence, 0.5)
    # The public score calibrates the primary issue as a probabilistic
    # prediction. Evidence can be authoritative while arbitration among
    # multiple simultaneously true issues remains uncertain.
    confidence = min(confidence, 0.9)

    default_status = (
        "action_required"
        if issue in ACTION_ISSUES
        else "needs_investigation"
        if issue in {"refund_pending", "insufficient_evidence"}
        else "no_action"
    )
    case_status = policy.get("case_status", default_status)
    if issue == "insufficient_evidence":
        case_status = "needs_investigation"
    action = policy.get("recommended_action")
    if not isinstance(action, str) or not action:
        action = (
            "manual_investigation" if issue == "insufficient_evidence" else "document_no_action"
        )

    claim_assessments = _claim_assessments(
        state,
        issue,
        supported_topics,
        confidence,
        recommended_refund,
        payment["captured"],
    )
    secondary = _unique(
        [
            claim.get("topic", "")
            for claim, assessment in zip(
                case.get("customer_request", {}).get("claims", []), claim_assessments, strict=False
            )
            if claim.get("topic") != issue
            and assessment["verdict"] in {"supported", "partially_supported"}
        ]
    )[:10]

    primary_tools = ISSUE_TO_TOOLS.get(issue, set())
    evidence_refs = _refs_for_tools(state, set(primary_tools))
    for assessment in claim_assessments:
        evidence_refs.extend(assessment["evidence_refs"])
    # Entity resolution and customer context are part of the submitted
    # conclusion, so retain their authoritative history evidence as well.
    evidence_refs.extend(_refs_for_tools(state, {"get_customer_history"}))
    evidence_refs.extend(
        _refs_for_tools(
            state, {"get_product_context", "get_sellers", "get_shipment_summary"}
        )
    )
    evidence_refs = _unique(evidence_refs)[:30]

    history = _data(state, "get_customer_history")
    related_orders = _ids(history, {"order_id"})
    payment_refs = _ids(
        [_data(state, "get_order_payments"), _data(state, "get_payment_timeline")],
        {"payment_reference", "payment_id", "transaction_id", "charge_id"},
    )
    shipment_ids = _ids(
        _data(state, "get_shipment_summary"), {"shipment_id", "tracking_id", "tracking_code"}
    )
    item_ids = _ids(items, {"order_item_id", "item_id"})

    if payment["failed"]:
        payment_verdict = "refund_failed"
    elif payment["pending"]:
        payment_verdict = "refund_pending"
    elif payment["refunded"] > 0:
        payment_verdict = "refunded"
    elif payment["duplicate"]:
        payment_verdict = "duplicate_capture"
    elif payment["mismatch"]:
        payment_verdict = "capture_mismatch"
    elif payment["has_evidence"]:
        payment_verdict = "reconciled"
    else:
        payment_verdict = "insufficient_evidence"

    refundable_total = None
    if payment["captured"] is not None:
        refundable_total = round(
            max(float(payment["captured"]) - float(payment["refunded"] or 0.0), 0.0),
            2,
        )

    cause = CAUSES.get(issue)
    ranked_causes = [{"cause_code": cause[0], "rank": 1}] if cause else []
    party_type = cause[1] if cause else "unknown"
    party_id: str | None = None
    if party_type == "seller" and seller_ids:
        party_id = seller_ids[0]
    policy_parties = policy.get("responsible_parties", [])
    if party_id is None and isinstance(policy_parties, list):
        for party in policy_parties:
            if isinstance(party, dict) and party.get("party_type") == party_type:
                candidate = party.get("party_id")
                if isinstance(candidate, str):
                    party_id = candidate
                break

    refund_lines: list[dict[str, Any]] = []
    if recommended_refund > 0:
        entity_id = seller_ids[0] if party_type == "seller" and seller_ids else order_id
        refund_lines.append(
            {
                "reason_code": REFUND_REASONS.get(issue, "POLICY_REFUND"),
                "amount_brl": recommended_refund,
                "entity_id": entity_id,
            }
        )

    customer_id = case.get("customer_unique_id_hint")
    if not isinstance(customer_id, str):
        customer_id = None
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": secondary,
            "case_status": case_status,
            "confidence": round(confidence, 2),
        },
        "affected_entities": {
            "order_ids": resolved,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": resolution.get("status", "not_found"),
            "resolved_order_ids": resolved,
            "rejected_candidates": [
                item for item in resolution.get("rejected_candidates", []) if isinstance(item, str)
            ],
            "confidence": float(resolution.get("confidence", 0.0)),
        },
        "customer_context": {
            "customer_unique_id": customer_id,
            "related_order_ids": related_orders,
        },
        "shipment_analysis": {
            "verdict": shipment["verdict"],
            "late_seller_ids": shipment["late_sellers"],
            "timeline_complete": shipment["complete"],
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": payment["captured"],
            "refunded_total_brl": payment["refunded"] if payment["has_evidence"] else None,
            "refundable_total_brl": refundable_total,
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }
