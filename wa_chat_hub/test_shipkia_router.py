from __future__ import annotations


def run():
    import frappe

    from wa_chat_hub.api import ai_bot
    from wa_chat_hub import shipkia_qualification
    mixed_text = "brand name Beyond Ethnic, 2.5k monthly shipments, using Shiprocket, rates Delhi to Mumbai 500g"
    mixed_details = ai_bot._extract_shipkia_rate_details(mixed_text)
    mixed_decision = ai_bot._shipkia_route_user_message(mixed_text, None, {}, {}, mixed_details)
    low_conf_text = "Beyond Ethnic"
    low_conf_details = ai_bot._extract_shipkia_rate_details(low_conf_text)
    low_conf_decision = ai_bot._shipkia_route_user_message(low_conf_text, None, {}, {}, low_conf_details)

    results = [
        _check("monthly_2_5k", ai_bot._shipkia_parse_monthly_shipments("around 2.5k", expected=True) == 2500),
        _check("junk_business_name", ai_bot._shipkia_extract_business_name("jldi bolo") == ""),
        _check(
            "business_name_correction",
            ai_bot._shipkia_extract_correction_entities("actually brand name is Beyond Ethnic")[0].get("business_name")
            == "Beyond Ethnic",
        ),
        _check(
            "aggregator_correction",
            ai_bot._shipkia_extract_correction_entities("not shiprocket, shipmozo")[0].get("current_aggregator_name")
            == "Shipmozo",
        ),
        _check(
            "price_objection_hinglish",
            ai_bot._shipkia_price_objection_intent("Paise jda h apke") is True,
        ),
        _check(
            "feedback_not_callback_time",
            ai_bot._shipkia_extract_callback_time("ek baar mein ek cheez puchha kro") == "",
        ),
        _check(
            "feature_reply_decline_no_auto_cancel",
            "automatically order cancel nahi karta"
            in ai_bot._shipkia_feature_reply("what if customer decline the order").lower(),
        ),
        _check(
            "feature_reply_ndr_whatsapp_ivr",
            "whatsapp aur ivr" in ai_bot._shipkia_feature_reply("NDR flow kaise manage karte ho?").lower()
            and "mobile number ya address update" in ai_bot._shipkia_feature_reply("NDR flow kaise manage karte ho?").lower(),
        ),
        _check(
            "feature_reply_order_confirmation",
            "order confirmation" in ai_bot._shipkia_feature_reply("order confirmation ke liye bhi hai?").lower()
            and "ivr call" in ai_bot._shipkia_feature_reply("order confirmation ke liye bhi hai?").lower(),
        ),
        _check(
            "mixed_profile_rate_saves_profile",
            mixed_decision.get("intent") == "rate_request"
            and mixed_decision.get("should_save") is True
            and mixed_decision.get("entities", {}).get("business_name") == "Beyond Ethnic"
            and mixed_decision.get("entities", {}).get("monthly_shipments") == 2500
            and mixed_decision.get("entities", {}).get("current_aggregator_name") == "Shiprocket",
        ),
        _check(
            "low_confidence_single_field_needs_confirmation",
            low_conf_decision.get("intent") == "qualification_update"
            and low_conf_decision.get("should_save") is False
            and low_conf_decision.get("entities", {}).get("business_name") == "Beyond Ethnic",
        ),
        _check(
            "standalone_weight_not_rate_intent",
            ai_bot._extract_shipkia_rate_details("500g").get("rate_intent") is False
            and ai_bot._extract_shipkia_rate_details("500g").get("weight_grams") == 500,
        ),
        _check(
            "standalone_payment_not_rate_intent",
            ai_bot._extract_shipkia_rate_details("Dono").get("rate_intent") is False
            and ai_bot._extract_shipkia_rate_details("Dono").get("payment_type") == "Both",
        ),
        _check(
            "route_weight_still_rate_intent",
            ai_bot._extract_shipkia_rate_details("Bombay to Bangalore 500g").get("rate_intent") is True,
        ),
        _check(
            "completed_rate_details_detected",
            ai_bot._shipkia_completed_rate_details(
                {
                    "pickup_city": "Mumbai",
                    "delivery_city": "Bangalore",
                    "weight_grams": 500,
                    "payment_type": "Both",
                }
            )
            is True,
        ),
        _check(
            "frustrated_qualified_lead_hot_review",
            _frustrated_qualified_score(shipkia_qualification),
        ),
    ]
    results.extend(_test_pending_confirmation_flow(ai_bot, frappe))
    ok = all(item["ok"] for item in results)
    for item in results:
        print(f"[{'PASS' if item['ok'] else 'FAIL'}] {item['name']}")
    return {"ok": ok, "results": results}


def _check(name: str, ok: bool) -> dict[str, object]:
    return {"name": name, "ok": bool(ok)}


def _test_pending_confirmation_flow(ai_bot, frappe) -> list[dict[str, object]]:
    suffix = frappe.generate_hash(length=8)
    phone = "9199" + "".join(str(ord(char) % 10) for char in suffix)[:8]
    lead_name = ""
    contact_name = ""
    conversation_name = ""
    results: list[dict[str, object]] = []
    try:
        lead = frappe.get_doc(
            {
                "doctype": "Lead",
                "first_name": f"Router Test {suffix}",
                "lead_name": f"Router Test {suffix}",
                "mobile_no": phone,
            }
        )
        if frappe.get_meta("Lead").has_field("status"):
            lead.status = "Open"
        lead.insert(ignore_permissions=True)
        lead_name = lead.name

        contact = frappe.get_doc(
            {
                "doctype": "Chat Contact",
                "name": phone,
                "display_name": f"Router Test {suffix}",
                "phone_number": phone,
                "linked_lead": lead_name,
                "source_doctype": "Lead",
                "source_name": lead_name,
            }
        )
        contact.insert(ignore_permissions=True)
        contact_name = contact.name

        conversation = frappe.get_doc(
            {
                "doctype": "Chat Conversation",
                "channel_account": "Shipkia Test",
                "contact": contact_name,
                "status": "Open",
                "linked_reference_doctype": "Lead",
                "linked_reference_name": lead_name,
            }
        )
        conversation.insert(ignore_permissions=True)
        conversation_name = conversation.name
        frappe.db.commit()

        lead_meta = frappe.get_meta("Lead")
        rate_context_updates = {
            "shipkia_pickup_city": "Mumbai",
            "shipkia_delivery_city": "Bangalore",
            "shipkia_average_weight": "500g",
            "shipkia_payment_mode": "Both",
        }
        rate_context_updates = {
            fieldname: value
            for fieldname, value in rate_context_updates.items()
            if lead_meta.has_field(fieldname)
        }
        if rate_context_updates:
            frappe.db.set_value("Lead", lead_name, rate_context_updates)
            frappe.db.commit()

        reply, handled = ai_bot._build_shipkia_rate_autoreply("Dono", str(conversation_name))
        duplicate_ack_ok = handled is True and "rate" not in reply.lower() and "noted" in reply.lower()
        results.append(
            _check(
                "completed_rate_duplicate_payment_ack",
                duplicate_ack_ok,
            )
        )

        reply, handled = ai_bot._build_shipkia_rate_autoreply("Beyond Ethnic", str(conversation_name))
        saved_before = frappe.db.get_value("Lead", lead_name, "shipkia_business_name")
        results.append(
            _check(
                "pending_confirmation_created",
                handled is True
                and "confirm" in reply.lower()
                and not saved_before,
            )
        )

        reply, handled = ai_bot._build_shipkia_rate_autoreply("yes", str(conversation_name))
        saved_after = frappe.db.get_value("Lead", lead_name, "shipkia_business_name")
        state = frappe.db.get_value("Chat Conversation", conversation_name, ["shipkia_last_bot_question", "shipkia_pending_slots"], as_dict=True)
        results.append(
            _check(
                "pending_confirmation_saved_on_yes",
                handled is True
                and saved_after == "Beyond Ethnic"
                and not (state or {}).get("shipkia_last_bot_question")
                and not (state or {}).get("shipkia_pending_slots"),
            )
        )
        if results[-1]["ok"] is False:
            print(
                "pending_confirmation_saved_on_yes_debug:",
                {
                    "reply": reply,
                    "handled": handled,
                    "saved_after": saved_after,
                    "state": state,
                },
            )

        ai_bot._build_shipkia_rate_autoreply("Another Brand", str(conversation_name))
        reply, handled = ai_bot._build_shipkia_rate_autoreply("no", str(conversation_name))
        saved_after_no = frappe.db.get_value("Lead", lead_name, "shipkia_business_name")
        state_after_no = frappe.db.get_value("Chat Conversation", conversation_name, ["shipkia_last_bot_question", "shipkia_pending_slots"], as_dict=True)
        results.append(
            _check(
                "pending_confirmation_cancelled_on_no",
                handled is True
                and saved_after_no == "Beyond Ethnic"
                and not (state_after_no or {}).get("shipkia_last_bot_question")
                and not (state_after_no or {}).get("shipkia_pending_slots"),
            )
        )
        if results[-1]["ok"] is False:
            print(
                "pending_confirmation_cancelled_on_no_debug:",
                {
                    "reply": reply,
                    "handled": handled,
                    "saved_after_no": saved_after_no,
                    "state": state_after_no,
                },
            )
    except Exception as exc:
        results.append(_check("pending_confirmation_flow_exception", False))
        print(f"pending_confirmation_flow_exception: {exc}")
    finally:
        frappe.db.rollback()
        for doctype, name in (
            ("Chat Conversation", conversation_name),
            ("Chat Contact", contact_name),
            ("Lead", lead_name),
        ):
            if name and frappe.db.exists(doctype, name):
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
        frappe.db.commit()
    return results


def _frustrated_qualified_score(shipkia_qualification) -> bool:
    scorecard = shipkia_qualification._score_lead(
        {
            "shipkia_business_type": "B2C",
            "shipkia_business_name": "Dhruv Store",
            "shipkia_current_aggregator_status": "No",
            "shipkia_current_aggregator_name": "ShipPrime",
            "shipkia_current_shipping_rate": 38,
            "shipkia_rto_percentage": 40,
            "shipkia_monthly_shipments": 5000,
        },
        [
            {"direction": "Inbound", "body": "B2c h"},
            {"direction": "Inbound", "body": "5k"},
            {"direction": "Inbound", "body": "Shipprime"},
            {"direction": "Inbound", "body": "For zone d its 38 and rto is 40%"},
            {"direction": "Inbound", "body": "Sab batane ke bad bhi dubara dubara puch rhe ho"},
        ],
    )
    return (
        scorecard.get("temperature") == "Hot"
        and scorecard.get("status") == "Needs Human Review"
        and scorecard.get("score", 0) >= 70
    )
