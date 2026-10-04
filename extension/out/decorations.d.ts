import * as vscode from 'vscode';
import { Finding } from './types';
export declare const SEVERITY_ORDER: Record<string, number>;
export declare function sortFindingsBySeverity(findings: Finding[]): Finding[];
export declare function normalizePath(p?: string | null): string;
export declare function pathMatches(docPath?: string | null, filePath?: string | null): boolean;
export declare function clampLine(line: number | string | undefined, maxLines?: number): number;
/**
 * Normalize a finding's line bounds for editor use.
 *
 * Malformed or stale service data can carry inverted ranges (end < start);
 * constructing a vscode.Range from such data throws and aborts the entire
 * decoration pass, so bounds are always ordered and clamped here.
 */
export declare function findingLineBounds(finding: Finding, maxLines?: number): {
    start: number;
    end: number;
};
export declare function findFindingsAtCursor(findings?: Finding[] | null, docPath?: string | null, cursorLine?: number | null): Finding[];
export declare const decorationTypes: Record<string, vscode.TextEditorDecorationType>;
export declare function updateDecorations(editor: vscode.TextEditor | undefined, findings: Finding[], activeFinding: Finding | null): void;
export declare function disposeDecorations(): void;
