from __future__ import annotations

import csv
import os
import tempfile
import unittest
from unittest.mock import patch

from services.contract_review import (
    DocumentSet,
    _amount_status,
    _canonical_model,
    _extract_configurations,
    _extract_cqp_products,
    _extract_model_quantities,
    _extract_payment_terms,
    _extract_warranty_clause,
    _find_ta_start,
    _infer_incoterm,
    _match_customer_master,
    build_review_items,
    extract_contract,
)
from services.contract_llm_review import run_llm_contract_review
from services.pdf_evidence import ParsedPDF, ParsedPage, TextSpan, find_text_spans, normalize_for_match


class PdfEvidenceTests(unittest.TestCase):
    def test_normalization_handles_pdf_spacing(self) -> None:
        self.assertEqual(normalize_for_match("IRB 1200 - 7 / 0.9 LPS"), "irb1200 - 7/0.9 lps")
        self.assertEqual(normalize_for_match("增 值 税 率：13％"), "增值税率:13%")

    def test_word_boxes_are_merged_by_line(self) -> None:
        page = ParsedPage(
            page_num=1,
            width=612,
            height=792,
            text="含税金额 983,849.00",
            spans=[
                TextSpan("含税金额", (10, 20, 65, 35)),
                TextSpan("983,849.00", (70, 20, 145, 35)),
            ],
        )
        rects = find_text_spans(page, "983,849.00")
        self.assertEqual(rects, [(70, 20, 145, 35)])


class ContractExtractionTests(unittest.TestCase):
    def test_embedded_ta_requires_strong_cover_marker(self) -> None:
        parsed = ParsedPDF(
            filepath="sample.pdf",
            pages=[
                ParsedPage(1, 612, 792, "附件一 技术协议"),
                ParsedPage(10, 612, 792, "附件一 供货范围及技术协议"),
                ParsedPage(13, 612, 792, "Technical Agreement 技术协议书 Doc No. 3.02.F03"),
            ],
        )
        self.assertEqual(_find_ta_start(parsed), 13)

    def test_cqp_product_rows_are_parsed_from_visual_lines(self) -> None:
        parsed = ParsedPDF(
            filepath="cqp.pdf",
            pages=[
                ParsedPage(
                    page_num=4,
                    width=612,
                    height=792,
                    text=(
                        "1 IRB 1200-7/0.7 Gen2 5 60,773.81 303,869.03\n"
                        "(3HAC092345-001)\n"
                    ),
                )
            ],
        )
        products = _extract_cqp_products(parsed)
        self.assertEqual(products[0]["model"], "IRB 1200-7/0.7 Gen2")
        self.assertEqual(products[0]["qty"], 5)
        self.assertEqual(products[0]["item_code"], "3HAC092345-001")
        self.assertAlmostEqual(products[0]["line_total"], 303869.03)


# CONTRACT_REVIEW_EVIDENCE_PATCH_V2
class EvidenceFirstRegressionTests(unittest.TestCase):
    def test_chinese_adjacent_model_and_explicit_quantity_are_extracted(self) -> None:
        parsed = ParsedPDF(
            "contract.pdf",
            [ParsedPage(1, 612, 792, "供货范围\n32 台IRB 1200-7/0.7 Gen2\n最大负载 20 kg\n工作范围 60 mm")],
        )
        self.assertEqual(_extract_model_quantities(parsed), {"IRB 1200-7/0.7 Gen2": 32})

    def test_specification_numbers_are_not_guessed_as_quantity(self) -> None:
        parsed = ParsedPDF(
            "ta.pdf",
            [ParsedPage(1, 612, 792, "IRB 1200-7/0.7 Gen2\n最大负载 20 kg\n工作范围 60 mm\n防护等级 1")],
        )
        self.assertEqual(_extract_model_quantities(parsed), {})

    def test_configuration_parser_rejects_dates_voltage_and_cross_page_carryover(self) -> None:
        parsed = ParsedPDF(
            "ta.pdf",
            [
                ParsedPage(1, 612, 792, "IRB 2600-20/1.65\n配置清单\n2026-01 日期\n220-230 V 电压\n3000-1 Controller"),
                ParsedPage(2, 612, 792, "3016-3 30m cable"),
            ],
        )
        configs = _extract_configurations(parsed, "ta")
        self.assertEqual([(item["model"], item["code"]) for item in configs], [("IRB 2600-20/1.65", "3000-1")])

    def test_template_underscores_do_not_break_money_vat_or_delivery(self) -> None:
        parsed = ParsedPDF(
            "contract.pdf",
            [
                ParsedPage(
                    1,
                    612,
                    792,
                    "销售合同 M2026-0001\n"
                    "买方：Buyer Co 地址：Buyer Road\n卖方：ABB（上海）机器人投资有限公司 地址：Shanghai\n"
                    "32 台IRB 1200-7/0.7 Gen2 发货时间：__8___周\n"
                    "不含增值税总额为 RMB:__6,200.00\n"
                    "含增值税总额为 RMB:__7,006.00\n"
                    "增值税税率：_13%\n"
                    "附件二 付款条件\n1）合同总价的100%，合同生效后电汇。\n附件三 诚信条款",
                )
            ],
        )
        result = extract_contract(parsed)
        self.assertEqual(result["untaxed_amount"], 6200.0)
        self.assertEqual(result["tax_included_amount"], 7006.0)
        self.assertEqual(result["vat_rate"], 0.13)
        self.assertEqual(result["delivery_schedule"][0]["weeks"], 8)

    def test_payment_parser_excludes_penalty_and_tax_percentages(self) -> None:
        result = _extract_payment_terms(
            "附件二 付款条件\n"
            "1）合同总价的10%，合同生效后电汇。\n"
            "2）合同总价的40%，发货前电汇。\n"
            "3）合同总价的50%，验收后电汇。\n"
            "违约责任\n违约金为12%。\n增值税率13%。"
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["percentages"], [10.0, 40.0, 50.0])

    def test_model_extraction_failure_does_not_create_quantity_cascade_blockers(self) -> None:
        contract_pdf = ParsedPDF("contract.pdf", [ParsedPage(1, 612, 792, "销售合同")])
        cqp_pdf = ParsedPDF("cqp.pdf", [ParsedPage(1, 612, 792, "报价")])
        ta_pdf = ParsedPDF("ta.pdf", [ParsedPage(1, 612, 792, "Technical Agreement 技术协议书")])
        documents = DocumentSet(contract_pdf, contract_pdf, cqp_pdf, ta_pdf, False)
        model = "IRB 2600-20/1.65"
        contract = {"products": [], "total_qty": 0, "attachments": {}, "payment_terms": {}, "warranty": {}}
        cqp = {"products": [{"model": model, "qty": 2}], "total_qty": 2, "configurations": []}
        ta = {"products": [{"model": model, "qty": 2}], "total_qty": 2, "configurations": []}
        items = build_review_items(documents, contract, cqp, ta)
        by_id = {item["id"]: item for item in items}
        self.assertEqual(by_id["product_models"]["status"], "UNDETERMINED")
        self.assertEqual(by_id["product_models"]["severity"], "blocker")
        self.assertEqual(by_id["product_quantities"]["status"], "UNDETERMINED")
        self.assertEqual(by_id["product_quantities"]["severity"], "warning")
        self.assertEqual(by_id["total_quantity"]["severity"], "warning")

    def test_ddp_ship_to_uses_one_complete_source_tuple(self) -> None:
        result = _infer_incoterm(
            {
                "incoterm_detection": {"selected": "DDP", "conflict": False},
                "delivery_location": "Customer Destination",
                "ship_to_name": "Incomplete Ship-to",
                "ship_to_address": "",
                "end_customer_name": "End Customer Co",
                "end_customer_address": "End Customer Road",
                "buyer_name": "Buyer Co",
                "buyer_address": "Buyer Road",
            },
            {"delivery_term": "DDP Customer Destination"},
        )
        self.assertEqual(result["ship_to_name"], "End Customer Co")
        self.assertEqual(result["ship_to_address"], "End Customer Road")
        self.assertEqual(result["ship_to_source"], "合同最终用户")

class LlmGuardrailTests(unittest.TestCase):
    @patch("services.contract_llm_review.call_llm_chat")
    def test_llm_cannot_override_rule_engine_blocker(self, mocked_call) -> None:
        mocked_call.return_value = '{"overall_assessment":"Pass","summary":"看起来没问题","confidence":0.9}'
        result = run_llm_contract_review(
            {}, {}, {}, {},
            [{"check_name": "Incoterm", "status": "MISMATCH", "detail": "冲突", "is_blocker": True}],
            {}, {}, {},
            config={"api_key": "test-key"},
        )
        self.assertEqual(result["overall_assessment"], "Blocked")


class ReviewPolicyTests(unittest.TestCase):
    def test_generic_model_is_not_limited_to_sample_models(self) -> None:
        self.assertEqual(_canonical_model("IRB 2600 - 20 / 1.65"), "IRB 2600-20/1.65")

    def test_incoterm_can_be_inferred_from_blank_location_and_exw_cqp(self) -> None:
        result = _infer_incoterm(
            {
                "incoterm_detection": {"selected": "", "conflict": False},
                "delivery_location": "",
                "buyer_name": "Buyer Co",
                "buyer_address": "Buyer Address",
            },
            {"delivery_term": "EXW Shanghai"},
        )
        self.assertEqual(result["conclusion"], "EXW")
        self.assertEqual(result["status"], "WARNING")
        self.assertEqual(result["severity"], "warning")
        self.assertEqual(result["ship_to_name"], "Buyer Co")

    def test_explicit_incoterm_conflict_is_blocker(self) -> None:
        result = _infer_incoterm(
            {
                "incoterm_detection": {"selected": "DDP", "conflict": False},
                "delivery_location": "Customer Destination",
                "buyer_name": "Buyer Co",
                "buyer_address": "Buyer Address",
            },
            {"delivery_term": "EXW Shanghai"},
        )
        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["severity"], "blocker")

    def test_sub_one_rmb_difference_is_warning(self) -> None:
        self.assertEqual(_amount_status(100.0, 100.61), ("WARNING", "warning", 0.61))

    def test_payment_terms_keep_contract_text_and_parse_percentages(self) -> None:
        raw = (
            "付款条件\n"
            "1）合同总价的百分之十，签订合同后七个工作日内电汇。\n"
            "2）合同总价的百分之四十，设备发货前七个工作日内电汇。\n"
            "3）合同总价的百分之五十，设备发货前银行承兑汇票。\n"
            "注：如分批发货，按对应批次执行。"
        )
        result = _extract_payment_terms(raw)
        self.assertTrue(result["complete"])
        self.assertEqual(result["percentages"], [10.0, 40.0, 50.0])
        self.assertIn("注：如分批发货", result["raw"])

    def test_duplicate_payment_percentages_are_not_collapsed(self) -> None:
        result = _extract_payment_terms(
            "付款条件\n1）合同总价的50%，合同生效后电汇。\n2）合同总价的50%，发货前电汇。"
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["percentages"], [50.0, 50.0])

    def test_warranty_5_2_classification(self) -> None:
        standard = _extract_warranty_clause(
            "5.2 质保期为十八（18）个月或运行十二（12）个月，以先到为准。\n5.3 其他"
        )
        extended = _extract_warranty_clause(
            "5.2 延长质保期为二十四（24）个月或运行十八（18）个月。\n5.3 其他"
        )
        self.assertEqual(standard["primary_period"], "18/12")
        self.assertEqual(standard["classification"], "Standard Warranty")
        self.assertEqual(extended["classification"], "Extended Warranty")

    def test_customer_master_csv_matches_ids_without_guessing(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8-sig", newline="", delete=False) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["客户名称", "客户地址", "Ship-to ID", "End Customer ID", "GIS号"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "客户名称": "Buyer Co",
                    "客户地址": "No. 1 Road, Shanghai",
                    "Ship-to ID": "ST1001",
                    "End Customer ID": "EC2002",
                    "GIS号": "GIS3003",
                }
            )
            path = handle.name
        try:
            result = _match_customer_master(
                path,
                {
                    "buyer_name": "Buyer Co",
                    "buyer_address": "No. 1 Road, Shanghai",
                    "end_customer_name": "",
                    "end_customer_address": "",
                },
                {"ship_to_name": "Buyer Co", "ship_to_address": "No. 1 Road, Shanghai"},
            )
        finally:
            os.unlink(path)
        self.assertTrue(result["matched"])
        self.assertEqual(result["ship_to_id"], "ST1001")
        self.assertEqual(result["end_customer_id"], "EC2002")
        self.assertEqual(result["gis_number"], "GIS3003")

    def test_delivery_mismatch_is_non_blocking_warning(self) -> None:
        contract_pdf = ParsedPDF("contract.pdf", [ParsedPage(1, 612, 792, "销售合同")])
        cqp_pdf = ParsedPDF("cqp.pdf", [ParsedPage(1, 612, 792, "报价")])
        ta_pdf = ParsedPDF("ta.pdf", [ParsedPage(1, 612, 792, "Technical Agreement 技术协议书")])
        documents = DocumentSet(contract_pdf, contract_pdf, cqp_pdf, ta_pdf, False)
        base_product = [{"model": "IRB 2600-20/1.65", "qty": 2}]
        contract = {
            "contract_number": "M2026-0001",
            "cqp_reference": "CQ1234567",
            "seller_name": "ABB（上海）机器人投资有限公司",
            "buyer_name": "Buyer Co",
            "buyer_address": "No. 1 Road",
            "products": base_product,
            "total_qty": 2,
            "delivery_schedule": [{"model": "IRB 2600-20/1.65", "qty": 2, "weeks": 10}],
            "delivery_trigger": "预付款到账后",
            "incoterm_detection": {"selected": "EXW", "conflict": False, "lines": []},
            "delivery_location": "",
            "untaxed_amount": 100.0,
            "tax_included_amount": 113.0,
            "vat_rate": 0.13,
            "payment_terms": {"raw": "付款条件 100%", "complete": True, "percentages": [100.0], "installments": []},
            "warranty": {
                "raw": "5.2 18/12 标准质保",
                "periods": [{"period": "18/12", "first_months": 18, "second_months": 12, "context": "18/12 标准质保"}],
                "classification": "Standard Warranty",
            },
            "attachments": {"ta": True, "payment": True, "integrity": True},
        }
        cqp = {
            "cqp_number": "CQ1234567",
            "seller_name": "ABB（上海）机器人投资有限公司",
            "customer_name": "Buyer Co",
            "customer_address": "No. 1 Road",
            "products": [{"model": "IRB 2600-20/1.65", "qty": 2, "item_code": "3HAC1", "unit_price": 50.0, "line_total": 100.0}],
            "total_qty": 2,
            "delivery_weeks": 8,
            "delivery_time": "8周",
            "delivery_trigger": "合同生效",
            "delivery_term": "EXW Shanghai",
            "untaxed_total": 100.0,
            "tax_included_total": 113.0,
            "tax_amount": 13.0,
            "vat_rate": 0.13,
            "payment_terms": {"raw": "付款条件 100%", "complete": True, "percentages": [100.0], "installments": []},
            "configurations": [{"model": "IRB 2600-20/1.65", "code": "3000-1", "description": "Controller"}],
            "warranty_details_by_model": {"IRB 2600-20/1.65": {"code": "438-1", "description": "Standard Warranty", "classification": "Standard Warranty"}},
        }
        ta = {
            "contract_number": "M2026-0001",
            "seller_name": "ABB（上海）机器人投资有限公司",
            "buyer_name": "Buyer Co",
            "products": base_product,
            "total_qty": 2,
            "configurations": [
                {"model": "IRB 2600-20/1.65", "code": "3000-1", "description": "控制器"},
                {"model": "IRB 2600-20/1.65", "code": "438-1", "description": "Standard Warranty"},
            ],
            "warranty_details_by_model": {"IRB 2600-20/1.65": {"code": "438-1", "description": "Standard Warranty", "classification": "Standard Warranty"}},
            "responsibilities": {"buyer_integration": True, "buyer_installation": True, "seller_not_integration": True},
        }
        items = build_review_items(documents, contract, cqp, ta)
        delivery = next(item for item in items if item["id"] == "delivery_period")
        self.assertEqual(delivery["status"], "WARNING")
        self.assertEqual(delivery["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
