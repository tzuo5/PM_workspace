/**
 * Document Check - Main Controller
 */
var DocumentCheck = (function() {
    var apiBase = 'http://127.0.0.1:5050';

    function DocumentCheck() {
        this.caseId = null;
        this.caseData = null;
        this.documents = { A: [], B: [] };
        this.activeDocA = null;
        this.activeDocB = null;
        this.pageNumA = 1;
        this.pageNumB = 1;
        this.pageCountA = 0;
        this.pageCountB = 0;
        this.zoomA = 1.0;
        this.zoomB = 1.0;
        this.pdfDocA = null;
        this.pdfDocB = null;
        this.checkItems = [];
        this.bt09Draft = '';
        this.selectedItem = null;
    }

    DocumentCheck.prototype.init = function() {
        var self = this;
        this.setupUploadZone('A');
        this.setupUploadZone('B');
        this.bindButtons();
        this.setupResizers();
    };

    DocumentCheck.prototype.escapeHTML = function(str) {
        return String(str || '').replace(/&/g,'&').replace(/</g,'<').replace(/>/g,'>');
    };

    DocumentCheck.prototype.request = function(path, options) {
        options = options || {};
        options.headers = Object.assign({'Content-Type': 'application/json'}, options.headers || {});
        return fetch(apiBase + path, options).then(function(r) {
            return r.json().catch(function() { return {}; }).then(function(d) {
                if (!r.ok || d.ok === false) throw new Error(d.error || 'HTTP ' + r.status);
                return d;
            });
        });
    };

    DocumentCheck.prototype.showToast = function(msg, tone) {
        var container = document.getElementById('toastContainer');
        if (!container) return;
        var t = document.createElement('div');
        t.className = 'toast toast--' + (tone || 'success');
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(function() { t.style.opacity='0'; }, 2400);
        setTimeout(function() { if(t.parentNode) t.parentNode.removeChild(t); }, 2900);
    };

    DocumentCheck.prototype.setupUploadZone = function(ws) {
        var self = this;
        var zone = document.getElementById('ws' + ws + 'UploadZone');
        var input = document.getElementById('ws' + ws + 'FileInput');

        if (zone) {
            zone.addEventListener('click', function() { input.click(); });
            zone.addEventListener('dragover', function(e) { e.preventDefault(); zone.classList.add('drag-over'); });
            zone.addEventListener('dragleave', function() { zone.classList.remove('drag-over'); });
            zone.addEventListener('drop', function(e) {
                e.preventDefault();
                zone.classList.remove('drag-over');
                var files = e.dataTransfer.files;
                for (var i = 0; i < files.length; i++) self.uploadFile(ws, files[i]);
            });
        }
        if (input) {
            input.addEventListener('change', function() {
                for (var i = 0; i < input.files.length; i++) self.uploadFile(ws, input.files[i]);
                input.value = '';
            });
        }
    };

    DocumentCheck.prototype.uploadFile = function(ws, file) {
        var self = this;
        if (!this.caseId) {
            this.showToast('Please create a case first.', 'warning');
            return;
        }
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            this.showToast('Only PDF files are supported.', 'warning');
            return;
        }

        // Show uploading indicator
        self.showToast('Uploading ' + file.name + ' to Workspace ' + ws + '...', 'warning');

        var reader = new FileReader();
        reader.onload = function() {
            var base64 = reader.result.split(',')[1];
            var attemptCount = 0;
            var maxAttempts = 2;

            function doUpload() {
                attemptCount++;
                return self.request('/api/document-check/cases/' + self.caseId + '/documents', {
                    method: 'POST',
                    body: JSON.stringify({
                        file_content: base64,
                        original_filename: file.name,
                        workspace: ws
                    })
                });
            }

            function attempt() {
                doUpload().then(function(data) {
                    self.showToast('File uploaded: ' + file.name + ' (Workspace ' + ws + ')', 'success');
                    self.loadDocuments().then(function() {
                        // Auto-open the newly uploaded document
                        var doc = data.document;
                        if (doc && ws) {
                            self.openDocument(ws, doc);
                        }
                    });
                }).catch(function(err) {
                    if (attemptCount < maxAttempts) {
                        // Retry after short delay - server may need warm-up
                        self.showToast('Retrying upload... (attempt ' + (attemptCount + 1) + ')', 'warning');
                        setTimeout(function() {
                            attempt();
                        }, 1500);
                    } else {
                        self.showToast('Upload failed: ' + err.message, 'danger');
                        // Refresh document list to ensure UI is consistent
                        self.loadDocuments().catch(function() {});
                    }
                });
            }

            attempt();
        };
        reader.readAsDataURL(file);
    };

    DocumentCheck.prototype.loadDocuments = function() {
        var self = this;
        if (!this.caseId) return Promise.resolve();
        return this.request('/api/document-check/cases/' + this.caseId).then(function(data) {
            self.caseData = data.case;
            self.documents.A = [];
            self.documents.B = [];
            (data.documents || []).forEach(function(d) {
                var ws = d.workspace || 'A';
                if (!self.documents[ws]) self.documents[ws] = [];
                self.documents[ws].push(d);
            });
            self.renderTabs('A');
            self.renderTabs('B');
            self.updateStatus();
        });
    };

    DocumentCheck.prototype.renderTabs = function(ws) {
        var container = document.getElementById('ws' + ws + 'Tabs');
        var uploadEl = document.getElementById('ws' + ws + 'Upload');
        var viewerEl = document.getElementById('ws' + ws + 'Viewer');
        var docs = this.documents[ws] || [];
        var self = this;

        if (!container) return;

        // Show upload zone when no documents in this workspace
        if (docs.length === 0) {
            container.innerHTML = '';
            if (uploadEl) {
                uploadEl.style.display = 'flex';
                uploadEl.style.visibility = 'visible';
                uploadEl.style.pointerEvents = 'auto';
            }
            if (viewerEl) {
                viewerEl.hidden = true;
            }
            // Hide canvas element for this workspace
            var canvas = document.getElementById('ws' + ws + 'Canvas');
            if (canvas) {
                canvas.style.width = '0px';
                canvas.style.height = '0px';
                canvas.width = 0;
                canvas.height = 0;
            }
            return;
        }

        // Show file tabs, hide upload zone
        if (uploadEl) {
            uploadEl.style.display = 'none';
        }

        container.innerHTML = docs.map(function(d) {
            var activeClass = (ws === 'A' && self.activeDocA && self.activeDocA.id === d.id) ||
                              (ws === 'B' && self.activeDocB && self.activeDocB.id === d.id) ? ' active' : '';
            var dtype = d.manual_type || d.detected_type || 'OTHER';
            return '<div class="dc-file-tab' + activeClass + '" data-doc-id="' + d.id + '" data-ws="' + ws + '">' +
                '<span class="tab-name" title="' + self.escapeHTML(d.original_filename) + '">' + self.escapeHTML(d.original_filename) + '</span>' +
                '<span class="dc-file-type-select">' + dtype + '</span>' +
                '<span class="tab-close" data-doc-id="' + d.id + '">&times;</span>' +
                '</div>';
        }).join('');

        // Bind tab clicks
        container.querySelectorAll('.dc-file-tab').forEach(function(tab) {
            tab.addEventListener('click', function(e) {
                if (e.target.classList.contains('tab-close')) return;
                var docId = tab.dataset.docId;
                var ws = tab.dataset.ws;
                var doc = self.findDoc(docId);
                if (doc) self.openDocument(ws, doc);
            });
        });

        // Bind close buttons
        container.querySelectorAll('.tab-close').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                var docId = btn.dataset.docId;
                self.deleteDocument(docId);
            });
        });
    };

    DocumentCheck.prototype.findDoc = function(docId) {
        for (var ws in this.documents) {
            for (var i = 0; i < (this.documents[ws] || []).length; i++) {
                if (this.documents[ws][i].id === docId) return this.documents[ws][i];
            }
        }
        return null;
    };

    DocumentCheck.prototype.deleteDocument = function(docId) {
        var self = this;
        this.request('/api/document-check/documents/' + docId, { method: 'DELETE' }).then(function() {
            self.showToast('Document deleted', 'success');
            if (self.activeDocA && self.activeDocA.id === docId) self.activeDocA = null;
            if (self.activeDocB && self.activeDocB.id === docId) self.activeDocB = null;
            self.loadDocuments();
        }).catch(function(err) {
            self.showToast('Delete failed: ' + err.message, 'danger');
        });
    };

    DocumentCheck.prototype.openDocument = function(ws, doc, targetPage) {
        if (ws === 'A') {
            this.activeDocA = doc;
            this.pageNumA = targetPage || 1;
            this.loadPDF('A', doc);
        } else {
            this.activeDocB = doc;
            this.pageNumB = targetPage || 1;
            this.loadPDF('B', doc);
        }
        this.renderTabs(ws);
    };

    DocumentCheck.prototype.loadPDF = function(ws, doc) {
        var self = this;
        var viewerEl = document.getElementById('ws' + ws + 'Viewer');
        var canvas = document.getElementById('ws' + ws + 'Canvas');
        if (viewerEl) viewerEl.hidden = false;

        var url = apiBase + '/api/document-check/documents/' + doc.id + '/file';
        pdfjsLib.getDocument(url).promise.then(function(pdf) {
            if (ws === 'A') {
                self.pdfDocA = pdf;
                self.pageCountA = pdf.numPages;
                document.getElementById('wsAPageTotal').textContent = pdf.numPages;
            } else {
                self.pdfDocB = pdf;
                self.pageCountB = pdf.numPages;
                document.getElementById('wsBPageTotal').textContent = pdf.numPages;
            }
            self.renderPage(ws);
        }).catch(function(err) {
            self.showToast('Failed to load PDF: ' + err.message, 'danger');
        });
    };

    DocumentCheck.prototype.renderPage = function(ws) {
        var self = this;
        var pdf = ws === 'A' ? this.pdfDocA : this.pdfDocB;
        var pageNum = ws === 'A' ? this.pageNumA : this.pageNumB;
        var canvas = document.getElementById('ws' + ws + 'Canvas');
        var overlay = document.getElementById('ws' + ws + 'Overlay');
        var wrapper = document.getElementById('ws' + ws + 'CanvasWrapper');
        var zoom = ws === 'A' ? this.zoomA : this.zoomB;

        if (!pdf || !canvas) return;

        document.getElementById('ws' + ws + 'PageInput').value = pageNum;

        // HiDPI support: render at device pixel ratio for sharp text
        var dpr = window.devicePixelRatio || 1;

        pdf.getPage(pageNum).then(function(page) {
            var viewport = page.getViewport({ scale: zoom });
            // Set canvas buffer size at device pixel ratio
            canvas.width = Math.floor(viewport.width * dpr);
            canvas.height = Math.floor(viewport.height * dpr);
            // Set CSS display size
            canvas.style.width = viewport.width + 'px';
            canvas.style.height = viewport.height + 'px';

            if (overlay) {
                overlay.style.width = viewport.width + 'px';
                overlay.style.height = viewport.height + 'px';
            }

            var ctx = canvas.getContext('2d');
            // Scale context to match device pixel ratio
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            page.render({ canvasContext: ctx, viewport: viewport });
            ctx.setTransform(1, 0, 0, 1, 0, 0);

            // Render bbox overlays from evidence
            self.renderBBoxes(ws, overlay, viewport.width, viewport.height, pageNum);
        });
    };

    DocumentCheck.prototype.renderBBoxes = function(ws, overlay, w, h, pageNum) {
        if (!overlay) return;
        overlay.innerHTML = '';

        var self = this;
        this.checkItems.forEach(function(item) {
            (item.evidence_refs || []).forEach(function(evId) {
                // We store bbox data in check items; for now render based on what we have
                if (item._evidenceData && item._evidenceData[evId]) {
                    var ev = item._evidenceData[evId];
                    if (ev.page_number === pageNum) {
                        var bbox = ev.bbox || {};
                        var div = document.createElement('div');
                        div.className = 'dc-bbox ' + item.status.toLowerCase();
                        if (self.selectedItem && self.selectedItem.id === item.id) div.classList.add('active');
                        div.style.left = (bbox.x * w) + 'px';
                        div.style.top = (bbox.y * h) + 'px';
                        div.style.width = (bbox.width * w) + 'px';
                        div.style.height = (bbox.height * h) + 'px';
                        div.title = item.label + ': ' + item.summary;
                        div.addEventListener('click', function() {
                            self.scrollToCheckItem(item.id);
                        });
                        overlay.appendChild(div);
                    }
                }
            });
        });
    };

    DocumentCheck.prototype.showNewCaseDialog = function() {
        var self = this;
        var overlay = document.getElementById('dcNewCaseDialogOverlay');
        var input = document.getElementById('dcNewCaseName');
        var confirmBtn = document.getElementById('dcNewCaseDialogConfirm');
        var cancelBtn = document.getElementById('dcNewCaseDialogCancel');

        if (!overlay) return;

        input.value = '测试';
        overlay.classList.add('show');
        overlay.setAttribute('aria-hidden', 'false');
        input.focus();
        input.select();

        var onConfirm = function() {
            var caseName = input.value.trim();
            if (!caseName) caseName = '测试';
            self.closeNewCaseDialog();
            self.request('/api/document-check/cases', {
                method: 'POST',
                body: JSON.stringify({ name: caseName })
            }).then(function(d) {
                self.caseId = d.case.id;
                self.caseData = d.case;
                self.resetDocumentCheckState();
                self.showToast('New case created: ' + self.caseData.name, 'success');
            }).catch(function(err) {
                self.showToast('Failed to create case: ' + err.message, 'danger');
            });
        };

        var onCancel = function() {
            self.closeNewCaseDialog();
        };

        // Clean up old listeners by cloning nodes
        var newConfirm = confirmBtn.cloneNode(true);
        var newCancel = cancelBtn.cloneNode(true);
        confirmBtn.parentNode.replaceChild(newConfirm, confirmBtn);
        cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);
        newConfirm.addEventListener('click', onConfirm);
        newCancel.addEventListener('click', onCancel);

        // Enter key to confirm
        var onKeydown = function(e) {
            if (e.key === 'Enter') {
                onConfirm();
            } else if (e.key === 'Escape') {
                onCancel();
            }
        };
        input.addEventListener('keydown', onKeydown);

        // Click outside to close
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) onCancel();
        });
    };

    DocumentCheck.prototype.closeNewCaseDialog = function() {
        var overlay = document.getElementById('dcNewCaseDialogOverlay');
        if (overlay) {
            overlay.classList.remove('show');
            overlay.setAttribute('aria-hidden', 'true');
        }
    };

    DocumentCheck.prototype.resetDocumentCheckState = function() {
        // Reset all UI state for new case
        document.getElementById('dcCaseInfo').textContent = 'Case: ' + (this.caseData ? this.caseData.name : 'New');
        document.getElementById('btnRunReview').disabled = false;
        document.getElementById('dcStatusBadge').textContent = 'Ready';
        document.getElementById('dcStatusBadge').className = 'dc-status-badge status-uploaded';

        // Reset documents
        this.documents = { A: [], B: [] };
        this.activeDocA = null;
        this.activeDocB = null;
        this.pdfDocA = null;
        this.pdfDocB = null;
        this.pageNumA = 1;
        this.pageNumB = 1;
        this.pageCountA = 0;
        this.pageCountB = 0;
        this.checkItems = [];
        this.bt09Draft = '';
        this.selectedItem = null;
        this.zoomA = 1.0;
        this.zoomB = 1.0;

        // Reset UI elements
        var viewerA = document.getElementById('wsAViewer');
        var viewerB = document.getElementById('wsBViewer');
        if (viewerA) viewerA.hidden = true;
        if (viewerB) viewerB.hidden = true;

        var canvasA = document.getElementById('wsACanvas');
        var canvasB = document.getElementById('wsBCanvas');
        if (canvasA) { canvasA.width = 0; canvasA.height = 0; canvasA.style.width = '0px'; canvasA.style.height = '0px'; }
        if (canvasB) { canvasB.width = 0; canvasB.height = 0; canvasB.style.width = '0px'; canvasB.style.height = '0px'; }

        var overlayA = document.getElementById('wsAOverlay');
        var overlayB = document.getElementById('wsBOverlay');
        if (overlayA) overlayA.innerHTML = '';
        if (overlayB) overlayB.innerHTML = '';

        this.renderTabs('A');
        this.renderTabs('B');
        this.updateStatus();

        // Reset result panel
        var resultBody = document.getElementById('dcResultBody');
        if (resultBody) {
            resultBody.innerHTML = '<div class="dc-result-empty"><p>Upload documents and run review to see results.</p></div>';
        }
        var concEl = document.getElementById('dcConclusion');
        if (concEl) { concEl.textContent = '-'; concEl.className = 'dc-conclusion'; }

        // Reset buttons
        var btnCopy = document.getElementById('btnCopyBT09');
        if (btnCopy) btnCopy.disabled = true;
    };

    DocumentCheck.prototype.bindButtons = function() {
        var self = this;

        document.getElementById('btnNewCase').addEventListener('click', function() {
            self.showNewCaseDialog();
        });

        document.getElementById('btnRunReview').addEventListener('click', function() {
            if (!self.caseId) return;
            var btn = document.getElementById('btnRunReview');
            btn.disabled = true;
            btn.textContent = 'Running...';
            document.getElementById('dcStatusBadge').textContent = 'Running';
            document.getElementById('dcStatusBadge').className = 'dc-status-badge status-running';

            self.request('/api/document-check/cases/' + self.caseId + '/run', { method: 'POST', body: '{}' }).then(function() {
                // Poll for status
                self.pollReviewStatus();
            }).catch(function(err) {
                self.showToast('Review failed: ' + err.message, 'danger');
                btn.disabled = false;
                btn.textContent = 'Run Review';
            });
        });

        document.getElementById('btnCopyBT09').addEventListener('click', function() {
            if (self.bt09Draft) {
                navigator.clipboard.writeText(self.bt09Draft).then(function() {
                    self.showToast('BT09 draft copied to clipboard', 'success');
                });
            }
        });

        // Page navigation A
        ['APrev', 'ANext'].forEach(function(id) {
            var btn = document.getElementById('ws' + id);
            var isPrev = id.indexOf('Prev') >= 0;
            var ws = 'A';
            if (btn) btn.addEventListener('click', function() {
                if (isPrev) self.pageNumA = Math.max(1, self.pageNumA - 1);
                else self.pageNumA = Math.min(self.pageCountA, self.pageNumA + 1);
                self.renderPage(ws);
            });
        });
        document.getElementById('wsAPageInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { self.pageNumA = parseInt(e.target.value) || 1; self.renderPage('A'); }
        });
        document.getElementById('wsAZoomIn').addEventListener('click', function() { self.zoomA *= 1.2; self.renderPage('A'); self.updateZoomLabel('A'); });
        document.getElementById('wsAZoomOut').addEventListener('click', function() { self.zoomA = Math.max(0.3, self.zoomA / 1.2); self.renderPage('A'); self.updateZoomLabel('A'); });
        document.getElementById('wsAFitWidth').addEventListener('click', function() {
            var wrapper = document.getElementById('wsACanvasWrapper');
            if (wrapper) { self.zoomA = (wrapper.clientWidth - 20) / (self.pdfDocA ? 612 : 612); self.renderPage('A'); self.updateZoomLabel('A'); }
        });

        // Page navigation B
        ['BPrev', 'BNext'].forEach(function(id) {
            var btn = document.getElementById('ws' + id);
            var isPrev = id.indexOf('Prev') >= 0;
            var ws = 'B';
            if (btn) btn.addEventListener('click', function() {
                if (isPrev) self.pageNumB = Math.max(1, self.pageNumB - 1);
                else self.pageNumB = Math.min(self.pageCountB, self.pageNumB + 1);
                self.renderPage(ws);
            });
        });
        document.getElementById('wsBPageInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { self.pageNumB = parseInt(e.target.value) || 1; self.renderPage('B'); }
        });
        document.getElementById('wsBZoomIn').addEventListener('click', function() { self.zoomB *= 1.2; self.renderPage('B'); self.updateZoomLabel('B'); });
        document.getElementById('wsBZoomOut').addEventListener('click', function() { self.zoomB = Math.max(0.3, self.zoomB / 1.2); self.renderPage('B'); self.updateZoomLabel('B'); });
        document.getElementById('wsBFitWidth').addEventListener('click', function() {
            var wrapper = document.getElementById('wsBCanvasWrapper');
            if (wrapper) { self.zoomB = (wrapper.clientWidth - 20) / (self.pdfDocB ? 612 : 612); self.renderPage('B'); self.updateZoomLabel('B'); }
        });
    };

    DocumentCheck.prototype.updateZoomLabel = function(ws) {
        var el = document.getElementById('ws' + ws + 'ZoomLabel');
        if (el) el.textContent = Math.round((ws === 'A' ? this.zoomA : this.zoomB) * 100) + '%';
    };

    DocumentCheck.prototype.pollReviewStatus = function() {
        var self = this;
        var count = 0;
        function check() {
            self.request('/api/document-check/cases/' + self.caseId + '/status').then(function(data) {
                var st = data.status || {};
                if (st.status === 'completed') {
                    document.getElementById('dcStatusBadge').textContent = 'Completed';
                    document.getElementById('dcStatusBadge').className = 'dc-status-badge status-completed';
                    var btn = document.getElementById('btnRunReview');
                    btn.disabled = false;
                    btn.textContent = 'Re-run';
                    self.loadResults();
                } else if (st.status === 'failed') {
                    document.getElementById('dcStatusBadge').textContent = 'Failed';
                    document.getElementById('dcStatusBadge').className = 'dc-status-badge status-failed';
                    var btn = document.getElementById('btnRunReview');
                    btn.disabled = false;
                    btn.textContent = 'Retry';
                    self.showToast('Review failed: ' + (st.error || 'Unknown error'), 'danger');
                } else {
                    count++;
                    if (count < 30) setTimeout(check, 1500);
                    else {
                        var btn = document.getElementById('btnRunReview');
                        btn.disabled = false;
                        btn.textContent = 'Run Review';
                        self.showToast('Review timed out', 'warning');
                    }
                }
            });
        }
        setTimeout(check, 1000);
    };

    DocumentCheck.prototype.loadResults = function() {
        var self = this;
        this.request('/api/document-check/cases/' + this.caseId + '/results').then(function(data) {
            // Populate _evidenceData on each check item from the evidence map
            var evMap = data.evidence || {};
            var items = data.check_items || [];
            items.forEach(function(item) {
                item._evidenceData = {};
                (item.evidence_refs || []).forEach(function(evId) {
                    if (evMap[evId]) {
                        item._evidenceData[evId] = evMap[evId];
                    }
                });
            });
            self.checkItems = items;
            self.renderResults(data);

            // Re-render PDF pages to show bboxes
            if (self.activeDocA && self.pdfDocA) self.renderPage('A');
            if (self.activeDocB && self.pdfDocB) self.renderPage('B');

            // Generate BT09
            self.request('/api/document-check/cases/' + self.caseId + '/generate-bt09', { method: 'POST', body: '{}' }).then(function(btData) {
                if (btData.bt09 && btData.bt09.available) {
                    self.bt09Draft = btData.bt09.draft;
                    document.getElementById('btnCopyBT09').disabled = false;
                    var body = document.getElementById('dcResultBody');
                    var draftDiv = document.createElement('div');
                    draftDiv.className = 'dc-bt09-draft';
                    draftDiv.innerHTML = '<h3>BT09 Draft</h3><div class="dc-bt09-content">' + self.escapeHTML(self.bt09Draft) + '</div>';
                    body.appendChild(draftDiv);
                }
            }).catch(function() {});
        });
    };

    DocumentCheck.prototype.renderResults = function(data) {
        var self = this;
        var body = document.getElementById('dcResultBody');
        if (!body) return;

        // Conclusion badge
        var conclusion = (data.case && data.case.overall_conclusion) || 'pending';
        var concEl = document.getElementById('dcConclusion');
        if (concEl) {
            concEl.textContent = conclusion.replace(/_/g, ' ');
            concEl.className = 'dc-conclusion ' + conclusion.toLowerCase();
        }

        // Group items by category
        var items = data.check_items || [];
        var categories = {};
        items.forEach(function(item) {
            var cat = item.category || 'GENERAL';
            if (!categories[cat]) categories[cat] = [];
            categories[cat].push(item);
        });

        var blockers = [];
        var nonBlockers = [];

        var html = '';
        // BLOCKER section first
        blockers = items.filter(function(i) { return i.is_blocker; });
        nonBlockers = items.filter(function(i) { return !i.is_blocker; });

        if (blockers.length > 0) {
            html += '<div class="dc-section-title">BLOCKER Issues</div>';
            html += blockers.map(function(item) { return self.renderCheckItem(item); }).join('');
        }

        html += '<div class="dc-section-title">Checks</div>';

        for (var cat in categories) {
            html += '<div style="font-size:11px;font-weight:600;color:var(--text-tertiary);padding:4px 0 2px 4px;">' + cat + '</div>';
            categories[cat].forEach(function(item) {
                if (!item.is_blocker) html += self.renderCheckItem(item);
            });
        }

        // BT09 section
        html += '<div class="dc-section-title">BT09 Draft</div>';
        html += '<div id="dcBT09Content" style="padding:8px;">';
        html += '<button class="btn btn--outline" onclick="document.getElementById(\'btnCopyBT09\').click()" style="width:100%;">Copy BT09 Draft</button>';
        html += '</div>';

        body.innerHTML = html;
    };

    DocumentCheck.prototype.renderCheckItem = function(item) {
        var self = this;
        var statusIcon = '<span class="dc-check-item__icon ' + item.status.toLowerCase() + '"></span>';
        var blockerTag = item.is_blocker ? '<span class="dc-check-item__blocker">BLOCKER</span>' : '';
        var valuesHtml = '';
        var vals = item.values || {};
        if (vals.contract || vals.cqp || vals.ta) {
            valuesHtml = '<div class="dc-check-item__values">';
            if (vals.contract) valuesHtml += '<span title="Contract">C: ' + self.escapeHTML(vals.contract) + '</span>';
            if (vals.cqp) valuesHtml += '<span title="CQP">Q: ' + self.escapeHTML(vals.cqp) + '</span>';
            if (vals.ta) valuesHtml += '<span title="TA">T: ' + self.escapeHTML(vals.ta) + '</span>';
            valuesHtml += '</div>';
        }

        return '<div class="dc-check-item" data-item-id="' + item.id + '" onclick="DocumentCheck.instance.scrollToCheckItem(\'' + item.id + '\')">' +
            '<div class="dc-check-item__header">' +
                statusIcon +
                '<span class="dc-check-item__label">' + self.escapeHTML(item.label || item.rule_id) + '</span>' +
                blockerTag +
            '</div>' +
            '<div class="dc-check-item__summary">' + self.escapeHTML(item.summary || '') + '</div>' +
            (item.details ? '<div class="dc-check-item__details">' + self.escapeHTML(item.details) + '</div>' : '') +
            valuesHtml +
        '</div>';
    };

    DocumentCheck.prototype.scrollToCheckItem = function(itemId) {
        var self = this;
        var item = this.checkItems.find(function(i) { return i.id === itemId; });
        if (!item) return;

        // Update selection state
        this.selectedItem = item;

        // Highlight the clicked item in result panel
        var allItems = document.querySelectorAll('.dc-check-item');
        allItems.forEach(function(el) { el.classList.remove('active'); });
        var targetEl = document.querySelector('[data-item-id="' + itemId + '"]');
        if (targetEl) {
            targetEl.classList.add('active');
            targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        // If evidence exists, navigate to the first evidence page in the PDF
        var evRefs = item.evidence_refs || item.evidenceRefs || [];
        if (evRefs.length > 0 && item._evidenceData) {
            var ev = item._evidenceData[evRefs[0]];
            if (ev && ev.document_id) {
                var doc = this.findDoc(ev.document_id);
                if (doc) {
                    var ws = doc.workspace || 'A';
                    var targetPage = ev.page_number || 1;
                    // Set page before calling openDocument to avoid reset
                    if (ws === 'A') {
                        this.pageNumA = targetPage;
                    } else {
                        this.pageNumB = targetPage;
                    }
                    // Pass target page so openDocument doesn't reset it
                    this.openDocument(ws, doc, targetPage);
                }
            }
        }

        // Re-render BBoxes to show the active (pulsing) bbox
        if (this.activeDocA && this.pdfDocA) {
            var overlay = document.getElementById('wsAOverlay');
            var canvas = document.getElementById('wsACanvas');
            if (overlay && canvas) {
                this.renderBBoxes('A', overlay, parseFloat(canvas.style.width) || 612, parseFloat(canvas.style.height) || 792, this.pageNumA);
            }
        }
        if (this.activeDocB && this.pdfDocB) {
            var overlay = document.getElementById('wsBOverlay');
            var canvas = document.getElementById('wsBCanvas');
            if (overlay && canvas) {
                this.renderBBoxes('B', overlay, parseFloat(canvas.style.width) || 612, parseFloat(canvas.style.height) || 792, this.pageNumB);
            }
        }
    };

    DocumentCheck.prototype.setupResizers = function() {
        var resizers = [
            { id: 'dcResizer1', left: 'workspaceA', right: 'workspaceB' },
            { id: 'dcResizer2', left: 'workspaceB', right: 'dcResultPanel' }
        ];

        resizers.forEach(function(r) {
            var el = document.getElementById(r.id);
            if (!el) return;
            var startX, startLWidth, startRWidth;

            el.addEventListener('mousedown', function(e) {
                e.preventDefault();
                startX = e.clientX;
                var leftEl = document.getElementById(r.left);
                var rightEl = document.getElementById(r.right);
                if (leftEl) startLWidth = leftEl.offsetWidth;
                if (rightEl) startRWidth = rightEl.offsetWidth;

                function onMove(ev) {
                    var dx = ev.clientX - startX;
                    if (leftEl && startLWidth) leftEl.style.width = Math.max(280, startLWidth + dx) + 'px';
                    if (rightEl && startRWidth) rightEl.style.width = Math.max(280, startRWidth - dx) + 'px';
                }
                function onUp() {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                }
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
        });
    };

    DocumentCheck.prototype.updateStatus = function() {
        if (!this.caseData) return;
        var badge = document.getElementById('dcStatusBadge');
        if (!badge) return;
        var st = (this.caseData.status || '').toLowerCase();
        badge.textContent = this.caseData.status || 'No files';
        if (st === 'completed') { badge.className = 'dc-status-badge status-completed'; }
        else if (st === 'uploaded') { badge.className = 'dc-status-badge status-uploaded'; }
        else if (st === 'failed') { badge.className = 'dc-status-badge status-failed'; }
        else { badge.className = 'dc-status-badge'; }
    };

    DocumentCheck.instance = new DocumentCheck();
    return DocumentCheck;
})();

// Initialize when DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { DocumentCheck.instance.init(); });
} else {
    DocumentCheck.instance.init();
}