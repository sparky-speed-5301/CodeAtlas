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

export const FIX_PREVIEW_SCHEME = 'codeatlas-fix-preview';

/** Refuse absurd preview payloads; service proposals are bounded far below this. */
export const MAX_PREVIEW_BYTES = 512 * 1024;

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

export class PatchPreviewError extends Error {}

/**
 * Parse a single-file unified diff. Rejects multi-file diffs, malformed
 * headers, count mismatches, and oversized payloads; never follows or trusts
 * paths — the caller must verify the path against the proposal's target file.
 */
export function parseUnifiedDiff(patchText: string, maxBytes: number = MAX_PREVIEW_BYTES): ParsedPatch {
  if (typeof patchText !== 'string' || !patchText.length) {
    throw new PatchPreviewError('The fix proposal contains no patch.');
  }
  if (Buffer.byteLength(patchText, 'utf8') > maxBytes) {
    throw new PatchPreviewError('The fix proposal patch exceeds the preview size limit.');
  }
  const lines = patchText.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  if (lines.length < 4 || !lines[0].startsWith('--- ') || !lines[1].startsWith('+++ ')) {
    throw new PatchPreviewError('The fix proposal patch is malformed.');
  }
  const oldPath = lines[0].slice(4).trim();
  const newPath = lines[1].slice(4).trim();
  if (oldPath !== newPath || !oldPath.startsWith('a/') || !newPath.startsWith('b/')) {
    throw new PatchPreviewError('The fix proposal patch must modify one file in place.');
  }
  const path = oldPath.slice(2);
  const hunks: ParsedHunk[] = [];
  let i = 2;
  while (i < lines.length) {
    const header = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(lines[i]);
    if (!header) {
      throw new PatchPreviewError('The fix proposal patch contains an invalid hunk header.');
    }
    const oldStart = parseInt(header[1], 10);
    const oldLines = header[2] === undefined ? 1 : parseInt(header[2], 10);
    const newStart = parseInt(header[3], 10);
    const newLines = header[4] === undefined ? 1 : parseInt(header[4], 10);
    if (oldStart < 0 || newStart < 0 || oldLines < 0 || newLines < 0) {
      throw new PatchPreviewError('The fix proposal patch contains invalid hunk coordinates.');
    }
    i += 1;
    const body: string[] = [];
    let seenOld = 0;
    let seenNew = 0;
    while (i < lines.length && (seenOld < oldLines || seenNew < newLines)) {
      const line = lines[i];
      const tag = line.charAt(0);
      if (tag === '\\') {
        // "\ No newline at end of file": no content line; keep scanning.
        i += 1;
        continue;
      }
      if (tag === ' ') {
        seenOld += 1;
        seenNew += 1;
      } else if (tag === '+') {
        seenNew += 1;
      } else if (tag === '-') {
        seenOld += 1;
      } else {
        throw new PatchPreviewError('The fix proposal patch contains an invalid hunk line.');
      }
      body.push(line);
      i += 1;
    }
    if (seenOld !== oldLines || seenNew !== newLines) {
      throw new PatchPreviewError('The fix proposal patch hunk line counts do not match its header.');
    }
    hunks.push({ oldStart, oldLines, newStart, newLines, lines: body });
  }
  if (!hunks.length) {
    throw new PatchPreviewError('The fix proposal patch contains no changes.');
  }
  return { path, hunks };
}

/**
 * Apply a parsed patch to the exact original content. Context and removed
 * lines must match the file at exact, ordered, non-overlapping coordinates;
 * any mismatch throws instead of fuzzily applying.
 */
export function applyPatchToContent(original: string, patch: ParsedPatch): string {
  const endsWithNewline = original.endsWith('\n');
  const lines = original.split('\n');
  if (endsWithNewline) lines.pop();
  let previousEnd = 0;
  let delta = 0;
  const edits: Array<{ start: number; old: string[]; new: string[] }> = [];
  for (const hunk of patch.hunks) {
    const start = hunk.oldLines === 0 ? hunk.oldStart : hunk.oldStart - 1;
    const newStart = hunk.newLines === 0 ? hunk.newStart : hunk.newStart - 1;
    const old = hunk.lines.filter((l) => l.charAt(0) === ' ' || l.charAt(0) === '-').map((l) => l.slice(1));
    const updated = hunk.lines.filter((l) => l.charAt(0) === ' ' || l.charAt(0) === '+').map((l) => l.slice(1));
    if (
      start < previousEnd ||
      start < 0 ||
      start > lines.length ||
      newStart !== start + delta ||
      JSON.stringify(lines.slice(start, start + old.length)) !== JSON.stringify(old)
    ) {
      throw new PatchPreviewError(
        'The proposal no longer matches the current file content; regenerate the fix proposal.'
      );
    }
    previousEnd = start + old.length;
    delta += updated.length - old.length;
    edits.push({ start, old, new: updated });
  }
  for (const edit of edits.reverse()) {
    lines.splice(edit.start, edit.old.length, ...edit.new);
  }
  return lines.join('\n') + (endsWithNewline ? '\n' : '');
}

/**
 * Virtual, read-only content provider for fix previews. Content is held in
 * memory under a bounded number of keys and cleared on disposal.
 */
export class FixPreviewDocumentProvider implements vscode.TextDocumentContentProvider {
  readonly scheme = FIX_PREVIEW_SCHEME;
  private readonly contents = new Map<string, string>();
  private readonly emitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.emitter.event;

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString()) ?? '';
  }

  /** Store bounded preview content and return its read-only virtual URI. */
  registerContent(label: string, targetPath: string, content: string): vscode.Uri {
    if (Buffer.byteLength(content, 'utf8') > MAX_PREVIEW_BYTES) {
      throw new PatchPreviewError('The preview content exceeds the size limit.');
    }
    if (this.contents.size >= 16) {
      this.contents.clear();
    }
    const safeName = label === 'original' || label === 'proposed' ? label : 'proposed';
    const uri = vscode.Uri.from({
      scheme: this.scheme,
      path: `/${safeName}/${targetPath.replace(/\\/g, '/')}`,
      query: String(this.contents.size),
    });
    this.contents.set(uri.toString(), content);
    return uri;
  }

  dispose(): void {
    this.contents.clear();
    this.emitter.dispose();
  }
}

/** Compute the proposed content for a proposal, failing closed on any mismatch. */
export function renderProposalPreview(originalContent: string, patchText: string, expectedPath: string): string {
  const patch = parseUnifiedDiff(patchText);
  if (patch.path !== expectedPath.replace(/\\/g, '/')) {
    throw new PatchPreviewError('The fix proposal targets a different file than the finding.');
  }
  return applyPatchToContent(originalContent, patch);
}
