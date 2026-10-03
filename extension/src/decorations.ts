import * as vscode from 'vscode';
import { Finding } from './types';

// Severity ordering for ranking multiple findings on one line
export const SEVERITY_ORDER: Record<string, number> = {
  blocker: 5,
  high: 4,
  medium: 3,
  low: 2,
  info: 1,
};

export function sortFindingsBySeverity(findings: Finding[]): Finding[] {
  return [...findings].sort((a, b) => {
    const sa = SEVERITY_ORDER[(a.severity || 'info').toLowerCase()] || 0;
    const sb = SEVERITY_ORDER[(b.severity || 'info').toLowerCase()] || 0;
    return sb - sa;
  });
}

export function normalizePath(p?: string | null): string {
  return (p || '').replace(/\\/g, '/').toLowerCase();
}

export function pathMatches(docPath?: string | null, filePath?: string | null): boolean {
  const normDoc = normalizePath(docPath);
  const normFile = normalizePath(filePath);
  if (!normDoc || !normFile) return false;
  return (
    normDoc === normFile ||
    normDoc.endsWith('/' + normFile) ||
    normFile.endsWith('/' + normDoc) ||
    normDoc.endsWith(normFile)
  );
}

export function clampLine(line: number | string | undefined, maxLines?: number): number {
  const num = typeof line === 'number' ? line : parseInt(String(line), 10);
  if (isNaN(num) || num < 1) return 1;
  if (maxLines && num > maxLines) return maxLines;
  return num;
}

export function findFindingsAtCursor(
  findings?: Finding[] | null,
  docPath?: string | null,
  cursorLine?: number | null
): Finding[] {
  if (!findings || !docPath || !cursorLine) return [];
  const matches = findings.filter((f) => {
    if (f.dismissed) return false;
    if (!pathMatches(docPath, f.file)) return false;
    const start = f.start_line || f.line || 1;
    const end = f.end_line || f.line || 1;
    return cursorLine >= start && cursorLine <= end;
  });
  return sortFindingsBySeverity(matches);
}

// Decoration types by severity with visible text badges (not relying on color alone)
export const decorationTypes: Record<string, vscode.TextEditorDecorationType> = {
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

export function updateDecorations(
  editor: vscode.TextEditor | undefined,
  findings: Finding[],
  activeFinding: Finding | null
): void {
  if (!editor) return;

  const docPath = editor.document.uri.fsPath;
  const buckets: Record<string, vscode.DecorationOptions[]> = {
    blocker: [],
    high: [],
    medium: [],
    low: [],
    info: [],
  };
  const activeLineDecs: vscode.DecorationOptions[] = [];

  for (const f of findings) {
    if (f.dismissed) continue;
    if (pathMatches(docPath, f.file)) {
      const docLines = editor.document.lineCount;
      const startLine = clampLine(f.start_line || f.line || 1, docLines) - 1;
      const endLine = clampLine(f.end_line || f.line || 1, docLines) - 1;
      const range = new vscode.Range(startLine, 0, endLine, 999);

      const hoverText = new vscode.MarkdownString();
      hoverText.appendMarkdown(
        `**CodeAtlas [${(f.severity || 'INFO').toUpperCase()}]:** ${f.claim}\n\n`
      );
      hoverText.appendMarkdown(`- **Category:** \`${f.category}\`\n`);
      hoverText.appendMarkdown(`- **Confidence:** ${f.confidence}\n`);
      hoverText.appendMarkdown(`- **Evidence:** ${f.evidence_strength}\n`);
      hoverText.appendMarkdown(`- **Status:** \`${f.status}\``);

      const decoration: vscode.DecorationOptions = { range, hoverMessage: hoverText };
      const sev = (f.severity || 'info').toLowerCase();
      if (buckets[sev]) {
        buckets[sev].push(decoration);
      } else {
        buckets.info.push(decoration);
      }

      if (activeFinding && f.id === activeFinding.id) {
        activeLineDecs.push({ range });
      }
    }
  }

  for (const [sev, decs] of Object.entries(buckets)) {
    if (decorationTypes[sev]) {
      editor.setDecorations(decorationTypes[sev], decs);
    }
  }

  if (decorationTypes.activeCursor) {
    editor.setDecorations(decorationTypes.activeCursor, activeLineDecs);
  }
}

export function disposeDecorations(): void {
  for (const type of Object.values(decorationTypes)) {
    if (type && typeof type.dispose === 'function') {
      type.dispose();
    }
  }
}
