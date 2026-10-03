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
exports.FindingsTreeDataProvider = void 0;
const vscode = __importStar(require("vscode"));
class FindingsTreeDataProvider {
    _onDidChangeTreeData;
    onDidChangeTreeData;
    findings;
    activeFindingId;
    filterSeverity;
    filterCategory;
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        this.findings = [];
        this.activeFindingId = null;
        this.filterSeverity = null;
        this.filterCategory = null;
    }
    refresh(findings, activeFindingId) {
        this.findings = findings || [];
        if (activeFindingId !== undefined) {
            this.activeFindingId = activeFindingId;
        }
        this._onDidChangeTreeData.fire();
    }
    setActiveFinding(id) {
        this.activeFindingId = id;
        this._onDidChangeTreeData.fire();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren(element) {
        if (element)
            return [];
        let filtered = this.findings;
        if (this.filterSeverity) {
            filtered = filtered.filter((f) => (f.severity || '').toLowerCase() === this.filterSeverity.toLowerCase());
        }
        if (this.filterCategory) {
            filtered = filtered.filter((f) => (f.category || '').toLowerCase() === this.filterCategory.toLowerCase());
        }
        if (!filtered.length) {
            return [
                new vscode.TreeItem(this.findings.length ? 'No findings matching filter' : 'No findings found'),
            ];
        }
        return filtered.map((f) => {
            const badge = `[${(f.severity || 'info').toUpperCase()}]`;
            const isActive = this.activeFindingId && f.id === this.activeFindingId;
            const activeMark = isActive ? ' ◀ ACTIVE' : '';
            const dismissedMark = f.dismissed ? ' (Dismissed)' : '';
            const label = `${badge} ${f.category}: ${f.file}:${f.line}${activeMark}${dismissedMark}`;
            const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
            item.description = f.claim;
            item.tooltip = `Claim: ${f.claim}\nConfidence: ${f.confidence ?? 'unknown'}\nEvidence: ${f.evidence_strength ?? 'unknown'}\nStatus: ${f.status ?? 'open'}`;
            item.command = {
                command: 'codeatlas.openFindingLocation',
                title: 'Open Finding',
                arguments: [f],
            };
            item.contextValue = 'findingItem';
            item.finding = f;
            return item;
        });
    }
}
exports.FindingsTreeDataProvider = FindingsTreeDataProvider;
//# sourceMappingURL=findings.js.map