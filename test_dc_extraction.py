# -*- coding: utf-8 -*-
"""Test PDF extraction with coordinates for Document Check."""
import json
import os
import sys
import tempfile

# Setup path
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services import pdf_parser, document_check_service as dcsvc
from services import document_check_db as dcdb

DATA_DIR = os.path.join(BACKEND_DIR, "data", "attachments", "document_check")

def test_parse_and_extract():
    """Test that a PDF can be parsed and fields extracted with coordinates."""
    # Find any PDF in the document check directory
    pdf_files = []
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.lower().endswith('.pdf') and not f.endswith('.spans.json'):
                pdf_files.append(os.path.join(DATA_DIR, f))

    if not pdf_files:
        print("No PDF files found in", DATA_DIR)
        print("Skipping extraction test - no PDFs available.")
        return True

    filepath = pdf_files[0]
    print(f"Testing with: {os.path.basename(filepath)}")

    # Parse PDF
    print("  Parsing PDF...")
    result = pdf_parser.parse_pdf(filepath)
    print(f"  Pages: {result.page_count}, Spans: {sum(len(p.spans) for p in result.pages)}, Errors: {len(result.parse_errors)}")

    if result.parse_errors:
        print(f"  Parse errors: {result.parse_errors}")

    # Check spans on first page with text
    for page in result.pages:
        if page.spans:
            print(f"  Page {page.page_number}: {len(page.spans)} spans, first span:")
            s = page.spans[0]
            print(f"    text='{s.text[:60]}...', bbox={s.bbox}, normalized_bbox={s.normalized_bbox}")
            break

    # Classify
    doc_type = dcsvc._classify_document(filepath, result)
    print(f"  Detected type: {doc_type}")

    # Test span search
    if result.pages and result.pages[0].spans:
        test_patterns = ["合同", "IRB", "ABB", "买方", "卖方", "Contract", "DDP", "EXW", "RMB"]
        for pat in test_patterns:
            matches = pdf_parser.search_text_on_pages(result.pages, pat)
            if matches:
                m = matches[0]
                print(f"  Found '{pat}': page {m.page_number}, bbox={m.normalized_bbox}, text='{m.text[:50]}...'")

    # Test save/load parse result
    print("  Testing save/load parse result...")
    json_path = pdf_parser.save_parse_result(result, filepath)
    print(f"  Saved to: {json_path}")
    loaded = pdf_parser.load_parse_result(filepath)
    if loaded:
        print(f"  Loaded: {loaded.page_count} pages, {sum(len(p.spans) for p in loaded.pages)} spans")
    else:
        print("  ERROR: Failed to load parse result")
        return False

    # Test extraction on a DB case
    print("  Testing field extraction...")
    dcdb.init_dc_db()
    case = dcdb.create_review_case("test_extraction")
    doc = dcdb.add_review_document(
        case_id=case["id"],
        original_filename=os.path.basename(filepath),
        stored_filename=os.path.basename(filepath),
        sha256="test_sha",
        workspace="A",
        file_size=os.path.getsize(filepath),
    )

    fields = dcsvc._extract_fields_from_document(
        filepath, result, doc_type, case["id"], doc["id"]
    )
    print(f"  Extracted {len(fields)} fields:")
    for f in fields:
        print(f"    {f['field_name']}: {f['value'][:80]}")

    # Check evidence coordinates
    evidence = dcdb.list_evidence_for_document(doc["id"])
    print(f"  Evidence records: {len(evidence)}")
    ev_with_bbox = [e for e in evidence if e.get("bbox", {}).get("width", 0) > 0]
    print(f"  Evidence with valid bbox: {len(ev_with_bbox)}")
    for ev in ev_with_bbox[:3]:
        bbox = ev.get("bbox", {})
        print(f"    page={ev['page_number']}, bbox={bbox}, raw_text='{ev.get('raw_text','')[:60]}'")

    # Clean up
    dcdb.delete_review_case(case["id"])
    print("  Test passed!")
    return True

if __name__ == "__main__":
    ok = test_parse_and_extract()
    sys.exit(0 if ok else 1)