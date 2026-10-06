/**
 * Phase 11C-B: read-only fix diff preview.
 *
 * Renders a fix proposal's exact patch against the workspace file using VS
 * Code's diff editor backed by a virtual, read-only document provider. The
 * provider only ever serves in-memory strings: nothing is written into the
 * repository, the user's files, or the Git index, and no temporary files are
 * created. Patch application is strict — hunks must match the current file
 * content at exact coordinates or the preview fails closed.
 */
import * as vscode from 'vscode';
export declare const FIX_PREVIEW_SCHEME = "codeatlas-fix-preview";
/** Refuse absurd preview payloads; service proposals are bounded far below this. */
export declare const MAX_PREVIEW_BYTES: number;
export interface ParsedHunk {
    oldStart: number;
    oldLines: number;
    newStart: number;
    newLines: number;
    lines: string[];
}
export interface ParsedPatch {
    path: string;
    hunks: ParsedHunk[];
}
export declare class PatchPreviewError extends Error {
}
/**
 * Parse a single-file unified diff. Rejects multi-file diffs, malformed
 * headers, count mismatches, and oversized payloads; never follows or trusts
 * paths — the caller must verify the path against the proposal's target file.
 */
export declare function parseUnifiedDiff(patchText: string, maxBytes?: number): ParsedPatch;
/**
 * Apply a parsed patch to the exact original content. Context and removed
 * lines must match the file at exact, ordered, non-overlapping coordinates;
 * any mismatch throws instead of fuzzily applying.
 */
export declare function applyPatchToContent(original: string, patch: ParsedPatch): string;
/**
 * Virtual, read-only content provider for fix previews. Content is held in
 * memory under a bounded number of keys and cleared on disposal.
 */
export declare class FixPreviewDocumentProvider implements vscode.TextDocumentContentProvider {
    readonly scheme = "codeatlas-fix-preview";
    private readonly contents;
    private readonly emitter;
    readonly onDidChange: vscode.Event<vscode.Uri>;
    provideTextDocumentContent(uri: vscode.Uri): string;
    /** Store bounded preview content and return its read-only virtual URI. */
    registerContent(label: string, targetPath: string, content: string): vscode.Uri;
    dispose(): void;
}
/** Compute the proposed content for a proposal, failing closed on any mismatch. */
export declare function renderProposalPreview(originalContent: string, patchText: string, expectedPath: string): string;
