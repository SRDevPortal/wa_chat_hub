from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, flt, now_datetime

from wa_chat_hub.services import normalize_phone


DEFAULT_QA_WINDOW_HOURS = 3
MAX_MESSAGES_PER_RUN = 5000


@dataclass
class Finding:
    issue_type: str
    severity: str
    impact: float
    conversation: str
    lead: str = ""
    phone: str = ""
    user_message: str = ""
    bot_reply: str = ""
    expected_behavior: str = ""
    suggested_fix: str = ""
    needs_human_review: bool = False


@frappe.whitelist()
def run_shipkia_qa_now(hours: int | str = DEFAULT_QA_WINDOW_HOURS) -> dict[str, Any]:
    """Manual QA trigger for Desk/API use."""
    return run_scheduled_qa(hours=hours)


def run_scheduled_qa(hours: int | str = DEFAULT_QA_WINDOW_HOURS) -> dict[str, Any]:
    """Audit recent ShipKia WhatsApp bot conversations and store a QA report."""
    hours = cint(hours) or DEFAULT_QA_WINDOW_HOURS
    period_end = now_datetime()
    period_start = add_to_date(period_end, hours=-hours)

    qa_run = frappe.get_doc(
        {
            "doctype": "ShipKia Bot QA Run",
            "status": "Running",
            "period_start": period_start,
            "period_end": period_end,
            "run_started_at": period_end,
        }
    )
    qa_run.insert(ignore_permissions=True)
    frappe.db.commit()

    try:
        rows = _recent_shipkia_messages(period_start, period_end)
        grouped = _group_by_conversation(rows)
        summary = _evaluate_conversations(grouped, qa_run.name)
        regression = _run_regression_tests()
        previous = _previous_completed_run(qa_run.name)
        comparison = _comparison_summary(summary["average_quality_score"], previous)

        qa_run.reload()
        qa_run.update(
            {
                "status": "Completed",
                "run_finished_at": now_datetime(),
                "conversations_checked": summary["conversations_checked"],
                "messages_checked": len(rows),
                "average_quality_score": summary["average_quality_score"],
                "previous_average_quality_score": comparison["previous_score"],
                "score_delta": comparison["score_delta"],
                "high_risk_findings": summary["severity_counts"].get("High", 0),
                "medium_risk_findings": summary["severity_counts"].get("Medium", 0),
                "low_risk_findings": summary["severity_counts"].get("Low", 0),
                "human_review_needed": summary["human_review_needed"],
                "repeated_question_incidents": summary["issue_counts"].get("repeated_question", 0),
                "stale_context_incidents": summary["issue_counts"].get("stale_context_reply", 0),
                "wrong_rate_risk_incidents": summary["issue_counts"].get("wrong_rate_risk", 0),
                "mapping_issue_incidents": summary["issue_counts"].get("lead_mapping_mismatch", 0),
                "hot_leads": summary["temperature_counts"].get("Hot", 0),
                "warm_leads": summary["temperature_counts"].get("Warm", 0),
                "cold_leads": summary["temperature_counts"].get("Cold", 0),
                "callbacks_requested": summary["callbacks_requested"],
                "regression_total": regression["total"],
                "regression_passed": regression["passed"],
                "regression_failed": regression["failed"],
                "summary": summary["summary_text"],
                "comparison_summary": comparison["text"],
            }
        )
        qa_run.save(ignore_permissions=True)
        frappe.db.commit()
        return {
            "qa_run": qa_run.name,
            "status": qa_run.status,
            "conversations_checked": qa_run.conversations_checked,
            "average_quality_score": qa_run.average_quality_score,
            "findings": summary["finding_count"],
            "regression_failed": qa_run.regression_failed,
        }
    except Exception:
        qa_run.reload()
        qa_run.status = "Failed"
        qa_run.run_finished_at = now_datetime()
        qa_run.error = frappe.get_traceback()
        qa_run.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.log_error(frappe.get_traceback(), "ShipKia Bot QA Failed")
        raise


def _recent_shipkia_messages(period_start: Any, period_end: Any) -> list[dict[str, Any]]:
    return frappe.db.sql(
        """
        select
            m.name,
            m.conversation,
            m.direction,
            m.sender_type,
            m.body,
            m.creation,
            c.contact,
            c.channel_account,
            c.linked_reference_doctype,
            c.linked_reference_name,
            cc.phone_number,
            cc.linked_lead,
            cc.source_doctype,
            cc.source_name
        from `tabChat Message` m
        left join `tabChat Conversation` c on c.name = m.conversation
        left join `tabChat Contact` cc on cc.name = c.contact
        where m.creation between %(period_start)s and %(period_end)s
          and m.direction in ('Inbound', 'Outbound')
          and (
            lower(coalesce(c.channel_account, '')) like '%%shipkia%%'
            or c.linked_reference_doctype = 'Lead'
            or cc.source_doctype = 'Lead'
            or coalesce(cc.linked_lead, '') != ''
          )
        order by m.conversation asc, m.creation asc
        limit %(limit)s
        """,
        {"period_start": period_start, "period_end": period_end, "limit": MAX_MESSAGES_PER_RUN},
        as_dict=True,
    )


def _group_by_conversation(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("conversation"):
            grouped[str(row.conversation)].append(row)
    return grouped


def _evaluate_conversations(grouped: dict[str, list[dict[str, Any]]], qa_run_name: str) -> dict[str, Any]:
    findings: list[Finding] = []
    quality_scores: list[float] = []
    temperature_counts: Counter[str] = Counter()
    callbacks_requested = 0

    for conversation, messages in grouped.items():
        if not messages:
            continue
        context = _conversation_context(messages[0])
        conv_findings = _conversation_findings(conversation, messages, context)
        findings.extend(conv_findings)
        quality_scores.append(max(0, 100 - sum(item.impact for item in conv_findings)))
        temperature_counts[_lead_temperature(messages)] += 1
        if any(_has_callback_intent(str(msg.get("body") or "")) for msg in messages if msg.get("direction") == "Inbound"):
            callbacks_requested += 1

    finding_docs = [_insert_finding(qa_run_name, finding) for finding in findings]
    for doc, finding in zip(finding_docs, findings, strict=False):
        if finding.severity in {"High", "Medium"}:
            _insert_training_example(qa_run_name, doc.name, finding)

    severity_counts = Counter(item.severity for item in findings)
    issue_counts = Counter(item.issue_type for item in findings)
    average_quality_score = round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else 100.0
    summary_text = _summary_text(grouped, findings, average_quality_score)

    return {
        "conversations_checked": len(grouped),
        "finding_count": len(findings),
        "severity_counts": severity_counts,
        "issue_counts": issue_counts,
        "temperature_counts": temperature_counts,
        "callbacks_requested": callbacks_requested,
        "human_review_needed": sum(1 for item in findings if item.needs_human_review),
        "average_quality_score": average_quality_score,
        "summary_text": summary_text,
    }


def _conversation_context(first_message: dict[str, Any]) -> dict[str, str]:
    lead = _resolve_lead(first_message)
    phone = normalize_phone(first_message.get("phone_number") or "")
    return {
        "lead": lead,
        "phone": phone,
        "linked_reference_doctype": str(first_message.get("linked_reference_doctype") or ""),
        "linked_reference_name": str(first_message.get("linked_reference_name") or ""),
        "linked_lead": str(first_message.get("linked_lead") or ""),
        "source_doctype": str(first_message.get("source_doctype") or ""),
        "source_name": str(first_message.get("source_name") or ""),
    }


def _resolve_lead(row: dict[str, Any]) -> str:
    if row.get("linked_reference_doctype") == "Lead" and row.get("linked_reference_name"):
        linked_reference_name = str(row.linked_reference_name)
        if frappe.db.exists("Lead", linked_reference_name):
            return linked_reference_name
    for candidate in (row.get("linked_lead"), row.get("source_name")):
        if candidate and frappe.db.exists("Lead", candidate):
            return str(candidate)
    phone = normalize_phone(row.get("phone_number") or "")
    if not phone:
        return ""
    last10 = phone[-10:] if len(phone) >= 10 else phone
    for fieldname in ("mobile_no", "phone", "whatsapp_no", "custom_whatsapp_number"):
        if frappe.get_meta("Lead").has_field(fieldname):
            lead = frappe.db.get_value("Lead", {fieldname: ["like", f"%{last10}%"]}, "name")
            if lead:
                return str(lead)
    return ""


def _conversation_findings(conversation: str, messages: list[dict[str, Any]], context: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_mapping_findings(conversation, messages, context))
    findings.extend(_repetition_findings(conversation, messages, context))
    findings.extend(_reply_pair_findings(conversation, messages, context))
    findings.extend(_lead_data_findings(conversation, context))
    return findings


def _mapping_findings(conversation: str, messages: list[dict[str, Any]], context: dict[str, str]) -> list[Finding]:
    lead = context["lead"]
    mismatched = False
    if context["linked_reference_doctype"] and context["linked_reference_doctype"] != "Lead":
        mismatched = True
    if (
        context["linked_reference_doctype"] == "Lead"
        and context["linked_reference_name"]
        and not frappe.db.exists("Lead", context["linked_reference_name"])
    ):
        mismatched = True
    if context["linked_reference_name"] and lead and context["linked_reference_name"] != lead:
        mismatched = True
    if context["linked_lead"] and lead and context["linked_lead"] != lead:
        mismatched = True
    if context["source_doctype"] and context["source_doctype"] != "Lead":
        mismatched = True
    if not mismatched:
        return []

    return [
        Finding(
            issue_type="lead_mapping_mismatch",
            severity="High",
            impact=40,
            conversation=conversation,
            lead=lead,
            phone=context["phone"],
            user_message=_last_inbound(messages),
            expected_behavior="One WhatsApp conversation/contact should point to the same Lead record in Lead List.",
            suggested_fix="Repair Chat Conversation and Chat Contact mappings before qualification data is trusted.",
            needs_human_review=True,
        )
    ]


def _repetition_findings(conversation: str, messages: list[dict[str, Any]], context: dict[str, str]) -> list[Finding]:
    outbound = [_normalize_text(msg.get("body")) for msg in messages if msg.get("direction") == "Outbound"]
    repeated = [body for body, count in Counter(outbound).items() if body and count > 1 and len(body) > 25]
    if not repeated:
        return []
    return [
        Finding(
            issue_type="repeated_question",
            severity="Medium",
            impact=15,
            conversation=conversation,
            lead=context["lead"],
            phone=context["phone"],
            bot_reply=repeated[0][:500],
            expected_behavior="The bot should remember recently collected details and avoid asking the same question again.",
            suggested_fix="Review pending-slot state and delayed reply buffering for this conversation.",
            needs_human_review=True,
        )
    ]


def _reply_pair_findings(conversation: str, messages: list[dict[str, Any]], context: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    pending_user_texts: list[str] = []
    for msg in messages:
        body = str(msg.get("body") or "").strip()
        if not body:
            continue
        if msg.get("direction") == "Inbound":
            pending_user_texts.append(body)
            continue
        if msg.get("direction") != "Outbound":
            continue
        user_text = " ".join(pending_user_texts).strip()
        if not user_text:
            continue
        findings.extend(_inspect_pair(conversation, context, user_text, body))
        pending_user_texts = []
    return findings


def _inspect_pair(conversation: str, context: dict[str, str], user_text: str, bot_reply: str) -> list[Finding]:
    findings: list[Finding] = []
    user_norm = _normalize_text(user_text)
    bot_norm = _normalize_text(bot_reply)

    if _is_greeting(user_norm) and not _has_greeting(bot_norm):
        findings.append(
            _finding(
                "missing_greeting",
                "Low",
                5,
                conversation,
                context,
                user_text,
                bot_reply,
                "A first hello should sound warm and human, with the lead name if known.",
                "Let the LLM produce a natural greeting from the system prompt instead of using robotic fixed wording.",
            )
        )

    if _asks_rate(user_norm) and _contains_rate(bot_norm) and "exact" in user_norm and "callback" not in bot_norm:
        findings.append(
            _finding(
                "exact_rate_without_callback",
                "High",
                30,
                conversation,
                context,
                user_text,
                bot_reply,
                "Exact-rate requests should offer a ShipKia team callback and save callback timing.",
                "Route exact/final rate intent to callback scheduling instead of quoting a fixed exact amount.",
                True,
            )
        )

    if _contains_rate(bot_norm) and not _asks_rate(user_norm) and not _has_recent_route_details(user_norm):
        findings.append(
            _finding(
                "wrong_rate_risk",
                "High",
                30,
                conversation,
                context,
                user_text,
                bot_reply,
                "Rates should be shared only when the user asks for them or gives route/weight context.",
                "Keep rate output behind rate intent detection and starting-rate logic.",
                True,
            )
        )

    if _asks_rate(user_norm) and _has_recent_route_details(user_norm) and "start" not in bot_norm and "vary" not in bot_norm:
        findings.append(
            _finding(
                "starting_rate_disclaimer_missing",
                "Medium",
                15,
                conversation,
                context,
                user_text,
                bot_reply,
                "City-level rates should be phrased as starting rates and say exact rate varies by pincode/serviceability.",
                "Ensure every city-based rate reply uses the starting-rate disclaimer.",
            )
        )

    if _price_objection(user_norm) and not _handles_price_objection(bot_norm):
        findings.append(
            _finding(
                "price_objection_missed",
                "Medium",
                20,
                conversation,
                context,
                user_text,
                bot_reply,
                "When the lead says rates are high, acknowledge it and offer a callback/custom review.",
                "Add price objection as a high-priority sales intent before normal qualification questions.",
                True,
            )
        )

    if _asks_features(user_norm):
        feature_issue = _feature_issue(user_norm, bot_norm)
        if feature_issue:
            findings.append(
                _finding(
                    "feature_answer_incomplete",
                    "Medium",
                    18,
                    conversation,
                    context,
                    user_text,
                    bot_reply,
                    feature_issue,
                    "Strengthen ShipKia feature FAQ context in the prompt and deterministic guardrails.",
                )
            )

    if _frustrated(user_norm) and not _acknowledges_frustration(bot_norm):
        findings.append(
            _finding(
                "frustration_not_acknowledged",
                "Medium",
                15,
                conversation,
                context,
                user_text,
                bot_reply,
                "Frustrated leads should get a short apology/acknowledgement before the next question.",
                "Make acknowledgement mandatory before continuing workflow after frustration language.",
                True,
            )
        )

    if _stale_context(user_norm, bot_norm):
        findings.append(
            _finding(
                "stale_context_reply",
                "High",
                30,
                conversation,
                context,
                user_text,
                bot_reply,
                "The reply should answer the latest user message, not an earlier pending question.",
                "Check message debounce/buffer timing and pending slot confirmation state.",
                True,
            )
        )

    return findings


def _lead_data_findings(conversation: str, context: dict[str, str]) -> list[Finding]:
    lead = context["lead"]
    if not lead or not frappe.db.exists("Lead", lead):
        return []

    bad_values = {"h", "hi", "hello", "hlo", "jldi bolo", "jaldi bolo", "ke h", "kya h"}
    meta = frappe.get_meta("Lead")
    fields = [
        field
        for field in ("shipkia_business_name", "shipkia_current_aggregator_name", "shipkia_pickup_city", "shipkia_delivery_city")
        if meta.has_field(field)
    ]
    if not fields:
        return []
    values = frappe.db.get_value("Lead", lead, fields, as_dict=True) or {}
    bad_fields = [field for field, value in values.items() if _normalize_text(value) in bad_values]
    if not bad_fields:
        return []
    return [
        Finding(
            issue_type="bad_lead_field_value",
            severity="High",
            impact=25,
            conversation=conversation,
            lead=lead,
            phone=context["phone"],
            expected_behavior="Lead fields should store business/shipping facts, not casual chat fragments.",
            suggested_fix=f"Review extraction confidence for: {', '.join(bad_fields)}.",
            needs_human_review=True,
        )
    ]


def _finding(
    issue_type: str,
    severity: str,
    impact: float,
    conversation: str,
    context: dict[str, str],
    user_message: str,
    bot_reply: str,
    expected_behavior: str,
    suggested_fix: str,
    needs_human_review: bool = False,
) -> Finding:
    return Finding(
        issue_type=issue_type,
        severity=severity,
        impact=impact,
        conversation=conversation,
        lead=context["lead"],
        phone=context["phone"],
        user_message=user_message,
        bot_reply=bot_reply,
        expected_behavior=expected_behavior,
        suggested_fix=suggested_fix,
        needs_human_review=needs_human_review,
    )


def _insert_finding(qa_run_name: str, finding: Finding) -> Any:
    lead = finding.lead if finding.lead and frappe.db.exists("Lead", finding.lead) else ""
    doc = frappe.get_doc(
        {
            "doctype": "ShipKia Bot QA Finding",
            "qa_run": qa_run_name,
            "severity": finding.severity,
            "issue_type": finding.issue_type,
            "conversation": finding.conversation,
            "lead": lead,
            "customer_phone": finding.phone,
            "quality_score_impact": finding.impact,
            "needs_human_review": 1 if finding.needs_human_review else 0,
            "resolved_status": "Open",
            "user_message": finding.user_message[:8000],
            "bot_reply": finding.bot_reply[:8000],
            "expected_behavior": finding.expected_behavior[:8000],
            "suggested_fix": finding.suggested_fix[:8000],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _insert_training_example(qa_run_name: str, finding_name: str, finding: Finding) -> None:
    doc = frappe.get_doc(
        {
            "doctype": "ShipKia Bot Training Example",
            "qa_run": qa_run_name,
            "finding": finding_name,
            "conversation": finding.conversation,
            "detected_intent": _guess_intent(finding.user_message),
            "expected_intent": _expected_intent(finding.issue_type, finding.user_message),
            "outcome": finding.issue_type,
            "approved_for_training": 0,
            "user_message": finding.user_message[:8000],
            "bot_reply": finding.bot_reply[:8000],
            "corrected_reply": _corrected_reply_hint(finding.issue_type),
            "extracted_fields": "",
            "expected_fields": "",
        }
    )
    doc.insert(ignore_permissions=True)


def _previous_completed_run(current_name: str) -> dict[str, Any] | None:
    rows = frappe.get_all(
        "ShipKia Bot QA Run",
        filters={"status": "Completed", "name": ["!=", current_name]},
        fields=["name", "average_quality_score", "high_risk_findings", "regression_failed"],
        order_by="creation desc",
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _comparison_summary(current_score: float, previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {"previous_score": 0, "score_delta": 0, "text": "No previous completed QA run found for comparison."}
    previous_score = flt(previous.get("average_quality_score"))
    delta = round(current_score - previous_score, 2)
    direction = "better" if delta > 0 else "worse" if delta < 0 else "unchanged"
    if direction == "unchanged":
        text = f"Current score is {current_score}, previous score was {previous_score}; delta {delta}. Overall quality is unchanged."
    else:
        text = (
            f"Current score is {current_score}, previous score was {previous_score}; "
            f"delta {delta}. Overall quality is {direction} than the previous run."
        )
    return {"previous_score": previous_score, "score_delta": delta, "text": text}


def _run_regression_tests() -> dict[str, int]:
    try:
        from wa_chat_hub import test_shipkia_router

        result = test_shipkia_router.run()
        results = result.get("results") or []
        total = len(results)
        passed = sum(1 for item in results if item.get("ok"))
        return {"total": total, "passed": passed, "failed": total - passed}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "ShipKia QA Regression Failed")
        return {"total": 1, "passed": 0, "failed": 1}


def _summary_text(grouped: dict[str, list[dict[str, Any]]], findings: list[Finding], score: float) -> str:
    severity_counts = Counter(item.severity for item in findings)
    issue_counts = Counter(item.issue_type for item in findings)
    top_issues = ", ".join(f"{issue}: {count}" for issue, count in issue_counts.most_common(5)) or "none"
    return (
        f"Checked {len(grouped)} ShipKia conversations. Average quality score: {score}. "
        f"Findings: High {severity_counts.get('High', 0)}, Medium {severity_counts.get('Medium', 0)}, "
        f"Low {severity_counts.get('Low', 0)}. Top issues: {top_issues}."
    )


def _lead_temperature(messages: list[dict[str, Any]]) -> str:
    inbound = " ".join(str(msg.get("body") or "") for msg in messages if msg.get("direction") == "Inbound")
    text = _normalize_text(inbound)
    if _has_callback_intent(text) or _price_objection(text) or _asks_rate(text) or "onboard" in text:
        return "Hot"
    if _asks_features(text) or "aggregator" in text or "shiprocket" in text or "shipmozo" in text:
        return "Warm"
    return "Cold"


def _normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _contains_rate(text: str) -> bool:
    return bool(
        re.search(
            r"(₹\s*\d|\brs\.?\s*\d|\binr\s*\d|\d+(?:\.\d+)?\s*(?:rs|inr|rupees|/-))",
            text,
        )
    )


def _asks_rate(text: str) -> bool:
    return any(token in text for token in ("rate", "rates", "price", "pricing", "charges", "cost", "kitna", "paise"))


def _has_recent_route_details(text: str) -> bool:
    return bool(re.search(r"\bto\b|\bse\b|\bfrom\b|\d+\s*(?:g|gm|gram|kg|kgs)", text))


def _is_greeting(text: str) -> bool:
    return text in {"hi", "hii", "hello", "helo", "hlo", "hey", "namaste"}


def _has_greeting(text: str) -> bool:
    return any(token in text for token in ("hi", "hello", "namaste", "welcome back", "kaise"))


def _price_objection(text: str) -> bool:
    return any(token in text for token in ("zyada", "jada", "jda", "mehnga", "high", "expensive", "discount", "negotiate"))


def _handles_price_objection(text: str) -> bool:
    return any(token in text for token in ("custom", "callback", "team", "review", "better rate", "negotiate"))


def _has_callback_intent(text: str) -> bool:
    return any(token in text for token in ("callback", "call back", "call kar", "phone", "baat kar", "team se baat"))


def _asks_features(text: str) -> bool:
    return any(
        token in text
        for token in (
            "feature",
            "features",
            "ndr",
            "rto",
            "order confirmation",
            "customer decline",
            "decline order",
            "courier partner",
            "rate comparison",
            "comparison",
            "dashboard",
        )
    )


def _feature_issue(user_text: str, bot_text: str) -> str:
    if "order confirmation" in user_text and not ("whatsapp" in bot_text and "ivr" in bot_text):
        return "Order confirmation answers must mention WhatsApp engagement and IVR call."
    if ("ndr" in user_text or "rto" in user_text) and not (
        "whatsapp" in bot_text and "ivr" in bot_text and ("address" in bot_text or "mobile" in bot_text)
    ):
        return "NDR/RTO answers must mention WhatsApp plus IVR flow and customer mobile/address update ability."
    if "decline" in user_text and not ("not automatically" in bot_text or "automatically cancel" in bot_text):
        return "Declined orders should not be described as auto-cancelled; seller action should happen from dashboard."
    if "feature" in user_text and not ("courier" in bot_text and ("comparison" in bot_text or "compare" in bot_text)):
        return "General feature answers should mention multiple courier partners and better rate comparison."
    return ""


def _frustrated(text: str) -> bool:
    return any(token in text for token in ("baar baar", "bar bar", "repeat", "samajh nahi", "not reading", "ek baar", "stuck"))


def _acknowledges_frustration(text: str) -> bool:
    return any(token in text for token in ("sorry", "maaf", "samajh gaya", "understood", "theek hai"))


def _stale_context(user_text: str, bot_text: str) -> bool:
    if "business name" in user_text and _asks_rate(bot_text):
        return True
    if ("aggregator" in user_text or "shiprocket" in user_text or "shipmozo" in user_text) and "business type" in bot_text:
        return True
    return False


def _last_inbound(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("direction") == "Inbound":
            return str(msg.get("body") or "")
    return ""


def _guess_intent(text: str) -> str:
    normalized = _normalize_text(text)
    if _asks_rate(normalized):
        return "rate_request"
    if _asks_features(normalized):
        return "feature_question"
    if _price_objection(normalized):
        return "price_objection"
    if _has_callback_intent(normalized):
        return "callback_request"
    if _is_greeting(normalized):
        return "greeting"
    return "qualification_update"


def _expected_intent(issue_type: str, user_message: str) -> str:
    if issue_type == "exact_rate_without_callback":
        return "callback_request"
    if issue_type == "feature_answer_incomplete":
        return "feature_question"
    return _guess_intent(user_message)


def _corrected_reply_hint(issue_type: str) -> str:
    hints = {
        "exact_rate_without_callback": "Exact rate ke liye ShipKia team callback arrange kar du? Aap convenient time share kar dijiye.",
        "wrong_rate_risk": "Latest question ka direct answer do; rates tabhi share karo jab user rates/route clearly pooche.",
        "repeated_question": "Already collected details ko acknowledge karo aur next missing detail par move karo.",
        "feature_answer_incomplete": "ShipKia features: multiple courier partners, better rate comparison, WhatsApp/IVR order confirmation, and NDR flow.",
        "price_objection_missed": "Rate concern acknowledge karo, then custom pricing/callback offer karo.",
        "frustration_not_acknowledged": "Short apology/acknowledgement ke baad workflow continue karo.",
        "stale_context_reply": "Latest user message ko answer karo; purane pending slot ko blindly continue mat karo.",
    }
    return hints.get(issue_type, "Review this case and add a better expected response before approving for training.")
