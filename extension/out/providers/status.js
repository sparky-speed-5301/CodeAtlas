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
exports.StatusTreeDataProvider = void 0;
const vscode = __importStar(require("vscode"));
class StatusTreeDataProvider {
    _onDidChangeTreeData;
    onDidChangeTreeData;
    status;
    serviceManager;
    profileManager;
    constructor(serviceManager = null, profileManager = null) {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        this.status = null;
        this.serviceManager = serviceManager;
        this.profileManager = profileManager;
    }
    refresh(status) {
        if (status !== undefined) {
            this.status = status;
        }
        this._onDidChangeTreeData.fire();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren(element) {
        if (element)
            return [];
        const items = [];
        // Service & Discovery Status
        const configuredMode = this.serviceManager?.serviceMode ?? 'external';
        const mode = ['external', 'managed', 'disabled'].includes(configuredMode)
            ? configuredMode
            : 'invalid';
        const sUrl = this.serviceManager ? this.serviceManager.currentUrl : null;
        const ownership = this.serviceManager && this.serviceManager.owned
            ? `managed (PID: ${this.serviceManager.managedPid})`
            : 'external / unowned';
        const healthStatus = this.serviceManager ? this.serviceManager.lastHealthStatus : 'unknown';
        const activeProf = this.profileManager ? this.profileManager.activeProfileName : 'default';
        items.push(new vscode.TreeItem(`Service Mode: ${mode} (${ownership})`));
        items.push(new vscode.TreeItem(`Service URL: ${sUrl || 'none'}`));
        items.push(new vscode.TreeItem(`Service Health: ${healthStatus}`));
        items.push(new vscode.TreeItem(`Active Profile: ${activeProf}`));
        const h = this.serviceManager?.lastHealth;
        items.push(new vscode.TreeItem(`Service Version: ${h?.version ?? 'unknown'}`));
        items.push(new vscode.TreeItem(`Active Reviews: ${h?.active_reviews ?? 'unknown'}`));
        items.push(new vscode.TreeItem(`Service Provider: ${h?.provider ?? 'unknown'}`));
        items.push(new vscode.TreeItem(`Last Health Check: ${this.serviceManager?.lastHealthCheck ?? 'not checked'}`));
        if (this.serviceManager?.message)
            items.push(new vscode.TreeItem(this.serviceManager.message));
        if (this.profileManager?.error) {
            items.push(new vscode.TreeItem(`Profile Error: ${this.profileManager.error}`));
        }
        if (this.serviceManager && this.serviceManager.lastErrorMessage) {
            items.push(new vscode.TreeItem(`Service Error: ${this.serviceManager.lastErrorMessage}`));
        }
        // Review Run Status
        if (!this.status) {
            items.push(new vscode.TreeItem('--- No active review run ---'));
            return items;
        }
        items.push(new vscode.TreeItem('--- Active Review ---'));
        const itemStatus = new vscode.TreeItem(`Status: ${this.status.status}`);
        itemStatus.description = this.status.progress_text;
        items.push(itemStatus);
        if (this.status.run_id) {
            items.push(new vscode.TreeItem(`Run ID: ${this.status.run_id}`));
        }
        if (this.status.policy_decision) {
            items.push(new vscode.TreeItem(`Policy Decision: ${this.status.policy_decision}`));
        }
        if (this.status.finding_counts) {
            const fc = this.status.finding_counts;
            items.push(new vscode.TreeItem(`Findings: ${fc.total || 0} (Blocker: ${fc.blocker || 0}, High: ${fc.high || 0}, Med: ${fc.medium || 0}, Low: ${fc.low || 0})`));
        }
        items.push(new vscode.TreeItem(`Tests Status: ${this.status.test_status || 'not_run'}`));
        items.push(new vscode.TreeItem(`Patch Validation: ${this.status.patch_validation_status || 'not_run'}`));
        return items;
    }
}
exports.StatusTreeDataProvider = StatusTreeDataProvider;
//# sourceMappingURL=status.js.map