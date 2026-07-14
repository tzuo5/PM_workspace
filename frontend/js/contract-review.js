/**
 * Contract Review - Three-column review workspace with PDF.js
 */

(function() {
    "use strict";

    // ========================================================================
    // PDFViewer - single PDF viewer panel
    // ========================================================================
    function PDFViewer(containerEl, panelId) {
        this.container = containerEl;
        this.panelId = panelId;  // "left" or "mid"
        this.pdfDoc = null;
        this.currentPage = 1;
        this.totalPages = 0;
        this.scale = 1.2;
        this.defaultScale = 1.2;
        this.renderTask = null;
        this.objectURL = null;
        this.fileName = "";
        this.documentType = ""; // "contract", "cqp", "ta"
        this.highlights = [];  // list of {rects, class} for current page
        this.pageRendering = false;
        this.pendingPageNum = null;
        this._init();
    }

    PDFViewer.prototype._init = function() {
        var self = this;
        var c = this.container;

        c.innerHTML = '<div class="cr-pdf-toolbar">' +
            '<span class="cr-pdf-doc-label" data-role="doc-label">' + (this.panelId === 'left' ? '左侧查看器' : '中间查看器') + '</span>' +
            '<span class="cr-pdf-filename" data-role="filename"></span>' +
            '<button class="cr-pdf-btn" data-action="prev" title="上一页"><</button>' +
            '<input class="cr-pdf-page-input" data-action="page-input" type="text" value="1" size="3">' +
            '<span class="cr-pdf-page-info" data-role="page-info">/ 0</span>' +
            '<button class="cr-pdf-btn" data-action="next" title="下一页">></button>' +
            '<button class="cr-pdf-btn" data-action="zoomin" title="放大">+</button>' +
            '<button class="cr-pdf-btn" data-action="zoomout" title="缩小">-</button>' +
            '<button class="cr-pdf-btn" data-action="fitwidth" title="适合宽度">[]</button>' +
            '<button class="cr-pdf-btn" data-action="resetzoom" title="默认缩放">1:1</button>' +
        '</div>' +
        '<div class="cr-pdf-viewer-container" data-role="viewer-container">' +
            '<div class="cr-placeholder">请上传 PDF 文件</div>' +
        '</div>';

        this.docLabel = c.querySelector('[data-role="doc-label"]');
        this.filenameEl = c.querySelector('[data-role="filename"]');
        this.pageInput = c.querySelector('[data-action="page-input"]');
        this.pageInfo = c.querySelector('[data-role="page-info"]');
        this.viewerContainer = c.querySelector('[data-role="viewer-container"]');
        this.prevBtn = c.querySelector('[data-action="prev"]');
        this.nextBtn = c.querySelector('[data-action="next"]');

        // Event binding
        c.querySelector('[data-action="prev"]').addEventListener('click', function() { self.prevPage(); });
        c.querySelector('[data-action="next"]').addEventListener('click', function() { self.nextPage(); });
        c.querySelector('[data-action="zoomin"]').addEventListener('click', function() { self.zoomIn(); });
        c.querySelector('[data-action="zoomout"]').addEventListener('click', function() { self.zoomOut(); });
        c.querySelector('[data-action="fitwidth"]').addEventListener('click', function() { self.fitWidth(); });
        c.querySelector('[data-action="resetzoom"]').addEventListener('click', function() { self.resetZoom(); });

        this.pageInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                var num = parseInt(self.pageInput.value, 10);
                if (num >= 1 && num <= self.totalPages) {
                    self.goToPage(num);
                } else {
                    self.pageInput.value = String(self.currentPage);
                }
            }
        });

        // Track scroll to maintain center when zooming
        this.viewerContainer.addEventListener('scroll', function() {
            self._lastScrollTop = self.viewerContainer.scrollTop;
        });
    };

    PDFViewer.prototype.loadPDF = function(file, fileRole) {
        var self = this;
        // Clean up previous
        this.release();

        this.fileName = file.name || "";
        this.documentType = fileRole || "";
        if (this.docLabel) {
            this.docLabel.textContent = (fileRole || "File").toUpperCase();
        }
        if (this.filenameEl) {
            this.filenameEl.textContent = this.fileName;
        }

        this.objectURL = URL.createObjectURL(file);

        var loadingTask = pdfjsLib.getDocument({ url: this.objectURL });
        loadingTask.promise.then(function(pdfDoc) {
            self.pdfDoc = pdfDoc;
            self.totalPages = pdfDoc.numPages;
            self.pageInfo.textContent = "/ " + self.totalPages;
            self.currentPage = 1;
            self.pageInput.value = "1";
            self.renderPage(1);
        }).catch(function(err) {
            console.error("PDF load error:", err);
            self.viewerContainer.innerHTML = '<div class="cr-placeholder">PDF 加载失败: ' + self._escape(err.message) + '</div>';
        });
    };

    PDFViewer.prototype._escape = function(str) {
        return String(str || "").replace(/&/g, "&").replace(/</g, "<").replace(/>/g, ">");
    };

    PDFViewer.prototype.renderPage = function(pageNum) {
        var self = this;
        if (!this.pdfDoc) return;

        // Cancel any pending render
        if (this.renderTask) {
            this.renderTask.cancel();
            this.renderTask = null;
        }

        this.currentPage = pageNum;
        this.pageInput.value = String(pageNum);
        this.pageRendering = true;

        this.pdfDoc.getPage(pageNum).then(function(page) {
            var viewport = page.getViewport({ scale: self.scale });

            // Create canvas
            var wrapper = document.createElement("div");
            wrapper.className = "cr-pdf-page-wrapper";
            wrapper.style.width = viewport.width + "px";
            wrapper.style.height = viewport.height + "px";

            var canvas = document.createElement("canvas");
            canvas.className = "cr-pdf-canvas";
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            wrapper.appendChild(canvas);

            // Text layer (simplified)
            var textLayerDiv = document.createElement("div");
            textLayerDiv.className = "cr-pdf-text-layer";
            textLayerDiv.style.width = viewport.width + "px";
            textLayerDiv.style.height = viewport.height + "px";
            wrapper.appendChild(textLayerDiv);

            // Highlight overlay
            var highlightOverlay = document.createElement("div");
            highlightOverlay.className = "cr-highlight-overlay";
            highlightOverlay.setAttribute("data-role", "highlight-overlay");
            highlightOverlay.style.width = viewport.width + "px";
            highlightOverlay.style.height = viewport.height + "px";
            wrapper.appendChild(highlightOverlay);

            self.viewerContainer.innerHTML = "";
            self.viewerContainer.appendChild(wrapper);

            // Render canvas
            var ctx = canvas.getContext("2d");
            var renderContext = { canvasContext: ctx, viewport: viewport };

            self.renderTask = page.render(renderContext);
            self.renderTask.promise.then(function() {
                self.renderTask = null;
                self.pageRendering = false;

                // Render text layer
                page.getTextContent().then(function(textContent) {
                    pdfjsLib.renderTextLayer({
                        textContent: textContent,
                        container: textLayerDiv,
                        viewport: viewport,
                        textDivs: [],
                    });
                    // Draw highlights after text layer is ready
                    self._drawHighlights(viewport);
                });

                // Process pending page request
                if (self.pendingPageNum && self.pendingPageNum !== pageNum) {
                    var p = self.pendingPageNum;
                    self.pendingPageNum = null;
                    self.renderPage(p);
                }

                // Scroll to center if evidence navigation happened
                if (self._targetScrollCenter) {
                    var container = self.viewerContainer;
                    var target = self._targetScrollCenter;
                    container.scrollTop = Math.max(0, target - container.clientHeight / 3);
                    self._targetScrollCenter = null;
                }
            }).catch(function(err) {
                if (err && err.name === "RenderingCancelledException") {
                    // Expected when switching pages quickly
                    return;
                }
                self.renderTask = null;
                self.pageRendering = false;
            });
        }).catch(function(err) {
            console.error("Page render error:", err);
            self.pageRendering = false;
        });
    };

    PDFViewer.prototype.goToPage = function(pageNum) {
        if (!this.pdfDoc) return;
        if (pageNum < 1) pageNum = 1;
        if (pageNum > this.totalPages) pageNum = this.totalPages;
        if (this.pageRendering) {
            this.pendingPageNum = pageNum;
            return;
        }
        this.renderPage(pageNum);
    };

    PDFViewer.prototype.prevPage = function() {
        this.goToPage(this.currentPage - 1);
    };

    PDFViewer.prototype.nextPage = function() {
        this.goToPage(this.currentPage + 1);
    };

    PDFViewer.prototype.zoomIn = function() {
        this.scale = Math.min(3.0, this.scale + 0.2);
        this.renderPage(this.currentPage);
    };

    PDFViewer.prototype.zoomOut = function() {
        this.scale = Math.max(0.4, this.scale - 0.2);
        this.renderPage(this.currentPage);
    };

    PDFViewer.prototype.fitWidth = function() {
        var containerWidth = this.viewerContainer.clientWidth - 24;
        if (!this.pdfDoc || containerWidth <= 0) return;
        var self = this;
        this.pdfDoc.getPage(this.currentPage).then(function(page) {
            var vp = page.getViewport({ scale: 1 });
            self.scale = Math.max(0.4, containerWidth / vp.width);
            self.renderPage(self.currentPage);
        }).catch(function() {});
    };

    PDFViewer.prototype.resetZoom = function() {
        this.scale = this.defaultScale;
        this.renderPage(this.currentPage);
    };

    // ---- Highlights ----

    PDFViewer.prototype.clearHighlights = function() {
        this.highlights = [];
        var overlay = this.container.querySelector('[data-role="highlight-overlay"]');
        if (overlay) overlay.innerHTML = "";
    };

    PDFViewer.prototype.setHighlights = function(highlightRects, highlightClass) {
        this.highlights = (highlightRects || []).map(function(r) {
            return { rects: r, cls: highlightClass || "cr-highlight-rect--left" };
        });
        this._drawHighlights(null);
    };

    PDFViewer.prototype._drawHighlights = function(viewport) {
        var overlay = this.container.querySelector('[data-role="highlight-overlay"]');
        if (!overlay) return;
        overlay.innerHTML = "";

        if (!this.highlights || this.highlights.length === 0) return;

        var self = this;
        var hasPulse = false;

        this.highlights.forEach(function(hl) {
            if (!hl.rects) return;
            hl.rects.forEach(function(rect) {
                if (!rect || rect.length < 4) return;
                var el = document.createElement("div");
                el.className = "cr-highlight-rect " + (hl.cls || "cr-highlight-rect--left");
                // rect is [x0, y0, x1, y1] in PDF points
                // Need to convert to current viewport scale
                var scale = self.scale;
                el.style.left = (rect[0] * scale) + "px";
                el.style.top = (rect[1] * scale) + "px";
                el.style.width = ((rect[2] - rect[0]) * scale) + "px";
                el.style.height = ((rect[3] - rect[1]) * scale) + "px";
                overlay.appendChild(el);
                hasPulse = true;
            });
        });

        // Pulse animation for newly set highlights
        if (hasPulse && this._doPulse !== false) {
            var rects = overlay.querySelectorAll('.cr-highlight-rect');
            rects.forEach(function(r) { r.classList.add("pulse"); });
            this._doPulse = false;
        }
    };

    PDFViewer.prototype.navigateToEvidence = function(evidenceList, highlightClass) {
        // evidenceList: [{ document_type, page, rects }]
        var found = false;
        for (var i = 0; i < evidenceList.length; i++) {
            var ev = evidenceList[i];
            if (ev.document_type === this.documentType && ev.page > 0) {
                this.clearHighlights();
                if (ev.rects && ev.rects.length > 0) {
                    this.setHighlights(ev.rects, highlightClass || "cr-highlight-rect--left");
                }
                this._doPulse = true;
                this.goToPage(ev.page);
                found = true;
                break;
            }
        }
        if (!found) {
            this.clearHighlights();
        }
    };

    PDFViewer.prototype.release = function() {
        if (this.renderTask) {
            this.renderTask.cancel();
            this.renderTask = null;
        }
        if (this.objectURL) {
            URL.revokeObjectURL(this.objectURL);
            this.objectURL = null;
        }
        this.pdfDoc = null;
        this.totalPages = 0;
        this.currentPage = 1;
        this.highlights = [];
        this.viewerContainer.innerHTML = '<div class="cr-placeholder">请上传 PDF 文件</div>';
        this.pageInfo.textContent = "/ 0";
        this.pageInput.value = "1";
    };

    // ========================================================================
    // ContractReview - Main controller
    // ========================================================================

    function ContractReview() {
        this.selectedFiles = { contract: null, cqp: null, ta: null };
        this.fileObjects = { contract: null, cqp: null, ta: null };
        this.reviewResult = null;
        this.currentFilter = "needs_check"; // "needs_check", "blocker", "warning", "all", "pass"
        this.selectedItemId = null;
        this.status = "idle"; // "idle", "running", "done", "error"

        this.leftViewer = null;
        this.midViewer = null;

        // PDF.js worker path
        if (typeof pdfjsLib !== "undefined") {
            pdfjsLib.GlobalWorkerOptions.workerSrc = "vendor/pdfjs/build/pdf.worker.js";
        }

        this._init();
    }

    ContractReview.prototype._init = function() {
        var self = this;

        // Build UI inside contractReviewContent
        var container = document.getElementById("contractReviewContent");
        if (!container) return;
        container.innerHTML = "";

        var html = '<div class="cr-page">' +
            // Toolbar
            '<div class="cr-toolbar">' +
                '<div class="cr-toolbar__files">' +
                    '<div class="cr-file-slot" data-slot="contract">' +
                        '<span class="cr-file-slot__label">Contract</span>' +
                        '<span class="cr-file-slot__name" data-name="contract">未选择</span>' +
                        '<input type="file" accept=".pdf" class="cr-file-input-hidden" data-input="contract">' +
                    '</div>' +
                    '<div class="cr-file-slot" data-slot="cqp">' +
                        '<span class="cr-file-slot__label">CQP</span>' +
                        '<span class="cr-file-slot__name" data-name="cqp">未选择</span>' +
                        '<input type="file" accept=".pdf" class="cr-file-input-hidden" data-input="cqp">' +
                    '</div>' +
                    '<div class="cr-file-slot" data-slot="ta">' +
                        '<span class="cr-file-slot__label">TA</span>' +
                        '<span class="cr-file-slot__name" data-name="ta">未选择(可选)</span>' +
                        '<input type="file" accept=".pdf" class="cr-file-input-hidden" data-input="ta">' +
                    '</div>' +
                '</div>' +
                '<div class="cr-toolbar__actions">' +
                    '<span class="cr-status-text"><span class="cr-status-dot cr-status-dot--idle" data-role="status-dot"></span><span data-role="status-text">就绪</span></span>' +
                    '<button class="btn btn--primary" id="crBtnRun" disabled>运行测试</button>' +
                '</div>' +
            '</div>' +
            // Workspace
            '<div class="cr-workspace">' +
                '<div class="cr-pdf-panel" id="crLeftPanel"></div>' +
                '<div class="cr-pdf-panel" id="crMidPanel"></div>' +
                '<div class="cr-results-panel" id="crResultsPanel">' +
                    '<div class="cr-results-header">' +
                        '<div class="cr-results-title">检查结果</div>' +
                        '<div class="cr-summary-stats" data-role="summary-stats"></div>' +
                    '</div>' +
                    '<div class="cr-filter-bar" data-role="filter-bar">' +
                        '<button class="cr-filter-btn active" data-filter="needs_check">需要检查</button>' +
                        '<button class="cr-filter-btn" data-filter="blocker">Blocker</button>' +
                        '<button class="cr-filter-btn" data-filter="warning">Warning</button>' +
                        '<button class="cr-filter-btn" data-filter="all">全部</button>' +
                        '<button class="cr-filter-btn" data-filter="pass">已通过</button>' +
                    '</div>' +
                    '<div class="cr-results-list" data-role="results-list">' +
                        '<div class="cr-placeholder">请上传 Contract 和 CQP 文件后运行测试</div>' +
                    '</div>' +
                    '<div class="cr-ai-notice" data-role="ai-notice" hidden></div>' +
                '</div>' +
            '</div>' +
        '</div>';

        container.innerHTML = html;

        // Create PDF viewers
        this.leftViewer = new PDFViewer(document.getElementById("crLeftPanel"), "left");
        this.midViewer = new PDFViewer(document.getElementById("crMidPanel"), "mid");

        // Bind file slots
        document.querySelectorAll(".cr-file-slot").forEach(function(slot) {
            slot.addEventListener("click", function() {
                var input = slot.querySelector(".cr-file-input-hidden");
                if (input) input.click();
            });
        });

        // Bind file inputs
        var fileInputs = ["contract", "cqp", "ta"];
        fileInputs.forEach(function(role) {
            var input = container.querySelector('[data-input="' + role + '"]');
            var nameEl = container.querySelector('[data-name="' + role + '"]');
            var slot = container.querySelector('[data-slot="' + role + '"]');
            if (!input || !nameEl) return;

            input.addEventListener("change", function() {
                var file = input.files[0];
                if (file) {
                    self.selectedFiles[role] = file;
                    nameEl.textContent = file.name;
                    if (slot) slot.classList.add("has-file");
                    // Show PDF immediately
                    self._displayFile(role, file);
                    // Clear old results
                    self._clearResults();
                } else {
                    self.selectedFiles[role] = null;
                    nameEl.textContent = role === "ta" ? "未选择(可选)" : "未选择";
                    if (slot) slot.classList.remove("has-file");
                }
                self._updateRunButton();
            });
        });

        // Run button
        var runBtn = document.getElementById("crBtnRun");
        if (runBtn) {
            runBtn.addEventListener("click", function() {
                self._runReview();
            });
        }

        // Filter buttons
        var filterBar = container.querySelector('[data-role="filter-bar"]');
        if (filterBar) {
            filterBar.addEventListener("click", function(e) {
                var btn = e.target.closest(".cr-filter-btn");
                if (!btn) return;
                filterBar.querySelectorAll(".cr-filter-btn").forEach(function(b) { b.classList.remove("active"); });
                btn.classList.add("active");
                self.currentFilter = btn.getAttribute("data-filter") || "needs_check";
                self._renderResults();
            });
        }

        // Results list delegation
        var resultsList = container.querySelector('[data-role="results-list"]');
        if (resultsList) {
            resultsList.addEventListener("click", function(e) {
                var item = e.target.closest(".cr-result-item");
                if (!item) return;
                var itemId = item.getAttribute("data-item-id");
                if (itemId) {
                    self._selectItem(itemId);
                }
                // Source button handling
                var srcBtn = e.target.closest(".cr-source-btn");
                if (srcBtn) {
                    e.stopPropagation();
                    var docType = srcBtn.getAttribute("data-doc-type");
                    var viewer = srcBtn.getAttribute("data-viewer");
                    if (docType && viewer) {
                        self._switchViewerDoc(viewer, docType);
                    }
                }
            });
        }
    };

    ContractReview.prototype._displayFile = function(role, file) {
        // Default display: left = contract, mid = cqp
        if (role === "contract") {
            this.leftViewer.loadPDF(file, "contract");
            this.leftViewer.documentType = "contract";
        } else if (role === "cqp") {
            this.midViewer.loadPDF(file, "cqp");
            this.midViewer.documentType = "cqp";
        } else if (role === "ta") {
            // TA goes to mid viewer if no CQP, otherwise could be shown on demand
            if (!this.selectedFiles.cqp) {
                this.midViewer.loadPDF(file, "ta");
                this.midViewer.documentType = "ta";
            }
            // We keep the file ready for switching
        }
        this.fileObjects[role] = file;
    };

    ContractReview.prototype._switchViewerDoc = function(viewer, docType) {
        var targetViewer = viewer === "left" ? this.leftViewer : this.midViewer;
        var file = this.fileObjects[docType];
        if (file && targetViewer) {
            targetViewer.loadPDF(file, docType);
            targetViewer.documentType = docType;
        }
    };

    ContractReview.prototype._updateRunButton = function() {
        var btn = document.getElementById("crBtnRun");
        if (!btn) return;
        var hasBoth = !!(this.selectedFiles.contract && this.selectedFiles.cqp);
        btn.disabled = !hasBoth;
        if (this.status === "running") {
            btn.textContent = "测试中...";
            btn.disabled = true;
        } else if (this.status === "done") {
            btn.textContent = "重新测试";
        } else {
            btn.textContent = "运行测试";
        }
    };

    ContractReview.prototype._setStatus = function(status) {
        this.status = status;
        var dot = document.querySelector('[data-role="status-dot"]');
        var textEl = document.querySelector('[data-role="status-text"]');
        if (dot) {
            dot.className = "cr-status-dot";
            if (status === "idle") dot.classList.add("cr-status-dot--idle");
            else if (status === "running") dot.classList.add("cr-status-dot--running");
            else if (status === "done") dot.classList.add("cr-status-dot--done");
            else if (status === "error") dot.classList.add("cr-status-dot--error");
        }
        if (textEl) {
            if (status === "idle") textEl.textContent = "就绪";
            else if (status === "running") textEl.textContent = "解析中...";
            else if (status === "done") textEl.textContent = "完成";
            else if (status === "error") textEl.textContent = "错误";
        }
        this._updateRunButton();
    };

    ContractReview.prototype._clearResults = function() {
        this.reviewResult = null;
        this.selectedItemId = null;
        this.leftViewer.clearHighlights();
        this.midViewer.clearHighlights();
        var list = document.querySelector('[data-role="results-list"]');
        if (list) list.innerHTML = '<div class="cr-placeholder">请上传 Contract 和 CQP 文件后运行测试</div>';
        var stats = document.querySelector('[data-role="summary-stats"]');
        if (stats) stats.innerHTML = "";
        var aiNotice = document.querySelector('[data-role="ai-notice"]');
        if (aiNotice) aiNotice.hidden = true;
        this.currentFilter = "needs_check";
        var filterBar = document.querySelector('[data-role="filter-bar"]');
        if (filterBar) {
            filterBar.querySelectorAll(".cr-filter-btn").forEach(function(b) {
                b.classList.toggle("active", b.getAttribute("data-filter") === "needs_check");
            });
        }
        this._setStatus("idle");
    };

    ContractReview.prototype._runReview = function() {
        var self = this;
        if (this.status === "running") return;

        this._setStatus("running");
        this.reviewResult = null;
        this.selectedItemId = null;

        var list = document.querySelector('[data-role="results-list"]');
        if (list) list.innerHTML = '<div class="cr-loading"><div class="cr-spinner"></div><div>正在解析 PDF 并执行交叉验证...</div></div>';

        // Build FormData with named fields
        var formData = new FormData();
        if (this.selectedFiles.contract) formData.append("contract", this.selectedFiles.contract);
        if (this.selectedFiles.cqp) formData.append("cqp", this.selectedFiles.cqp);
        if (this.selectedFiles.ta) formData.append("ta", this.selectedFiles.ta);

        fetch("/api/contract-review", {
            method: "POST",
            body: formData,
        })
        .then(function(resp) {
            if (!resp.ok) {
                return resp.json().then(function(err) {
                    throw new Error(err.error || "请求失败");
                });
            }
            return resp.json();
        })
        .then(function(report) {
            self.reviewResult = report;
            self._setStatus("done");
            self._renderResults();
        })
        .catch(function(err) {
            self._setStatus("error");
            if (list) {
                list.innerHTML = '<div class="cr-placeholder" style="color:var(--abb-red)">审查失败: ' + self._escape(err.message || "未知错误") + '</div>';
            }
            self._showToast(err.message || "审查请求失败", "danger");
        });
    };

    ContractReview.prototype._escape = function(str) {
        return String(str || "").replace(/&/g, "&").replace(/</g, "<").replace(/>/g, ">");
    };

    ContractReview.prototype._renderResults = function() {
        var self = this;
        if (!this.reviewResult) return;

        var items = this.reviewResult.review_items || [];
        var llm = this.reviewResult.llm_review || {};

        // Update summary stats
        var stats = document.querySelector('[data-role="summary-stats"]');
        var blockers = 0, warnings = 0, passes = 0, aiAvailable = !llm.error;
        items.forEach(function(item) {
            if (item.severity === "blocker" || item.status === "BLOCKER" || item.status === "MISMATCH") blockers++;
            else if (item.severity === "warning" || item.status === "WARNING" || item.status === "UNDETERMINED") warnings++;
            else if (item.status === "PASS") passes++;
        });

        if (stats) {
            stats.innerHTML =
                '<span class="cr-stat-item"><span class="cr-stat-dot cr-stat-dot--blocker"></span>Blocker: ' + blockers + '</span>' +
                '<span class="cr-stat-item"><span class="cr-stat-dot cr-stat-dot--warning"></span>Warning: ' + warnings + '</span>' +
                '<span class="cr-stat-item"><span class="cr-stat-dot cr-stat-dot--pass"></span>已通过: ' + passes + '</span>' +
                '<span class="cr-stat-item"><span class="cr-stat-dot cr-stat-dot--ai"></span>AI: ' + (aiAvailable ? '可用' : '不可用') + '</span>';
        }

        // AI notice
        var aiNotice = document.querySelector('[data-role="ai-notice"]');
        if (aiNotice) {
            if (llm.error) {
                aiNotice.hidden = false;
                aiNotice.className = "cr-ai-notice cr-ai-notice--error";
                aiNotice.textContent = "AI 审核不可用: " + (llm.error || "未知错误");
            } else if (llm.summary) {
                aiNotice.hidden = false;
                aiNotice.className = "cr-ai-notice";
                aiNotice.textContent = "AI: " + llm.summary;
            } else {
                aiNotice.hidden = true;
            }
        }

        // Filter items
        var filtered = this._filterItems(items);
        var list = document.querySelector('[data-role="results-list"]');
        if (!list) return;

        if (filtered.length === 0) {
            list.innerHTML = '<div class="cr-placeholder">无匹配的检查结果</div>';
            return;
        }

        var html = "";
        filtered.forEach(function(item) {
            var isSelected = item.id === self.selectedItemId;
            var sevLabel = item.severity === "blocker" ? "BLOCKER" : item.severity === "warning" ? "WARNING" : item.severity === "info" ? "INFO" : (item.severity || "").toUpperCase();
            var sevClass = "cr-result-item__severity--" + (item.severity || "info");

            html += '<button class="cr-result-item' + (isSelected ? " selected" : "") + '" data-item-id="' + self._escape(item.id) + '" type="button">';
            html += '<div class="cr-result-item__header">';
            html += '<span class="cr-result-item__title">' + self._escape(item.title) + '</span>';
            html += '<span class="cr-result-item__severity ' + sevClass + '">' + self._escape(sevLabel) + '</span>';
            html += '</div>';
            html += '<div class="cr-result-item__summary">' + self._escape(item.summary || "") + '</div>';

            // Values
            if (item.values) {
                html += '<div class="cr-result-item__meta">';
                for (var key in item.values) {
                    if (item.values.hasOwnProperty(key)) {
                        html += '<span class="cr-result-item__meta-item">' + self._escape(key) + ': ' + self._escape(String(item.values[key])) + '</span>';
                    }
                }
                html += '</div>';
            }

            // Evidence pages
            if (item.evidence && item.evidence.length > 0) {
                html += '<div class="cr-result-item__meta" style="margin-top:4px">';
                item.evidence.forEach(function(ev) {
                    html += '<span class="cr-result-item__meta-item">' + self._escape(ev.document_type || "") + ' 第' + (ev.page || "?") + '页</span>';
                });
                html += '</div>';
            }

            // Source buttons for sub-items
            if (item.sub_items && item.sub_items.length > 0) {
                html += '<div class="cr-source-btns">';
                item.sub_items.slice(0, 5).forEach(function(sub) {
                    html += '<button class="cr-source-btn" data-doc-type="cqp" data-viewer="mid" type="button">' + self._escape(sub.code || "") + ' CQP</button>';
                });
                html += '</div>';
            }

            html += '</button>';
        });

        list.innerHTML = html;
    };

    ContractReview.prototype._filterItems = function(items) {
        var self = this;
        return items.filter(function(item) {
            if (self.currentFilter === "all") return true;
            if (self.currentFilter === "blocker") return item.severity === "blocker" || item.status === "BLOCKER" || item.status === "MISMATCH";
            if (self.currentFilter === "warning") return item.severity === "warning" || item.status === "WARNING" || item.status === "UNDETERMINED";
            if (self.currentFilter === "pass") return item.status === "PASS";
            // "needs_check" - default
            return item.severity !== "info" && item.status !== "PASS";
        });
    };

    ContractReview.prototype._selectItem = function(itemId) {
        if (!this.reviewResult) return;

        var items = this.reviewResult.review_items || [];
        var item = null;
        for (var i = 0; i < items.length; i++) {
            if (items[i].id === itemId) { item = items[i]; break; }
        }
        if (!item) return;

        this.selectedItemId = itemId;
        this._renderResults();

        // Navigate PDF viewers to evidence
        var evidence = item.evidence || [];

        // Determine which docs are involved and assign to viewers
        var hasContract = false, hasCqp = false, hasTa = false;
        evidence.forEach(function(ev) {
            if (ev.document_type === "contract") hasContract = true;
            if (ev.document_type === "cqp") hasCqp = true;
            if (ev.document_type === "ta") hasTa = true;
        });

        // Default: left=contract, mid=cqp
        // If only contract+ta: left=contract, mid=ta
        // If only cqp+ta: left=cqp, mid=ta
        if (!hasContract && hasCqp && hasTa) {
            this._switchViewerDoc("left", "cqp");
            this._switchViewerDoc("mid", "ta");
        } else if (hasContract && !hasCqp && hasTa) {
            this._switchViewerDoc("left", "contract");
            this._switchViewerDoc("mid", "ta");
        } else {
            if (hasContract && this.leftViewer.documentType !== "contract") {
                this._switchViewerDoc("left", "contract");
            }
            if (hasCqp && this.midViewer.documentType !== "cqp") {
                this._switchViewerDoc("mid", "cqp");
            }
        }

        // Separate evidence by target viewer
        var leftEvidence = evidence.filter(function(ev) {
            return ev.document_type === "contract" || (ev.document_type === "cqp" && !hasContract);
        });
        var midEvidence = evidence.filter(function(ev) {
            return ev.document_type === "cqp" || ev.document_type === "ta";
        });

        // Apply
        if (leftEvidence.length > 0) {
            this.leftViewer.navigateToEvidence(leftEvidence, "cr-highlight-rect--left");
        }
        if (midEvidence.length > 0) {
            this.midViewer.navigateToEvidence(midEvidence, "cr-highlight-rect--mid");
        }

        // If no evidence but has page numbers, still navigate
        if (evidence.length === 0) {
            this.leftViewer.clearHighlights();
            this.midViewer.clearHighlights();
        }
    };

    ContractReview.prototype._showToast = function(message, tone) {
        try {
            var container = document.getElementById("toastContainer");
            if (!container) return;
            var toast = document.createElement("div");
            toast.className = "toast toast--" + (tone || "success");
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(function() { toast.style.opacity = "0"; toast.style.transform = "translateY(8px)"; }, 2400);
            setTimeout(function() { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 2900);
        } catch (e) {}
    };

    // Export
    window.ContractReview = ContractReview;
})();