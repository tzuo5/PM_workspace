/**
 * Contract-review extensions:
 * 1. Export a self-contained AI debugging JSON report.
 * 2. Let a human override each top-level check to green/yellow/red.
 * 3. Enable one-click BT09 .eml export after every check is green.
 */
(function () {
    "use strict";

    var REPORT_SCHEMA = "pm-contract-debug-report/v1";
    var latestReviewResult = null;
    var activeController = null;
    var originalFetch = window.fetch.bind(window);
    var manualAudit = [];

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function injectStyles() {
        if (document.getElementById("crManualReviewStyles")) return;
        var style = document.createElement("style");
        style.id = "crManualReviewStyles";
        style.textContent = [
            ".cr-manual-review{display:flex;align-items:center;justify-content:flex-end;gap:7px;padding:0 9px 8px}",
            ".cr-manual-review__label{font-size:10px;color:var(--text-tertiary,#707984)}",
            ".cr-manual-review__buttons{display:inline-flex;align-items:center;gap:4px;padding:3px 5px;border:1px solid var(--border-color-light,#e2e6ec);border-radius:999px;background:var(--bg-secondary,#f7f8fa)}",
            ".cr-manual-status{width:17px;height:17px;padding:0;border:2px solid transparent;border-radius:50%;cursor:pointer;box-shadow:0 0 0 1px rgba(0,0,0,.08);transition:transform .12s ease,box-shadow .12s ease}",
            ".cr-manual-status:hover{transform:scale(1.13)}",
            ".cr-manual-status--pass{background:#218739}",
            ".cr-manual-status--warning{background:#d88a00}",
            ".cr-manual-status--blocker{background:#d71920}",
            ".cr-manual-status.is-selected{border-color:#fff;box-shadow:0 0 0 2px currentColor}",
            ".cr-manual-status--pass.is-selected{color:#218739}",
            ".cr-manual-status--warning.is-selected{color:#d88a00}",
            ".cr-manual-status--blocker.is-selected{color:#d71920}",
            ".cr-review-node.is-manually-reviewed{border-style:solid}",
            ".cr-manual-review__note{font-size:9px;font-weight:700;color:#51606f}",
            "#crBtnExportBT09:disabled{cursor:not-allowed;opacity:.48}"
        ].join("\n");
        document.head.appendChild(style);
    }

    function isContractReviewRequest(input, init) {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
        return method === "POST" && /\/api\/contract-review(?:\?|$)/.test(url);
    }

    function showToast(message, tone) {
        var container = document.getElementById("toastContainer");
        if (!container) return;
        var toast = document.createElement("div");
        toast.className = "toast toast--" + (tone || "success");
        toast.textContent = message;
        container.appendChild(toast);
        window.setTimeout(function () { toast.classList.add("toast--leaving"); }, 2200);
        window.setTimeout(function () {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 2700);
    }

    function captureReviewResult(result) {
        if (!result || result.ok === false) return;
        latestReviewResult = result;
        manualAudit = [];
        ensureToolbarButtons();
        updateButtonState();
    }

    window.fetch = function (input, init) {
        var reviewRequest = isContractReviewRequest(input, init);
        return originalFetch(input, init).then(function (response) {
            if (reviewRequest && response.ok) {
                response.clone().json().then(captureReviewResult).catch(function () {});
            }
            return response;
        });
    };

    function fileMetadata(role) {
        var input = document.querySelector('[data-input="' + role + '"]');
        var file = input && input.files && input.files[0];
        if (!file) return null;
        return {
            role: role,
            filename: file.name,
            size_bytes: file.size,
            mime_type: file.type || "application/pdf",
            last_modified_utc: file.lastModified ? new Date(file.lastModified).toISOString() : ""
        };
    }

    function currentUiContext() {
        var activeFilter = document.querySelector("#contractReviewContent .cr-filter-btn.active");
        var status = document.querySelector('#contractReviewContent [data-role="status-text"]');
        return {
            active_filter: activeFilter ? activeFilter.getAttribute("data-filter") : "",
            status_text: status ? status.textContent : "",
            exported_from_url: window.location.href,
            manual_review_changes: manualAudit.slice()
        };
    }

    function safeFilename(value) {
        return String(value || "contract-review")
            .replace(/[\\/:*?"<>|]+/g, "_")
            .replace(/\s+/g, "_")
            .replace(/^_+|_+$/g, "") || "contract-review";
    }

    function currentReviewResult() {
        return (activeController && activeController.reviewResult) || latestReviewResult;
    }

    function buildReport(userGap) {
        var reviewResult = currentReviewResult() || latestReviewResult;
        var contractNumber = reviewResult && reviewResult.extracted_data &&
            reviewResult.extracted_data.contract &&
            reviewResult.extracted_data.contract.contract_number;
        return {
            schema_version: REPORT_SCHEMA,
            generated_at_utc: new Date().toISOString(),
            user_reported_gap: userGap,
            debugging_goal: (
                "Compare the user-reported expected behavior with review_result. " +
                "Then inspect the original PDFs and the prompt snapshot in " +
                "review_result.debug_context before changing extraction or deterministic rules."
            ),
            repository_context: {
                repository: "tzuo5/PM_workspace",
                default_branch: "master",
                contract_number: contractNumber || "",
                prompt_path: "backend/config/contract_checker_prompt"
            },
            browser_input_files: [
                fileMetadata("contract"),
                fileMetadata("cqp"),
                fileMetadata("ta")
            ].filter(Boolean),
            ui_context: currentUiContext(),
            review_result: reviewResult
        };
    }

    function downloadReport() {
        if (!currentReviewResult()) {
            showToast("请先运行合同测试。", "warning");
            return;
        }
        var userGap = window.prompt(
            "请描述当前结果哪里不对，以及你认为正确结果应该是什么。\n" +
            "这一步很重要；没有 expected result，AI 只能猜。",
            ""
        );
        if (userGap === null) return;
        if (!String(userGap).trim()) {
            var proceed = window.confirm("你没有填写预期结果。仍然导出报告吗？");
            if (!proceed) return;
        }

        var report = buildReport(String(userGap || "").trim());
        var contractNumber = report.repository_context.contract_number;
        var timestamp = new Date().toISOString().replace(/[:.]/g, "-");
        var filename = safeFilename(contractNumber || "contract-review") + "_debug_" + timestamp + ".json";
        var blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json;charset=utf-8" });
        downloadBlob(blob, filename);
        showToast("AI 调试报告已导出；请同时保留原始 PDF。", "success");
    }

    function nodeKind(node) {
        var status = String((node && node.status) || "").toUpperCase();
        var severity = String((node && node.severity) || "").toLowerCase();
        if (severity === "blocker" || status === "BLOCKER" || status === "MISMATCH") return "blocker";
        if (severity === "warning" || status === "WARNING" || status === "UNDETERMINED") return "warning";
        if (status === "PASS") return "pass";
        return "info";
    }

    function manualControlsHtml(node, key, nested) {
        if (nested) return "";
        var kind = nodeKind(node);
        var reviewed = Boolean(node && node.manual_override);
        return '<div class="cr-manual-review" data-manual-review-key="' + escapeHtml(key) + '">' +
            '<span class="cr-manual-review__label">人工确认</span>' +
            '<span class="cr-manual-review__buttons" role="group" aria-label="人工修改检查状态">' +
                '<button class="cr-manual-status cr-manual-status--pass' + (kind === "pass" ? ' is-selected' : '') + '" data-manual-status="PASS" data-manual-key="' + escapeHtml(key) + '" type="button" title="设为绿色：通过" aria-label="设为通过"></button>' +
                '<button class="cr-manual-status cr-manual-status--warning' + (kind === "warning" ? ' is-selected' : '') + '" data-manual-status="WARNING" data-manual-key="' + escapeHtml(key) + '" type="button" title="设为黄色：警告" aria-label="设为警告"></button>' +
                '<button class="cr-manual-status cr-manual-status--blocker' + (kind === "blocker" ? ' is-selected' : '') + '" data-manual-status="BLOCKER" data-manual-key="' + escapeHtml(key) + '" type="button" title="设为红色：阻断" aria-label="设为阻断"></button>' +
            '</span>' +
            (reviewed ? '<span class="cr-manual-review__note">已人工修改</span>' : '') +
        '</div>';
    }

    function installControllerPatch() {
        if (!window.ContractReview || !window.ContractReview.prototype) return false;
        var proto = window.ContractReview.prototype;
        if (proto.__manualBt09ExtensionInstalled) return true;
        proto.__manualBt09ExtensionInstalled = true;

        var originalRenderNode = proto._renderReviewNode;
        proto._renderReviewNode = function (node, key, nested) {
            activeController = this;
            var html = originalRenderNode.call(this, node, key, nested);
            var controls = manualControlsHtml(node, key, nested);
            if (!controls) return html;
            var firstButtonEnd = html.indexOf("</button>");
            if (firstButtonEnd < 0) return html;
            var insertAt = firstButtonEnd + "</button>".length;
            return html.slice(0, insertAt) + controls + html.slice(insertAt);
        };

        var originalRenderResults = proto._renderResults;
        proto._renderResults = function () {
            activeController = this;
            var result = originalRenderResults.apply(this, arguments);
            markManualNodes();
            ensureToolbarButtons();
            updateButtonState();
            return result;
        };

        return true;
    }

    function setNodeStatusRecursive(node, status, severity, changedAt) {
        if (!node) return;
        if (!node._manual_original_status) {
            node._manual_original_status = node.status || "";
            node._manual_original_severity = node.severity || "";
        }
        node.status = status;
        node.severity = severity;
        node.manual_override = {
            status: status,
            severity: severity,
            changed_at_utc: changedAt
        };
        (node.sub_items || []).forEach(function (subItem) {
            setNodeStatusRecursive(subItem, status, severity, changedAt);
        });
    }

    function findNodeInResult(result, key) {
        if (!result) return null;
        var parts = String(key || "").split("::");
        var item = (result.review_items || []).find(function (entry) { return entry.id === parts[0]; });
        if (!item) return null;
        if (parts.length === 1) return item;
        return (item.sub_items || []).find(function (entry) { return entry.id === parts[1]; }) || null;
    }

    function recalculateOutcome(result) {
        if (!result) return;
        var items = result.review_items || [];
        var blockers = items.filter(function (item) { return nodeKind(item) === "blocker"; }).map(function (item) {
            return { type: item.id, detail: item.summary || "" };
        });
        var warnings = items.filter(function (item) { return nodeKind(item) === "warning"; }).map(function (item) {
            return { type: item.id, detail: item.summary || "" };
        });
        result.blockers = blockers;
        result.non_blockers = warnings;
        result.conclusion = blockers.length ? "Blocked" : (warnings.length ? "Pass with notes" : "Pass");
        result.manual_review = {
            all_green: items.length > 0 && items.every(function (item) {
                var kind = nodeKind(item);
                return kind === "pass" || kind === "info";
            }),
            changes: manualAudit.slice()
        };
        if (result.bt09_fields) {
            result.bt09_fields.ready = result.manual_review.all_green;
            result.bt09_fields.blocked_by = blockers.map(function (item) { return item.type; });
        }
    }

    function applyManualStatus(key, status) {
        if (!activeController || !activeController.reviewResult) {
            showToast("当前检查结果尚未准备好。", "warning");
            return;
        }
        var severity = status === "PASS" ? "info" : (status === "WARNING" ? "warning" : "blocker");
        var changedAt = new Date().toISOString();
        var found = activeController._findNode ? activeController._findNode(key) : null;
        if (!found || !found.node) return;

        setNodeStatusRecursive(found.node, status, severity, changedAt);
        if (found.parent && found.parent !== found.node && status !== "PASS") {
            setNodeStatusRecursive(found.parent, status, severity, changedAt);
        }
        var cloneNode = findNodeInResult(latestReviewResult, key);
        if (cloneNode && cloneNode !== found.node) setNodeStatusRecursive(cloneNode, status, severity, changedAt);

        manualAudit.push({
            key: key,
            title: found.node.title || found.node.code || key,
            status: status,
            changed_at_utc: changedAt
        });
        recalculateOutcome(activeController.reviewResult);
        if (latestReviewResult && latestReviewResult !== activeController.reviewResult) recalculateOutcome(latestReviewResult);
        activeController._renderResults();
        showToast(status === "PASS" ? "已人工确认通过。" : (status === "WARNING" ? "已人工设为警告。" : "已人工设为阻断。"), status === "BLOCKER" ? "danger" : (status === "WARNING" ? "warning" : "success"));
    }

    function markManualNodes() {
        document.querySelectorAll("#contractReviewContent .cr-review-node").forEach(function (article) {
            article.classList.toggle("is-manually-reviewed", Boolean(article.querySelector(".cr-manual-review__note")));
        });
    }

    function allTopLevelChecksGreen(result) {
        var items = result && result.review_items;
        return Array.isArray(items) && items.length > 0 && items.every(function (item) {
            var kind = nodeKind(item);
            return kind === "pass" || kind === "info";
        });
    }

    function displayValue(value, fallback) {
        var text = value == null ? "" : String(value).trim();
        return text || fallback;
    }

    function productModels(fields) {
        var seen = {};
        return (fields.products || []).map(function (item) {
            return displayValue(item && item.model, "");
        }).filter(function (model) {
            if (!model || seen[model]) return false;
            seen[model] = true;
            return true;
        }).join("、");
    }

    function buildBT09Email(fields) {
        var incoterm = String(fields.incoterm || "").toUpperCase();
        var buyer = displayValue(fields.buyer_name, "待确认买方");
        var quantity = displayValue(fields.total_qty, "待确认数量");
        var models = productModels(fields) || "待确认机器人型号";
        var contractNumber = displayValue(fields.contract_number, "待确认合同号");
        var cqpNumber = displayValue(fields.cqp_number, "待确认CQP号");
        var pm = displayValue(fields.pm, "待补充");
        var sales = displayValue(fields.sales_person, "待补充");
        var delivery = displayValue(fields.delivery_terms, "待确认");
        var payment = displayValue(fields.payment_terms_verbatim, "待确认");
        var shipToName = displayValue(fields.ship_to_name, incoterm === "EXW" ? buyer : "待补充");
        var shipToAddress = displayValue(fields.ship_to_address, incoterm === "EXW" ? displayValue(fields.buyer_address, "待补充") : "待补充");
        var shipToId = displayValue(fields.ship_to_id, "待补充");
        var endCustomer = displayValue(fields.end_customer_name, "待补充");
        var endCustomerId = displayValue(fields.end_customer_id, "待补充");
        var endCustomerAddress = displayValue(fields.end_customer_address, "待补充");
        var gis = displayValue(fields.gis_number, "待补充");
        var gm = displayValue(fields.gm, "待补充");
        var nm = displayValue(fields.nm, "待补充");
        var deliveryDescription = displayValue(fields.incoterm_2, incoterm ? incoterm + " Shanghai" : "待确认");
        var firstLine = buyer + " " + quantity + "台 " + models;
        var lines = [
            firstLine,
            "请创建BT09，谢谢！",
            "",
            contractNumber,
            "",
            "PM：" + pm + "  销售：" + sales,
            "",
            "CQP号：" + cqpNumber,
            "",
            "交货时间：" + delivery,
            "",
            "付款条件：",
            payment,
            ""
        ];

        if (incoterm === "EXW") {
            lines.push("Ship to信息：", "EXW Shanghai", shipToName, "地址：" + shipToAddress, "Ship to ID：" + shipToId, "");
        } else {
            lines.push("Ship to信息：" + shipToName, "地址：" + shipToAddress, "Ship to ID：" + shipToId, "");
        }
        lines.push(
            "Freight term (Incoterm 1) 和 Terms of delivery description (Incoterm 2)：" + deliveryDescription,
            "",
            "End customer：" + endCustomer,
            "End customer ID：" + endCustomerId,
            "地址：" + endCustomerAddress,
            "GIS号：" + gis,
            "",
            "Gross Margin: " + gm + "   Net Margin: " + nm
        );

        return {
            subject: firstLine + " 请创建BT09",
            body: lines.join("\r\n"),
            filename: safeFilename(contractNumber + "_BT09") + ".eml"
        };
    }

    function utf8Base64(text) {
        var bytes = new TextEncoder().encode(String(text));
        var binary = "";
        var chunkSize = 0x8000;
        for (var offset = 0; offset < bytes.length; offset += chunkSize) {
            binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunkSize));
        }
        return window.btoa(binary);
    }

    function wrapBase64(value) {
        return String(value).match(/.{1,76}/g).join("\r\n");
    }

    function buildEml(email) {
        return [
            "X-Unsent: 1",
            "MIME-Version: 1.0",
            "Date: " + new Date().toUTCString(),
            "To: ",
            "Subject: =?UTF-8?B?" + utf8Base64(email.subject) + "?=",
            'Content-Type: text/plain; charset="UTF-8"',
            "Content-Transfer-Encoding: base64",
            "",
            wrapBase64(utf8Base64(email.body)),
            ""
        ].join("\r\n");
    }

    function downloadBlob(blob, filename) {
        var url = URL.createObjectURL(blob);
        var anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }

    function exportBT09Email() {
        var result = currentReviewResult();
        if (!result || !allTopLevelChecksGreen(result)) {
            showToast("请先将所有检查项人工确认至绿色。", "warning");
            return;
        }
        if (!result.bt09_fields) {
            showToast("当前结果缺少 BT09 字段，无法导出。", "danger");
            return;
        }
        var email = buildBT09Email(result.bt09_fields);
        var blob = new Blob([buildEml(email)], { type: "message/rfc822;charset=utf-8" });
        downloadBlob(blob, email.filename);
        showToast("BT09 邮件已导出为 .eml，可直接用 Outlook 打开并补充收件人。", "success");
    }

    function updateButtonState() {
        var result = currentReviewResult();
        var debugButton = document.getElementById("crBtnExportDebug");
        if (debugButton) {
            debugButton.disabled = !result;
            debugButton.title = result ? "导出当前测试的 AI 调试报告" : "请先运行测试";
        }

        var bt09Button = document.getElementById("crBtnExportBT09");
        if (!bt09Button) return;
        var green = allTopLevelChecksGreen(result);
        var hasFields = Boolean(result && result.bt09_fields);
        bt09Button.disabled = !(green && hasFields);
        if (!result) bt09Button.title = "请先运行测试";
        else if (!green) bt09Button.title = "所有检查项变为绿色后才可导出";
        else if (!hasFields) bt09Button.title = "当前结果缺少 BT09 字段";
        else bt09Button.title = "导出可由 Outlook 打开的 BT09 邮件文件";
    }

    function ensureToolbarButtons() {
        injectStyles();
        var actions = document.querySelector("#contractReviewContent .cr-toolbar__actions");
        var runButton = document.getElementById("crBtnRun");
        if (!actions || !runButton) return;

        if (!document.getElementById("crBtnExportBT09")) {
            var bt09Button = document.createElement("button");
            bt09Button.className = "btn btn--primary";
            bt09Button.id = "crBtnExportBT09";
            bt09Button.type = "button";
            bt09Button.textContent = "导出 BT09 邮件";
            bt09Button.disabled = true;
            bt09Button.addEventListener("click", exportBT09Email);
            actions.insertBefore(bt09Button, runButton);
        }

        if (!document.getElementById("crBtnExportDebug")) {
            var debugButton = document.createElement("button");
            debugButton.className = "btn btn--outline";
            debugButton.id = "crBtnExportDebug";
            debugButton.type = "button";
            debugButton.textContent = "导出 AI 调试报告";
            debugButton.disabled = true;
            debugButton.addEventListener("click", downloadReport);
            actions.insertBefore(debugButton, document.getElementById("crBtnExportBT09") || runButton);
        }
        updateButtonState();
    }

    document.addEventListener("click", function (event) {
        var button = event.target && event.target.closest ? event.target.closest("[data-manual-status]") : null;
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        applyManualStatus(button.getAttribute("data-manual-key"), button.getAttribute("data-manual-status"));
    }, true);

    document.addEventListener("change", function (event) {
        if (!event.target || !event.target.matches('[data-input="contract"], [data-input="cqp"], [data-input="ta"]')) return;
        latestReviewResult = null;
        manualAudit = [];
        updateButtonState();
    });

    var patchAttempts = 0;
    var patchTimer = window.setInterval(function () {
        patchAttempts += 1;
        if (installControllerPatch() || patchAttempts > 400) window.clearInterval(patchTimer);
    }, 25);

    var observer = new MutationObserver(function () {
        installControllerPatch();
        ensureToolbarButtons();
        markManualNodes();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    injectStyles();
    installControllerPatch();
    ensureToolbarButtons();
})();
