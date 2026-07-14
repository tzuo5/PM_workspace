/**
 * PM Workplace - Project Tracker Main App
 */

var OrderTracker = (function() {

    function OrderTracker() {
        this.api = window.orderAPI;
        this.visibleColumns = new Set(ORDER_STAGES.map(function(s) { return s.id; }));
        this.currentView = "columns";
        this.currentArchiveTab = "active";
        this.searchInput = null;
        this.globalSearchInput = null;
        this.currentEditingId = null;
        this.pendingDialogAction = null;
        this.undoStack = [];
        this.undoLimit = 20;
        this.bulkDeleteSelection = {};
        this.activeSyncJobId = "";
        this.syncCancelRequested = false;
        this.init();
    }

    OrderTracker.prototype.init = function() {
        var self = this;
        this.initTheme();
        new Sidebar();
        this.bindEvents();
        this.api.on("ordersUpdated", function() { self.render(); });
        this.api.on("backendReady", function() {
            if (self.api.getAllOrders().length === 0) {
                self.showToast("已连接本地后端，当前暂无项目。点击“同步 Outlook”开始抓取邮件。", "warning");
            }
        });
        this.api.on("backendError", function() {
            self.showToast("未连接本地后端。请使用 python backend/server.py 启动系统。", "warning");
        });
        this.render();
    };

    OrderTracker.prototype.getEl = function(id) {
        return document.getElementById(id);
    };

    OrderTracker.prototype.bindClick = function(id, handler) {
        var el = this.getEl(id);
        if (el) el.addEventListener("click", handler);
    };

    OrderTracker.prototype.escapeHTML = function(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    };

    OrderTracker.prototype.initTheme = function() {
        var saved = "light";
        try {
            saved = localStorage.getItem("pm-workplace-theme") || "light";
        } catch (e) {}
        this.setTheme(saved === "dark" ? "dark" : "light", false);
    };

    OrderTracker.prototype.setTheme = function(theme, persist) {
        var isDark = theme === "dark";
        document.documentElement.setAttribute("data-theme", isDark ? "dark" : "light");
        if (!isDark) document.documentElement.removeAttribute("data-theme");
        var label = this.getEl("themeToggleText");
        if (label) label.textContent = isDark ? "Light" : "Dark";
        var btn = this.getEl("btnThemeToggle");
        if (btn) btn.setAttribute("aria-label", isDark ? "切换浅色视图" : "切换深色视图");
        if (persist !== false) {
            try { localStorage.setItem("pm-workplace-theme", isDark ? "dark" : "light"); } catch (e) {}
        }
    };

    OrderTracker.prototype.toggleTheme = function() {
        var isDark = document.documentElement.getAttribute("data-theme") === "dark";
        this.setTheme(isDark ? "light" : "dark", true);
    };

    OrderTracker.prototype.showToast = function(message, tone) {
        var container = this.getEl("toastContainer");
        if (!container) return;
        var toast = document.createElement("div");
        toast.className = "toast toast--" + (tone || "success");
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(function() {
            toast.style.opacity = "0";
            toast.style.transform = "translateY(8px)";
        }, 2400);
        setTimeout(function() {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 2900);
    };

    OrderTracker.prototype.createUndoSnapshot = function(label) {
        return {
            label: label || "上次操作",
            orders: this.api.cloneOrders ? this.api.cloneOrders() : JSON.parse(JSON.stringify(this.api.getAllOrders())),
            archiveTab: this.currentArchiveTab,
            view: this.currentView,
            visibleColumns: Array.from(this.visibleColumns)
        };
    };

    OrderTracker.prototype.pushUndo = function(label) {
        this.undoStack.push(this.createUndoSnapshot(label));
        if (this.undoStack.length > this.undoLimit) this.undoStack.shift();
        this.updateUndoButton();
    };

    OrderTracker.prototype.updateUndoButton = function() {
        var btn = this.getEl("btnUndo");
        if (!btn) return;
        var last = this.undoStack[this.undoStack.length - 1];
        btn.disabled = !last;
        btn.title = last ? "撤回：" + last.label : "暂无可撤回操作";
    };

    OrderTracker.prototype.undoLastOperation = function() {
        var snapshot = this.undoStack.pop();
        if (!snapshot) {
            this.showToast("暂无可撤回操作", "warning");
            this.updateUndoButton();
            return;
        }
        this.currentArchiveTab = snapshot.archiveTab || "active";
        this.currentView = snapshot.view || "columns";
        this.visibleColumns = new Set(snapshot.visibleColumns || ORDER_STAGES.map(function(s) { return s.id; }));
        this.api.restoreSnapshot(snapshot.orders || []);
        this.syncArchiveTabs();
        this.syncViewButtons();
        this.syncVisibleColumnControls();
        this.render();
        this.showToast("已撤回：" + snapshot.label, "success");
        this.updateUndoButton();
    };

    OrderTracker.prototype.syncViewButtons = function() {
        var self = this;
        document.querySelectorAll(".view-btn").forEach(function(btn) {
            btn.classList.toggle("active", btn.getAttribute("data-view") === self.currentView);
        });
    };

    OrderTracker.prototype.syncVisibleColumnControls = function() {
        var self = this;
        document.querySelectorAll("[data-column]").forEach(function(input) {
            input.checked = self.visibleColumns.has(input.getAttribute("data-column"));
        });
        var visibleCount = this.getEl("visibleCount");
        if (visibleCount) visibleCount.textContent = this.visibleColumns.size;
    };

    OrderTracker.prototype.showDialog = function(options) {
        var overlay = this.getEl("dialogOverlay");
        var title = this.getEl("dialogTitle");
        var message = this.getEl("dialogMessage");
        var confirm = this.getEl("dialogConfirm");
        var icon = this.getEl("dialogIcon");
        if (!overlay || !title || !message || !confirm || !icon) return;

        title.textContent = options.title || "确认操作";
        message.textContent = options.message || "该操作需要确认。";
        confirm.textContent = options.confirmText || "确认";
        confirm.className = "btn " + (options.tone === "danger" ? "btn--danger" : options.tone === "archive" ? "btn--archive" : "btn--primary");
        icon.textContent = options.icon || "!";
        this.pendingDialogAction = typeof options.onConfirm === "function" ? options.onConfirm : null;
        overlay.classList.add("show");
        overlay.setAttribute("aria-hidden", "false");
        confirm.focus();
    };

    OrderTracker.prototype.closeDialog = function() {
        var overlay = this.getEl("dialogOverlay");
        if (overlay) {
            overlay.classList.remove("show");
            overlay.setAttribute("aria-hidden", "true");
        }
        this.pendingDialogAction = null;
    };

    OrderTracker.prototype.bindEvents = function() {
        var self = this;

        this.searchInput = this.getEl("searchOrders");
        this.globalSearchInput = this.getEl("globalSearch");

        this.bindClick("btnAddOrder", function() { self.openModal(); });
        this.bindClick("modalClose", function() { self.closeModal(); });
        this.bindClick("btnCancelOrder", function() { self.closeModal(); });
        this.bindClick("btnConfirmOrder", function() { self.submitOrder(); });
        this.bindClick("btnDeleteOrder", function() { self.deleteCurrentOrder(); });
        this.bindClick("btnArchiveOrder", function() { self.toggleCurrentOrderArchive(); });
        this.bindClick("btnOpenOriginalEmail", function() { self.openOriginalEmailForCurrentOrder(); });
        var attachmentList = this.getEl("attachmentList");
        if (attachmentList) {
            attachmentList.addEventListener("click", function(e) {
                var target = e.target.closest("[data-open-attachment]");
                if (!target || target.disabled) return;
                self.openAttachmentForCurrentOrder(target.getAttribute("data-open-attachment"));
            });
        }
        this.bindClick("btnArchiveAll", function() { self.toggleAllVisibleArchiveState(); });
        this.bindClick("btnDeleteAll", function() { self.openBulkDeleteModal(); });
        this.bindClick("btnUndo", function() { self.undoLastOperation(); });
        this.bindClick("btnThemeToggle", function() { self.toggleTheme(); });
        this.bindClick("btnBackToTracker", function() { self.activateTrackerNav(); });
        this.bindClick("btnSyncOutlook", function() { self.openSyncModal(); });
        this.bindClick("syncModalClose", function() { self.closeSyncModal(); });
        this.bindClick("btnCancelSync", function() { self.handleSyncCancelButton(); });
        this.bindClick("btnConfirmSync", function() { self.submitOutlookSync(); });
        this.bindClick("bulkDeleteClose", function() { self.closeBulkDeleteModal(); });
        this.bindClick("btnCancelBulkDelete", function() { self.closeBulkDeleteModal(); });
        this.bindClick("btnConfirmBulkDelete", function() { self.confirmBulkDelete(); });

        var bulkSelectAll = this.getEl("bulkDeleteSelectAll");
        if (bulkSelectAll) {
            bulkSelectAll.addEventListener("change", function() { self.toggleBulkDeleteSelectAll(bulkSelectAll.checked); });
        }
        var bulkGroups = this.getEl("bulkDeleteGroups");
        if (bulkGroups) {
            bulkGroups.addEventListener("change", function(e) {
                if (e.target && e.target.matches("[data-bulk-delete-stage]")) self.updateBulkDeleteSummary();
            });
        }

        document.addEventListener("sidebar:navigate", function(e) {
            self.handleSidebarNavigation((e && e.detail) || {});
        });

        this.bindClick("dialogCancel", function() { self.closeDialog(); });
        this.bindClick("dialogConfirm", function() {
            var action = self.pendingDialogAction;
            self.closeDialog();
            if (action) action();
        });

        var modalOverlay = this.getEl("modalOverlay");
        if (modalOverlay) {
            modalOverlay.addEventListener("click", function(e) {
                if (e.target === e.currentTarget) self.closeModal();
            });
        }

        var dialogOverlay = this.getEl("dialogOverlay");
        if (dialogOverlay) {
            dialogOverlay.addEventListener("click", function(e) {
                if (e.target === e.currentTarget) self.closeDialog();
            });
        }

        var syncOverlay = this.getEl("syncModalOverlay");
        if (syncOverlay) {
            syncOverlay.addEventListener("click", function(e) {
                if (e.target === e.currentTarget && !syncOverlay.dataset.running) self.closeSyncModal();
            });
        }

        var bulkOverlay = this.getEl("bulkDeleteOverlay");
        if (bulkOverlay) {
            bulkOverlay.addEventListener("click", function(e) {
                if (e.target === e.currentTarget) self.closeBulkDeleteModal();
            });
        }

        document.querySelectorAll("[data-archive-tab]").forEach(function(tab) {
            tab.addEventListener("click", function(e) {
                e.preventDefault();
                self.currentArchiveTab = tab.getAttribute("data-archive-tab") === "archived" ? "archived" : "active";
                self.syncArchiveTabs();
                self.render();
            });
        });

        function applySearch(value, mirrorInput) {
            if (mirrorInput && mirrorInput.value !== value) mirrorInput.value = value;
            self.renderBoard(value);
        }

        if (this.searchInput) {
            this.searchInput.addEventListener("input", function(e) {
                applySearch(e.target.value, self.globalSearchInput);
            });
        }

        if (this.globalSearchInput) {
            this.globalSearchInput.addEventListener("input", function(e) {
                applySearch(e.target.value, self.searchInput);
            });
        }

        this.bindClick("btnVisibleColumns", function(e) {
            e.stopPropagation();
            var dropdown = self.getEl("columnsDropdown");
            if (dropdown) dropdown.classList.toggle("show");
        });

        var columnsDropdown = this.getEl("columnsDropdown");
        if (columnsDropdown) {
            columnsDropdown.addEventListener("change", function(e) {
                if (e.target.type === "checkbox") {
                    var col = e.target.getAttribute("data-column");
                    if (e.target.checked) {
                        self.visibleColumns.add(col);
                    } else {
                        self.visibleColumns.delete(col);
                    }
                    var visibleCount = self.getEl("visibleCount");
                    if (visibleCount) visibleCount.textContent = self.visibleColumns.size;
                    self.renderBoard();
                }
            });
        }

        document.addEventListener("click", function(e) {
            if (!e.target.closest(".visible-columns-wrapper")) {
                var dropdown = self.getEl("columnsDropdown");
                if (dropdown) dropdown.classList.remove("show");
            }
        });

        document.querySelectorAll(".view-btn").forEach(function(btn) {
            btn.addEventListener("click", function() {
                document.querySelectorAll(".view-btn").forEach(function(b) { b.classList.remove("active"); });
                btn.classList.add("active");
                self.currentView = btn.getAttribute("data-view");
                self.renderBoard();
            });
        });

        this.bindClick("btnExportExcel", function() { self.exportExcel(); });

        document.addEventListener("keydown", function(e) {
            if (e.key === "Escape") {
                var syncOverlay = self.getEl("syncModalOverlay");
                var bulkOverlay = self.getEl("bulkDeleteOverlay");
                var dialogOverlay = self.getEl("dialogOverlay");
                if (dialogOverlay && dialogOverlay.classList.contains("show")) {
                    self.closeDialog();
                } else if (bulkOverlay && bulkOverlay.classList.contains("show")) {
                    self.closeBulkDeleteModal();
                } else if (syncOverlay && syncOverlay.classList.contains("show") && !syncOverlay.dataset.running) {
                    self.closeSyncModal();
                } else {
                    self.closeModal();
                }
            }
        });
    };


    OrderTracker.prototype.getTodayDateInput = function() {
        var date = new Date();
        var offset = date.getTimezoneOffset();
        var local = new Date(date.getTime() - offset * 60000);
        return local.toISOString().split("T")[0];
    };

    OrderTracker.prototype.addDaysToDateInput = function(dateInput, days) {
        var date = new Date(dateInput + "T00:00:00");
        date.setDate(date.getDate() + days);
        return date.toISOString().split("T")[0];
    };

    OrderTracker.prototype.openSyncModal = function() {
        var overlay = this.getEl("syncModalOverlay");
        if (!overlay) return;
        var today = this.getTodayDateInput();
        var start = "2026-07-01";
        if (this.getEl("syncMailbox") && !this.getEl("syncMailbox").value) this.getEl("syncMailbox").value = "thomas-zhongyan.guo@cn.abb.com";
        if (this.getEl("syncFolder") && !this.getEl("syncFolder").value) this.getEl("syncFolder").value = "Inbox";
        if (this.getEl("syncStartDate")) this.getEl("syncStartDate").value = start;
        if (this.getEl("syncEndDate")) this.getEl("syncEndDate").value = today;
        this.resetSyncStatus();
        overlay.classList.add("show");
        overlay.setAttribute("aria-hidden", "false");
        this.getEl("syncMailbox").focus();
    };

    OrderTracker.prototype.closeSyncModal = function() {
        var overlay = this.getEl("syncModalOverlay");
        if (!overlay) return;
        if (overlay.dataset.running) return;
        overlay.classList.remove("show");
        overlay.setAttribute("aria-hidden", "true");
    };

    OrderTracker.prototype.handleSyncCancelButton = function() {
        var overlay = this.getEl("syncModalOverlay");
        if (overlay && overlay.dataset.running) {
            this.cancelOutlookSync();
            return;
        }
        this.closeSyncModal();
    };

    OrderTracker.prototype.cancelOutlookSync = function() {
        var self = this;
        if (!this.activeSyncJobId || this.syncCancelRequested) return;
        this.syncCancelRequested = true;
        var cancel = this.getEl("btnCancelSync");
        if (cancel) {
            cancel.disabled = true;
            cancel.textContent = "取消中...";
        }
        this.appendSyncLog("正在取消 Outlook 整合，本次结果将不会写入...", new Date().toISOString());
        this.api.cancelOutlookSync(this.activeSyncJobId).catch(function(error) {
            self.showToast(error.message || "取消整合失败", "danger");
            if (cancel) {
                cancel.disabled = false;
                cancel.textContent = "取消整合";
            }
            self.syncCancelRequested = false;
        });
    };

    OrderTracker.prototype.resetSyncStatus = function() {
        var status = this.getEl("syncStatus");
        var log = this.getEl("syncLog");
        var statusText = this.getEl("syncStatusText");
        var statusSummary = this.getEl("syncStatusSummary");
        var bar = this.getEl("syncProgressBar");
        var confirm = this.getEl("btnConfirmSync");
        var cancel = this.getEl("btnCancelSync");
        var overlay = this.getEl("syncModalOverlay");
        if (status) status.hidden = true;
        if (log) log.innerHTML = "";
        if (statusText) statusText.textContent = "准备同步...";
        if (statusSummary) statusSummary.textContent = "";
        if (bar) bar.style.width = "0%";
        if (confirm) {
            confirm.disabled = false;
            confirm.textContent = "确认同步";
        }
        if (cancel) {
            cancel.disabled = false;
            cancel.textContent = "取消";
        }
        if (overlay) delete overlay.dataset.running;
        this.activeSyncJobId = "";
        this.syncCancelRequested = false;
    };

    OrderTracker.prototype.appendSyncLog = function(message, time) {
        var log = this.getEl("syncLog");
        if (!log) return;
        var row = document.createElement("div");
        row.className = "sync-log__row";
        row.innerHTML = '<span class="sync-log__time">' + this.escapeHTML((time || "").slice(11, 19)) + '</span><span>' + this.escapeHTML(message) + '</span>';
        log.appendChild(row);
        log.scrollTop = log.scrollHeight;
    };

    OrderTracker.prototype.updateSyncStatus = function(job) {
        var status = this.getEl("syncStatus");
        var statusText = this.getEl("syncStatusText");
        var statusSummary = this.getEl("syncStatusSummary");
        var bar = this.getEl("syncProgressBar");
        if (status) status.hidden = false;
        if (job.jobId) this.activeSyncJobId = job.jobId;
        if (statusText) {
            statusText.textContent = job.status === "completed" ? "同步完成" :
                job.status === "failed" ? "同步失败" :
                job.status === "cancelled" ? "已取消" :
                job.status === "cancelling" ? "取消中..." : "同步中...";
        }
        if (bar) {
            var width = (job.status === "completed" || job.status === "failed" || job.status === "cancelled") ? "100%" : (job.status === "cancelling" ? "82%" : "62%");
            bar.style.width = width;
        }
        var log = this.getEl("syncLog");
        var existingCount = log ? parseInt(log.dataset.count || "0", 10) : 0;
        var logs = job.logs || [];
        for (var i = existingCount; i < logs.length; i++) {
            this.appendSyncLog(logs[i].message, logs[i].time);
        }
        if (log) log.dataset.count = String(logs.length);
        if (statusSummary && job.result && job.result.counts) {
            var c = job.result.counts;
            var aiPart = (c.aiReviewed || 0) ? (" · AI复核 " + (c.aiReviewed || 0) + " · AI确认 " + (c.aiConfirmed || 0) + " · AI转审 " + (c.aiReview || 0) + " · AI忽略 " + (c.aiIgnored || 0)) : "";
            var skippedPart = c.skipped ? " · 跳过旧结果 " + c.skipped : "";
            statusSummary.textContent = "邮件 " + (c.rawEmails || 0) + " · 候选 " + (c.candidateEmails || 0) + aiPart + " · 忽略 " + (c.ignoredEmails || 0) + " · 合同 " + (c.contracts || 0) + " · 新增 " + (c.created || 0) + " · 更新 " + (c.updated || 0) + skippedPart + " · 人工审核 " + (c.review || 0);
        }
    };

    OrderTracker.prototype.submitOutlookSync = function() {
        var self = this;
        var mailbox = this.getEl("syncMailbox").value.trim();
        var folderPath = this.getEl("syncFolder").value.trim();
        var startDate = this.getEl("syncStartDate").value;
        var endDate = this.getEl("syncEndDate").value;
        if (!mailbox || !folderPath) {
            this.showToast("请填写邮箱账号和文件夹路径", "warning");
            return;
        }
        if (startDate && endDate && endDate < startDate) {
            this.showToast("结束日期不能早于开始日期", "warning");
            return;
        }

        var status = this.getEl("syncStatus");
        var log = this.getEl("syncLog");
        var confirm = this.getEl("btnConfirmSync");
        var cancel = this.getEl("btnCancelSync");
        var overlay = this.getEl("syncModalOverlay");
        if (status) status.hidden = false;
        if (log) {
            log.innerHTML = "";
            log.dataset.count = "0";
        }
        if (confirm) {
            confirm.disabled = true;
            confirm.textContent = "同步中...";
        }
        if (cancel) {
            cancel.disabled = false;
            cancel.textContent = "取消整合";
        }
        this.activeSyncJobId = "";
        this.syncCancelRequested = false;
        if (overlay) overlay.dataset.running = "true";
        this.appendSyncLog("准备提交 Outlook 同步任务...", new Date().toISOString());

        this.api.syncOutlook({
            mailbox: mailbox,
            folder_path: folderPath,
            start_date: startDate,
            end_date: endDate,
            include_subfolders: this.getEl("syncIncludeSubfolders").checked,
            use_ai_review: this.getEl("syncUseAiReview") ? this.getEl("syncUseAiReview").checked : true
        }, function(job) {
            self.updateSyncStatus(job);
        }).then(function(job) {
            self.updateSyncStatus(job);
            if (job && job.status === "cancelled") {
                self.showToast("已取消，本次无数据写入", "warning");
            } else {
                self.showToast("Outlook 同步完成，项目已更新", "success");
            }
            if (confirm) {
                confirm.disabled = false;
                confirm.textContent = "再次同步";
            }
            if (cancel) {
                cancel.disabled = false;
                cancel.textContent = "关闭";
            }
            self.activeSyncJobId = "";
            self.syncCancelRequested = false;
            if (overlay) delete overlay.dataset.running;
        }).catch(function(error) {
            self.showToast(error.message || "Outlook 同步失败", "danger");
            self.appendSyncLog(error.message || "同步失败", new Date().toISOString());
            if (confirm) {
                confirm.disabled = false;
                confirm.textContent = "重新同步";
            }
            if (cancel) {
                cancel.disabled = false;
                cancel.textContent = "关闭";
            }
            self.activeSyncJobId = "";
            self.syncCancelRequested = false;
            if (overlay) delete overlay.dataset.running;
        });
    };

    OrderTracker.prototype.getBulkDeleteCandidatesByStage = function() {
        var query = this.getActiveSearchQuery().trim();
        var archived = this.isArchivedTab();
        var grouped = query ? this.api.searchOrders(query, { archived: archived }) : this.api.getOrdersByStage({ archived: archived });
        var result = [];
        var self = this;
        ORDER_STAGES.forEach(function(stage) {
            if (!archived && self.currentView === "columns" && !self.visibleColumns.has(stage.id)) return;
            var orders = (grouped[stage.id] || []).filter(function(order) { return !!order.archived === archived; });
            result.push({ stage: stage, orders: orders });
        });
        return result;
    };

    OrderTracker.prototype.openBulkDeleteModal = function() {
        var overlay = this.getEl("bulkDeleteOverlay");
        var groupsEl = this.getEl("bulkDeleteGroups");
        if (!overlay || !groupsEl) return;
        var groups = this.getBulkDeleteCandidatesByStage();
        var total = groups.reduce(function(sum, group) { return sum + group.orders.length; }, 0);
        var totalEl = this.getEl("bulkDeleteTotalCount");
        if (totalEl) totalEl.textContent = total + " 个";
        var title = this.getEl("bulkDeleteTitle");
        if (title) title.textContent = this.isArchivedTab() ? "删除已归档事件" : "删除全部";
        if (total === 0) {
            groupsEl.innerHTML = '<div class="bulk-delete__empty">当前没有可删除事件。</div>';
        } else {
            groupsEl.innerHTML = groups.map(function(group) {
                var disabled = group.orders.length === 0 ? " disabled" : "";
                var checked = group.orders.length > 0 ? " checked" : "";
                return '<label class="bulk-delete__group' + (group.orders.length === 0 ? ' is-empty' : '') + '">' +
                    '<input type="checkbox" data-bulk-delete-stage="' + group.stage.id + '"' + checked + disabled + '>' +
                    '<span class="bulk-delete__group-main">' + group.stage.label + '</span>' +
                    '<span class="bulk-delete__group-count">' + group.orders.length + '</span>' +
                '</label>';
            }).join("");
        }
        var selectAll = this.getEl("bulkDeleteSelectAll");
        if (selectAll) {
            selectAll.checked = total > 0;
            selectAll.disabled = total === 0;
            selectAll.indeterminate = false;
        }
        overlay.classList.add("show");
        overlay.setAttribute("aria-hidden", "false");
        this.updateBulkDeleteSummary();
    };

    OrderTracker.prototype.closeBulkDeleteModal = function() {
        var overlay = this.getEl("bulkDeleteOverlay");
        if (!overlay) return;
        overlay.classList.remove("show");
        overlay.setAttribute("aria-hidden", "true");
    };

    OrderTracker.prototype.toggleBulkDeleteSelectAll = function(checked) {
        document.querySelectorAll("[data-bulk-delete-stage]:not(:disabled)").forEach(function(input) {
            input.checked = checked;
        });
        this.updateBulkDeleteSummary();
    };

    OrderTracker.prototype.getSelectedBulkDeleteIds = function() {
        var groups = this.getBulkDeleteCandidatesByStage();
        var selectedStages = {};
        document.querySelectorAll("[data-bulk-delete-stage]:checked").forEach(function(input) {
            selectedStages[input.getAttribute("data-bulk-delete-stage")] = true;
        });
        var ids = [];
        var seen = {};
        groups.forEach(function(group) {
            if (!selectedStages[group.stage.id]) return;
            group.orders.forEach(function(order) {
                if (!seen[order.id]) {
                    ids.push(order.id);
                    seen[order.id] = true;
                }
            });
        });
        return ids;
    };

    OrderTracker.prototype.updateBulkDeleteSummary = function() {
        var ids = this.getSelectedBulkDeleteIds();
        var summary = this.getEl("bulkDeleteSummary");
        var confirm = this.getEl("btnConfirmBulkDelete");
        if (summary) {
            summary.textContent = ids.length > 0 ? "将删除 " + ids.length + " 个事件。可通过“返回”撤回。" : "请选择至少一个分类。";
        }
        if (confirm) confirm.disabled = ids.length === 0;
        var checkboxes = Array.from(document.querySelectorAll("[data-bulk-delete-stage]:not(:disabled)"));
        var selected = checkboxes.filter(function(input) { return input.checked; }).length;
        var selectAll = this.getEl("bulkDeleteSelectAll");
        if (selectAll) {
            selectAll.checked = checkboxes.length > 0 && selected === checkboxes.length;
            selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
        }
    };

    OrderTracker.prototype.confirmBulkDelete = function() {
        var self = this;
        var ids = this.getSelectedBulkDeleteIds();
        if (ids.length === 0) {
            this.showToast("请选择至少一个分类", "warning");
            return;
        }
        this.showDialog({
            title: "确认删除",
            message: "确定要删除已勾选分类内的 " + ids.length + " 个事件吗？删除后可以立即点击“返回”撤回。",
            confirmText: "删除",
            tone: "danger",
            icon: "×",
            onConfirm: function() {
                self.pushUndo("删除 " + ids.length + " 个事件");
                var changed = self.api.deleteOrders(ids);
                self.closeBulkDeleteModal();
                self.render();
                self.showToast("已删除 " + changed + " 个事件", "danger");
            }
        });
    };

    OrderTracker.prototype.isArchivedTab = function() {
        return this.currentArchiveTab === "archived";
    };

    OrderTracker.prototype.getArchiveOptions = function() {
        return { archived: this.isArchivedTab() };
    };

    OrderTracker.prototype.getActiveSearchQuery = function(searchQuery) {
        if (typeof searchQuery === "string") return searchQuery;
        if (this.searchInput && this.searchInput.value) return this.searchInput.value;
        if (this.globalSearchInput && this.globalSearchInput.value) return this.globalSearchInput.value;
        return "";
    };

    OrderTracker.prototype.handleSidebarNavigation = function(detail) {
        var section = detail.section || "tracker";
        var title = detail.title || "\u8be5\u6a21\u5757";
        if (section === "tracker") {
            this.showTrackerPage();
        } else if (section === "contract-review") {
            this.showContractReviewPage();
        } else {
            this.showUnderDevelopmentPage(title);
        }
    };

    OrderTracker.prototype.showTrackerPage = function() {
        var tracker = this.getEl("trackerContent");
        var underDevelopment = this.getEl("underDevelopment");
        var contractReview = this.getEl("contractReviewContent");
        if (tracker) tracker.hidden = false;
        if (underDevelopment) underDevelopment.hidden = true;
        if (contractReview) contractReview.hidden = true;
    };

    OrderTracker.prototype.showUnderDevelopmentPage = function(sectionTitle) {
        var tracker = this.getEl("trackerContent");
        var underDevelopment = this.getEl("underDevelopment");
        var contractReview = this.getEl("contractReviewContent");
        var title = this.getEl("underDevelopmentTitle");
        var desc = this.getEl("underDevelopmentDesc");
        if (tracker) tracker.hidden = true;
        if (underDevelopment) underDevelopment.hidden = false;
        if (contractReview) contractReview.hidden = true;
        if (title) title.textContent = sectionTitle + " \u00b7 \u6b63\u5728\u5f00\u53d1\u4e2d";
        if (desc) {
            desc.textContent = "\u201c" + sectionTitle + "\u201d\u6a21\u5757\u6682\u672a\u63a5\u5165\u6b63\u5f0f\u4e1a\u52a1\u903b\u8f91\u3002\u5f53\u524d\u7248\u672c\u53ea\u5f00\u653e\u9879\u76ee\u8ddf\u8e2a\uff1b\u8be5\u9875\u9762\u7528\u4e8e\u5360\u4f4d\uff0c\u907f\u514d\u70b9\u51fb\u5de6\u4fa7\u5bfc\u822a\u540e\u65e0\u53cd\u9988\u3002";
        }
    };

    OrderTracker.prototype.showContractReviewPage = function() {
        var tracker = this.getEl("trackerContent");
        var underDevelopment = this.getEl("underDevelopment");
        var contractReview = this.getEl("contractReviewContent");
        if (tracker) tracker.hidden = true;
        if (underDevelopment) underDevelopment.hidden = true;
        if (contractReview) {
            contractReview.hidden = false;
            // Initialize ContractReview module on first visit
            if (!this._contractReviewInstance && typeof ContractReview !== "undefined") {
                this._contractReviewInstance = new ContractReview();
            }
        }
    };

    OrderTracker.prototype.activateTrackerNav = function() {
        document.querySelectorAll(".nav-item").forEach(function(item) {
            item.classList.toggle("active", item.getAttribute("data-section") === "tracker");
        });
        this.showTrackerPage();
    };

    OrderTracker.prototype.render = function() {
        this.updateCounts();
        this.updateArchiveToolbar();
        this.updateUndoButton();
        this.renderBoard();
    };

    OrderTracker.prototype.updateCounts = function() {
        var counts = this.api.getCounts(this.getArchiveOptions());
        var sectionLabel = this.isArchivedTab() ? "已归档项目" : "进行中项目";
        var totalCount = this.getEl("totalCount");
        if (totalCount) totalCount.textContent = counts.total + " 个" + sectionLabel;

        var activeTabCount = this.getEl("activeTabCount");
        var archivedTabCount = this.getEl("archivedTabCount");
        if (activeTabCount) activeTabCount.textContent = counts.activeTotal;
        if (archivedTabCount) archivedTabCount.textContent = counts.archivedTotal;

        var badge = this.getEl("orderBadge");
        if (badge) badge.textContent = counts.activeTotal;

        ORDER_STAGES.forEach(function(stage) {
            var el = document.getElementById("count-" + stage.id);
            if (el) el.textContent = "(" + counts[stage.id] + ")";
        });
    };

    OrderTracker.prototype.updateArchiveToolbar = function() {
        var archiveText = this.getEl("btnArchiveAllText");
        if (archiveText) archiveText.textContent = this.isArchivedTab() ? "恢复全部" : "归档全部";

        var addButton = this.getEl("btnAddOrder");
        if (addButton) addButton.hidden = this.isArchivedTab();

        var visibleWrapper = document.querySelector(".visible-columns-wrapper");
        if (visibleWrapper) visibleWrapper.hidden = this.isArchivedTab();
    };

    OrderTracker.prototype.flattenGroupedOrders = function(grouped, useVisibleColumns) {
        var orders = [];
        var seen = {};
        var self = this;
        ORDER_STAGES.filter(function(stage) {
            return !useVisibleColumns || self.visibleColumns.has(stage.id);
        }).forEach(function(stage) {
            (grouped[stage.id] || []).forEach(function(order) {
                if (!seen[order.id]) {
                    orders.push(order);
                    seen[order.id] = true;
                }
            });
        });
        return orders;
    };

    OrderTracker.prototype.getBoardOrders = function(query, archived, useVisibleColumns) {
        var options = { archived: archived };
        var grouped = query ? this.api.searchOrders(query, options) : this.api.getOrdersByStage(options);
        return this.flattenGroupedOrders(grouped, useVisibleColumns);
    };

    OrderTracker.prototype.renderBoard = function(searchQuery) {
        var board = this.getEl("kanbanBoard");
        if (!board) return;
        var query = this.getActiveSearchQuery(searchQuery).trim();
        var self = this;

        if (this.isArchivedTab()) {
            var archivedOrders = this.getBoardOrders(query, true, false);
            if (this.currentView === "columns") {
                board.className = "kanban-board kanban-board--columns kanban-board--archived";
                board.innerHTML = this.renderArchiveColumn(archivedOrders);
            } else {
                board.className = "kanban-board kanban-board--list kanban-board--archived";
                board.innerHTML = this.renderArchiveListView(archivedOrders);
            }
        } else {
            var grouped = query ? this.api.searchOrders(query, { archived: false }) : this.api.getOrdersByStage({ archived: false });
            if (this.currentView === "columns") {
                board.className = "kanban-board kanban-board--columns";
                board.innerHTML = ORDER_STAGES
                    .filter(function(stage) { return self.visibleColumns.has(stage.id); })
                    .map(function(stage) { return self.renderColumn(stage, grouped[stage.id] || []); })
                    .join("");
            } else {
                board.className = "kanban-board kanban-board--list";
                board.innerHTML = this.renderListView(grouped);
            }
        }

        this.bindCardEvents();
    };

    OrderTracker.prototype.renderArchiveColumn = function(orders) {
        var self = this;
        var orderCards = orders.length > 0
            ? orders.map(function(o) { return self.renderCard(o); }).join("")
            : '<div class="empty-state"><div class="empty-state__icon">🗄</div><div class="empty-state__title">暂无归档项目</div><div class="empty-state__desc">归档后项目会集中显示在这一栏，恢复时会回到归档前阶段。</div></div>';

        return '<div class="kanban-column archive-column" data-stage="archived">' +
            '<div class="kanban-column__header">' +
                '<div class="column-drag-handle">' +
                    '<svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16">' +
                        '<path d="M3 4h18v4H3zM5 10h14v10H5z"/>' +
                    '</svg>' +
                '</div>' +
                '<h3 class="column-title">已归档</h3>' +
                '<span class="archive-column__hint">完成或不再跟进的项目集中在这里</span>' +
                '<span class="column-count-badge">' + orders.length + '</span>' +
            '</div>' +
            '<div class="kanban-column__body">' + orderCards + '</div>' +
        '</div>';
    };

    OrderTracker.prototype.renderColumn = function(stage, orders) {
        var self = this;
        var orderCards = "";

        if (orders.length > 0) {
            orderCards = orders.map(function(o) { return self.renderCard(o); }).join("");
        } else {
            var msg = emptyStateMessages[stage.id];
            orderCards = '<div class="empty-state">' +
                '<div class="empty-state__icon">' + self.escapeHTML(msg.icon) + '</div>' +
                '<div class="empty-state__title">' + self.escapeHTML(msg.title) + '</div>' +
                '<div class="empty-state__desc">' + self.escapeHTML(msg.desc) + '</div>' +
                '</div>';
        }

        return '<div class="kanban-column" data-stage="' + this.escapeHTML(stage.id) + '">' +
            '<div class="kanban-column__header">' +
                '<div class="column-drag-handle">' +
                    '<svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16">' +
                        '<circle cx="9" cy="6" r="1.5"/><circle cx="15" cy="6" r="1.5"/>' +
                        '<circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>' +
                        '<circle cx="9" cy="18" r="1.5"/><circle cx="15" cy="18" r="1.5"/>' +
                    '</svg>' +
                '</div>' +
                '<h3 class="column-title">' + this.escapeHTML(stage.label) + '</h3>' +
                '<span class="column-count-badge">' + orders.length + '</span>' +
            '</div>' +
            '<div class="kanban-column__body" data-stage="' + this.escapeHTML(stage.id) + '">' +
                orderCards +
            '</div>' +
        '</div>';
    };

    OrderTracker.prototype.renderCard = function(order) {
        var typeClass = order.type === "urgent" ? "card--urgent" :
                       order.type === "custom" ? "card--custom" : "";
        var typeLabel = order.type === "urgent" ? "加急" :
                       order.type === "custom" ? "定制" : "";
        var suspendedClass = order.suspended ? " card--suspended" : "";
        var archivedClass = order.archived ? " order-card--archived" : "";
        var favFill = order.favorite ? "currentColor" : "none";
        var favClass = order.favorite ? " active" : "";
        var projectDesc = this.escapeHTML(order.name);
        var client = this.escapeHTML(order.client);
        var amount = this.escapeHTML(order.amount);
        var projectNo = this.escapeHTML(order.contract || order.id || "-");
        var date = this.escapeHTML(order.date);
        var notes = this.escapeHTML(order.notes);
        var id = this.escapeHTML(order.id);
        var archivedFrom = this.escapeHTML(this.getStageLabel(order.archivedFromStage || order.stage));
        var draggable = order.archived ? "false" : "true";

        var llmRevisedClass = order.llmReviewed ? " order-card--llm-reviewed" : "";
        var html = '<div class="order-card ' + typeClass + suspendedClass + archivedClass + llmRevisedClass + '" data-id="' + id + '" draggable="' + draggable + '"';
        if (order.llmReviewed && order.llmSummary) {
            html += ' data-llm-summary="' + this.escapeHTML(order.llmSummary) + '"';
        }
        html += '>';

        if (order.archived) {
            html += '<div class="order-card__badge badge--archived">已归档</div>';
        } else if (order.needsReview) {
            html += '<div class="order-card__badge badge--review">需确认</div>';
        } else if (order.suspended) {
            html += '<div class="order-card__badge badge--suspended">挂起</div>';
        }

        html += '<div class="order-card__header">' +
            '<span class="order-card__name" title="' + projectNo + '">' + projectNo + '</span>' +
            '<button class="order-card__fav' + favClass + '" data-fav="' + id + '" aria-label="收藏项目">' +
                '<svg viewBox="0 0 24 24" fill="' + favFill + '" stroke="currentColor" stroke-width="2" width="16" height="16">' +
                    '<path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/>' +
                '</svg>' +
            '</button>' +
        '</div>';

        html += '<div class="order-card__client">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">' +
                '<path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>' +
            '</svg>' +
            client +
        '</div>';

        var currentProgress = this.escapeHTML(order.currentProgress || this.getStageLabel(order.stage));
        var latestEmailTime = this.escapeHTML(order.latestEmailTime || order.date || "");

        html += '<div class="order-card__meta">';
        if (order.amount) {
            html += '<span class="order-card__amount">' + amount + '</span>';
        }
        if (typeLabel) {
            html += '<span class="order-card__type type--' + this.escapeHTML(order.type) + '">' + typeLabel + '</span>';
        }
        if (order.archived) {
            html += '<span class="order-card__type type--archived">归档于 ' + this.escapeHTML(order.archivedAt || "-") + '</span>';
        }
        html += '</div>';

        if (!order.archived) {
            html += '<div class="order-card__sync">' +
                '<span>进展：' + currentProgress + '</span>' +
                '<span>最新邮件：' + latestEmailTime + '</span>' +
            '</div>';
        }

        if (order.archived) {
            html += '<div class="order-card__notes">归档前：' + archivedFrom + '</div>';
        } else if (order.needsReview) {
            html += '<div class="order-card__notes order-card__notes--review">' + this.escapeHTML(order.reviewReason || "邮件进展需要人工确认") + '</div>';
        } else if (order.notes) {
            html += '<div class="order-card__notes">' + notes + '</div>';
        }

        html += '<div class="order-card__footer">' +
            '<span class="order-card__contract" title="' + projectDesc + '">' + projectDesc + '</span>' +
            '<span class="order-card__date">' + date + '</span>' +
        '</div>';

        html += '</div>';
        return html;
    };

    OrderTracker.prototype.getStageIndex = function(stageId) {
        var index = ORDER_STAGES.findIndex(function(stage) { return stage.id === stageId; });
        return index >= 0 ? index : 0;
    };

    OrderTracker.prototype.formatDateBrief = function(dateValue) {
        if (!dateValue) return "";
        var value = String(dateValue);
        var parts = value.split("-");
        if (parts.length === 3) return parts[1] + "-" + parts[2];
        return value;
    };

    OrderTracker.prototype.getClientInitials = function(clientName) {
        var chars = Array.from(String(clientName || "项目"));
        return chars.slice(0, 2).join("").toUpperCase();
    };

    OrderTracker.prototype.getStageLabel = function(stageId) {
        var stage = ORDER_STAGES.find(function(s) { return s.id === stageId; });
        return stage ? stage.label : "未知阶段";
    };

    OrderTracker.prototype.renderWorkflowTimeline = function(order) {
        var self = this;
        var stageForTimeline = order.archivedFromStage || order.stage;
        var activeIndex = this.getStageIndex(stageForTimeline);
        var progress = ORDER_STAGES.length <= 1 ? 0 : (activeIndex / (ORDER_STAGES.length - 1)) * 100;
        var stageDates = order.stageDates || {};

        var html = '<div class="workflow-timeline" style="--workflow-progress:' + progress + '%">' +
            '<div class="workflow-timeline__track"><div class="workflow-timeline__track-fill"></div></div>';

        ORDER_STAGES.forEach(function(stage, index) {
            var stepClass = index < activeIndex ? " completed" : (index === activeIndex ? " active" : " future");
            var dateText = stageDates[stage.id] || (index === activeIndex ? order.date : "");
            html += '<div class="workflow-step' + stepClass + '" title="' + self.escapeHTML(stage.desc || stage.label) + '">' +
                '<div class="workflow-step__label">' + self.escapeHTML(stage.label) + '</div>' +
                '<div class="workflow-step__dot"></div>' +
                '<div class="workflow-step__date">' + self.escapeHTML(self.formatDateBrief(dateText)) + '</div>' +
            '</div>';
        });

        html += '</div>';
        return html;
    };

    OrderTracker.prototype.renderStageSelect = function(order) {
        var self = this;
        var disabled = order.archived ? " disabled" : "";
        var html = '<select class="workflow-stage-select" data-stage-select="' + this.escapeHTML(order.id) + '" aria-label="修改当前阶段"' + disabled + '>';
        ORDER_STAGES.forEach(function(stage) {
            var selected = stage.id === order.stage ? " selected" : "";
            html += '<option value="' + self.escapeHTML(stage.id) + '"' + selected + '>' + self.escapeHTML(stage.label) + '</option>';
        });
        html += '</select>';
        return html;
    };

    OrderTracker.prototype.renderListRow = function(order) {
        var typeLabel = order.type === "urgent" ? "加急" :
                       order.type === "custom" ? "定制" : "标准";
        var typeClass = order.type === "urgent" ? " type--urgent" :
                        order.type === "custom" ? " type--custom" : "";
        var favFill = order.favorite ? "currentColor" : "none";
        var favClass = order.favorite ? " active" : "";
        var statusClass = order.type === "urgent" ? " workflow-row--urgent" :
                          order.type === "custom" ? " workflow-row--custom" : "";
        var suspendedClass = order.suspended ? " workflow-row--suspended" : "";
        var archivedClass = order.archived ? " workflow-row--archived" : "";
        var id = this.escapeHTML(order.id);
        var projectNo = this.escapeHTML(order.contract || order.id || "-");
        var projectDesc = this.escapeHTML(order.name || "-");
        var client = this.escapeHTML(order.client || "-");
        var amount = this.escapeHTML(order.amount || "");
        var notes = this.escapeHTML(order.notes || "");
        var currentStage = this.escapeHTML(this.getStageLabel(order.stage));
        var archivedFromStage = this.escapeHTML(this.getStageLabel(order.archivedFromStage || order.stage));
        var initials = this.escapeHTML(this.getClientInitials(order.client));

        var llmRevisedRowClass = order.llmReviewed ? " workflow-row--llm-reviewed" : "";
        var html = '<div class="workflow-row' + statusClass + suspendedClass + archivedClass + llmRevisedRowClass + '" data-id="' + id + '"';
        if (order.llmReviewed && order.llmSummary) {
            html += ' data-llm-summary="' + this.escapeHTML(order.llmSummary) + '"';
        }
        html += '>';

        html += '<button class="workflow-row__fav' + favClass + '" data-fav="' + id + '" aria-label="收藏项目">' +
            '<svg viewBox="0 0 24 24" fill="' + favFill + '" stroke="currentColor" stroke-width="2" width="18" height="18">' +
                '<path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/>' +
            '</svg>' +
        '</button>';

        html += '<div class="workflow-row__project">' +
            '<div class="workflow-row__avatar">' + initials + '</div>' +
            '<div class="workflow-project">' +
                '<div class="workflow-project__no" title="' + projectNo + '">' + projectNo + '</div>' +
                '<div class="workflow-project__client">' +
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">' +
                        '<rect x="4" y="3" width="16" height="18" rx="2"/>' +
                        '<line x1="9" y1="7" x2="9" y2="7"/><line x1="15" y1="7" x2="15" y2="7"/>' +
                        '<line x1="9" y1="12" x2="9" y2="12"/><line x1="15" y1="12" x2="15" y2="12"/>' +
                    '</svg>' + client +
                '</div>' +
                '<div class="workflow-project__desc" title="' + projectDesc + '">' + projectDesc + '</div>' +
                '<div class="workflow-project__tags">';
        if (amount) {
            html += '<span class="workflow-project__amount">¥' + amount + '</span>';
        }
        html += '<span class="workflow-project__type' + typeClass + '">' + typeLabel + '</span>';
        if (order.currentProgress) {
            html += '<span class="workflow-project__sync">' + this.escapeHTML(order.currentProgress) + '</span>';
        }
        if (order.latestEmailTime) {
            html += '<span class="workflow-project__sync">' + this.escapeHTML(order.latestEmailTime) + '</span>';
        }
        if (order.suspended) {
            html += '<span class="workflow-project__suspended">挂起</span>';
        }
        if (order.archived) {
            html += '<span class="workflow-project__archived">归档于 ' + this.escapeHTML(order.archivedAt || "") + '</span>';
        }
        html += '</div>';
        if (order.archived) {
            html += '<div class="workflow-project__notes" title="归档前：' + archivedFromStage + '">归档前：' + archivedFromStage + '</div>';
        } else if (order.needsReview) {
            var reviewReason = this.escapeHTML(order.reviewReason || "邮件进展需要人工确认");
            html += '<div class="workflow-project__notes workflow-project__notes--review" title="' + reviewReason + '">需确认：' + reviewReason + '</div>';
        } else if (notes) {
            html += '<div class="workflow-project__notes" title="' + notes + '">' + notes + '</div>';
        }
        html += '</div></div>';

        html += '<div class="workflow-row__timeline">' + this.renderWorkflowTimeline(order) + '</div>';

        html += '<div class="workflow-row__actions">' +
            '<div class="workflow-current-stage">' + (order.archived ? '归档前：' + archivedFromStage : '当前：' + currentStage) + '</div>';
        if (order.archived) {
            html += '<button class="btn btn--outline workflow-restore-btn" data-restore="' + id + '" type="button">恢复项目</button>';
        } else {
            html += this.renderStageSelect(order);
        }
        html += '</div>';

        html += '</div>';
        return html;
    };

    OrderTracker.prototype.renderListView = function(grouped) {
        var self = this;
        var orders = this.flattenGroupedOrders(grouped, true);

        var html = '<div class="workflow-list">' +
            '<div class="workflow-list__header">' +
                '<div></div>' +
                '<div>项目</div>' +
                '<div>标准流程进度</div>' +
                '<div>状态操作</div>' +
            '</div>';

        if (orders.length === 0) {
            html += '<div class="workflow-list__empty">暂无符合条件的项目</div>';
        } else {
            orders.forEach(function(order) {
                html += self.renderListRow(order);
            });
        }

        html += '</div>';
        return html;
    };

    OrderTracker.prototype.renderArchiveListView = function(orders) {
        var self = this;
        var html = '<div class="workflow-list">' +
            '<div class="workflow-list__header">' +
                '<div></div>' +
                '<div>项目</div>' +
                '<div>归档前流程状态</div>' +
                '<div>归档操作</div>' +
            '</div>';

        if (orders.length === 0) {
            html += '<div class="workflow-list__empty">暂无已归档项目</div>';
        } else {
            orders.forEach(function(order) {
                html += self.renderListRow(order);
            });
        }

        html += '</div>';
        return html;
    };

    OrderTracker.prototype.bindCardEvents = function() {
        var self = this;

        document.querySelectorAll("[data-fav]").forEach(function(btn) {
            btn.addEventListener("click", function(e) {
                e.stopPropagation();
                self.pushUndo("切换收藏");
                self.api.toggleFavorite(btn.getAttribute("data-fav"));
            });
        });

        document.querySelectorAll("[data-restore]").forEach(function(btn) {
            btn.addEventListener("click", function(e) {
                e.stopPropagation();
                var order = self.api.getOrderById(btn.getAttribute("data-restore"));
                var projectNo = order ? (order.contract || order.id) : "该项目";
                self.showDialog({
                    title: "恢复项目",
                    message: "确定要将 " + projectNo + " 恢复到归档前阶段吗？",
                    confirmText: "恢复",
                    tone: "archive",
                    icon: "↩",
                    onConfirm: function() {
                        self.pushUndo("恢复项目");
                        self.api.restoreOrder(btn.getAttribute("data-restore"));
                        self.currentArchiveTab = "active";
                        self.syncArchiveTabs();
                        self.render();
                        self.showToast("项目已恢复到归档前状态", "success");
                    }
                });
            });
        });

        document.querySelectorAll(".order-card").forEach(function(card) {
            card.addEventListener("click", function(e) {
                if (e.target.closest("[data-fav]")) return;
                if (card.dataset.dragging === "true") return;
                self.openModal(card.getAttribute("data-id"));
            });
        });

        document.querySelectorAll("[data-stage-select]").forEach(function(select) {
            select.addEventListener("click", function(e) {
                e.stopPropagation();
            });
            select.addEventListener("change", function(e) {
                e.stopPropagation();
                self.pushUndo("修改项目阶段");
                self.api.updateOrderStage(select.getAttribute("data-stage-select"), select.value);
            });
        });

        document.querySelectorAll(".workflow-row[data-id], .list-item[data-id]").forEach(function(item) {
            item.addEventListener("click", function(e) {
                if (e.target.closest("[data-fav], [data-stage-select], [data-restore]")) return;
                self.openModal(item.getAttribute("data-id"));
            });
        });

        document.querySelectorAll('.order-card[draggable="true"]').forEach(function(card) {
            card.addEventListener("dragstart", function(e) {
                e.dataTransfer.setData("text/plain", card.getAttribute("data-id"));
                card.dataset.dragging = "true";
                card.classList.add("dragging");
            });
            card.addEventListener("dragend", function() {
                card.classList.remove("dragging");
                setTimeout(function() { delete card.dataset.dragging; }, 0);
            });
        });

        if (!this.isArchivedTab()) {
            document.querySelectorAll(".kanban-column__body").forEach(function(col) {
                col.addEventListener("dragover", function(e) {
                    e.preventDefault();
                    col.classList.add("drag-over");
                });
                col.addEventListener("dragleave", function() {
                    col.classList.remove("drag-over");
                });
                col.addEventListener("drop", function(e) {
                    e.preventDefault();
                    col.classList.remove("drag-over");
                    var orderId = e.dataTransfer.getData("text/plain");
                    var newStage = col.getAttribute("data-stage");
                    self.pushUndo("拖拽修改阶段");
                    self.api.updateOrderStage(orderId, newStage);
                });
            });
        }

        // LLM summary popup: delegated click on the kanban board.
        var board = this.getEl("kanbanBoard");
        if (board) {
            board.addEventListener("click", function(e) {
                var target = e.target.closest("[data-llm-summary]");
                if (!target) {
                    self.closeLlmSummaryPopup();
                    return;
                }
                var summary = target.getAttribute("data-llm-summary");
                if (summary) {
                    e.stopPropagation();
                    self.showLlmSummaryPopup(target, summary);
                }
            });
        }

        // Close LLM popup when clicking outside.
        document.addEventListener("click", function(e) {
            if (!e.target.closest("[data-llm-summary]") && !e.target.closest(".llm-summary-popup")) {
                self.closeLlmSummaryPopup();
            }
        });
    };

    OrderTracker.prototype.showLlmSummaryPopup = function(anchorEl, summary) {
        this.closeLlmSummaryPopup();
        var popup = document.createElement("div");
        popup.className = "llm-summary-popup";
        popup.innerHTML =
            '<button class="llm-summary-popup__close" type="button">&times;</button>' +
            '<div class="llm-summary-popup__title">' +
                '<svg viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" width="18" height="18">' +
                    '<path d="M12 2a4 4 0 014 4v1h2a2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V9a2 2 0 012-2h2V6a4 4 0 014-4z"/>' +
                    '<path d="M10 9h4v5h-4z"/>' +
                '</svg>' +
                'AI 审核结果' +
            '</div>' +
            '<div class="llm-summary-popup__body">' + this.escapeHTML(summary) + '</div>';
        document.body.appendChild(popup);
        popup.querySelector(".llm-summary-popup__close").addEventListener("click", this.closeLlmSummaryPopup.bind(this));

        var rect = anchorEl.getBoundingClientRect();
        var popupWidth = popup.offsetWidth;
        var popupHeight = popup.offsetHeight;
        var left = Math.min(rect.right + 8, window.innerWidth - popupWidth - 16);
        left = Math.max(left, 8);
        var top = Math.min(rect.top, window.innerHeight - popupHeight - 16);
        top = Math.max(top, 8);
        popup.style.left = left + "px";
        popup.style.top = top + "px";
        this._llmPopup = popup;
    };

    OrderTracker.prototype.closeLlmSummaryPopup = function() {
        if (this._llmPopup && this._llmPopup.parentNode) {
            this._llmPopup.parentNode.removeChild(this._llmPopup);
        }
        this._llmPopup = null;
    };

    OrderTracker.prototype.openModal = function(orderId) {
        this.resetForm();
        var status = this.getEl("orderStatus");
        var archiveMeta = this.getEl("archiveMeta");

        if (orderId) {
            this.clearFormDraft();
            var order = this.api.getOrderById(orderId);
            if (!order) return;
            this.currentEditingId = order.id;
            this.getEl("editingOrderId").value = order.id;
            this.getEl("modalTitleText").textContent = order.archived ? "编辑归档项目" : "编辑项目";
            this.getEl("btnConfirmOrder").textContent = "确定保存";
            this.getEl("btnDeleteOrder").hidden = false;
            this.getEl("btnArchiveOrder").hidden = false;
            this.updateOriginalEmailButton(order);
            this.getEl("btnArchiveOrder").textContent = order.archived ? "恢复项目" : "归档项目";
            this.fillForm(order);
            if (status) status.disabled = !!order.archived;
            if (archiveMeta) {
                if (order.archived) {
                    archiveMeta.hidden = false;
                    archiveMeta.textContent = "已归档于 " + (order.archivedAt || "-") + "。归档前阶段：" + this.getStageLabel(order.archivedFromStage || order.stage) + "。恢复后会回到该阶段。";
                } else {
                    archiveMeta.hidden = true;
                    archiveMeta.textContent = "";
                }
            }
            this.updateSyncMeta(order);
            this.loadProjectAttachments(order);
        } else {
            this.restoreFormDraft();
            this.currentEditingId = null;
            this.getEl("editingOrderId").value = "";
            this.getEl("modalTitleText").textContent = "新增项目";
            this.getEl("btnConfirmOrder").textContent = "确定添加";
            this.getEl("btnDeleteOrder").hidden = true;
            this.getEl("btnArchiveOrder").hidden = true;
            this.updateOriginalEmailButton(null);
            if (status) status.disabled = false;
            if (archiveMeta) {
                archiveMeta.hidden = true;
                archiveMeta.textContent = "";
            }
            this.updateSyncMeta(null);
            this.resetAttachmentPanel();
        }

        this.getEl("modalOverlay").classList.add("show");
        this.getEl("orderName").focus();
    };

    OrderTracker.prototype.closeModal = function() {
        if (!this.currentEditingId) {
            this.saveFormDraft();
        }
        this.getEl("modalOverlay").classList.remove("show");
        this.currentEditingId = null;
        this.resetForm();
        this.getEl("editingOrderId").value = "";
        this.getEl("modalTitleText").textContent = "新增项目";
        this.getEl("btnConfirmOrder").textContent = "确定添加";
        this.getEl("btnDeleteOrder").hidden = true;
        this.getEl("btnArchiveOrder").hidden = true;
        this.updateOriginalEmailButton(null);
        var status = this.getEl("orderStatus");
        if (status) status.disabled = false;
        var archiveMeta = this.getEl("archiveMeta");
        if (archiveMeta) {
            archiveMeta.hidden = true;
            archiveMeta.textContent = "";
        }
        this.updateSyncMeta(null);
        this.resetAttachmentPanel();
    };


    OrderTracker.prototype.formatFileSize = function(size) {
        var value = Number(size || 0);
        if (!value || value < 0) return "未知大小";
        var units = ["B", "KB", "MB", "GB"];
        var index = 0;
        while (value >= 1024 && index < units.length - 1) {
            value = value / 1024;
            index += 1;
        }
        var digits = index === 0 ? 0 : (value >= 10 ? 1 : 2);
        return value.toFixed(digits).replace(/\.0+$/, "") + " " + units[index];
    };

    OrderTracker.prototype.getAttachmentIcon = function(filename) {
        var ext = String(filename || "").split(".").pop().toLowerCase();
        if (!ext || ext === filename) return { label: "FILE", cls: "file" };
        if (ext === "pdf") return { label: "PDF", cls: "pdf" };
        if (["doc", "docx"].indexOf(ext) !== -1) return { label: "DOC", cls: "doc" };
        if (["xls", "xlsx", "xlsm", "csv"].indexOf(ext) !== -1) return { label: "XLS", cls: "xls" };
        if (["ppt", "pptx"].indexOf(ext) !== -1) return { label: "PPT", cls: "ppt" };
        if (["jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff"].indexOf(ext) !== -1) return { label: "IMG", cls: "img" };
        if (["zip", "rar", "7z"].indexOf(ext) !== -1) return { label: "ZIP", cls: "zip" };
        if (["msg", "eml"].indexOf(ext) !== -1) return { label: "MSG", cls: "msg" };
        return { label: ext.slice(0, 4).toUpperCase(), cls: "file" };
    };

    OrderTracker.prototype.resetAttachmentPanel = function() {
        var panel = this.getEl("attachmentPanel");
        var list = this.getEl("attachmentList");
        var subtitle = this.getEl("attachmentPanelSubtitle");
        if (panel) panel.hidden = true;
        if (list) list.innerHTML = "";
        if (subtitle) subtitle.textContent = "当前项目关联邮件中的全部附件";
    };

    OrderTracker.prototype.setAttachmentPanelMessage = function(message, cls) {
        var panel = this.getEl("attachmentPanel");
        var list = this.getEl("attachmentList");
        if (!panel || !list) return;
        panel.hidden = false;
        list.innerHTML = '<div class="' + (cls || "attachment-empty") + '">' + this.escapeHTML(message) + '</div>';
    };

    OrderTracker.prototype.renderAttachmentList = function(attachments) {
        var panel = this.getEl("attachmentPanel");
        var list = this.getEl("attachmentList");
        var subtitle = this.getEl("attachmentPanelSubtitle");
        if (!panel || !list) return;
        panel.hidden = false;
        attachments = Array.isArray(attachments) ? attachments : [];
        if (subtitle) {
            subtitle.textContent = attachments.length ? (attachments.length + " 个附件；点击即可用本机默认程序打开") : "当前项目暂无已保存附件";
        }
        if (!attachments.length) {
            list.innerHTML = '<div class="attachment-empty">没有已保存的附件。请先同步 Outlook；系统只显示已经保存到本地的邮件附件。</div>';
            return;
        }
        var self = this;
        list.innerHTML = attachments.map(function(file) {
            var icon = self.getAttachmentIcon(file.filename || "");
            var exists = file.exists !== false;
            var id = self.escapeHTML(file.id);
            var name = self.escapeHTML(file.filename || "未命名附件");
            var emailTime = self.escapeHTML(file.emailTime || file.savedAt || "-");
            var sender = self.escapeHTML(file.emailSender || "-");
            var subject = self.escapeHTML(file.emailSubject || "");
            var folder = self.escapeHTML(file.emailFolder || "");
            var size = self.escapeHTML(self.formatFileSize(file.fileSize));
            var source = subject ? ("来源邮件：" + subject) : "来源邮件：-";
            var title = name + "\n" + source;
            if (folder) title += "\n文件夹：" + folder;
            if (!exists) title += "\n本地文件不存在，需要重新同步。";
            return '<button class="attachment-card' + (exists ? '' : ' attachment-card--missing') + '" type="button" data-open-attachment="' + id + '" title="' + self.escapeHTML(title) + '"' + (exists ? '' : ' disabled') + '>' +
                '<div class="attachment-icon attachment-icon--' + self.escapeHTML(icon.cls) + '">' + self.escapeHTML(icon.label) + '</div>' +
                '<div class="attachment-card__main">' +
                    '<div class="attachment-card__name">' + name + '</div>' +
                    '<div class="attachment-card__meta">' + size + ' ｜ ' + sender + ' ｜ ' + emailTime + '</div>' +
                    '<div class="attachment-card__source">' + self.escapeHTML(source) + '</div>' +
                '</div>' +
            '</button>';
        }).join("");
    };

    OrderTracker.prototype.loadProjectAttachments = function(order) {
        if (!order || !order.id) {
            this.resetAttachmentPanel();
            return;
        }
        var self = this;
        var projectId = order.id;
        var subtitle = this.getEl("attachmentPanelSubtitle");
        if (subtitle) subtitle.textContent = "正在读取附件...";
        this.setAttachmentPanelMessage("正在读取当前项目的邮件附件...", "attachment-loading");
        this.api.getProjectAttachments(projectId)
            .then(function(attachments) {
                if (self.currentEditingId !== projectId) return;
                self.renderAttachmentList(attachments || []);
            })
            .catch(function(error) {
                if (self.currentEditingId !== projectId) return;
                self.setAttachmentPanelMessage(error.message || "附件读取失败", "attachment-error");
            });
    };

    OrderTracker.prototype.openAttachmentForCurrentOrder = function(attachmentId) {
        if (!this.currentEditingId || !attachmentId) return;
        var self = this;
        this.api.openAttachment(this.currentEditingId, attachmentId)
            .then(function(data) {
                self.showToast((data && data.message) || "已打开附件", "success");
            })
            .catch(function(error) {
                self.showToast(error.message || "无法打开附件", "danger");
            });
    };


    OrderTracker.prototype.updateSyncMeta = function(order) {
        var syncMeta = this.getEl("syncMeta");
        if (!syncMeta) return;
        if (!order || (!order.currentProgress && !order.latestEmailTime && !order.latestSender && !order.latestAttachmentDir && !order.latestEmailEntryId)) {
            syncMeta.hidden = true;
            syncMeta.textContent = "";
            return;
        }
        var parts = [];
        if (order.currentProgress) parts.push("邮件进展：" + order.currentProgress);
        if (order.latestEmailTime) parts.push("最新邮件时间：" + order.latestEmailTime);
        if (order.latestSender) parts.push("发件人：" + order.latestSender);
        if (order.latestEmailFolder) parts.push("邮件文件夹：" + order.latestEmailFolder);
        if (order.latestAttachmentDir) parts.push("附件目录：" + order.latestAttachmentDir);
        if (order.needsReview && order.reviewReason) parts.push("需确认：" + order.reviewReason);
        syncMeta.hidden = false;
        syncMeta.textContent = parts.join(" ｜ ");
    };


    OrderTracker.prototype.updateOriginalEmailButton = function(order) {
        var btn = this.getEl("btnOpenOriginalEmail");
        if (!btn) return;
        var canTry = !!(order && (order.latestEmailEntryId || order.currentProgress || order.latestEmailTime || order.latestEmailSubject));
        btn.hidden = !canTry;
        btn.disabled = false;
        btn.title = order && order.latestEmailEntryId
            ? "在传统 Outlook 中打开该项目的最新原始邮件"
            : "尝试按项目编号查找并打开最新原始邮件；旧数据可能需要重新同步";
    };

    OrderTracker.prototype.openOriginalEmailForCurrentOrder = function() {
        if (!this.currentEditingId) return;
        var order = this.api.getOrderById(this.currentEditingId);
        if (!order) return;
        var btn = this.getEl("btnOpenOriginalEmail");
        if (btn) {
            btn.disabled = true;
            btn.textContent = "正在打开...";
        }
        var self = this;
        this.api.openOriginalEmail(order)
            .then(function(data) {
                self.showToast((data && data.message) || "已打开原始邮件", "success");
            })
            .catch(function(error) {
                self.showToast(error.message || "无法打开原始邮件", "danger");
            })
            .finally(function() {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = "查看原始邮件";
                }
            });
    };

    OrderTracker.prototype.resetForm = function() {
        this.getEl("orderName").value = "";
        this.getEl("orderClient").value = "";
        this.getEl("orderAmount").value = "";
        this.getEl("orderContract").value = "";
        this.getEl("orderNotes").value = "";
        this.getEl("orderType").value = "standard";
        this.getEl("orderStatus").value = "sales-contract";
    };

    OrderTracker.prototype.saveFormDraft = function() {
        var data = this.getFormData();
        var value = JSON.stringify(data);
        this.api.saveDraft("pm-draft-order", value);
    };

    OrderTracker.prototype.restoreFormDraft = function() {
        var self = this;
        this.api.loadDraft("pm-draft-order").then(function(raw) {
            var draft = null;
            try {
                draft = JSON.parse(raw);
            } catch (e) {}
            if (!draft) { self.resetForm(); return; }
            self.getEl("orderName").value = draft.name || "";
            self.getEl("orderClient").value = draft.client || "";
            self.getEl("orderAmount").value = draft.amount || "";
            self.getEl("orderContract").value = draft.contract || "";
            self.getEl("orderNotes").value = draft.notes || "";
            self.getEl("orderType").value = draft.type || "standard";
            self.getEl("orderStatus").value = draft.stage || "sales-contract";
        }).catch(function() {
            self.resetForm();
        });
    };

    OrderTracker.prototype.clearFormDraft = function() {
        this.api.deleteDraft("pm-draft-order");
    };

    OrderTracker.prototype.fillForm = function(order) {
        this.getEl("orderName").value = order.name || "";
        this.getEl("orderClient").value = order.client || "";
        this.getEl("orderAmount").value = order.amount || "";
        this.getEl("orderContract").value = order.contract || "";
        this.getEl("orderNotes").value = order.notes || "";
        this.getEl("orderType").value = order.type || "standard";
        this.getEl("orderStatus").value = order.stage || "sales-contract";
    };

    OrderTracker.prototype.getFormData = function() {
        return {
            name: this.getEl("orderName").value.trim(),
            client: this.getEl("orderClient").value.trim(),
            amount: this.getEl("orderAmount").value.trim(),
            type: this.getEl("orderType").value,
            stage: this.getEl("orderStatus").value,
            contract: this.getEl("orderContract").value.trim(),
            notes: this.getEl("orderNotes").value.trim()
        };
    };

    OrderTracker.prototype.submitOrder = function() {
        var data = this.getFormData();

        if (!data.name || !data.client) {
            this.showToast("请填写项目名称描述和客户名称", "warning");
            return;
        }

        if (this.currentEditingId) {
            this.pushUndo("保存项目修改");
            this.api.updateOrder(this.currentEditingId, data);
            this.showToast("项目已保存", "success");
        } else {
            this.clearFormDraft();
            this.pushUndo("新增项目");
            this.api.addOrder(data);
            if (this.isArchivedTab()) this.currentArchiveTab = "active";
            this.showToast("项目已添加", "success");
        }

        this.closeModal();
    };

    OrderTracker.prototype.deleteCurrentOrder = function() {
        if (!this.currentEditingId) return;

        var self = this;
        var order = this.api.getOrderById(this.currentEditingId);
        var projectNo = order ? (order.contract || order.id) : this.currentEditingId;
        this.showDialog({
            title: "删除项目",
            message: "确定要删除项目 " + projectNo + " 吗？此操作不可恢复。",
            confirmText: "删除",
            tone: "danger",
            icon: "×",
            onConfirm: function() {
                self.pushUndo("删除项目");
                self.api.deleteOrder(self.currentEditingId);
                self.closeModal();
                self.showToast("项目已删除", "danger");
            }
        });
    };

    OrderTracker.prototype.toggleCurrentOrderArchive = function() {
        if (!this.currentEditingId) return;

        var self = this;
        var order = this.api.getOrderById(this.currentEditingId);
        if (!order) return;
        var projectNo = order.contract || order.id;
        var archivedFrom = this.getStageLabel(order.archivedFromStage || order.stage);

        if (order.archived) {
            this.showDialog({
                title: "恢复项目",
                message: "确定要将 " + projectNo + " 恢复到归档前阶段（" + archivedFrom + "）吗？",
                confirmText: "恢复",
                tone: "archive",
                icon: "↩",
                onConfirm: function() {
                    self.pushUndo("恢复项目");
                    self.api.restoreOrder(order.id);
                    self.currentArchiveTab = "active";
                    self.closeModal();
                    self.syncArchiveTabs();
                    self.render();
                    self.showToast("项目已恢复到归档前状态", "success");
                }
            });
        } else {
            this.showDialog({
                title: "归档项目",
                message: "确定要归档项目 " + projectNo + " 吗？系统会记录当前阶段（" + archivedFrom + "），之后可恢复。",
                confirmText: "归档",
                tone: "archive",
                icon: "✓",
                onConfirm: function() {
                    self.pushUndo("归档项目");
                    self.api.archiveOrder(order.id);
                    self.currentArchiveTab = "archived";
                    self.closeModal();
                    self.syncArchiveTabs();
                    self.render();
                    self.showToast("项目已归档", "success");
                }
            });
        }
    };

    OrderTracker.prototype.getVisibleOrders = function() {
        // Toolbar "归档全部 / 恢复全部" applies to the whole current archive state, not only a visible column.
        var orders = this.api.getOrders({ archived: this.isArchivedTab() });
        return orders.map(function(order) { return order.id; });
    };

    OrderTracker.prototype.toggleAllVisibleArchiveState = function() {
        var self = this;
        var ids = this.getVisibleOrders();
        if (ids.length === 0) {
            this.showToast(this.isArchivedTab() ? "当前没有可恢复的归档项目" : "当前没有可归档的项目", "warning");
            return;
        }

        if (this.isArchivedTab()) {
            this.showDialog({
                title: "恢复全部",
                message: "确定要恢复全部已归档的 " + ids.length + " 个项目吗？每个项目会回到各自的归档前阶段。",
                confirmText: "恢复全部",
                tone: "archive",
                icon: "↩",
                onConfirm: function() {
                    self.pushUndo("恢复全部");
                    self.api.restoreOrders(ids);
                    self.currentArchiveTab = "active";
                    self.syncArchiveTabs();
                    self.render();
                    self.showToast("已恢复 " + ids.length + " 个项目", "success");
                }
            });
        } else {
            this.showDialog({
                title: "归档全部",
                message: "确定要归档全部进行中的 " + ids.length + " 个项目吗？归档后会集中进入一个“已归档”栏，并记录每个项目的归档前阶段。",
                confirmText: "归档全部",
                tone: "archive",
                icon: "✓",
                onConfirm: function() {
                    self.pushUndo("归档全部");
                    self.api.archiveOrders(ids);
                    self.currentArchiveTab = "archived";
                    self.syncArchiveTabs();
                    self.render();
                    self.showToast("已归档 " + ids.length + " 个项目", "success");
                }
            });
        }
    };

    OrderTracker.prototype.syncArchiveTabs = function() {
        var self = this;
        document.querySelectorAll("[data-archive-tab]").forEach(function(tab) {
            tab.classList.toggle("active", tab.getAttribute("data-archive-tab") === self.currentArchiveTab);
        });
    };

    OrderTracker.prototype.exportExcel = function() {
        var orders = this.api.getOrders(this.getArchiveOptions());
        var headers = ["内部ID", "项目编号", "客户", "金额", "类型", "当前阶段", "项目名称描述", "日期", "备注", "是否归档", "归档日期", "归档前阶段", "挂起"];
        var stageMap = {};
        ORDER_STAGES.forEach(function(s) { stageMap[s.id] = s.label; });

        var rows = orders.map(function(o) {
            return [o.id, o.contract, o.client, o.amount, o.type, stageMap[o.stage] || o.stage, o.name, o.date, o.notes, o.archived ? "是" : "否", o.archivedAt || "", stageMap[o.archivedFromStage] || o.archivedFromStage || "", o.suspended ? "是" : "否"];
        });

        var csv = "\uFEFF" + headers.join(",") + "\n";
        rows.forEach(function(row) {
            csv += row.map(function(cell) { return '"' + String(cell || "").replace(/"/g, '""') + '"'; }).join(",") + "\n";
        });

        var blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = (this.isArchivedTab() ? "已归档项目_" : "进行中项目_") + new Date().toISOString().split("T")[0] + ".csv";
        link.click();
        URL.revokeObjectURL(link.href);
    };

    return OrderTracker;
})();

document.addEventListener("DOMContentLoaded", function() {
    new OrderTracker();
});

// ==============================
// Contract Review JS Logic
// ==============================

function initContractReview() {
    var selectedFiles = { contract: null, cqp: null, ta: null };

    // Upload card buttons
    document.querySelectorAll(".cr-upload-btn").forEach(function(btn) {
        btn.addEventListener("click", function(e) {
            e.stopPropagation();
            var targetId = btn.getAttribute("data-target");
            var fileInput = document.getElementById(targetId);
            if (fileInput) fileInput.click();
        });
    });

    // Also make entire card clickable
    document.querySelectorAll(".cr-upload-card").forEach(function(card) {
        card.addEventListener("click", function() {
            var btn = card.querySelector(".cr-upload-btn");
            if (btn) btn.click();
        });
    });

    // File input change handlers
    var fileInputs = [
        { id: "crFileContract", type: "contract", nameId: "crFileNameContract" },
        { id: "crFileCqp", type: "cqp", nameId: "crFileNameCqp" },
        { id: "crFileTa", type: "ta", nameId: "crFileNameTa" },
    ];

    fileInputs.forEach(function(fi) {
        var input = document.getElementById(fi.id);
        var nameEl = document.getElementById(fi.nameId);
        if (!input || !nameEl) return;

        input.addEventListener("change", function() {
            var file = input.files[0];
            if (file) {
                selectedFiles[fi.type] = file;
                nameEl.textContent = file.name;
                var card = input.closest(".cr-upload-card");
                if (card) card.classList.add("has-file");
            } else {
                selectedFiles[fi.type] = null;
                nameEl.textContent = "";
                var card = input.closest(".cr-upload-card");
                if (card) card.classList.remove("has-file");
            }
            updateStartButton();
        });
    });

    // Start review button
    var startBtn = document.getElementById("btnStartReview");
    if (startBtn) {
        startBtn.addEventListener("click", function() {
            startReview();
        });
    }

    function updateStartButton() {
        if (startBtn) {
            var hasRequired = !!(selectedFiles.contract || selectedFiles.cqp);
            startBtn.disabled = !hasRequired;
        }
    }

    function startReview() {
        var files = [];
        if (selectedFiles.contract) files.push(selectedFiles.contract);
        if (selectedFiles.cqp) files.push(selectedFiles.cqp);
        if (selectedFiles.ta) files.push(selectedFiles.ta);

        if (files.length === 0) {
            showCRToast("请至少上传一个 PDF 文件", "warning");
            return;
        }

        // Show loading
        var uploadSection = document.getElementById("crUploadSection");
        var resultsSection = document.getElementById("crResultsSection");
        var resultsContainer = document.getElementById("crResultsContainer");

        if (uploadSection) uploadSection.hidden = true;
        if (resultsSection) resultsSection.hidden = false;
        if (resultsContainer) {
            resultsContainer.innerHTML = '<div class="cr-loading"><div class="cr-spinner"></div><div class="cr-loading__text">正在解析PDF并执行交叉验证...</div></div>';
        }

        // Build FormData
        var formData = new FormData();
        files.forEach(function(f) {
            formData.append("files", f);
        });

        // Send to backend
        fetch("/api/contract-review", {
            method: "POST",
            body: formData,
        })
        .then(function(resp) {
            if (!resp.ok) {
                return resp.json().then(function(err) {
                    throw new Error(err.error || "Request failed");
                });
            }
            return resp.json();
        })
        .then(function(report) {
            renderReport(report);
        })
        .catch(function(err) {
            if (resultsContainer) {
                resultsContainer.innerHTML = '<div class="cr-result-card"><div class="cr-result-card__header"><div class="cr-status-icon cr-status-blocked">!</div>审查失败</div><div class="cr-result-card__body"><p>' + escapeHTML(err.message || "未知错误") + '</p><button class="btn btn--outline" onclick="location.reload()">重试</button></div></div>';
            }
            showCRToast(err.message || "审查请求失败", "danger");
        });
    }

    function renderReport(report) {
        var container = document.getElementById("crResultsContainer");
        if (!container) return;

        var html = "";

        // =============================================
        // 1. LLM DeepSeek Review (Primary AI Assessment)
        // =============================================
        var llmReview = report.llm_review || {};
        if (llmReview.overall_assessment || llmReview.summary) {
            var llmBannerClass = "cr-conclusion-banner--blocked";
            if (llmReview.overall_assessment === "Pass") llmBannerClass = "cr-conclusion-banner--pass";
            else if (llmReview.overall_assessment === "Pass with notes") llmBannerClass = "cr-conclusion-banner--warning";

            var llmTitle = llmReview.overall_assessment || "Unknown";
            if (llmReview.error) llmTitle = "AI 审核失败";
            var llmTitleLabel = {
                "Blocked": "AI 审查：阻塞",
                "Pass": "AI 审查：通过",
                "Pass with notes": "AI 审查：通过（有备注）",
                "Unknown": "AI 审查：无法判定",
            };

            html += '<div class="cr-result-card cr-result-card--ai">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-ai">🤖</div>AI 综合审核（DeepSeek）' +
                    (llmReview.confidence != null ? '<span class="cr-ai-confidence">置信度: ' + (llmReview.confidence * 100).toFixed(0) + '%</span>' : '') +
                '</div>' +
                '<div class="cr-result-card__body">' +
                    '<div class="cr-conclusion-banner ' + llmBannerClass + '">' +
                        '<div class="cr-conclusion-banner__title">' + escapeHTML(llmTitleLabel[llmTitle] || llmTitle) + '</div>' +
                    '</div>' +
                    (llmReview.summary ? '<div class="cr-ai-summary">' + escapeHTML(llmReview.summary) + '</div>' : '') +
                    (llmReview.completeness_notes ? '<div class="cr-ai-section"><strong>数据完整性评估：</strong>' + escapeHTML(llmReview.completeness_notes) + '</div>' : '') +
                    (llmReview.key_risks && llmReview.key_risks.length > 0 ?
                        '<div class="cr-ai-section"><strong>关键风险：</strong><div class="cr-issue-list">' +
                        llmReview.key_risks.map(function(r) {
                            var sevClass = "cr-risk--" + (r.severity || "medium");
                            return '<div class="cr-issue-item ' + sevClass + '">' +
                                '<div class="cr-issue-item__icon">!</div>' +
                                '<div class="cr-issue-item__text"><strong>' + escapeHTML(r.risk) + '</strong>' +
                                (r.suggestion ? '<br><em>' + escapeHTML(r.suggestion) + '</em>' : '') + '</div>' +
                            '</div>';
                        }).join("") + '</div></div>' : "") +
                    (llmReview.non_blocker_issues && llmReview.non_blocker_issues.length > 0 ?
                        '<div class="cr-ai-section"><strong>非阻塞备注：</strong><div class="cr-issue-list">' +
                        llmReview.non_blocker_issues.map(function(i) {
                            return '<div class="cr-issue-item cr-issue-item--non-blocker">' +
                                '<div class="cr-issue-item__icon">i</div>' +
                                '<div class="cr-issue-item__text"><strong>' + escapeHTML(i.issue) + '</strong>' +
                                (i.note ? '<br><em>' + escapeHTML(i.note) + '</em>' : '') + '</div>' +
                            '</div>';
                        }).join("") + '</div></div>' : "") +
                    (llmReview.next_steps && llmReview.next_steps.length > 0 ?
                        '<div class="cr-ai-section"><strong>推荐操作：</strong><ul class="cr-next-steps">' +
                        llmReview.next_steps.map(function(s) { return '<li>' + escapeHTML(s) + '</li>'; }).join("") +
                        '</ul></div>' : "") +
                    (llmReview.error ? '<div class="cr-ai-error">' + escapeHTML(llmReview.error) + '</div>' : '') +
                '</div>' +
            '</div>';
        }

        // =============================================
        // 2. Rule-based Conclusion Banner
        // =============================================
        var conclusion = report.conclusion || "Unknown";
        var bannerClass = "cr-conclusion-banner--blocked";
        if (conclusion === "Pass") bannerClass = "cr-conclusion-banner--pass";
        else if (conclusion.indexOf("notes") >= 0 || conclusion.indexOf("Pass") >= 0) bannerClass = "cr-conclusion-banner--warning";

        var conclusionLabels = {
            "Blocked": "规则审查：阻塞",
            "Pass": "规则审查：通过",
            "Pass with notes": "规则审查：通过（有备注）",
        };

        html += '<div class="cr-result-card">' +
            '<div class="cr-result-card__header">' +
                '<div class="cr-status-icon cr-status-blocked">' + (conclusion === "Pass" ? "✓" : "!") + '</div>' +
                '规则审查结论' +
            '</div>' +
            '<div class="cr-result-card__body">' +
                '<div class="cr-conclusion-banner ' + bannerClass + '">' +
                    '<div class="cr-conclusion-banner__title">' + escapeHTML(conclusionLabels[conclusion] || conclusion) + '</div>' +
                    (report.blockers && report.blockers.length > 0 ? '<div class="cr-conclusion-banner__reason">阻塞原因: ' + escapeHTML(report.blockers.map(function(b) { return b.type; }).join("; ")) + '</div>' : "") +
                '</div>' +
            '</div>' +
        '</div>';

        // =============================================
        // 3. Source Recognition
        // =============================================
        var sources = report.source_recognition || {};
        html += '<div class="cr-result-card">' +
            '<div class="cr-result-card__header">' +
                '<div class="cr-status-icon cr-status-info">📄</div>文件识别' +
            '</div>' +
            '<div class="cr-result-card__body">' +
                '<div class="cr-source-list">' +
                    '<div class="cr-source-item"><span class="cr-source-item__dot cr-source-item__dot--' + (sources.contract && sources.contract.status === "found" ? "found" : "not-found") + '"></span>销售合同: ' + escapeHTML((sources.contract && sources.contract.status) || "未知") + (sources.contract && sources.contract.page_count ? ' (' + sources.contract.page_count + '页)' : '') + '</div>' +
                    '<div class="cr-source-item"><span class="cr-source-item__dot cr-source-item__dot--' + (sources.cqp && sources.cqp.status === "found" ? "found" : "not-found") + '"></span>报价单 CQP: ' + escapeHTML((sources.cqp && sources.cqp.status) || "未知") + (sources.cqp && sources.cqp.page_count ? ' (' + sources.cqp.page_count + '页)' : '') + '</div>' +
                    '<div class="cr-source-item"><span class="cr-source-item__dot cr-source-item__dot--' + (sources.ta && (sources.ta.status === "standalone" || sources.ta.status === "embedded") ? "found" : (sources.ta && sources.ta.status === "embedded" ? "embedded" : "not-found")) + '"></span>技术协议 TA: ' + escapeHTML((sources.ta && sources.ta.status) || "未知") + (sources.ta && sources.ta.page_count ? ' (' + sources.ta.page_count + '页)' : '') + '</div>' +
                '</div>' +
            '</div>' +
        '</div>';

        // =============================================
        // 4. Extracted Data (Raw extraction results from PDFs)
        // =============================================
        var extractedData = report.extracted_data || {};
        if (extractedData.contract || extractedData.cqp || extractedData.ta) {
            html += '<div class="cr-result-card">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-info">📋</div>原始提取数据（过程）' +
                '</div>' +
                '<div class="cr-result-card__body">';

            // Contract extracted fields
            if (extractedData.contract && Object.keys(extractedData.contract).length > 0) {
                html += '<div class="cr-extract-section">' +
                    '<h4 class="cr-extract-section__title">📄 销售合同 提取字段</h4>' +
                    '<div class="cr-extract-table-wrap"><table class="cr-extract-table">' +
                    renderExtractRow("合同编号", extractedData.contract.contract_number) +
                    renderExtractRow("卖方", extractedData.contract.seller_name) +
                    renderExtractRow("买方", extractedData.contract.buyer_name) +
                    renderExtractRow("买方地址", extractedData.contract.buyer_address) +
                    renderExtractRow("最终用户", extractedData.contract.end_customer_name) +
                    renderExtractRow("安装地点", extractedData.contract.end_customer_address) +
                    renderExtractRow("贸易条款", extractedData.contract.incoterm_selection) +
                    renderExtractRow("交付地点", extractedData.contract.delivery_location) +
                    renderExtractRow("增值税率", extractedData.contract.vat_rate) +
                    renderExtractRow("未税金额", extractedData.contract.untaxed_amount) +
                    renderExtractRow("含税金额", extractedData.contract.tax_included_amount) +
                    renderExtractRow("总数量", extractedData.contract.total_qty) +
                    renderExtractRow("销售", extractedData.contract.sales_person) +
                    renderExtractRow("PM", extractedData.contract.pm) +
                    renderExtractRobotModels("机器人型号", extractedData.contract.robot_models) +
                    renderExtractRow("质保条款", extractedData.contract.warranty_clause_5_2 ? (extractedData.contract.warranty_clause_5_2.standard || "") : "") +
                    '</table></div></div>';
            }

            // CQP extracted fields
            if (extractedData.cqp && Object.keys(extractedData.cqp).length > 0) {
                html += '<div class="cr-extract-section">' +
                    '<h4 class="cr-extract-section__title">💰 报价单 CQP 提取字段</h4>' +
                    '<div class="cr-extract-table-wrap"><table class="cr-extract-table">' +
                    renderExtractRow("CQP编号", extractedData.cqp.cqp_number) +
                    renderExtractRow("客户名称", extractedData.cqp.customer_name) +
                    renderExtractRow("客户地址", extractedData.cqp.customer_address) +
                    renderExtractRow("最终用户", extractedData.cqp.end_user) +
                    renderExtractRow("交付条款", extractedData.cqp.delivery_term) +
                    renderExtractRow("交付周期", extractedData.cqp.delivery_time) +
                    renderExtractRow("付款条款", extractedData.cqp.payment_terms) +
                    renderExtractRow("质保条款", extractedData.cqp.warranty_terms) +
                    renderExtractRow("增值税率", extractedData.cqp.vat_rate) +
                    renderExtractRow("未税总价", extractedData.cqp.untaxed_total) +
                    renderExtractRow("含税总价", extractedData.cqp.tax_included_total) +
                    renderExtractCqpModels("机器人型号/价格", extractedData.cqp.robot_models) +
                    renderExtractRow("质保代码", extractedData.cqp.warranty_codes ? extractedData.cqp.warranty_codes.join(", ") : "") +
                    '</table></div></div>';
            }

            // TA extracted fields
            if (extractedData.ta && Object.keys(extractedData.ta).length > 0) {
                html += '<div class="cr-extract-section">' +
                    '<h4 class="cr-extract-section__title">🔧 技术协议 TA 提取字段</h4>' +
                    '<div class="cr-extract-table-wrap"><table class="cr-extract-table">' +
                    renderExtractRobotModels("机器人型号", extractedData.ta.robot_models) +
                    renderExtractRow("质保代码", extractedData.ta.warranty_codes ? extractedData.ta.warranty_codes.join(", ") : "") +
                    '</table></div></div>';
            }

            html += '</div></div>';
        }

        // Incoterm
        var incoterm = report.incoterm || {};
        html += '<div class="cr-result-card">' +
            '<div class="cr-result-card__header">' +
                '<div class="cr-status-icon cr-status-info">🌐</div>贸易术语判定' +
            '</div>' +
            '<div class="cr-result-card__body">' +
                '<p><strong>结论:</strong> ' + escapeHTML(incoterm.conclusion || "未确定") + '</p>' +
                '<p><strong>合同证据:</strong> ' + escapeHTML(incoterm.contract_evidence || "无") + '</p>' +
                '<p><strong>CQP证据:</strong> ' + escapeHTML(incoterm.cqp_evidence || "无") + '</p>' +
                '<p><strong>一致性:</strong> ' + (incoterm.consistent ? "✓ 一致" : "✗ 不一致") + '</p>' +
            '</div>' +
        '</div>';

        // Key Checks Table
        var keyChecks = report.key_checks || [];
        if (keyChecks.length > 0) {
            html += '<div class="cr-result-card">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-info">✅</div>关键校验项' +
                '</div>' +
                '<div class="cr-result-card__body">' +
                    '<table class="cr-check-table">' +
                        '<thead><tr><th>校验项</th><th>状态</th><th>详情</th></tr></thead>' +
                        '<tbody>';
            keyChecks.forEach(function(check) {
                var badgeClass = "cr-badge--pass";
                if (check.status === "MISMATCH") badgeClass = "cr-badge--mismatch";
                else if (check.status === "WARNING") badgeClass = "cr-badge--warning";
                html += '<tr>' +
                    '<td>' + escapeHTML(check.check_name) + '</td>' +
                    '<td><span class="cr-badge ' + badgeClass + '">' + escapeHTML(check.status) + '</span></td>' +
                    '<td>' + escapeHTML(check.detail || "") + '</td>' +
                '</tr>';
            });
            html += '</tbody></table></div></div>';
        }

        // Blockers
        var blockers = report.blockers || [];
        if (blockers.length > 0) {
            html += '<div class="cr-result-card">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-blocked">🚫</div>BLOCKER 问题 (' + blockers.length + ')' +
                '</div>' +
                '<div class="cr-result-card__body"><div class="cr-issue-list">';
            blockers.forEach(function(b) {
                html += '<div class="cr-issue-item cr-issue-item--blocker">' +
                    '<div class="cr-issue-item__icon">!</div>' +
                    '<div class="cr-issue-item__text"><strong>' + escapeHTML(b.type) + ':</strong> ' + escapeHTML(b.detail || "") + '</div>' +
                '</div>';
            });
            html += '</div></div></div>';
        }

        // Non-blockers
        var nonBlockers = report.non_blockers || [];
        if (nonBlockers.length > 0) {
            html += '<div class="cr-result-card">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-warning">⚠</div>非阻塞备注 (' + nonBlockers.length + ')' +
                '</div>' +
                '<div class="cr-result-card__body"><div class="cr-issue-list">';
            nonBlockers.forEach(function(nb) {
                html += '<div class="cr-issue-item cr-issue-item--non-blocker">' +
                    '<div class="cr-issue-item__icon">i</div>' +
                    '<div class="cr-issue-item__text"><strong>' + escapeHTML(nb.type) + ':</strong> ' + escapeHTML(nb.detail || "") + '</div>' +
                '</div>';
            });
            html += '</div></div></div>';
        }

        // Warranty
        var warranty = report.warranty || {};
        html += '<div class="cr-result-card">' +
            '<div class="cr-result-card__header">' +
                '<div class="cr-status-icon cr-status-info">🛡</div>质保检查' +
            '</div>' +
            '<div class="cr-result-card__body">' +
                '<p><strong>一致性:</strong> ' + (warranty.consistent ? "✓ 一致" : "✗ 不一致") + '</p>' +
                '<p>' + escapeHTML(warranty.detail || "") + '</p>' +
                (warranty.cqp_warranty_codes && warranty.cqp_warranty_codes.length > 0 ?
                    '<p><strong>CQP质保代码:</strong> ' + escapeHTML(warranty.cqp_warranty_codes.join(", ")) + '</p>' : "") +
            '</div>' +
        '</div>';

        // Financial Summary
        var financial = report.financial || {};
        if (financial.vat_check) {
            var vat = financial.vat_check;
            var untaxed = financial.untaxed_check || {};
            var taxed = financial.tax_included_check || {};
            html += '<div class="cr-result-card">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-info">💰</div>财务校验' +
                '</div>' +
                '<div class="cr-result-card__body">' +
                    '<p><strong>增值税率:</strong> ' + escapeHTML(vat.status) + ' (合同: ' + (vat.contract_vat * 100).toFixed(0) + '%, CQP: ' + (vat.cqp_vat * 100).toFixed(0) + '%)</p>' +
                    '<p><strong>未税金额:</strong> ' + escapeHTML(untaxed.status) + ' (差异: ¥' + (untaxed.diff || 0).toFixed(2) + ')</p>' +
                    '<p><strong>含税金额:</strong> ' + escapeHTML(taxed.status) + ' (差异: ¥' + (taxed.diff || 0).toFixed(4) + (taxed.is_rounding ? ', 舍入误差' : "") + ')</p>' +
                '</div>' +
            '</div>';
        }

        // BT09 Draft
        if (report.bt09_draft) {
            html += '<div class="cr-result-card">' +
                '<div class="cr-result-card__header">' +
                    '<div class="cr-status-icon cr-status-pass">✉</div>BT09 邮件草稿' +
                '</div>' +
                '<div class="cr-result-card__body">' +
                    '<div class="cr-bt09-draft">' + escapeHTML(report.bt09_draft) + '</div>' +
                '</div>' +
            '</div>';
        }

        container.innerHTML = html;
    }

    function showCRToast(message, tone) {
        try {
            // Reuse OrderTracker toast if available
            var container = document.getElementById("toastContainer");
            if (!container) return;
            var toast = document.createElement("div");
            toast.className = "toast toast--" + (tone || "success");
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(function() { toast.style.opacity = "0"; toast.style.transform = "translateY(8px)"; }, 2400);
            setTimeout(function() { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 2900);
        } catch (e) {}
    }

    function escapeHTML(str) {
        return String(str || "")
            .replace(/\x26/g, "\x26amp;")
            .replace(/</g, "\x26lt;")
            .replace(/>/g, "\x26gt;")
            .replace(/"/g, "\x26quot;")
            .replace(/'/g, "\x26#039;");
    }

    function renderExtractRow(label, value) {
        var display = (value != null && value !== "") ? String(value) : '<span class="cr-extract-na">未提取</span>';
        if (typeof value === "number") display = value.toLocaleString ? value.toLocaleString() : String(value);
        return '<tr><td class="cr-extract-label">' + escapeHTML(label) + '</td><td class="cr-extract-value">' + (value != null && value !== "" ? escapeHTML(String(value)) : '<span class="cr-extract-na">未提取</span>') + '</td></tr>';
    }

    function renderExtractRobotModels(label, models) {
        if (!models || !Array.isArray(models) || models.length === 0) {
            return '<tr><td class="cr-extract-label">' + escapeHTML(label) + '</td><td class="cr-extract-value"><span class="cr-extract-na">未提取</span></td></tr>';
        }
        var parts = models.map(function(m) {
            return escapeHTML(m.model || "?") + ' ×' + (m.qty || 1);
        });
        return '<tr><td class="cr-extract-label">' + escapeHTML(label) + '</td><td class="cr-extract-value">' + parts.join("; ") + '</td></tr>';
    }

    function renderExtractCqpModels(label, models) {
        if (!models || !Array.isArray(models) || models.length === 0) {
            return '<tr><td class="cr-extract-label">' + escapeHTML(label) + '</td><td class="cr-extract-value"><span class="cr-extract-na">未提取</span></td></tr>';
        }
        var parts = models.map(function(m) {
            var s = escapeHTML(m.model || "?") + ' ×' + (m.qty || 1);
            if (m.unit_price) s += ' @¥' + m.unit_price;
            if (m.total_price) s += ' (=¥' + m.total_price + ')';
            return s;
        });
        return '<tr><td class="cr-extract-label">' + escapeHTML(label) + '</td><td class="cr-extract-value">' + parts.join("; ") + '</td></tr>';
    }
}
