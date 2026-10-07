"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.CodeAtlasClient = void 0;
const http = __importStar(require("http"));
const url_1 = require("url");
const service_1 = require("./service");
class CodeAtlasClient {
    serviceUrl;
    timeoutMs;
    enabled;
    pending;
    constructor(serviceUrl) {
        this.serviceUrl = serviceUrl ? (0, service_1.validateServiceUrl)(serviceUrl) : null;
        this.timeoutMs = 30000;
        this.enabled = true;
        this.pending = new Set();
    }
    request(method, endpoint, body = null, timeoutMs = this.timeoutMs) {
        return new Promise((resolve, reject) => {
            if (!this.enabled || !this.serviceUrl) {
                reject(new Error(this.enabled ? service_1.SETUP_MESSAGE : 'CodeAtlas service is disabled.'));
                return;
            }
            const parsed = new url_1.URL((0, service_1.validateServiceUrl)(this.serviceUrl) + endpoint);
            const postData = body ? JSON.stringify(body) : null;
            const options = {
                hostname: parsed.hostname.replace(/^\[|\]$/g, ''),
                port: parsed.port || 80,
                path: parsed.pathname + parsed.search,
                method: method,
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': postData ? Buffer.byteLength(postData) : 0,
                },
                timeout: timeoutMs,
            };
            const req = http.request(options, (res) => {
                let responseBody = '';
                let responseBytes = 0;
                res.setEncoding('utf8');
                res.on('data', (chunk) => {
                    responseBytes += Buffer.byteLength(chunk);
                    if (responseBytes > 2 * 1024 * 1024) {
                        req.destroy();
                        reject(new Error('CodeAtlas response exceeded the size limit.'));
                        return;
                    }
                    responseBody += chunk;
                });
                res.on('error', () => reject(new Error('CodeAtlas response was interrupted.')));
                res.on('end', () => {
                    try {
                        const data = responseBody ? JSON.parse(responseBody) : {};
                        if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
                            resolve(data);
                        }
                        else {
                            // Never echo response content into errors; it may be credential-bearing.
                            reject(new Error(`CodeAtlas request failed (HTTP ${res.statusCode}).`));
                        }
                    }
                    catch (_) {
                        reject(new Error('Malformed response from CodeAtlas service.'));
                    }
                });
            });
            const timer = setTimeout(() => {
                req.destroy();
                reject(new Error('Request to CodeAtlas service timed out.'));
            }, timeoutMs);
            this.pending.add(req);
            req.on('close', () => {
                clearTimeout(timer);
                this.pending.delete(req);
            });
            req.on('error', () => {
                reject(new Error('CodeAtlas service is unreachable. Check service health and configuration.'));
            });
            if (postData) {
                req.write(postData);
            }
            req.end();
        });
    }
    dispose() {
        this.enabled = false;
        for (const request of this.pending)
            request.destroy();
        this.pending.clear();
    }
    getHealth(timeoutMs = 1000) {
        return this.request('GET', '/health', null, timeoutMs);
    }
    startReview(repo, options = {}) {
        return this.request('POST', '/reviews', {
            repo,
            base: options.base || 'main',
            head: options.head || 'HEAD',
            review_provider: options.review_provider,
            provider_model: options.provider_model,
            provider_timeout: options.provider_timeout,
            allow_patch_suggestions: options.allow_patch_suggestions,
            max_findings: options.max_findings ?? 50,
        });
    }
    getReview(runId) {
        return this.request('GET', `/reviews/${runId}`);
    }
    getFindings(runId, filters = {}) {
        const query = [];
        if (filters.severity)
            query.push(`severity=${encodeURIComponent(filters.severity)}`);
        if (filters.category)
            query.push(`category=${encodeURIComponent(filters.category)}`);
        if (filters.status)
            query.push(`status=${encodeURIComponent(filters.status)}`);
        if (filters.include_dismissed)
            query.push('include_dismissed=true');
        const qs = query.length ? '?' + query.join('&') : '';
        return this.request('GET', `/reviews/${runId}/findings${qs}`);
    }
    getFindingDetail(runId, findingId) {
        return this.request('GET', `/reviews/${runId}/findings/${findingId}`);
    }
    cancelReview(runId) {
        return this.request('POST', `/reviews/${runId}/cancel`);
    }
    explainFinding(findingId, runId) {
        return this.request('POST', `/findings/${findingId}/explain`, {
            run_id: runId,
        });
    }
    proposePatch(findingId, runId) {
        return this.request('POST', `/findings/${findingId}/patch-proposal`, {
            run_id: runId,
        });
    }
    validateProposal(proposalId, approvalToken, runTests = false, runFullSuite = false) {
        return this.request('POST', `/proposals/${proposalId}/validate`, {
            approval_token: approvalToken,
            run_tests: runTests,
            run_full_suite: runFullSuite,
        });
    }
    // -------------------------------------------------------------------------
    // Phase 11C-B: bounded fix proposals (preview only; never applied here)
    // -------------------------------------------------------------------------
    getFixEligibility(runId, findingId) {
        return this.request('GET', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-eligibility`);
    }
    requestFixProposal(runId, findingId) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal`, { run_id: runId });
    }
    getFixProposal(runId, findingId, proposalId) {
        if (proposalId) {
            const query = `run_id=${encodeURIComponent(runId)}&finding_id=${encodeURIComponent(findingId)}`;
            return this.request('GET', `/fix-proposals/${encodeURIComponent(proposalId)}?${query}`);
        }
        return this.request('GET', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal`);
    }
    rejectFixProposal(runId, findingId, proposalId) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/reject`, { run_id: runId, proposal_id: proposalId });
    }
    regenerateFixProposal(runId, findingId) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/regenerate`, { run_id: runId });
    }
    approveFixProposalForValidation(runId, findingId, proposalId, approvalToken) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/approve-validation`, { run_id: runId, finding_id: findingId, proposal_id: proposalId, approval_token: approvalToken });
    }
    validateFixProposal(runId, findingId, proposalId, approvalToken, runTests = false, runFullSuite = false) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/validate`, {
            run_id: runId,
            finding_id: findingId,
            proposal_id: proposalId,
            approval_token: approvalToken,
            run_tests: runTests,
            run_full_suite: runFullSuite,
        });
    }
    // -------------------------------------------------------------------------
    // Phase 11C-D/E: explicit apply of a validated FixProposal, revert, history
    // -------------------------------------------------------------------------
    getFixApplyHistory(runId, findingId, proposalId) {
        if (proposalId) {
            const query = `run_id=${encodeURIComponent(runId)}&finding_id=${encodeURIComponent(findingId)}`;
            return this.request('GET', `/fix-proposals/${encodeURIComponent(proposalId)}/apply-history?${query}`);
        }
        return this.request('GET', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/apply-history`);
    }
    applyFixProposal(runId, findingId, proposalId, approvalToken, confirmedPatchHash) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/apply`, {
            run_id: runId,
            finding_id: findingId,
            proposal_id: proposalId,
            approval_token: approvalToken,
            confirmed_patch_hash: confirmedPatchHash,
        });
    }
    revertFixProposal(runId, findingId, proposalId, confirmedPatchHash) {
        return this.request('POST', `/reviews/${encodeURIComponent(runId)}/findings/${encodeURIComponent(findingId)}/fix-proposal/revert`, {
            run_id: runId,
            finding_id: findingId,
            proposal_id: proposalId,
            confirmed_patch_hash: confirmedPatchHash,
        });
    }
}
exports.CodeAtlasClient = CodeAtlasClient;
//# sourceMappingURL=client.js.map