/**
 * Contract Review workspace.
 *
 * The PDF bytes are only read by PDF.js. Highlights are HTML overlays and are
 * never written back into the source files.
 */
(function () {
    "use strict";

    var CATEGORY_ORDER = ["customer_information", "product_information", "other_information"];
    var CATEGORY_FALLBACK = {
        customer_information: "客户信息",
        product_information: "产品信息",
        other_information: "其他信息"
    };

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function statusKind(node) {
        var status = String(node.status || "").toUpperCase();
        var severity = String(node.severity || "").toLowerCase();
        if (severity === "blocker" || status === "BLOCKER" || status === "MISMATCH") return "blocker";
        if (severity === "warning" || status === "WARNING" || status === "UNDETERMINED") return "warning";
        if (status === "PASS") return "pass";
        return "info";
    }

    function statusLabel(node) {
        var status = String(node.status || "").toUpperCase();
        if (status === "MISMATCH") return "不一致";
        if (status === "UNDETERMINED") return "待确认";
        if (status === "PASS") return "通过";
        if (status === "WARNING") return "警告";
        if (status === "BLOCKER") return "阻断";
        return status || "信息";
    }

    function uniqueEvidence(entries) {
        var seen = {};
        return (entries || []).filter(function (entry) {
            if (!entry) return false;
            var key = [entry.document_type, entry.page, entry.label, entry.quote].join("|");
            if (seen[key]) return false;
            seen[key] = true;
            return true;
        });
    }

    // ---------------------------------------------------------------------
    // PDF viewer
    // ---------------------------------------------------------------------

    function PDFViewer(container, panelId) {
        this.container = container;
        this.panelId = panelId;
        this.pdfDoc = null;
        this.loadingTask = null;
        this.renderTask = null;
        this.loadSerial = 0;
        this.renderSerial = 0;
        this.objectURL = null;
        this.fileName = "";
        this.documentType = "";
        this.currentPage = 1;
        this.totalPages = 0;
        this.scale = 1.15;
        this.defaultScale = 1.15;
        this.highlights = [];
        this.targetEvidence = null;
        this.readyPromise = Promise.resolve();
        this._init();
    }

    PDFViewer.prototype._init = function () {
        var self = this;
        this.container.innerHTML =
            '<div class="cr-pdf-toolbar">' +
                '<div class="cr-pdf-identity">' +
                    '<span class="cr-pdf-doc-label" data-role="doc-label">PDF</span>' +
                    '<span class="cr-pdf-filename" data-role="filename"></span>' +
                '</div>' +
                '<div class="cr-pdf-controls">' +
                    '<button class="cr-pdf-btn" data-action="prev" type="button" aria-label="上一页">‹</button>' +
                    '<input class="cr-pdf-page-input" data-action="page-input" type="text" inputmode="numeric" value="1" aria-label="页码">' +
                    '<span class="cr-pdf-page-info" data-role="page-info">/ 0</span>' +
                    '<button class="cr-pdf-btn" data-action="next" type="button" aria-label="下一页">›</button>' +
                    '<button class="cr-pdf-btn" data-action="zoomout" type="button" aria-label="缩小">−</button>' +
                    '<button class="cr-pdf-btn" data-action="zoomin" type="button" aria-label="放大">＋</button>' +
                    '<button class="cr-pdf-btn cr-pdf-btn--text" data-action="fitwidth" type="button">适宽</button>' +
                    '<button class="cr-pdf-btn cr-pdf-btn--text" data-action="resetzoom" type="button">100%</button>' +
                '</div>' +
            '</div>' +
            '<div class="cr-pdf-location" data-role="location" hidden></div>' +
            '<div class="cr-pdf-viewer-container" data-role="viewer-container">' +
                '<div class="cr-placeholder">请上传 PDF 文件</div>' +
            '</div>';

        this.docLabel = this.container.querySelector('[data-role="doc-label"]');
        this.fileNameEl = this.container.querySelector('[data-role="filename"]');
        this.pageInput = this.container.querySelector('[data-action="page-input"]');
        this.pageInfo = this.container.querySelector('[data-role="page-info"]');
        this.locationEl = this.container.querySelector('[data-role="location"]');
        this.viewerContainer = this.container.querySelector('[data-role="viewer-container"]');

        this.container.querySelector('[data-action="prev"]').addEventListener("click", function () {
            self.goToPage(self.currentPage - 1);
        });
        this.container.querySelector('[data-action="next"]').addEventListener("click", function () {
            self.goToPage(self.currentPage + 1);
        });
        this.container.querySelector('[data-action="zoomin"]').addEventListener("click", function () {
            self.scale = Math.min(3, self.scale + 0.15);
            self.renderPage(self.currentPage);
        });
        this.container.querySelector('[data-action="zoomout"]').addEventListener("click", function () {
            self.scale = Math.max(0.45, self.scale - 0.15);
            self.renderPage(self.currentPage);
        });
        this.container.querySelector('[data-action="resetzoom"]').addEventListener("click", function () {
            self.scale = self.defaultScale;
            self.renderPage(self.currentPage);
        });
        this.container.querySelector('[data-action="fitwidth"]').addEventListener("click", function () {
            self.fitWidth();
        });
        this.pageInput.addEventListener("keydown", function (event) {
            if (event.key !== "Enter") return;
            var page = parseInt(self.pageInput.value, 10);
            if (!Number.isFinite(page)) page = self.currentPage;
            self.goToPage(page);
        });
    };

    PDFViewer.prototype._setLocationMessage = function (message) {
        if (!message) {
            this.locationEl.hidden = true;
            this.locationEl.textContent = "";
            return;
        }
        this.locationEl.hidden = false;
        this.locationEl.textContent = message;
    };

    PDFViewer.prototype.loadPDF = function (file, documentType) {
        var self = this;
        var serial = ++this.loadSerial;
        this._releaseDocument();
        this.documentType = documentType || "";
        this.fileName = file ? file.name : "";
        this.docLabel.textContent = documentType === "contract" ? "合同" : documentType === "cqp" ? "CQP" : documentType === "ta" ? "TA" : "PDF";
        this.fileNameEl.textContent = this.fileName;
        this.clearHighlights();
        this._setLocationMessage("");
        this.viewerContainer.innerHTML = '<div class="cr-placeholder">PDF 加载中...</div>';

        if (!file || typeof pdfjsLib === "undefined") {
            this.viewerContainer.innerHTML = '<div class="cr-placeholder">PDF.js 未加载或文件不可用</div>';
            this.readyPromise = Promise.reject(new Error("PDF.js 未加载或文件不可用"));
            return this.readyPromise;
        }

        this.objectURL = URL.createObjectURL(file);
        this.loadingTask = pdfjsLib.getDocument({
            url: this.objectURL,
            cMapUrl: "vendor/pdfjs/web/cmaps/",
            cMapPacked: true
        });
        this.readyPromise = this.loadingTask.promise.then(function (doc) {
            if (serial !== self.loadSerial) {
                try { doc.destroy(); } catch (ignore) {}
                throw new Error("PDF load superseded");
            }
            self.pdfDoc = doc;
            self.totalPages = doc.numPages;
            self.currentPage = 1;
            self.pageInfo.textContent = "/ " + self.totalPages;
            self.pageInput.value = "1";
            return self.renderPage(1);
        }).catch(function (error) {
            if (serial === self.loadSerial) {
                self.viewerContainer.innerHTML = '<div class="cr-placeholder">PDF 加载失败：' + escapeHtml(error && error.message) + '</div>';
            }
            throw error;
        });
        return this.readyPromise;
    };

    PDFViewer.prototype.renderPage = function (pageNumber) {
        var self = this;
        if (!this.pdfDoc) return Promise.resolve();
        pageNumber = Math.max(1, Math.min(this.totalPages, Number(pageNumber) || 1));
        var serial = ++this.renderSerial;
        if (this.renderTask) {
            try { this.renderTask.cancel(); } catch (ignore) {}
            this.renderTask = null;
        }
        this.currentPage = pageNumber;
        this.pageInput.value = String(pageNumber);

        return this.pdfDoc.getPage(pageNumber).then(function (page) {
            if (serial !== self.renderSerial) return;
            var viewport = page.getViewport({ scale: self.scale });
            var pixelRatio = window.devicePixelRatio || 1;
            var wrapper = document.createElement("div");
            wrapper.className = "cr-pdf-page-wrapper";
            wrapper.style.width = viewport.width + "px";
            wrapper.style.height = viewport.height + "px";

            var canvas = document.createElement("canvas");
            canvas.className = "cr-pdf-canvas";
            canvas.width = Math.floor(viewport.width * pixelRatio);
            canvas.height = Math.floor(viewport.height * pixelRatio);
            canvas.style.width = viewport.width + "px";
            canvas.style.height = viewport.height + "px";
            wrapper.appendChild(canvas);

            var textLayer = document.createElement("div");
            textLayer.className = "cr-pdf-text-layer";
            textLayer.style.width = viewport.width + "px";
            textLayer.style.height = viewport.height + "px";
            wrapper.appendChild(textLayer);

            var overlay = document.createElement("div");
            overlay.className = "cr-highlight-overlay";
            overlay.style.width = viewport.width + "px";
            overlay.style.height = viewport.height + "px";
            wrapper.appendChild(overlay);

            self.viewerContainer.innerHTML = "";
            self.viewerContainer.appendChild(wrapper);
            var context = canvas.getContext("2d");
            self.renderTask = page.render({
                canvasContext: context,
                viewport: viewport,
                transform: pixelRatio === 1 ? null : [pixelRatio, 0, 0, pixelRatio, 0, 0]
            });
            return self.renderTask.promise.then(function () {
                if (serial !== self.renderSerial) return;
                self.renderTask = null;
                self._drawHighlights(viewport, overlay, wrapper);
                return page.getTextContent().then(function (textContent) {
                    if (serial !== self.renderSerial || !pdfjsLib.renderTextLayer) return;
                    try {
                        var task = pdfjsLib.renderTextLayer({
                            textContentSource: textContent,
                            textContent: textContent,
                            container: textLayer,
                            viewport: viewport,
                            textDivs: []
                        });
                        return task && task.promise ? task.promise.catch(function () {}) : undefined;
                    } catch (ignore) {}
                });
            }).catch(function (error) {
                if (!error || error.name !== "RenderingCancelledException") throw error;
            });
        }).catch(function (error) {
            if (!error || error.name !== "RenderingCancelledException") {
                console.error("PDF page render failed", error);
            }
        });
    };

    PDFViewer.prototype._drawHighlights = function (viewport, overlay, wrapper) {
        var self = this;
        var firstTop = null;
        this.highlights.forEach(function (highlight) {
            var evidence = highlight.evidence || {};
            if (Number(evidence.page) !== self.currentPage) return;
            var pageWidth = Number(evidence.page_width) || (viewport.width / self.scale);
            var pageHeight = Number(evidence.page_height) || (viewport.height / self.scale);
            var scaleX = viewport.width / pageWidth;
            var scaleY = viewport.height / pageHeight;
            (evidence.rects || []).forEach(function (rect) {
                if (!Array.isArray(rect) || rect.length !== 4) return;
                var left = Number(rect[0]) * scaleX;
                var top = Number(rect[1]) * scaleY;
                var width = Math.max(2, (Number(rect[2]) - Number(rect[0])) * scaleX);
                var height = Math.max(4, (Number(rect[3]) - Number(rect[1])) * scaleY);
                var marker = document.createElement("div");
                marker.className = "cr-highlight-rect " + (highlight.className || "") + " pulse";
                marker.style.left = left + "px";
                marker.style.top = top + "px";
                marker.style.width = width + "px";
                marker.style.height = height + "px";
                overlay.appendChild(marker);
                if (firstTop === null || top < firstTop) firstTop = top;
            });
        });
        if (firstTop !== null) {
            window.requestAnimationFrame(function () {
                self.viewerContainer.scrollTop = Math.max(0, wrapper.offsetTop + firstTop - self.viewerContainer.clientHeight / 3);
            });
        }
    };

    PDFViewer.prototype.goToPage = function (pageNumber) {
        if (!this.pdfDoc) return this.readyPromise || Promise.resolve();
        pageNumber = Math.max(1, Math.min(this.totalPages, Number(pageNumber) || 1));
        return this.renderPage(pageNumber);
    };

    PDFViewer.prototype.fitWidth = function () {
        var self = this;
        if (!this.pdfDoc) return Promise.resolve();
        return this.pdfDoc.getPage(this.currentPage).then(function (page) {
            var base = page.getViewport({ scale: 1 });
            var available = Math.max(200, self.viewerContainer.clientWidth - 28);
            self.scale = Math.max(0.45, Math.min(3, available / base.width));
            return self.renderPage(self.currentPage);
        });
    };

    PDFViewer.prototype.navigateToEvidence = function (entries, className) {
        var self = this;
        var relevant = uniqueEvidence(entries).filter(function (entry) {
            return entry.document_type === self.documentType;
        });
        this.highlights = relevant.map(function (evidence) {
            return { evidence: evidence, className: className };
        });
        var target = relevant.find(function (entry) { return Number(entry.page) > 0; });
        if (!target) {
            this._setLocationMessage("无法确定位置");
            return Promise.resolve(false);
        }
        var exact = target.location_status === "exact" && Array.isArray(target.rects) && target.rects.length > 0;
        this._setLocationMessage(exact ? "" : "无法确定位置");
        return this.goToPage(Number(target.page)).then(function () { return exact; });
    };

    PDFViewer.prototype.clearHighlights = function () {
        this.highlights = [];
        this.targetEvidence = null;
        this._setLocationMessage("");
        var overlay = this.container.querySelector(".cr-highlight-overlay");
        if (overlay) overlay.innerHTML = "";
    };

    PDFViewer.prototype._releaseDocument = function () {
        ++this.renderSerial;
        if (this.renderTask) {
            try { this.renderTask.cancel(); } catch (ignore) {}
            this.renderTask = null;
        }
        if (this.loadingTask && this.loadingTask.destroy) {
            try { this.loadingTask.destroy(); } catch (ignore2) {}
        }
        this.loadingTask = null;
        if (this.pdfDoc && this.pdfDoc.destroy) {
            try { this.pdfDoc.destroy(); } catch (ignore3) {}
        }
        this.pdfDoc = null;
        if (this.objectURL) {
            URL.revokeObjectURL(this.objectURL);
            this.objectURL = null;
        }
    };

    PDFViewer.prototype.release = function () {
        ++this.loadSerial;
        this._releaseDocument();
        this.documentType = "";
        this.fileName = "";
        this.totalPages = 0;
        this.currentPage = 1;
        this.highlights = [];
        this.pageInfo.textContent = "/ 0";
        this.pageInput.value = "1";
        this.viewerContainer.innerHTML = '<div class="cr-placeholder">请上传 PDF 文件</div>';
        this._setLocationMessage("");
    };

    // ---------------------------------------------------------------------
    // Review controller
    // ---------------------------------------------------------------------

    function ContractReview() {
        this.selectedFiles = { contract: null, cqp: null, ta: null };
        this.fileObjects = { contract: null, cqp: null, ta: null };
        this.reviewResult = null;
        this.currentFilter = "needs_check";
        this.selectedKey = "";
        this.status = "idle";
        this.categoryOpen = {
            customer_information: false,
            product_information: false,
            other_information: false
        };
        if (typeof pdfjsLib !== "undefined") {
            pdfjsLib.GlobalWorkerOptions.workerSrc = "vendor/pdfjs/build/pdf.worker.js";
        }
        this._init();
    }

    ContractReview.prototype._init = function () {
        var self = this;
        var container = document.getElementById("contractReviewContent");
        if (!container) return;
        container.innerHTML =
            '<div class="cr-page">' +
                '<div class="cr-toolbar">' +
                    '<div class="cr-toolbar__files">' +
                        this._fileSlotHtml("contract", "Contract", "未选择") +
                        this._fileSlotHtml("cqp", "CQP", "未选择") +
                        this._fileSlotHtml("ta", "TA", "未选择（可选）") +
                    '</div>' +
                    '<div class="cr-toolbar__actions">' +
                        '<span class="cr-status-text"><span class="cr-status-dot cr-status-dot--idle" data-role="status-dot"></span><span data-role="status-text">就绪</span></span>' +
                        '<button class="btn btn--primary" id="crBtnRun" type="button" disabled>运行测试</button>' +
                    '</div>' +
                '</div>' +
                '<div class="cr-workspace">' +
                    '<section class="cr-pdf-panel" id="crLeftPanel" aria-label="左侧PDF查看器"></section>' +
                    '<section class="cr-pdf-panel" id="crMidPanel" aria-label="中间PDF查看器"></section>' +
                    '<aside class="cr-results-panel" id="crResultsPanel">' +
                        '<div class="cr-results-header"><div class="cr-results-title">检查结果</div><div class="cr-summary-stats" data-role="summary-stats"></div></div>' +
                        '<div class="cr-filter-bar" data-role="filter-bar">' +
                            '<button class="cr-filter-btn active" data-filter="needs_check" type="button">需要检查</button>' +
                            '<button class="cr-filter-btn" data-filter="blocker" type="button">阻断</button>' +
                            '<button class="cr-filter-btn" data-filter="warning" type="button">警告</button>' +
                            '<button class="cr-filter-btn" data-filter="all" type="button">全部</button>' +
                            '<button class="cr-filter-btn" data-filter="pass" type="button">已通过</button>' +
                        '</div>' +
                        '<div class="cr-ai-notice" data-role="ai-notice" hidden></div>' +
                        '<div class="cr-results-list" data-role="results-list"><div class="cr-placeholder">请上传 Contract 和 CQP 后运行测试</div></div>' +
                    '</aside>' +
                '</div>' +
            '</div>';

        this.container = container;
        this.leftViewer = new PDFViewer(document.getElementById("crLeftPanel"), "left");
        this.midViewer = new PDFViewer(document.getElementById("crMidPanel"), "mid");

        ["contract", "cqp", "ta"].forEach(function (role) {
            var slot = container.querySelector('[data-slot="' + role + '"]');
            var input = container.querySelector('[data-input="' + role + '"]');
            slot.addEventListener("click", function (event) {
                if (event.target !== input) input.click();
            });
            input.addEventListener("click", function (event) { event.stopPropagation(); });
            input.addEventListener("change", function () {
                self._onFileChange(role, input.files && input.files[0] ? input.files[0] : null);
            });
        });

        document.getElementById("crBtnRun").addEventListener("click", function () { self._runReview(); });
        container.querySelector('[data-role="filter-bar"]').addEventListener("click", function (event) {
            var button = event.target.closest(".cr-filter-btn");
            if (!button) return;
            self.currentFilter = button.getAttribute("data-filter") || "needs_check";
            container.querySelectorAll(".cr-filter-btn").forEach(function (entry) { entry.classList.remove("active"); });
            button.classList.add("active");
            self._renderResults();
        });
        container.querySelector('[data-role="results-list"]').addEventListener("click", function (event) {
            var category = event.target.closest("[data-category-toggle]");
            if (category) {
                var categoryId = category.getAttribute("data-category-toggle");
                self.categoryOpen[categoryId] = !self.categoryOpen[categoryId];
                self._renderResults();
                return;
            }
            var evidenceButton = event.target.closest("[data-evidence-index]");
            if (evidenceButton) {
                self._navigateEvidenceChip(evidenceButton);
                return;
            }
            var reviewButton = event.target.closest("[data-review-key]");
            if (reviewButton) self._selectNode(reviewButton.getAttribute("data-review-key"));
        });
    };

    ContractReview.prototype._fileSlotHtml = function (role, label, emptyText) {
        return '<div class="cr-file-slot" data-slot="' + role + '" role="button" tabindex="0">' +
            '<span class="cr-file-slot__label">' + label + '</span>' +
            '<span class="cr-file-slot__name" data-name="' + role + '">' + emptyText + '</span>' +
            '<input class="cr-file-input-hidden" data-input="' + role + '" type="file" accept="application/pdf,.pdf">' +
        '</div>';
    };

    ContractReview.prototype._onFileChange = function (role, file) {
        this.selectedFiles[role] = file;
        this.fileObjects[role] = file;
        var slot = this.container.querySelector('[data-slot="' + role + '"]');
        var name = this.container.querySelector('[data-name="' + role + '"]');
        name.textContent = file ? file.name : (role === "ta" ? "未选择（可选）" : "未选择");
        slot.classList.toggle("has-file", Boolean(file));
        if (file) {
            if (role === "contract") this.leftViewer.loadPDF(file, "contract").catch(function () {});
            if (role === "cqp") this.midViewer.loadPDF(file, "cqp").catch(function () {});
            if (role === "ta" && !this.selectedFiles.cqp) this.midViewer.loadPDF(file, "ta").catch(function () {});
        }
        this._clearResults();
        this._updateRunButton();
    };

    ContractReview.prototype._updateRunButton = function () {
        var button = document.getElementById("crBtnRun");
        var ready = Boolean(this.selectedFiles.contract && this.selectedFiles.cqp && this.status !== "running");
        button.disabled = !ready;
        button.textContent = this.reviewResult ? "重新测试" : "运行测试";
    };

    ContractReview.prototype._setStatus = function (status, text) {
        this.status = status;
        var dot = this.container.querySelector('[data-role="status-dot"]');
        var label = this.container.querySelector('[data-role="status-text"]');
        dot.className = "cr-status-dot cr-status-dot--" + status;
        label.textContent = text || ({ idle: "就绪", running: "正在审查", done: "审查完成", error: "运行失败" }[status] || status);
        this._updateRunButton();
    };

    ContractReview.prototype._clearResults = function () {
        this.reviewResult = null;
        this.selectedKey = "";
        this.leftViewer.clearHighlights();
        this.midViewer.clearHighlights();
        var list = this.container.querySelector('[data-role="results-list"]');
        if (list) list.innerHTML = '<div class="cr-placeholder">文件已变化，请重新运行测试</div>';
        var stats = this.container.querySelector('[data-role="summary-stats"]');
        if (stats) stats.innerHTML = "";
        this._setStatus("idle");
    };

    ContractReview.prototype._runReview = function () {
        var self = this;
        if (!this.selectedFiles.contract || !this.selectedFiles.cqp) return;
        this._setStatus("running");
        this.selectedKey = "";
        this.leftViewer.clearHighlights();
        this.midViewer.clearHighlights();
        this.container.querySelector('[data-role="results-list"]').innerHTML = '<div class="cr-placeholder"><span class="cr-spinner"></span>正在读取并校对文件...</div>';

        var formData = new FormData();
        formData.append("contract", this.selectedFiles.contract);
        formData.append("cqp", this.selectedFiles.cqp);
        if (this.selectedFiles.ta) formData.append("ta", this.selectedFiles.ta);

        fetch("/api/contract-review", { method: "POST", body: formData })
            .then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if (!response.ok) throw new Error(payload.error || "请求失败");
                    return payload;
                });
            })
            .then(function (result) {
                self.reviewResult = result;
                var taSource = result.document_sources && result.document_sources.ta;
                self.fileObjects.ta = taSource && taSource.physical_role === "contract" ? self.selectedFiles.contract : self.selectedFiles.ta;
                self._initializeCategoryState();
                self._setStatus("done");
                self._renderResults();
            })
            .catch(function (error) {
                self._setStatus("error");
                self.container.querySelector('[data-role="results-list"]').innerHTML = '<div class="cr-placeholder cr-placeholder--error">审查失败：' + escapeHtml(error.message) + '</div>';
                self._showToast(error.message || "审查失败", "danger");
            });
    };

    ContractReview.prototype._initializeCategoryState = function () {
        var self = this;
        CATEGORY_ORDER.forEach(function (category) {
            var items = (self.reviewResult.review_items || []).filter(function (item) { return item.category === category; });
            self.categoryOpen[category] = items.some(function (item) { return statusKind(item) !== "pass" && statusKind(item) !== "info"; });
        });
    };

    ContractReview.prototype._passesFilter = function (node) {
        var kind = statusKind(node);
        if (this.currentFilter === "all") return true;
        if (this.currentFilter === "blocker") return kind === "blocker";
        if (this.currentFilter === "warning") return kind === "warning";
        if (this.currentFilter === "pass") return kind === "pass";
        return kind === "blocker" || kind === "warning";
    };

    ContractReview.prototype._renderResults = function () {
        if (!this.reviewResult) return;
        var items = this.reviewResult.review_items || [];
        this._renderSummary(items);
        this._renderAiNotice();
        var categories = {};
        CATEGORY_ORDER.forEach(function (id) { categories[id] = []; });
        items.forEach(function (item) {
            if (!categories[item.category]) categories[item.category] = [];
            categories[item.category].push(item);
        });

        var html = "";
        var anyVisible = false;
        for (var c = 0; c < CATEGORY_ORDER.length; c++) {
            var categoryId = CATEGORY_ORDER[c];
            var visible = categories[categoryId].filter(this._passesFilter.bind(this));
            if (!visible.length) continue;
            anyVisible = true;
            var issueCount = visible.filter(function (item) { return statusKind(item) !== "pass"; }).length;
            var open = Boolean(this.categoryOpen[categoryId]);
            html += '<section class="cr-category' + (open ? " is-open" : "") + '">';
            html += '<button class="cr-category__header" data-category-toggle="' + categoryId + '" type="button" aria-expanded="' + open + '">';
            html += '<span class="cr-category__chevron">›</span><span class="cr-category__title">' + escapeHtml(CATEGORY_FALLBACK[categoryId]) + '</span>';
            html += '<span class="cr-category__count">' + visible.length + ' 项' + (issueCount ? ' · ' + issueCount + ' 待处理' : '') + '</span></button>';
            if (open) {
                html += '<div class="cr-category__body">';
                for (var i = 0; i < visible.length; i++) html += this._renderReviewNode(visible[i], visible[i].id, false);
                html += '</div>';
            }
            html += '</section>';
        }
        this.container.querySelector('[data-role="results-list"]').innerHTML = anyVisible ? html : '<div class="cr-placeholder">当前筛选条件下没有检查项</div>';
    };

    ContractReview.prototype._renderReviewNode = function (node, key, nested) {
        var selected = this.selectedKey === key;
        var kind = statusKind(node);
        var html = '<article class="cr-review-node ' + (nested ? 'cr-review-node--nested ' : '') + (selected ? 'is-selected' : '') + '">';
        html += '<button class="cr-review-node__button" data-review-key="' + escapeHtml(key) + '" type="button">';
        html += '<span class="cr-review-node__main"><span class="cr-review-node__title">' + escapeHtml(node.title || node.code || "检查项") + '</span>';
        html += '<span class="cr-review-node__summary">' + escapeHtml(node.summary || "") + '</span></span>';
        html += '<span class="cr-status-badge cr-status-badge--' + kind + '">' + escapeHtml(statusLabel(node)) + '</span></button>';

        if (node.values && Object.keys(node.values).length) {
            html += '<dl class="cr-review-values">';
            Object.keys(node.values).forEach(function (label) {
                html += '<div><dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(String(node.values[label])) + '</dd></div>';
            });
            html += '</dl>';
        }
        var evidence = uniqueEvidence(node.evidence || []);
        if (evidence.length) {
            html += '<div class="cr-evidence-list">';
            evidence.forEach(function (entry, index) {
                var loc = Number(entry.page) > 0 ? (entry.document_type.toUpperCase() + ' 第' + entry.page + '页') : '无法确定位置';
                var exactClass = entry.location_status === "exact" && entry.rects && entry.rects.length ? "" : " cr-evidence-chip--uncertain";
                html += '<button class="cr-evidence-chip' + exactClass + '" data-review-key="' + escapeHtml(key) + '" data-evidence-index="' + index + '" type="button">' + escapeHtml(loc) + '</button>';
            });
            html += '</div>';
        }
        if (node.sub_items && node.sub_items.length) {
            html += '<div class="cr-subitems">';
            for (var i = 0; i < node.sub_items.length; i++) {
                html += this._renderReviewNode(node.sub_items[i], key + "::" + node.sub_items[i].id, true);
            }
            html += '</div>';
        }
        html += '</article>';
        return html;
    };

    ContractReview.prototype._renderSummary = function (items) {
        var counts = { blocker: 0, warning: 0, pass: 0 };
        items.forEach(function (item) {
            var kind = statusKind(item);
            if (counts.hasOwnProperty(kind)) counts[kind] += 1;
        });
        this.container.querySelector('[data-role="summary-stats"]').innerHTML =
            '<span class="cr-stat-item"><i class="cr-stat-dot cr-stat-dot--blocker"></i>阻断 ' + counts.blocker + '</span>' +
            '<span class="cr-stat-item"><i class="cr-stat-dot cr-stat-dot--warning"></i>警告 ' + counts.warning + '</span>' +
            '<span class="cr-stat-item"><i class="cr-stat-dot cr-stat-dot--pass"></i>通过 ' + counts.pass + '</span>';
    };

    ContractReview.prototype._renderAiNotice = function () {
        var notice = this.container.querySelector('[data-role="ai-notice"]');
        var llm = this.reviewResult.llm_review || {};
        if (llm.error) {
            notice.hidden = false;
            notice.className = "cr-ai-notice cr-ai-notice--error";
            notice.textContent = "AI 审核不可用；规则检查和 PDF 定位仍可使用。";
        } else if (llm.summary) {
            notice.hidden = false;
            notice.className = "cr-ai-notice";
            notice.textContent = "AI：" + llm.summary;
        } else {
            notice.hidden = true;
        }
    };

    ContractReview.prototype._findNode = function (key) {
        var parts = String(key || "").split("::");
        var items = this.reviewResult ? this.reviewResult.review_items || [] : [];
        var item = items.find(function (entry) { return entry.id === parts[0]; });
        if (!item) return null;
        if (parts.length === 1) return { node: item, parent: item, key: key };
        var sub = (item.sub_items || []).find(function (entry) { return entry.id === parts[1]; });
        return sub ? { node: sub, parent: item, key: key } : null;
    };

    ContractReview.prototype._chooseViewerRoles = function (evidence) {
        var docs = {};
        evidence.forEach(function (entry) { if (entry.document_type) docs[entry.document_type] = true; });
        if (docs.contract && docs.cqp) return { left: "contract", mid: "cqp" };
        if (docs.contract && docs.ta) return { left: "contract", mid: "ta" };
        if (docs.cqp && docs.ta) return { left: "cqp", mid: "ta" };
        if (docs.ta) return { left: this.leftViewer.documentType || "contract", mid: "ta" };
        if (docs.cqp) return { left: this.leftViewer.documentType || "contract", mid: "cqp" };
        return { left: "contract", mid: this.midViewer.documentType || "cqp" };
    };

    ContractReview.prototype._switchViewerDoc = function (panel, role) {
        var viewer = panel === "left" ? this.leftViewer : this.midViewer;
        var file = this.fileObjects[role];
        if (!file) {
            viewer._setLocationMessage("无法确定位置");
            return Promise.resolve(false);
        }
        if (viewer.documentType === role && viewer.pdfDoc) return Promise.resolve(true);
        return viewer.loadPDF(file, role).then(function () { return true; }).catch(function () { return false; });
    };

    ContractReview.prototype._selectNode = function (key) {
        var self = this;
        var found = this._findNode(key);
        if (!found) return;
        this.selectedKey = key;
        this.categoryOpen[found.parent.category] = true;
        this._renderResults();
        var evidence = uniqueEvidence(found.node.evidence && found.node.evidence.length ? found.node.evidence : found.parent.evidence || []);
        var roles = this._chooseViewerRoles(evidence);
        Promise.all([
            this._switchViewerDoc("left", roles.left),
            this._switchViewerDoc("mid", roles.mid)
        ]).then(function () {
            var leftEvidence = evidence.filter(function (entry) { return entry.document_type === roles.left; });
            var midEvidence = evidence.filter(function (entry) { return entry.document_type === roles.mid; });
            return Promise.all([
                self.leftViewer.navigateToEvidence(leftEvidence, statusKind(found.node) === "blocker" ? "cr-highlight-rect--left cr-highlight-rect--error" : "cr-highlight-rect--left"),
                self.midViewer.navigateToEvidence(midEvidence, statusKind(found.node) === "blocker" ? "cr-highlight-rect--mid cr-highlight-rect--error" : "cr-highlight-rect--mid")
            ]);
        }).then(function (exactResults) {
            if (!evidence.length || exactResults.some(function (value) { return value === false; })) self._showToast("无法确定位置", "warning");
        });
    };

    ContractReview.prototype._navigateEvidenceChip = function (button) {
        var key = button.getAttribute("data-review-key");
        var index = Number(button.getAttribute("data-evidence-index"));
        var found = this._findNode(key);
        if (!found) return;
        var evidence = uniqueEvidence(found.node.evidence && found.node.evidence.length ? found.node.evidence : found.parent.evidence || []);
        var entry = evidence[index];
        if (!entry) return;
        var panel = entry.document_type === "contract" ? "left" : "mid";
        var viewer = panel === "left" ? this.leftViewer : this.midViewer;
        var self = this;
        this._switchViewerDoc(panel, entry.document_type).then(function () {
            return viewer.navigateToEvidence([entry], panel === "left" ? "cr-highlight-rect--left" : "cr-highlight-rect--mid");
        }).then(function (exact) {
            if (!exact) self._showToast("无法确定位置", "warning");
        });
    };

    ContractReview.prototype._showToast = function (message, tone) {
        var container = document.getElementById("toastContainer");
        if (!container) return;
        var toast = document.createElement("div");
        toast.className = "toast toast--" + (tone || "success");
        toast.textContent = message;
        container.appendChild(toast);
        window.setTimeout(function () { toast.classList.add("toast--leaving"); }, 2200);
        window.setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 2700);
    };

    window.ContractReview = ContractReview;
})();
