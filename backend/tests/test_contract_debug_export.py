from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import contract_review


class ContractDebugExportTests(unittest.TestCase):
    def test_run_review_attaches_prompt_snapshot_and_input_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contract_path = os.path.join(temp_dir, "contract.pdf")
            cqp_path = os.path.join(temp_dir, "cqp.pdf")
            with open(contract_path, "wb") as handle:
                handle.write(b"%PDF-contract")
            with open(cqp_path, "wb") as handle:
                handle.write(b"%PDF-cqp")

            engine_result = {
                "review_items": [
                    {
                        "id": "sample",
                        "evidence": [{"location_status": "uncertain"}],
                        "sub_items": [],
                    }
                ],
                "blockers": [{"type": "sample"}],
                "non_blockers": [],
            }
            knowledge = SimpleNamespace(
                source_files=("Agent_Prompt.txt", "合同审核规则手册.md"),
                rule_context="prompt snapshot",
            )
            roles = {"contract": contract_path, "cqp": cqp_path}

            with patch.object(contract_review, "_engine_run_review", return_value=engine_result.copy()), patch.object(
                contract_review, "get_contract_review_knowledge", return_value=knowledge
            ):
                result = contract_review.run_review([contract_path, cqp_path], file_roles=roles)

            context = result["debug_context"]
            self.assertEqual(context["schema_version"], "pm-contract-debug-context/v1")
            self.assertEqual(context["prompt_path"], "backend/config/contract_checker_prompt")
            self.assertEqual(context["prompt_snapshot"], "prompt snapshot")
            self.assertEqual(context["diagnostic_counters"]["uncertain_evidence_count"], 1)
            by_role = {entry["role"]: entry for entry in context["input_files"]}
            self.assertEqual(by_role["contract"]["filename"], "contract.pdf")
            self.assertEqual(
                by_role["contract"]["sha256"],
                hashlib.sha256(b"%PDF-contract").hexdigest(),
            )

    def test_debug_snapshot_failure_never_breaks_review(self) -> None:
        with patch.object(contract_review, "_engine_run_review", return_value={"review_items": []}), patch.object(
            contract_review, "_build_debug_context", side_effect=RuntimeError("snapshot failed")
        ):
            result = contract_review.run_review([])
        self.assertEqual(result["debug_context"]["error"], "snapshot failed")


if __name__ == "__main__":
    unittest.main()
