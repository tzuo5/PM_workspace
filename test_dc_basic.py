# -*- coding: utf-8 -*-
"""Quick test of Document Check rule engine."""
import sys
sys.path.insert(0, "backend")

from services.document_check_rules import ExtractedData, run_all_rules, compute_overall_conclusion

data = ExtractedData()
data.has_contract = True
data.has_cqp = True

data.contract_fields["contract_number"] = {"value": "M4367-3607"}
data.contract_fields["seller_legal_entity"] = {"value": "ABB（上海）机器人投资有限公司"}
data.cqp_fields["incoterm"] = {"value": "DDP Shanghai"}

results = run_all_rules(data)
conc, desc = compute_overall_conclusion(results)

print(f"{len(results)} rules run")
print(f"Conclusion: {conc} - {desc}")
print()
for r in results:
    print(f"  [{r.status:20s}] {r.rule_id:40s} {'BLOCKER' if r.is_blocker else ''} {r.summary[:80]}")