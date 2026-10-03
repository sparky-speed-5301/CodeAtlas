"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.formatQuickPickItem = formatQuickPickItem;
function formatQuickPickItem(finding) {
    const sev = (finding.severity || 'info').toUpperCase();
    const dismissedTag = finding.dismissed ? ' [DISMISSED]' : '';
    return {
        label: `[${sev}] ${finding.category} - ${finding.file}:${finding.line}${dismissedTag}`,
        description: finding.claim || '',
        detail: `Status: ${finding.status || 'open'} | Confidence: ${finding.confidence || 'unknown'} | Evidence: ${finding.evidence_strength || 'unknown'}`,
        finding,
    };
}
//# sourceMappingURL=quickpick.js.map