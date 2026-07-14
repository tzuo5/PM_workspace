from __future__ import annotations

import unittest

from services.contract_review import _extract_cqp_products, _find_ta_start
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


if __name__ == "__main__":
    unittest.main()
