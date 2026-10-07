from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from server.focus_policy import (
    FocusModifierAudit,
    apply_focus_modifier_audit,
    audit_focus_modifier,
)


class FocusPolicyTests(unittest.TestCase):
    @staticmethod
    def _audit(attrs: dict, reply: str) -> FocusModifierAudit:
        with patch("llm.client.remote_llm_messages_query", return_value=reply):
            return asyncio.run(audit_focus_modifier(attrs))

    def test_explicit_compound_switch_is_confirmed(self) -> None:
        audit = self._audit(
            {
                "intent": "execute",
                "focus": "set",
                "project_id": "project-a",
                "task": "create route-note.txt",
                "_host_source_user_text": "切到 amadeus，并新建 route-note.txt。",
            },
            "SET",
        )

        self.assertTrue(audit.allowed)
        self.assertEqual(audit.outcome, "confirmed")

    def test_audit_sends_its_role_free_contract_and_original_payload(self) -> None:
        attrs = {
            "intent": "execute",
            "focus": "set",
            "project_id": "project-a",
            "_host_source_user_text": "切到 amadeus，并新建 route-note.txt。",
        }
        with patch("llm.client.remote_llm_messages_query", return_value="SET") as query:
            audit = asyncio.run(audit_focus_modifier(attrs))

        messages = query.call_args.args[0]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertTrue(messages[0]["content"].startswith("You are a narrow control-plane validator."))
        self.assertIn("exactly one token: SET, CLEAR, or NONE", messages[0]["content"])
        self.assertEqual(messages[1]["content"], json.dumps({
            "user_message": attrs["_host_source_user_text"],
            "proposed_focus": "set",
            "operation_intent": "execute",
            "project_id_present": True,
        }, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(query.call_args.kwargs["temperature"], 0.0)
        self.assertFalse(query.call_args.kwargs["json_output"])
        self.assertGreaterEqual(query.call_args.kwargs["max_tokens"], 900)
        self.assertEqual(query.call_args.kwargs["timeout"], 10.0)
        self.assertTrue(audit.allowed)

    def test_cross_project_target_loses_only_the_persistent_modifier(self) -> None:
        attrs = {
            "intent": "amend",
            "focus": "set",
            "project_id": "project-chess",
            "workspace_ref": "work-note",
            "task": "append reviewed",
            "_host_source_user_text": "给象棋项目的 route-note.txt 加一行 reviewed。",
        }
        audit = self._audit(attrs, "NONE")
        apply_focus_modifier_audit(attrs, audit)

        self.assertNotIn("focus", attrs)
        self.assertEqual(attrs["project_id"], "project-chess")
        self.assertEqual(attrs["workspace_ref"], "work-note")
        self.assertEqual(attrs["_host_focus_guard"], "removed")
        self.assertNotIn("one_off", attrs)

    def test_rejected_clear_keeps_current_task_in_drafts(self) -> None:
        attrs = {
            "intent": "execute",
            "focus": "clear",
            "task": "create timer",
            "_host_source_user_text": "另外做个一次性的番茄钟。",
        }
        audit = self._audit(attrs, "NONE")
        apply_focus_modifier_audit(attrs, audit)

        self.assertNotIn("focus", attrs)
        self.assertEqual(attrs["one_off"], "true")
        self.assertEqual(attrs["_host_focus_guard"], "removed")

    def test_audit_failure_denies_the_modifier(self) -> None:
        attrs = {
            "intent": "amend",
            "focus": "set",
            "project_id": "project-chess",
            "task": "append reviewed",
            "_host_source_user_text": "给象棋项目的文件加一行。",
        }
        with patch("llm.client.remote_llm_messages_query", side_effect=RuntimeError("offline")):
            audit = asyncio.run(audit_focus_modifier(attrs))
        apply_focus_modifier_audit(attrs, audit)

        self.assertFalse(audit.allowed)
        self.assertEqual(audit.outcome, "audit_unavailable")
        self.assertNotIn("focus", attrs)
        self.assertEqual(attrs["project_id"], "project-chess")

    def test_internal_call_without_user_source_preserves_declared_contract(self) -> None:
        attrs = {
            "intent": "execute",
            "focus": "set",
            "project_id": "project-a",
            "task": "create file",
        }

        audit = asyncio.run(audit_focus_modifier(attrs))

        self.assertTrue(audit.allowed)
        self.assertEqual(audit.outcome, "trusted_internal")


if __name__ == "__main__":
    unittest.main()

