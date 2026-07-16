from __future__ import annotations

import unittest

from services import contract_review_engine as engine
from services.contract_review_knowledge import config_family, get_contract_review_knowledge
from services.pdf_evidence import ParsedPDF, ParsedPage, TextSpan


class ContractReviewRegression20260716Tests(unittest.TestCase):
    def setUp(self) -> None:
        get_contract_review_knowledge.cache_clear()

    def test_spaced_contract_number_is_normalized(self) -> None:
        parsed = ParsedPDF("contract.pdf", [ParsedPage(1, 612, 792, "销售合同\n合同编号：M 4 3 6 7 - 3 1 4 9")])
        self.assertEqual(engine.extract_contract(parsed)["contract_number"], "M4367-3149")

    def test_ta_range_uses_page_1_of_n(self) -> None:
        parsed = ParsedPDF("bundle.pdf", [
            ParsedPage(1, 612, 792, "销售合同 M4367-3149"),
            ParsedPage(13, 612, 792, "Technical Agreement 技术协议书 Doc No. 3.02.F03 Page 1 of 19"),
            ParsedPage(31, 612, 792, "Doc No. 3.02.F03 Page 19 of 19"),
            ParsedPage(32, 612, 792, "附件三 诚信条款"),
        ])
        self.assertEqual(engine._find_ta_start(parsed), 13)
        self.assertEqual(engine._find_ta_end(parsed, 13), 32)

    def test_inline_numbered_payment_clauses_keep_50_percent_method(self) -> None:
        result = engine._extract_payment_terms(
            "付款条件\nPurchase Order, 合同总价的百分之十，应在签订本合同后七个工作日内以电汇方式支付；"
            "2）合同总价的百分之四十，应在设备发货前七个工作日内以电汇方式支付。"
            "3）合同总价的百分之五十，应在设备发货前七个工作日内以ABB认可的电子银行承兑汇票方式支付。\n交货时间\n8周"
        )
        self.assertEqual(result["percentages"], [10.0, 40.0, 50.0])
        self.assertEqual(result["installments"][2]["method"], "银行承兑汇票")

    def test_cross_page_configuration_stays_with_previous_model(self) -> None:
        parsed = ParsedPDF("ta.pdf", [
            ParsedPage(1, 612, 792, "1. IRB 1100-4/0.58 Industry Robot\n3107-1 Collision detection"),
            ParsedPage(2, 612, 792, "3151-1 Program package\n2. IRB 1200-7/0.7 Gen2 Industry Robot\n3300-122 IRB 1200-7/0.7 Gen2"),
        ])
        configs = engine._extract_configurations(parsed, "ta")
        by_code = {item["code"]: item["model"] for item in configs}
        self.assertEqual(by_code["3151-1"], "IRB 1100-4/0.58")
        self.assertEqual(by_code["3300-122"], "IRB 1200-7/0.7 Gen2")

    def test_model_fragment_is_not_configuration_code(self) -> None:
        parsed = ParsedPDF("ta.pdf", [ParsedPage(1, 612, 792, "1. IRB 5710-90/2.7 Industry Robot\n/0.7Gen2 型工业机器人\n3151-1 Program package")])
        codes = [item["code"] for item in engine._extract_configurations(parsed, "ta")]
        self.assertNotIn("1200-7", codes)

    def test_independent_function_codes_are_not_one_conflict_family(self) -> None:
        get_contract_review_knowledge.cache_clear()
        # Both codes intentionally have no mutually-exclusive family. Empty
        # values therefore mean “do not infer a family conflict,” not equality.
        self.assertEqual(config_family("3107-1"), "")
        self.assertEqual(config_family("3151-1"), "")
        result = engine._config_match(
            {"model": "IRB 1100-4/0.58", "code": "3151-1", "description": "Program package"},
            [{"model": "IRB 1100-4/0.58", "code": "3107-1", "description": "Collision detection"}],
            "",
        )
        self.assertFalse(result["matched"])
        self.assertIsNone(result["conflict"])

    def test_quantity_contradiction_is_preserved(self) -> None:
        parsed = ParsedPDF("ta.pdf", [
            ParsedPage(1, 612, 792, "甲方采购 1 台 IRB 5710-90/2.7"),
            ParsedPage(2, 612, 792, "根据买方选择提供 11 台 IRB 5710-90/2.7"),
            ParsedPage(3, 612, 792, "卖方供货 1 台 IRB 5710-90/2.7"),
        ])
        details = engine._extract_model_quantity_details(parsed)
        self.assertEqual(details["selected"]["IRB 5710-90/2.7"], 1)
        self.assertEqual(details["conflicts"][0]["values"], [1, 11])

    def test_visual_checkbox_selects_ddp(self) -> None:
        page = ParsedPage(
            2, 612, 792,
            "买方工厂的到货价\n卖方工厂出厂价",
            spans=[
                TextSpan("买方工厂的到货价", (100, 100, 250, 112)),
                TextSpan("卖方工厂出厂价", (100, 140, 250, 152)),
            ],
            checkbox_marks=[
                {"bbox": [82, 101, 92, 111], "checked": True},
                {"bbox": [82, 141, 92, 151], "checked": False},
            ],
        )
        result = engine._detect_contract_incoterm(ParsedPDF("contract.pdf", [page]))
        self.assertEqual(result["selected"], "DDP")

    def test_ta_red_flags_find_unit_and_blank_safemove(self) -> None:
        parsed = ParsedPDF("ta.pdf", [ParsedPage(22, 612, 792, "位置重复精度：0.011m\nSafemove 功能：\n5.机器人在系统中的功能")])
        kinds = {item["type"] for item in engine._extract_ta_technical_red_flags(parsed)}
        self.assertEqual(kinds, {"repeatability_unit", "blank_safemove"})

    def test_llm_console_forwards_return_metadata(self) -> None:
        from services.llm_console import logged_llm_call

        seen = {}

        def fake_call(config, messages, *, return_metadata=False):
            seen["return_metadata"] = return_metadata
            return {"content": "ok", "finish_reason": "stop", "usage": {}}

        response = logged_llm_call(fake_call, {}, [], return_metadata=True)
        self.assertTrue(seen["return_metadata"])
        self.assertEqual(response["content"], "ok")


if __name__ == "__main__":
    unittest.main()
