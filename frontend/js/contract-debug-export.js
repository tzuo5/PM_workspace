/**
 * Export a self-contained JSON report after a contract comparison finishes.
 *
 * The report intentionally does not embed PDF bytes. Attach the exported JSON
 * together with the original PDFs when asking an AI to debug the comparison.
 */
(function () {
    "use strict";

    var REPORT_SCHEMA = "pm-contract-debug-report/v1";
    var latestReviewResult = null;
    var originalFetch = window.fetch.bind(window);

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
        ensureExportButton();
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
            exported_from_url: window.location.href
        };
    }

    function safeFilename(value) {
        return String(value || "contract-review")
            .replace(/[^a-zA-Z0-9._-]+/g, "_")
            .replace(/^_+|_+$/g, "") || "contract-review";
    }

    function buildReport(userGap) {
        var contractNumber = latestReviewResult && latestReviewResult.extracted_data &&
            latestReviewResult.extracted_data.contract &&
            latestReviewResult.extracted_data.contract.contract_number;
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
            review_result: latestReviewResult
        };
    }

    function downloadReport() {
        if (!latestReviewResult) {
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
        var url = URL.createObjectURL(blob);
        var anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        showToast("AI 调试报告已导出；请同时保留原始 PDF。", "success");
    }

    function updateButtonState() {
        var button = document.getElementById("crBtnExportDebug");
        if (!button) return;
        button.disabled = !latestReviewResult;
        button.title = latestReviewResult ? "导出当前测试的 AI 调试报告" : "请先运行测试";
    }

    function ensureExportButton() {
        if (document.getElementById("crBtnExportDebug")) return;
        var actions = document.querySelector("#contractReviewContent .cr-toolbar__actions");
        var runButton = document.getElementById("crBtnRun");
        if (!actions || !runButton) return;
        var button = document.createElement("button");
        button.className = "btn btn--outline";
        button.id = "crBtnExportDebug";
        button.type = "button";
        button.textContent = "导出 AI 调试报告";
        button.disabled = true;
        button.addEventListener("click", downloadReport);
        actions.insertBefore(button, runButton);
        updateButtonState();
    }

    document.addEventListener("change", function (event) {
        if (!event.target || !event.target.matches('[data-input="contract"], [data-input="cqp"], [data-input="ta"]')) return;
        latestReviewResult = null;
        updateButtonState();
    });

    var observer = new MutationObserver(function () { ensureExportButton(); });
    observer.observe(document.body, { childList: true, subtree: true });
    ensureExportButton();
})();
