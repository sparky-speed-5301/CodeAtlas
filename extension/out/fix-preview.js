"use strict";
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
exports.FixPreviewDocumentProvider = exports.PatchPreviewError = exports.MAX_PREVIEW_BYTES = exports.FIX_PREVIEW_SCHEME = void 0;
exports.parseUnifiedDiff = parseUnifiedDiff;
exports.applyPatchToContent = applyPatchToContent;
exports.renderProposalPreview = renderProposalPreview;
const vscode = __importStar(require("vscode"));
exports.FIX_PREVIEW_SCHEME = 'codeatlas-fix-preview';
/** Refuse absurd preview payloads; service proposals are bounded far below this. */
exports.MAX_PREVIEW_BYTES = 512 * 1024;
class PatchPreviewError extends Error {
}
exports.PatchPreviewError = PatchPreviewError;
/**
 * Parse a single-file unified diff. Rejects multi-file diffs, malformed
 * headers, count mismatches, and oversized payloads; never follows or trusts
 * paths — the caller must verify the path against the proposal's target file.
 */
function parseUnifiedDiff(patchText, maxBytes = exports.MAX_PREVIEW_BYTES) {
    if (typeof patchText !== 'string' || !patchText.length) {
        throw new PatchPreviewError('The fix proposal contains no patch.');
    }
    if (Buffer.byteLength(patchText, 'utf8') > maxBytes) {
        throw new PatchPreviewError('The fix proposal patch exceeds the preview size limit.');
    }
    const lines = patchText.split('\n');
    if (lines[lines.length - 1] === '')
        lines.pop();
    if (lines.length < 4 || !lines[0].startsWith('--- ') || !lines[1].startsWith('+++ ')) {
        throw new PatchPreviewError('The fix proposal patch is malformed.');
    }
    const oldPath = lines[0].slice(4).trim();
    const newPath = lines[1].slice(4).trim();
    if (oldPath !== newPath || !oldPath.startsWith('a/') || !newPath.startsWith('b/')) {
        throw new PatchPreviewError('The fix proposal patch must modify one file in place.');
    }
    const path = oldPath.slice(2);
    const hunks = [];
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
        const body = [];
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
            }
            else if (tag === '+') {
                seenNew += 1;
            }
            else if (tag === '-') {
                seenOld += 1;
            }
            else {
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
function applyPatchToContent(original, patch) {
    const endsWithNewline = original.endsWith('\n');
    const lines = original.split('\n');
    if (endsWithNewline)
        lines.pop();
    let previousEnd = 0;
    let delta = 0;
    const edits = [];
    for (const hunk of patch.hunks) {
        const start = hunk.oldLines === 0 ? hunk.oldStart : hunk.oldStart - 1;
        const newStart = hunk.newLines === 0 ? hunk.newStart : hunk.newStart - 1;
        const old = hunk.lines.filter((l) => l.charAt(0) === ' ' || l.charAt(0) === '-').map((l) => l.slice(1));
        const updated = hunk.lines.filter((l) => l.charAt(0) === ' ' || l.charAt(0) === '+').map((l) => l.slice(1));
        if (start < previousEnd ||
            start < 0 ||
            start > lines.length ||
            newStart !== start + delta ||
            JSON.stringify(lines.slice(start, start + old.length)) !== JSON.stringify(old)) {
            throw new PatchPreviewError('The proposal no longer matches the current file content; regenerate the fix proposal.');
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
class FixPreviewDocumentProvider {
    scheme = exports.FIX_PREVIEW_SCHEME;
    contents = new Map();
    emitter = new vscode.EventEmitter();
    onDidChange = this.emitter.event;
    provideTextDocumentContent(uri) {
        return this.contents.get(uri.toString()) ?? '';
    }
    /** Store bounded preview content and return its read-only virtual URI. */
    registerContent(label, targetPath, content) {
        if (Buffer.byteLength(content, 'utf8') > exports.MAX_PREVIEW_BYTES) {
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
    dispose() {
        this.contents.clear();
        this.emitter.dispose();
    }
}
exports.FixPreviewDocumentProvider = FixPreviewDocumentProvider;
/** Compute the proposed content for a proposal, failing closed on any mismatch. */
function renderProposalPreview(originalContent, patchText, expectedPath) {
    const patch = parseUnifiedDiff(patchText);
    if (patch.path !== expectedPath.replace(/\\/g, '/')) {
        throw new PatchPreviewError('The fix proposal targets a different file than the finding.');
    }
    return applyPatchToContent(originalContent, patch);
}
//# sourceMappingURL=fix-preview.js.map