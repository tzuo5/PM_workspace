/**
 * PM Workplace - Project Tracker API
 *
 * The front end keeps an in-memory copy for instant UI response, while all
 * project data is persisted to the local Flask/SQLite backend when available.
 */

var OrderTrackerAPI = (function() {

    function OrderTrackerAPI() {
        this.orders = [];
        this.listeners = {};
        this.nextId = 1;
        this.backendAvailable = false;
        this.apiBase = this.detectApiBase();
        this.loadFromServer();
    }

    OrderTrackerAPI.prototype.detectApiBase = function() {
        if (window.location.protocol === "file:") {
            return "http://127.0.0.1:5050";
        }
        return "";
    };

    OrderTrackerAPI.prototype.request = function(path, options) {
        var opts = options || {};
        opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
        return fetch(this.apiBase + path, opts).then(function(response) {
            return response.json().catch(function() { return {}; }).then(function(data) {
                if (!response.ok || data.ok === false) {
                    throw new Error(data.error || data.message || ("HTTP " + response.status));
                }
                return data;
            });
        });
    };

    OrderTrackerAPI.prototype.normalizeArchivedFlag = function(options) {
        if (!options || typeof options.archived === "undefined") return false;
        return !!options.archived;
    };

    OrderTrackerAPI.prototype.normalizeOrder = function(order) {
        order = order || {};
        order.id = order.id || order.contract || ("ORD-" + String(this.nextId++).padStart(3, "0"));
        order.contract = order.contract || order.id;
        order.name = order.name || order.latestEmailSubject || order.contract || order.id;
        order.client = order.client || "";
        order.amount = order.amount || "";
        order.type = order.type || "standard";
        order.stage = order.stage || "sales-contract";
        order.date = order.date || (order.latestEmailTime ? String(order.latestEmailTime).slice(0, 10) : new Date().toISOString().split("T")[0]);
        order.notes = order.notes || "";
        order.favorite = !!order.favorite;
        order.suspended = !!order.suspended;
        order.archived = !!order.archived;
        order.archivedAt = order.archivedAt || "";
        order.archivedFromStage = order.archivedFromStage || (order.archived ? order.stage : "");
        order.stageDates = order.stageDates || {};
        order.currentProgress = order.currentProgress || "";
        order.latestEmailTime = order.latestEmailTime || "";
        order.latestEmailSubject = order.latestEmailSubject || "";
        order.latestSender = order.latestSender || "";
        order.needsReview = !!order.needsReview;
        order.reviewReason = order.reviewReason || "";
        order.manualOverride = !!order.manualOverride;
        order.latestAttachmentDir = order.latestAttachmentDir || "";
        order.latestEmailEntryId = order.latestEmailEntryId || "";
        order.latestEmailStoreId = order.latestEmailStoreId || "";
        order.latestEmailFolder = order.latestEmailFolder || "";
        return order;
    };

    OrderTrackerAPI.prototype.cloneOrders = function() {
        return JSON.parse(JSON.stringify(this.orders || []));
    };

    OrderTrackerAPI.prototype.restoreSnapshot = function(snapshot) {
        var self = this;
        var restored = Array.isArray(snapshot) ? snapshot : [];
        this.orders = restored.map(function(order) { return self.normalizeOrder(order); });
        this.recomputeNextId();
        this.emit("ordersUpdated");
        return this.request("/api/projects/snapshot", {
            method: "POST",
            body: JSON.stringify({ projects: this.cloneOrders() })
        }).catch(function(error) {
            console.warn("Project snapshot restore failed:", error);
        });
    };

    OrderTrackerAPI.prototype.recomputeNextId = function() {
        var max = 0;
        this.orders.forEach(function(order) {
            var match = String(order.id || "").match(/^ORD-(\d+)$/);
            if (match) max = Math.max(max, parseInt(match[1], 10));
        });
        this.nextId = max + 1;
    };

    OrderTrackerAPI.prototype.loadFromServer = function() {
        var self = this;
        return this.request("/api/projects")
            .then(function(data) {
                self.backendAvailable = true;
                self.orders = (data.projects || []).map(function(order) { return self.normalizeOrder(order); });
                self.recomputeNextId();
                self.emit("ordersUpdated");
                self.emit("backendReady");
                return self.orders;
            })
            .catch(function(error) {
                self.backendAvailable = false;
                self.orders = [];
                self.emit("ordersUpdated");
                self.emit("backendError", error);
                return [];
            });
    };

    OrderTrackerAPI.prototype.persistOrder = function(order) {
        var method = "PUT";
        var path = "/api/projects/" + encodeURIComponent(order.id);
        return this.request(path, { method: method, body: JSON.stringify(order) })
            .catch(function(error) {
                console.warn("Project persistence failed:", error);
            });
    };

    OrderTrackerAPI.prototype.deleteFromServer = function(orderId) {
        return this.request("/api/projects/" + encodeURIComponent(orderId), { method: "DELETE" })
            .catch(function(error) {
                console.warn("Project delete persistence failed:", error);
            });
    };

    OrderTrackerAPI.prototype.deleteOrdersFromServer = function(orderIds) {
        return this.request("/api/projects/bulk-delete", {
            method: "POST",
            body: JSON.stringify({ ids: orderIds || [] })
        }).catch(function(error) {
            console.warn("Project bulk delete persistence failed:", error);
        });
    };

    OrderTrackerAPI.prototype.filterByArchiveState = function(orders, options) {
        var archived = this.normalizeArchivedFlag(options);
        return orders.filter(function(o) { return !!o.archived === archived; });
    };

    OrderTrackerAPI.prototype.getOrders = function(options) {
        return this.filterByArchiveState(this.orders, options).slice();
    };

    OrderTrackerAPI.prototype.getAllOrders = function() {
        return this.orders.slice();
    };

    OrderTrackerAPI.prototype.getOrderById = function(orderId) {
        return this.orders.find(function(o) { return o.id === orderId; }) || null;
    };

    OrderTrackerAPI.prototype.getOrdersByStage = function(options) {
        var grouped = {};
        var orders = this.filterByArchiveState(this.orders, options);
        ORDER_STAGES.forEach(function(stage) {
            grouped[stage.id] = orders.filter(function(o) { return o.stage === stage.id; });
        });
        return grouped;
    };

    OrderTrackerAPI.prototype.getCounts = function(options) {
        var counts = {};
        var currentOrders = this.filterByArchiveState(this.orders, options);
        var activeOrders = this.filterByArchiveState(this.orders, { archived: false });
        var archivedOrders = this.filterByArchiveState(this.orders, { archived: true });
        ORDER_STAGES.forEach(function(stage) {
            counts[stage.id] = currentOrders.filter(function(o) { return o.stage === stage.id; }).length;
        });
        counts.total = currentOrders.length;
        counts.activeTotal = activeOrders.length;
        counts.archivedTotal = archivedOrders.length;
        counts.allTotal = this.orders.length;
        return counts;
    };

    OrderTrackerAPI.prototype.addOrder = function(orderData) {
        var id = "ORD-" + String(this.nextId++).padStart(3, "0");
        var today = new Date().toISOString().split("T")[0];
        var stage = orderData.stage || "sales-contract";
        var stageDates = {};
        stageDates[stage] = today;
        var order = this.normalizeOrder({
            id: id,
            name: orderData.name,
            client: orderData.client,
            amount: orderData.amount || "",
            type: orderData.type || "standard",
            stage: stage,
            contract: orderData.contract || id,
            date: today,
            stageDates: stageDates,
            notes: orderData.notes || "",
            favorite: false,
            suspended: false,
            archived: false,
            archivedAt: "",
            archivedFromStage: "",
            manualOverride: true,
            source: "manual"
        });
        this.orders.unshift(order);
        this.emit("ordersUpdated");
        this.persistOrder(order);
        return order;
    };

    OrderTrackerAPI.prototype.updateOrder = function(orderId, orderData) {
        var order = this.getOrderById(orderId);
        if (!order) return null;
        var previousStage = order.stage;
        order.name = orderData.name;
        order.client = orderData.client;
        order.amount = orderData.amount || "";
        order.type = orderData.type || "standard";
        if (!order.archived) {
            order.stage = orderData.stage || "sales-contract";
        }
        order.contract = orderData.contract || order.id;
        order.notes = orderData.notes || "";
        order.manualOverride = true;
        order.source = "manual";
        order.needsReview = false;
        order.reviewReason = "";
        if (!order.archived && previousStage !== order.stage) {
            var today = new Date().toISOString().split("T")[0];
            order.date = today;
            order.stageDates = order.stageDates || {};
            order.stageDates[order.stage] = today;
        }
        this.emit("ordersUpdated");
        this.persistOrder(order);
        return order;
    };

    OrderTrackerAPI.prototype.updateOrderStage = function(orderId, newStage) {
        var order = this.getOrderById(orderId);
        if (order && !order.archived && order.stage !== newStage) {
            var today = new Date().toISOString().split("T")[0];
            order.stage = newStage;
            order.date = today;
            order.stageDates = order.stageDates || {};
            order.stageDates[newStage] = today;
            order.manualOverride = true;
            order.source = "manual";
            order.needsReview = false;
            order.reviewReason = "";
            this.emit("ordersUpdated");
            this.persistOrder(order);
        }
    };

    OrderTrackerAPI.prototype.toggleFavorite = function(orderId) {
        var order = this.getOrderById(orderId);
        if (order) {
            order.favorite = !order.favorite;
            this.emit("ordersUpdated");
            this.persistOrder(order);
        }
    };

    OrderTrackerAPI.prototype.toggleSuspended = function(orderId) {
        var order = this.getOrderById(orderId);
        if (order) {
            order.suspended = !order.suspended;
            order.manualOverride = true;
            this.emit("ordersUpdated");
            this.persistOrder(order);
        }
    };

    OrderTrackerAPI.prototype.archiveOrder = function(orderId) {
        var order = this.getOrderById(orderId);
        if (!order || order.archived) return null;
        order.archived = true;
        order.archivedAt = new Date().toISOString().split("T")[0];
        order.archivedFromStage = order.stage;
        order.manualOverride = true;
        this.emit("ordersUpdated");
        this.persistOrder(order);
        return order;
    };

    OrderTrackerAPI.prototype.restoreOrder = function(orderId) {
        var order = this.getOrderById(orderId);
        if (!order || !order.archived) return null;
        order.stage = order.archivedFromStage || order.stage;
        order.archived = false;
        order.archivedAt = "";
        order.archivedFromStage = "";
        order.manualOverride = true;
        this.emit("ordersUpdated");
        this.persistOrder(order);
        return order;
    };

    OrderTrackerAPI.prototype.archiveOrders = function(orderIds) {
        var self = this;
        var today = new Date().toISOString().split("T")[0];
        var changed = 0;
        this.orders.forEach(function(order) {
            if (orderIds.indexOf(order.id) !== -1 && !order.archived) {
                order.archived = true;
                order.archivedAt = today;
                order.archivedFromStage = order.stage;
                order.manualOverride = true;
                changed += 1;
                self.persistOrder(order);
            }
        });
        if (changed) this.emit("ordersUpdated");
        return changed;
    };

    OrderTrackerAPI.prototype.restoreOrders = function(orderIds) {
        var self = this;
        var changed = 0;
        this.orders.forEach(function(order) {
            if (orderIds.indexOf(order.id) !== -1 && order.archived) {
                order.stage = order.archivedFromStage || order.stage;
                order.archived = false;
                order.archivedAt = "";
                order.archivedFromStage = "";
                order.manualOverride = true;
                changed += 1;
                self.persistOrder(order);
            }
        });
        if (changed) this.emit("ordersUpdated");
        return changed;
    };

    OrderTrackerAPI.prototype.deleteOrder = function(orderId) {
        this.deleteOrders([orderId]);
    };

    OrderTrackerAPI.prototype.deleteOrders = function(orderIds) {
        var ids = Array.isArray(orderIds) ? orderIds.filter(Boolean) : [];
        if (ids.length === 0) return 0;
        var lookup = {};
        ids.forEach(function(id) { lookup[id] = true; });
        var before = this.orders.length;
        this.orders = this.orders.filter(function(o) { return !lookup[o.id]; });
        var changed = before - this.orders.length;
        if (changed) {
            this.emit("ordersUpdated");
            this.deleteOrdersFromServer(ids);
        }
        return changed;
    };

    OrderTrackerAPI.prototype.searchOrders = function(query, options) {
        var grouped = {};
        var q = (query || "").toLowerCase();
        var filtered = this.filterByArchiveState(this.orders, options);
        if (q) {
            filtered = filtered.filter(function(o) {
                return String(o.name || "").toLowerCase().indexOf(q) !== -1 ||
                       String(o.client || "").toLowerCase().indexOf(q) !== -1 ||
                       String(o.contract || "").toLowerCase().indexOf(q) !== -1 ||
                       String(o.id || "").toLowerCase().indexOf(q) !== -1 ||
                       String(o.currentProgress || "").toLowerCase().indexOf(q) !== -1 ||
                       String(o.latestEmailSubject || "").toLowerCase().indexOf(q) !== -1 ||
                       String(o.notes || "").toLowerCase().indexOf(q) !== -1;
            });
        }
        ORDER_STAGES.forEach(function(stage) {
            grouped[stage.id] = filtered.filter(function(o) { return o.stage === stage.id; });
        });
        return grouped;
    };

    OrderTrackerAPI.prototype.openOriginalEmail = function(order) {
        order = order || {};
        return this.request("/api/outlook/open-email", {
            method: "POST",
            body: JSON.stringify({
                projectId: order.id || "",
                contract: order.contract || "",
                entryId: order.latestEmailEntryId || "",
                storeId: order.latestEmailStoreId || ""
            })
        });
    };

    OrderTrackerAPI.prototype.getProjectAttachments = function(projectId) {
        if (!projectId) return Promise.resolve([]);
        return this.request("/api/projects/" + encodeURIComponent(projectId) + "/attachments")
            .then(function(data) { return data.attachments || []; });
    };

    OrderTrackerAPI.prototype.openAttachment = function(projectId, attachmentId) {
        return this.request("/api/attachments/open", {
            method: "POST",
            body: JSON.stringify({
                projectId: projectId || "",
                attachmentId: attachmentId
            })
        });
    };

    OrderTrackerAPI.prototype.syncOutlook = function(payload, onUpdate) {
        var self = this;
        var pollTimer = null;
        function poll(jobId, resolve, reject) {
            self.request("/api/outlook/sync/" + encodeURIComponent(jobId))
                .then(function(data) {
                    var job = data.job;
                    if (typeof onUpdate === "function") onUpdate(job);
                    if (job.status === "completed") {
                        clearTimeout(pollTimer);
                        self.loadFromServer().then(function() { resolve(job); });
                    } else if (job.status === "cancelled") {
                        clearTimeout(pollTimer);
                        resolve(job);
                    } else if (job.status === "failed") {
                        clearTimeout(pollTimer);
                        reject(new Error(job.error || "同步失败"));
                    } else {
                        pollTimer = setTimeout(function() { poll(jobId, resolve, reject); }, 900);
                    }
                })
                .catch(function(error) {
                    clearTimeout(pollTimer);
                    reject(error);
                });
        }

        return this.request("/api/outlook/sync", { method: "POST", body: JSON.stringify(payload) })
            .then(function(data) {
                if (typeof onUpdate === "function") onUpdate({ jobId: data.jobId, status: "queued", logs: [] });
                return new Promise(function(resolve, reject) {
                    poll(data.jobId, resolve, reject);
                });
            });
    };

    OrderTrackerAPI.prototype.saveDraft = function(key, value) {
        // Always persist to localStorage as fallback
        try { localStorage.setItem(key, value); } catch (e) {}
        // Persist to backend database for durable storage
        return this.request("/api/drafts", {
            method: "POST",
            body: JSON.stringify({ key: key, value: value })
        }).catch(function(error) {
            console.warn("Draft persistence to backend failed:", error);
        });
    };

    OrderTrackerAPI.prototype.loadDraft = function(key) {
        var self = this;
        // Try backend first, fall back to localStorage
        return this.request("/api/drafts/" + encodeURIComponent(key))
            .then(function(data) {
                var value = data.value;
                if (value != null && value !== "") {
                    // Sync localStorage with backend value
                    try { localStorage.setItem(key, value); } catch (e) {}
                    return value;
                }
                // Backend returned empty, try localStorage
                try { return localStorage.getItem(key) || null; } catch (e) { return null; }
            })
            .catch(function() {
                // Backend unavailable, use localStorage
                try { return localStorage.getItem(key) || null; } catch (e) { return null; }
            });
    };

    OrderTrackerAPI.prototype.deleteDraft = function(key) {
        try { localStorage.removeItem(key); } catch (e) {}
        return this.request("/api/drafts/" + encodeURIComponent(key), { method: "DELETE" })
            .catch(function(error) {
                console.warn("Draft deletion from backend failed:", error);
            });
    };

    OrderTrackerAPI.prototype.cancelOutlookSync = function(jobId) {
        if (!jobId) return Promise.reject(new Error("没有正在运行的同步任务。"));
        return this.request("/api/outlook/sync/" + encodeURIComponent(jobId) + "/cancel", {
            method: "POST",
            body: JSON.stringify({})
        });
    };

    OrderTrackerAPI.prototype.on = function(event, callback) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(callback);
    };

    OrderTrackerAPI.prototype.emit = function(event, payload) {
        if (this.listeners[event]) {
            this.listeners[event].forEach(function(cb) { cb(payload); });
        }
    };

    return OrderTrackerAPI;
})();

window.orderAPI = new OrderTrackerAPI();
