# Contract Review AI Debug Report

The contract comparison page now exposes **导出 AI 调试报告** after a successful test.

## Workflow

1. Upload Contract and CQP; TA remains optional.
2. Run the contract test.
3. Click **导出 AI 调试报告**.
4. Describe the incorrect result and the expected result.
5. Send the exported JSON together with the exact PDFs used in that run.

The JSON contains the complete review response, extracted structured fields, evidence locations, input-file hashes, the active prompt knowledge snapshot, and hashes of the relevant review-engine files.

## Why the expected result is required

A current result alone does not define a bug. The user-reported gap separates extraction failures, rule-policy disagreements, evidence-location failures, and stale prompt knowledge.

## Privacy

The JSON does not embed PDF bytes, API keys, Outlook data, or `backend/config/llm_config.json`. Contract contents already present in extracted review fields and prompt knowledge remain potentially confidential; treat the report like the source contracts.
