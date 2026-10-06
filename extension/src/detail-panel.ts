import { FindingDetail } from './types';

export function getFindingWebviewHtml(detail: FindingDetail): string {
  const safeStr = (s: unknown): string =>
    s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Finding Detail</title>
  <style>
    body { font-family: sans-serif; padding: 16px; color: var(--vscode-foreground); background: var(--vscode-editor-background); line-height: 1.5; }
    h2 { margin-top: 0; color: var(--vscode-editorWarning-foreground); }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
    table { width: 100%; border-collapse: collapse; margin: 12px 0; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--vscode-panel-border); }
    button { padding: 8px 14px; margin-right: 8px; margin-bottom: 8px; cursor: pointer; background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; border-radius: 2px; }
    button:hover { background: var(--vscode-button-hoverBackground); }
    pre { background: var(--vscode-textCodeBlock-background); padding: 8px; border-radius: 4px; overflow-x: auto; }
  </style>
</head>
<body>
  <h2>${safeStr(detail.claim)}</h2>
  <p><span class="badge">[${safeStr((detail.severity || '').toUpperCase())}]</span> <code>${safeStr(
    detail.category
  )}</code> - ${safeStr(detail.file)}:${safeStr(detail.line)}</p>

  <table>
    <tr><th>Confidence</th><td>${safeStr(detail.confidence ?? 'unknown')}</td></tr>
    <tr><th>Evidence Strength</th><td>${safeStr(detail.evidence_strength)}</td></tr>
    <tr><th>Quality Decision</th><td>${safeStr(detail.quality_decision || 'review_only')} (${safeStr(detail.quality_score ?? 0)})</td></tr>
    <tr><th>Changed-Line Support</th><td>${safeStr(detail.changed_line_support ?? 0)}</td></tr>
    <tr><th>Repository Context Support</th><td>${safeStr(detail.repository_context_support ?? 0)}</td></tr>
    <tr><th>Ambiguity</th><td>${safeStr(detail.ambiguity_score ?? 0)}</td></tr>
    <tr><th>Status</th><td>${safeStr(detail.status)}</td></tr>
    <tr><th>Impact</th><td>${safeStr(detail.impact)}</td></tr>
    <tr><th>Policy Decision</th><td>${safeStr(detail.policy_decision)}</td></tr>
    <tr><th>Test Status</th><td>${safeStr(detail.test_result)}</td></tr>
  </table>

  <div>
    <button onclick="vscode.postMessage({action: 'explain'})">Explain Finding</button>
    <button onclick="vscode.postMessage({action: 'generateFix'})">Generate Fix</button>
    <button onclick="vscode.postMessage({action: 'previewFix'})">Preview Fix</button>
    <button onclick="vscode.postMessage({action: 'rejectFix'})">Reject Fix</button>
    <button onclick="vscode.postMessage({action: 'regenerateFix'})">Regenerate Fix</button>
    <button onclick="vscode.postMessage({action: 'draftFix'})">Generate Draft Fix</button>
    <button onclick="vscode.postMessage({action: 'copy'})">Copy Finding</button>
    <button onclick="vscode.postMessage({action: 'dismiss'})">${
      detail.dismissed ? 'Restore Finding' : 'Dismiss Locally'
    }</button>
  </div>

  <h3>Evidence</h3>
  <pre>${safeStr(JSON.stringify(detail.deterministic_evidence, null, 2))}</pre>

  <h3>Limitations</h3>
  <ul>
    ${(detail.limitations || []).map((l) => `<li>${safeStr(l)}</li>`).join('')}
  </ul>
  ${detail.abstention_reason ? `<h3>Abstention Reason</h3><p>${safeStr(detail.abstention_reason)}</p>` : ''}
  ${detail.quality_limitations?.length ? `<h3>Quality Limitations</h3><ul>${detail.quality_limitations.map((l) => `<li>${safeStr(l)}</li>`).join('')}</ul>` : ''}

  <script>
    const vscode = acquireVsCodeApi();
  </script>
</body>
</html>`;
}

export function getNoFindingWebviewHtml(cursorLine?: number | null): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Finding Detail</title>
  <style>
    body { font-family: sans-serif; padding: 24px; color: var(--vscode-foreground); background: var(--vscode-editor-background); line-height: 1.5; }
    h3 { color: var(--vscode-descriptionForeground); }
  </style>
</head>
<body>
  <h3>No finding at cursor position${cursorLine ? ' (line ' + cursorLine + ')' : ''}.</h3>
  <p>Place your cursor on an annotated line or use <code>CodeAtlas: Find Finding</code> to inspect an issue.</p>
</body>
</html>`;
}
