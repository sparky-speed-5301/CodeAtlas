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
exports.ContextTreeDataProvider = void 0;
const vscode = __importStar(require("vscode"));
class ContextTreeDataProvider {
    _onDidChangeTreeData;
    onDidChangeTreeData;
    context;
    activeFinding;
    cursorLine;
    multipleFindingsCount;
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        this.context = null;
        this.activeFinding = null;
        this.cursorLine = null;
        this.multipleFindingsCount = 0;
    }
    refresh(context, activeFinding = null, cursorLine = null, multipleFindingsCount = 0) {
        this.context = context;
        this.activeFinding = activeFinding;
        this.cursorLine = cursorLine;
        this.multipleFindingsCount = multipleFindingsCount;
        this._onDidChangeTreeData.fire();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren(element) {
        if (element)
            return [];
        if (!this.context) {
            if (this.cursorLine) {
                return [
                    new vscode.TreeItem(`No finding at cursor (line ${this.cursorLine})`),
                    new vscode.TreeItem('Move cursor to a flagged line or select a finding to view context'),
                ];
            }
            return [new vscode.TreeItem('Select a finding or place cursor on a flagged line')];
        }
        const items = [];
        if (this.activeFinding) {
            const f = this.activeFinding;
            const badge = `[${(f.severity || 'INFO').toUpperCase()}]`;
            const label = `Active: ${badge} ${f.category} (${f.file}:${f.line})`;
            const fItem = new vscode.TreeItem(label);
            fItem.description = f.claim;
            items.push(fItem);
            if (this.multipleFindingsCount > 1) {
                items.push(new vscode.TreeItem(`Multiple findings on this line: ${this.multipleFindingsCount} (showing primary)`));
            }
        }
        const lines = this.context.changed_lines || [];
        items.push(new vscode.TreeItem(`Changed lines: ${lines.length ? lines.join(', ') : 'none'}`));
        if (this.context.containing_symbol) {
            const s = this.context.containing_symbol;
            items.push(new vscode.TreeItem(`Symbol: ${s.kind || 'symbol'} ${s.name || ''} (lines ${s.start_line}-${s.end_line})`));
        }
        else {
            items.push(new vscode.TreeItem('Symbol: none'));
        }
        const impCount = (this.context.relevant_imports || []).length;
        items.push(new vscode.TreeItem(`Relevant imports: ${impCount}`));
        const refCount = (this.context.relevant_references || []).length;
        items.push(new vscode.TreeItem(`Relevant references: ${refCount}`));
        const tests = this.context.related_tests || [];
        items.push(new vscode.TreeItem(`Related tests: ${tests.length ? tests.join(', ') : 'none'}`));
        const cands = (this.context.retrieved_context_candidates || []).length;
        items.push(new vscode.TreeItem(`Retrieved context candidates: ${cands}`));
        items.push(new vscode.TreeItem(`Truncation status: ${this.context.truncation_status ? 'truncated' : 'full'}`));
        const ev = (this.context.evidence_sources || []).join(', ');
        items.push(new vscode.TreeItem(`Evidence sources: ${ev || 'deterministic'}`));
        return items;
    }
}
exports.ContextTreeDataProvider = ContextTreeDataProvider;
//# sourceMappingURL=context.js.map