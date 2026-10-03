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
exports.decorationTypes = exports.SEVERITY_ORDER = void 0;
exports.sortFindingsBySeverity = sortFindingsBySeverity;
exports.normalizePath = normalizePath;
exports.pathMatches = pathMatches;
exports.clampLine = clampLine;
exports.findFindingsAtCursor = findFindingsAtCursor;
exports.updateDecorations = updateDecorations;
exports.disposeDecorations = disposeDecorations;
const vscode = __importStar(require("vscode"));
// Severity ordering for ranking multiple findings on one line
exports.SEVERITY_ORDER = {
    blocker: 5,
    high: 4,
    medium: 3,
    low: 2,
    info: 1,
};
function sortFindingsBySeverity(findings) {
    return [...findings].sort((a, b) => {
        const sa = exports.SEVERITY_ORDER[(a.severity || 'info').toLowerCase()] || 0;
        const sb = exports.SEVERITY_ORDER[(b.severity || 'info').toLowerCase()] || 0;
        return sb - sa;
    });
}
function normalizePath(p) {
    return (p || '').replace(/\\/g, '/').toLowerCase();
}
function pathMatches(docPath, filePath) {
    const normDoc = normalizePath(docPath);
    const normFile = normalizePath(filePath);
    if (!normDoc || !normFile)
        return false;
    return (normDoc === normFile ||
        normDoc.endsWith('/' + normFile) ||
        normFile.endsWith('/' + normDoc) ||
        normDoc.endsWith(normFile));
}
function clampLine(line, maxLines) {
    const num = typeof line === 'number' ? line : parseInt(String(line), 10);
    if (isNaN(num) || num < 1)
        return 1;
    if (maxLines && num > maxLines)
        return maxLines;
    return num;
}
function findFindingsAtCursor(findings, docPath, cursorLine) {
    if (!findings || !docPath || !cursorLine)
        return [];
    const matches = findings.filter((f) => {
        if (f.dismissed)
            return false;
        if (!pathMatches(docPath, f.file))
            return false;
        const start = f.start_line || f.line || 1;
        const end = f.end_line || f.line || 1;
        return cursorLine >= start && cursorLine <= end;
    });
    return sortFindingsBySeverity(matches);
}
// Decoration types by severity with visible text badges (not relying on color alone)
exports.decorationTypes = {
    blocker: vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(255, 0, 0, 0.2)',
        isWholeLine: true,
        overviewRulerColor: 'red',
        overviewRulerLane: vscode.OverviewRulerLane.Right,
        after: {
            contentText: ' ⚠️ [BLOCKER]',
            color: '#ff4d4f',
            fontWeight: 'bold',
        },
    }),
    high: vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(255, 140, 0, 0.15)',
        isWholeLine: true,
        overviewRulerColor: 'darkorange',
        overviewRulerLane: vscode.OverviewRulerLane.Right,
        after: {
            contentText: ' ⚠️ [HIGH]',
            color: '#faad14',
            fontWeight: 'bold',
        },
    }),
    medium: vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(255, 215, 0, 0.1)',
        isWholeLine: true,
        overviewRulerColor: 'gold',
        overviewRulerLane: vscode.OverviewRulerLane.Right,
        after: {
            contentText: ' ℹ️ [MEDIUM]',
            color: '#d4b106',
        },
    }),
    low: vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(0, 122, 204, 0.08)',
        isWholeLine: true,
        overviewRulerColor: 'blue',
        overviewRulerLane: vscode.OverviewRulerLane.Right,
        after: {
            contentText: ' ℹ️ [LOW]',
            color: '#1890ff',
        },
    }),
    info: vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(128, 128, 128, 0.08)',
        isWholeLine: true,
        after: {
            contentText: ' ℹ️ [INFO]',
            color: '#8c8c8c',
        },
    }),
    activeCursor: vscode.window.createTextEditorDecorationType({
        borderWidth: '1px',
        borderStyle: 'solid',
        borderColor: 'rgba(243, 133, 24, 0.8)',
        backgroundColor: 'rgba(243, 133, 24, 0.12)',
        isWholeLine: true,
    }),
};
function updateDecorations(editor, findings, activeFinding) {
    if (!editor)
        return;
    const docPath = editor.document.uri.fsPath;
    const buckets = {
        blocker: [],
        high: [],
        medium: [],
        low: [],
        info: [],
    };
    const activeLineDecs = [];
    for (const f of findings) {
        if (f.dismissed)
            continue;
        if (pathMatches(docPath, f.file)) {
            const docLines = editor.document.lineCount;
            const startLine = clampLine(f.start_line || f.line || 1, docLines) - 1;
            const endLine = clampLine(f.end_line || f.line || 1, docLines) - 1;
            const range = new vscode.Range(startLine, 0, endLine, 999);
            const hoverText = new vscode.MarkdownString();
            hoverText.appendMarkdown(`**CodeAtlas [${(f.severity || 'INFO').toUpperCase()}]:** ${f.claim}\n\n`);
            hoverText.appendMarkdown(`- **Category:** \`${f.category}\`\n`);
            hoverText.appendMarkdown(`- **Confidence:** ${f.confidence}\n`);
            hoverText.appendMarkdown(`- **Evidence:** ${f.evidence_strength}\n`);
            hoverText.appendMarkdown(`- **Status:** \`${f.status}\``);
            const decoration = { range, hoverMessage: hoverText };
            const sev = (f.severity || 'info').toLowerCase();
            if (buckets[sev]) {
                buckets[sev].push(decoration);
            }
            else {
                buckets.info.push(decoration);
            }
            if (activeFinding && f.id === activeFinding.id) {
                activeLineDecs.push({ range });
            }
        }
    }
    for (const [sev, decs] of Object.entries(buckets)) {
        if (exports.decorationTypes[sev]) {
            editor.setDecorations(exports.decorationTypes[sev], decs);
        }
    }
    if (exports.decorationTypes.activeCursor) {
        editor.setDecorations(exports.decorationTypes.activeCursor, activeLineDecs);
    }
}
function disposeDecorations() {
    for (const type of Object.values(exports.decorationTypes)) {
        if (type && typeof type.dispose === 'function') {
            type.dispose();
        }
    }
}
//# sourceMappingURL=decorations.js.map