from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.reasoning import normalize_draft, verify_output
from student_agent.rule_engine import generate_rule_draft


def _evidence(tool_name: str, sequence: int, data: Any) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "arguments": {},
        "evidence_ref": f"ev_rule_reference_{sequence:04d}_abcdef",
        "result_hash": f"sha256:{sequence:064x}",
        "domain": tool_name.removeprefix("get_"),
        "data": data,
        "warnings": [],
    }


def test_rule_engine_detects_actual_issue_instead_of_claim_topic() -> None:
    order_id = "order-018"
    seller_id = "seller-018"
    case = {
        "case_id": "L3B_CASE_018",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ]
        },
        "customer_unique_id_hint": "customer-018",
    }
    items = [
        {
            "order_item_id": "item-018",
            "seller_id": seller_id,
            "price": "79.00",
            "freight_value": "18.00",
            "shipping_limit_date": "2018-05-28T09:00:00-03:00",
        },
        {
            "order_item_id": "item-018",
            "seller_id": seller_id,
            "price": "79.00",
            "freight_value": "10.00",
            "shipping_limit_date": "2018-09-09T09:00:00-03:00",
        },
    ]
    evidence = [
        _evidence("get_order", 1, {"order_id": order_id, "order_status": "delivered"}),
        _evidence("get_order_items", 2, items),
        _evidence(
            "get_shipment_summary",
            3,
            {
                "delivered_customer_at": "2018-06-08T09:00:00-03:00",
                "estimated_delivery_at": "2018-06-04T09:00:00-03:00",
                "events": [
                    {
                        "event_type": "delivered_late",
                        "actor": "seller",
                        "status": "confirmed",
                    }
                ],
            },
        ),
        _evidence(
            "get_order_payments",
            4,
            [{"payment_value": "18.00"}, {"payment_value": "79.00"}],
        ),
        _evidence(
            "get_payment_timeline",
            5,
            {
                "events": [
                    {"event_type": "captured", "amount_brl": "18.00", "status": "confirmed"},
                    {"event_type": "captured", "amount_brl": "79.00", "status": "confirmed"},
                ]
            },
        ),
        _evidence(
            "get_policy",
            6,
            {
                "rules": {
                    "late_delivery_seller": {
                        "case_status": "action_required",
                        "recommended_action": "refund_freight",
                        "refund_brl": 18.0,
                        "responsible_parties": [
                            {"party_type": "seller", "party_id": None}
                        ],
                    }
                }
            },
        ),
    ]
    state = {
        "case": case,
        "order_id": order_id,
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [order_id],
            "rejected_candidates": ["candidate-018"],
            "confidence": 0.99,
        },
        "evidence": {str(index): value for index, value in enumerate(evidence)},
    }

    draft = asyncio.run(generate_rule_draft(state))
    output = normalize_draft(draft, state)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")

    assert verify_output(output, state, contracts) == []
    assert output["assessment"] == {
        "primary_issue": "late_delivery_seller",
        "secondary_issues": ["requested_full_refund"],
        "case_status": "action_required",
        "confidence": 0.65,
    }
    assert output["shipment_analysis"]["late_seller_ids"] == [seller_id]
    assert output["payment_analysis"]["captured_total_brl"] == 97.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 18.0
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
    assert output["data_conflicts"]

    inconsistent = copy.deepcopy(output)
    inconsistent["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "logistics_provider", "party_id": None}
    ]
    assert any(
        "requires responsible party seller" in error
        for error in verify_output(inconsistent, state, contracts)
    )


def test_rule_engine_preserves_evidence_supported_secondary_claim() -> None:
    order_id = "order-secondary"
    case = {
        "case_id": "L3B_CASE_SECONDARY",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-delay", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-refund", "topic": "requested_full_refund"},
            ]
        },
    }
    evidence = [
        _evidence("get_order", 11, {"order_id": order_id, "order_status": "delivered"}),
        _evidence(
            "get_order_items",
            12,
            [{"order_item_id": "item-1", "price": "70.00", "freight_value": "0.00"}],
        ),
        _evidence(
            "get_shipment_summary",
            13,
            {
                "events": [
                    {
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                        "status": "confirmed",
                    }
                ]
            },
        ),
        _evidence("get_order_payments", 14, [{"payment_value": "105.00"}]),
        _evidence(
            "get_payment_timeline",
            15,
            {
                "events": [
                    {"event_type": "captured", "amount_brl": "105.00", "status": "confirmed"}
                ]
            },
        ),
        _evidence(
            "get_policy",
            16,
            {
                "rules": {
                    "payment_mismatch": {
                        "case_status": "action_required",
                        "recommended_action": "reconcile_payment",
                        "refund_brl": 35.0,
                    }
                }
            },
        ),
    ]
    state = {
        "case": case,
        "order_id": order_id,
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [order_id],
            "rejected_candidates": [],
            "confidence": 0.99,
        },
        "evidence": {str(index): value for index, value in enumerate(evidence)},
    }

    output = normalize_draft(asyncio.run(generate_rule_draft(state)), state)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")

    assert verify_output(output, state, contracts) == []
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] == 0.7
    assert output["assessment"]["secondary_issues"] == []
    assert output["claim_assessments"][0]["verdict"] == "supported"
