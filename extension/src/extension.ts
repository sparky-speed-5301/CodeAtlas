/**
 * CodeAtlas VS Code Extension
 * Evidence-first, repository-aware code review for VS Code.
 * Phase 10D: TypeScript migration of the extension runtime.
 */

import * as vscode from 'vscode';
import { CodeAtlasClient } from './client';
import {
  clampLine,
  decorationTypes,
  disposeDecorations,
  findFindingsAtCursor,
  findingLineBounds,
  normalizePath,
  pathMatches,
  SEVERITY_ORDER,
  sortFindingsBySeverity,
} from './decorations';
import { getFindingWebviewHtml, getNoFindingWebviewHtml } from './detail-panel';
import {
  FixPreviewDocumentProvider,
  FIX_PREVIEW_SCHEME,
  PatchPreviewError,
  renderProposalPreview,
  applyPatchToContent,
  parseUnifiedDiff,
} from './fix-preview';
import {
  ALLOWED_PROFILE_KEYS,
  configurationPath,
  DEFAULT_PROFILE,
  FORBIDDEN_PROFILE_KEYS,
  ProfileManager,
  readWorkspaceConfiguration,
  validateProfile,
  validateProfileName,
} from './profiles';
import { ContextTreeDataProvider } from './providers/context';
import { FindingsTreeDataProvider } from './providers/findings';
import { StatusTreeDataProvider } from './providers/status';
import { formatQuickPickItem } from './quickpick';
import {
  parseServiceCommand,
  safeHealth,
  ServiceLifecycleManager,
  SETUP_MESSAGE,
  validateServiceUrl,
} from './service';
import {
  ExtensionActivationResult,
  ExtensionSession,
  Finding,
  FindingDetail,
  FindingQuickPickItem,
  FixProposal,
  ProfileQuickPickItem,
  ReviewStatus,
} from './types';

// Re-export symbols for backward compatibility and test coverage
export {
  CodeAtlasClient,
  StatusTreeDataProvider,
  FindingsTreeDataProvider,
  ContextTreeDataProvider,
  ProfileManager,
  ServiceLifecycleManager,
  validateProfile,
  validateProfileName,
  DEFAULT_PROFILE,
  ALLOWED_PROFILE_KEYS,
  FORBIDDEN_PROFILE_KEYS,
  readWorkspaceConfiguration,
  configurationPath,
  validateServiceUrl,
  parseServiceCommand,
  safeHealth,
  SETUP_MESSAGE,
  findFindingsAtCursor,
  findingLineBounds,
  formatQuickPickItem,  clampLine,
  sortFindingsBySeverity,
  normalizePath,
  pathMatches,
  getFindingWebviewHtml,
  getNoFindingWebviewHtml,
  SEVERITY_ORDER,
  decorationTypes,
  FixPreviewDocumentProvider,
  FIX_PREVIEW_SCHEME,
  PatchPreviewError,
  parseUnifiedDiff,
  applyPatchToContent,
  renderProposalPreview,
};

let activeSession: ExtensionSession | null = null;

export function activate(context: vscode.ExtensionContext): ExtensionActivationResult {
  let config = vscode.workspace.getConfiguration('codeatlas');
  const workspaceFolders = vscode.workspace.workspaceFolders;
  const workspaceRoot =
    workspaceFolders && workspaceFolders.length ? workspaceFolders[0].uri.fsPath : null;

  const profileManager = new ProfileManager(workspaceRoot);
  const serviceManager = new ServiceLifecycleManager({ workspaceRoot });
  const client = new CodeAtlasClient(null);

  const statusProvider = new StatusTreeDataProvider(serviceManager, profileManager);
  const findingsProvider = new FindingsTreeDataProvider();
  const contextProvider = new ContextTreeDataProvider();

  function loadProfileSettings(): void {
    profileManager.error = null;
    try {
      const inspected = config.inspect?.('profiles');
      profileManager.userProfiles = inspected
        ? ((inspected.globalValue as Record<string, unknown>) ?? {})
        : ((config.get('profiles') as Record<string, unknown>) ?? {});
      profileManager.baseSettings = Object.fromEntries(
        Object.keys(DEFAULT_PROFILE)
          .map((key) => [key, config.get(key)])
          .filter(([, value]) => value !== undefined)
      );
      profileManager.profilePath =
        (config.get('profilePath') as string) ?? '.codeatlas/profiles.json';
      profileManager.activeProfileName = validateProfileName(
        (config.get('activeProfile') as string) ?? 'default'
      );
      client.timeoutMs = profileManager.getActiveProfile().timeout * 1000;
    } catch (error: any) {
      profileManager.error = error.message;
      // Do not surface an invalid, potentially secret-bearing profile name.
      profileManager.activeProfileName = 'invalid';
      vscode.window.showErrorMessage(`CodeAtlas: ${error.message}`);
    }
  }

  function loadServiceSettings(): void {
    const inspected = config.inspect?.('serviceUrl');
    // The contributed localhost default is a hint, not an explicit override.
    serviceManager.explicitServiceUrl = inspected
      ? (inspected.workspaceFolderValue as string) ??
        (inspected.workspaceValue as string) ??
        (inspected.globalValue as string) ??
        null
      : (config.get('serviceUrl') as string) ?? null;
    serviceManager.serviceMode = (config.get('serviceMode') as any) ?? 'external';
    serviceManager.serviceHost = (config.get('serviceHost') as string) ?? '127.0.0.1';
    serviceManager.servicePort = (config.get('servicePort') as number) ?? 8765;
    serviceManager.serviceCommand = (config.get('serviceCommand') as string) ?? 'codeatlas serve';
    serviceManager.autoStartService = config.get('autoStartService') === true;
    serviceManager.trusted = (vscode.workspace as any).isTrusted !== false;
    client.enabled = serviceManager.serviceMode !== 'disabled';
  }

  serviceManager.onChange = () => {
    if (client.serviceUrl && client.serviceUrl !== serviceManager.currentUrl) resetReviewState();
    client.serviceUrl = serviceManager.currentUrl;
    statusProvider.refresh();
  };
  serviceManager.onCrash = (message: string) => {
    // A crashed service can no longer back the current run: never display
    // resolved revisions as if they were still current.
    statusProvider.clearShas();
    vscode.window.showErrorMessage(`CodeAtlas: ${message}`);
  };
  loadProfileSettings();
  loadServiceSettings();

  vscode.window.registerTreeDataProvider('codeatlas.statusView', statusProvider);
  vscode.window.registerTreeDataProvider('codeatlas.findingsView', findingsProvider);
  vscode.window.registerTreeDataProvider('codeatlas.contextView', contextProvider);

  let currentRunId: string | null = null;
  let currentFindings: Finding[] = [];
  let activeFinding: Finding | null = null;
  let activeDetailPanel: vscode.WebviewPanel | null = null;
  // The webview message handler is registered once per panel; it must act on
  // the finding currently displayed, never the one the panel was created with.
  let currentDetail: FindingDetail | null = null;
  const dismissedFindingIds = new Set<string>();
  // Phase 11C-B: latest fix proposal per finding for preview/reject/regenerate.
  const latestFixProposals = new Map<string, FixProposal>();
  // The virtual document provider needs vscode APIs that test doubles may not
  // implement; fix commands degrade gracefully when it is unavailable.
  let fixPreviewProvider: FixPreviewDocumentProvider | null = null;
  if (typeof vscode.EventEmitter === 'function' && vscode.workspace.registerTextDocumentContentProvider) {
    fixPreviewProvider = new FixPreviewDocumentProvider();
    context.subscriptions.push(
      vscode.workspace.registerTextDocumentContentProvider(FIX_PREVIEW_SCHEME, fixPreviewProvider),
      fixPreviewProvider
    );
  }
  const reviewTimers = new Set<NodeJS.Timeout>();
  let healthTimer: NodeJS.Timeout | null = null;
  let disposed = false;
  let checkingHealth = false;
  let configurationUpdate: Promise<void> = Promise.resolve();
  let disposal: Promise<void> | null = null;

  const session: ExtensionSession = {
    dispose(): Promise<void> {
      if (disposal) return disposal;
      disposed = true;
      if (healthTimer) clearInterval(healthTimer);
      for (const timer of reviewTimers) clearInterval(timer);
      client.dispose();
      disposal = serviceManager.dispose();
      return disposal;
    },
  };
  activeSession = session;
  context.subscriptions.push({
    dispose: () => {
      session.dispose().catch(() => {});
    },
  });

  function resetReviewState(): void {
    currentRunId = null;
    currentFindings = [];
    activeFinding = null;
    for (const timer of reviewTimers) clearInterval(timer);
    reviewTimers.clear();
    dismissedFindingIds.clear();
    latestFixProposals.clear();
    findingsProvider.refresh([]);
    contextProvider.refresh(null);
    statusProvider.setActiveRunId(null);
    statusProvider.refresh(null);
    currentDetail = null;
    if (activeDetailPanel) activeDetailPanel.webview.html = getNoFindingWebviewHtml();
    updateDecorationsForEditor();
  }

  function startHealthTimer(): void {
    if (healthTimer) clearInterval(healthTimer);
    const seconds = config.get('healthCheckInterval') as number ?? 30;
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 86400) {
      serviceManager.lastErrorMessage = 'healthCheckInterval must be between 0 and 86400 seconds.';
      statusProvider.refresh();
      return;
    }
    if (serviceManager.serviceMode === 'disabled' || seconds === 0) return;
    healthTimer = setInterval(async () => {
      if (
        disposed ||
        checkingHealth ||
        serviceManager.lastHealthStatus === 'starting' ||
        !serviceManager.currentUrl
      )
        return;
      checkingHealth = true;
      try {
        await serviceManager.checkHealth();
      } catch (_) {
        /* Status view reports failure. */
      } finally {
        checkingHealth = false;
      }
    }, Math.max(1, seconds) * 1000);
  }

  async function discoverService(): Promise<void> {
    try {
      await serviceManager.discover();
      if (serviceManager.lastHealthStatus === 'not_configured') {
        vscode.window.showInformationMessage(`CodeAtlas: ${SETUP_MESSAGE}`);
      }
    } catch (error: any) {
      vscode.window.showWarningMessage(`CodeAtlas: ${error.message}`);
    }
  }

  function applyDismissalState(): void {
    for (const f of currentFindings) {
      f.dismissed = dismissedFindingIds.has(f.id);
    }
  }

  function updateDecorationsForEditor(): void {
    const editor = vscode.window.activeTextEditor;
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

    for (const f of currentFindings) {
      if (f.dismissed) continue;
      if (pathMatches(docPath, f.file)) {
        const docLines = editor.document.lineCount;
        const bounds = findingLineBounds(f, docLines);
        const range = new vscode.Range(bounds.start - 1, 0, bounds.end - 1, 999);

        const hoverText = new vscode.MarkdownString();
        hoverText.appendMarkdown(
          `**CodeAtlas [${(f.severity || 'INFO').toUpperCase()}]:** ${f.claim}\n\n`
        );
        hoverText.appendMarkdown(`- **Category:** \`${f.category}\`\n`);
        hoverText.appendMarkdown(`- **Confidence:** ${f.confidence ?? 'unknown'}\n`);
        hoverText.appendMarkdown(`- **Evidence:** ${f.evidence_strength ?? 'unknown'}\n`);
        hoverText.appendMarkdown(`- **Status:** \`${f.status ?? 'open'}\``);

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

  async function syncActiveFinding(
    finding: Finding | null,
    cursorLine: number | null = null,
    multipleCount: number = 0
  ): Promise<void> {
    activeFinding = finding;
    findingsProvider.setActiveFinding(finding ? finding.id : null);
    updateDecorationsForEditor();

    if (finding && currentRunId) {
      try {
        const runId = currentRunId;
        const detail = await client.getFindingDetail(runId, finding.id);
        if (currentRunId !== runId || activeFinding?.id !== finding.id) return;
        detail.dismissed = dismissedFindingIds.has(finding.id);
        currentDetail = detail;
        contextProvider.refresh(detail.context || null, finding, cursorLine, multipleCount);
        if (activeDetailPanel && activeDetailPanel.visible) {
          activeDetailPanel.webview.html = getFindingWebviewHtml(detail);
        }
      } catch (err: any) {
        if (err.message && err.message.includes('404')) {
          handleStaleRun();
        } else {
          contextProvider.refresh(null, finding, cursorLine, multipleCount);
        }
      }
    } else {
      contextProvider.refresh(null, null, cursorLine, 0);
      if (activeDetailPanel && activeDetailPanel.visible) {
        activeDetailPanel.webview.html = getNoFindingWebviewHtml(cursorLine);
      }
    }
  }

  function handleStaleRun(): void {
    vscode.window.showWarningMessage(
      'CodeAtlas: Current review run expired or was not found on service. Please start a new review.'
    );
    currentRunId = null;
    currentFindings = [];
    activeFinding = null;
    latestFixProposals.clear();
    statusProvider.setActiveRunId(null);
    statusProvider.refresh(null);
    findingsProvider.refresh([]);
    contextProvider.refresh(null);
    updateDecorationsForEditor();
  }

  async function handleCursorSelectionChange(editor: vscode.TextEditor): Promise<void> {
    if (!editor || !editor.selection) return;
    const docPath = editor.document.uri.fsPath;
    const cursorLine = editor.selection.active.line + 1;

    const atCursor = findFindingsAtCursor(currentFindings, docPath, cursorLine);
    if (atCursor.length > 0) {
      const primary = atCursor[0];
      if (!activeFinding || activeFinding.id !== primary.id) {
        await syncActiveFinding(primary, cursorLine, atCursor.length);
      }
    } else {
      if (activeFinding !== null) {
        await syncActiveFinding(null, cursorLine, 0);
      }
    }
  }

  vscode.window.onDidChangeActiveTextEditor(
    (editor) => {
      updateDecorationsForEditor();
      if (editor) {
        handleCursorSelectionChange(editor);
      }
    },
    null,
    context.subscriptions
  );

  vscode.window.onDidChangeTextEditorSelection(
    (e) => {
      if (e.textEditor === vscode.window.activeTextEditor) {
        handleCursorSelectionChange(e.textEditor);
      }
    },
    null,
    context.subscriptions
  );

  vscode.workspace.onDidChangeTextDocument(
    (e) => {
      if (
        vscode.window.activeTextEditor &&
        e.document === vscode.window.activeTextEditor.document
      ) {
        updateDecorationsForEditor();
      }
    },
    null,
    context.subscriptions
  );

  async function navigateToFinding(finding: Finding): Promise<void> {
    if (!finding) return;

    const file = typeof finding.file === 'string' ? finding.file.replace(/\\/g, '/') : '';
    if (
      !file ||
      file.startsWith('/') ||
      file.includes(':') ||
      file.includes('\0') ||
      file.split('/').includes('..')
    ) {
      vscode.window.showWarningMessage(
        'CodeAtlas: Finding location must be a workspace-relative file.'
      );
      return;
    }

    if (!workspaceFolders || !workspaceFolders.length) {
      vscode.window.showWarningMessage('CodeAtlas: No workspace folder open.');
      await syncActiveFinding(finding);
      return;
    }

    const fileUri = vscode.Uri.joinPath(workspaceFolders[0].uri, file);
    try {
      const doc = await vscode.workspace.openTextDocument(fileUri);
      const editor = await vscode.window.showTextDocument(doc);
      const targetLine =
        clampLine(finding.start_line || finding.line || 1, doc.lineCount) - 1;
      const range = new vscode.Range(targetLine, 0, targetLine, 0);
      editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      editor.selection = new vscode.Selection(range.start, range.end);
    } catch (_) {
      vscode.window.showWarningMessage(`CodeAtlas: File not found or moved: ${finding.file}`);
    }

    await syncActiveFinding(finding);
  }

  async function explainFindingAction(finding: Finding): Promise<void> {
    if (!finding) return;
    if (!currentRunId) {
      vscode.window.showInformationMessage('CodeAtlas: No active review run.');
      return;
    }
    try {
      const explanation = await client.explainFinding(finding.id, currentRunId);
      vscode.window.showInformationMessage(
        `CodeAtlas Explanation (${explanation.category}): ${explanation.explanation}\n\nRemediation: ${explanation.remediation_advice}`,
        { modal: true }
      );
    } catch (err: any) {
      vscode.window.showErrorMessage(`CodeAtlas: Explanation failed: ${err.message}`);
    }
  }

  async function generateDraftFixAction(finding: Finding): Promise<void> {
    if (!finding) return;
    if (!currentRunId) {
      vscode.window.showInformationMessage('CodeAtlas: No active review run.');
      return;
    }
    try {
      const proposal = await client.proposePatch(finding.id, currentRunId);
      vscode.window
        .showInformationMessage(
          `CodeAtlas: Draft fix proposal created (${proposal.proposal_id}). Requires approval token to validate in sandbox.`,
          'Validate Approved Fix'
        )
        .then((selection) => {
          if (selection === 'Validate Approved Fix') {
            vscode.commands.executeCommand('codeatlas.validateApprovedFix', proposal);
          }
        });
    } catch (err: any) {
      vscode.window.showErrorMessage(`CodeAtlas: Draft fix generation failed: ${err.message}`);
    }
  }

  function isServiceNotFound(err: any): boolean {
    return typeof err?.message === 'string' && err.message.includes('404');
  }

  /**
   * Phase 11C-B: request (or regenerate) a bounded fix proposal for one finding.
   * The proposal is a preview-only draft; nothing is ever applied here.
   */
  async function generateFixAction(finding: Finding, regenerate: boolean = false): Promise<void> {
    if (!finding) return;
    if (!currentRunId) {
      vscode.window.showInformationMessage('CodeAtlas: No active review run.');
      return;
    }
    const runId = currentRunId;
    try {
      const proposal = regenerate
        ? await client.regenerateFixProposal(runId, finding.id)
        : await client.requestFixProposal(runId, finding.id);
      if (currentRunId !== runId) return;
      if (!proposal || proposal.finding_id !== finding.id || proposal.run_id !== runId) {
        vscode.window.showWarningMessage(
          'CodeAtlas: The service returned a fix proposal that does not match the selected finding or run.'
        );
        return;
      }
      if (proposal.generation_status === 'draft_ready' && proposal.proposal_id) {
        latestFixProposals.set(finding.id, proposal);
        void vscode.window
          .showInformationMessage(
            `CodeAtlas: Fix proposal ${proposal.proposal_id} ready (risk ${proposal.risk_level}, confidence ${proposal.confidence}). Approval is required and nothing has been applied.`,
            'Preview Fix',
            'Reject Fix'
          )
          .then((selection) => {
            if (selection === 'Preview Fix') {
              vscode.commands.executeCommand('codeatlas.previewFix', finding);
            } else if (selection === 'Reject Fix') {
              vscode.commands.executeCommand('codeatlas.rejectFix', finding);
            }
          });
      } else {
        const explanation = proposal.rejection_explanation || 'Fix generation did not produce a proposal.';
        const statusText = String(proposal.generation_status || 'unavailable').replace(/_/g, ' ');
        vscode.window.showWarningMessage(`CodeAtlas: Fix proposal ${statusText}: ${explanation}`);
      }
    } catch (err: any) {
      if (isServiceNotFound(err)) {
        handleStaleRun();
      } else {
        vscode.window.showErrorMessage(`CodeAtlas: Fix generation failed: ${err.message}`);
      }
    }
  }

  /** Read-only diff preview of a fix proposal against the workspace file. */
  async function previewFixAction(finding: Finding): Promise<void> {
    if (!finding) return;
    if (!currentRunId) {
      vscode.window.showInformationMessage('CodeAtlas: No active review run.');
      return;
    }
    if (!workspaceFolders || !workspaceFolders.length) {
      vscode.window.showWarningMessage('CodeAtlas: No workspace folder open.');
      return;
    }
    const runId = currentRunId;
    let proposal = latestFixProposals.get(finding.id);
    if (!proposal) {
      try {
        proposal = await client.getFixProposal(runId, finding.id);
      } catch (err: any) {
        if (isServiceNotFound(err)) {
          vscode.window.showInformationMessage(
            'CodeAtlas: No fix proposal exists for this finding yet. Run Generate Fix first.'
          );
        } else {
          vscode.window.showErrorMessage(`CodeAtlas: Fix preview failed: ${err.message}`);
        }
        return;
      }
    }
    if (currentRunId !== runId) return;
    if (!proposal || proposal.finding_id !== finding.id || proposal.run_id !== runId) {
      vscode.window.showWarningMessage(
        'CodeAtlas: The stored fix proposal does not match the selected finding or run.'
      );
      return;
    }
    if (proposal.generation_status !== 'draft_ready' || !proposal.patch_text || !proposal.approval_required) {
      const explanation = proposal.rejection_explanation || 'The fix proposal is not in a previewable state.';
      vscode.window.showWarningMessage(`CodeAtlas: Fix proposal not previewable: ${explanation}`);
      return;
    }
    const targetFile = (proposal.target_files && proposal.target_files[0]) || finding.file || '';
    const normalized = typeof targetFile === 'string' ? targetFile.replace(/\\/g, '/') : '';
    if (
      !normalized ||
      normalized.startsWith('/') ||
      normalized.includes(':') ||
      normalized.includes('\0') ||
      normalized.split('/').includes('..')
    ) {
      vscode.window.showWarningMessage('CodeAtlas: The fix proposal target must be a workspace-relative file.');
      return;
    }
    try {
      if (!fixPreviewProvider) {
        vscode.window.showWarningMessage(
          'CodeAtlas: The fix diff preview is unavailable in this environment.'
        );
        return;
      }
      const fileUri = vscode.Uri.joinPath(workspaceFolders[0].uri, normalized);
      const doc = await vscode.workspace.openTextDocument(fileUri);
      const proposed = renderProposalPreview(doc.getText(), proposal.patch_text, normalized);
      const leftUri = fixPreviewProvider.registerContent('original', normalized, doc.getText());
      const rightUri = fixPreviewProvider.registerContent('proposed', normalized, proposed);
      await vscode.commands.executeCommand(
        'vscode.diff',
        leftUri,
        rightUri,
        `CodeAtlas Fix Preview (read-only): ${normalized} — ${proposal.proposal_id}`,
        { preview: true }
      );
      const decision = String((proposal.policy_decision as any)?.decision ?? 'requires_human_approval');
      const meta = [
        `Proposal ${proposal.proposal_id} — preview only, nothing has been applied.`,
        `Patch hash: ${proposal.patch_hash.slice(0, 16)}…`,
        `Risk: ${proposal.risk_level} | Confidence: ${proposal.confidence} | Evidence: ${proposal.evidence_strength}`,
        `Quality decision: ${proposal.quality_decision} | Policy: ${decision} | Approval required: yes`,
        proposal.assumptions.length
          ? `Assumptions: ${proposal.assumptions.slice(0, 3).join(' ')}`
          : '',
        proposal.limitations.length
          ? `Limitations: ${proposal.limitations.slice(0, 3).map((l: string) => `• ${l}`).join(' ')}`
          : '',
      ]
        .filter(Boolean)
        .join('\n');
      vscode.window.showInformationMessage(meta, { modal: true });
    } catch (err: any) {
      if (err instanceof PatchPreviewError) {
        vscode.window.showWarningMessage(`CodeAtlas: ${err.message}`);
      } else {
        vscode.window.showWarningMessage(
          `CodeAtlas: Fix preview failed: ${String(err?.message || 'unexpected error').slice(0, 200)}`
        );
      }
    }
  }

  /** Reject a fix proposal; the ID must match the current run and finding. */
  async function rejectFixAction(finding: Finding): Promise<void> {
    if (!finding) return;
    if (!currentRunId) {
      vscode.window.showInformationMessage('CodeAtlas: No active review run.');
      return;
    }
    const runId = currentRunId;
    let proposalId: string | undefined = latestFixProposals.get(finding.id)?.proposal_id;
    if (!proposalId) {
      try {
        const fetched = await client.getFixProposal(runId, finding.id);
        if (fetched && fetched.finding_id === finding.id && fetched.run_id === runId) {
          proposalId = fetched.proposal_id;
        }
      } catch (_) {
        /* No proposal exists; report below. */
      }
    }
    if (!proposalId) {
      vscode.window.showInformationMessage('CodeAtlas: No fix proposal to reject for this finding.');
      return;
    }
    try {
      const rejected = await client.rejectFixProposal(runId, finding.id, proposalId);
      if (currentRunId !== runId) return;
      if (!rejected || rejected.finding_id !== finding.id || rejected.proposal_id !== proposalId) {
        vscode.window.showWarningMessage(
          'CodeAtlas: The service rejected a fix proposal that does not match the selected finding or run.'
        );
        return;
      }
      if (rejected.generation_status === 'rejected') {
        latestFixProposals.delete(finding.id);
        vscode.window.showInformationMessage(
          `CodeAtlas: Fix proposal ${proposalId} rejected. Nothing was applied to the workspace.`
        );
      } else {
        vscode.window.showWarningMessage('CodeAtlas: The fix proposal could not be rejected in its current state.');
      }
    } catch (err: any) {
      if (isServiceNotFound(err)) {
        handleStaleRun();
      } else {
        vscode.window.showErrorMessage(`CodeAtlas: Reject failed: ${err.message}`);
      }
    }
  }

  /** Resolve the finding a fix command should act on: argument, active, or quick pick. */
  async function resolveFixFinding(item?: any): Promise<Finding | null> {
    const finding = item ? item.finding || item : activeFinding;
    if (finding) return finding;
    if (!currentRunId) {
      vscode.window.showInformationMessage('CodeAtlas: No active review run.');
      return null;
    }
    if (!currentFindings.length) {
      vscode.window.showInformationMessage('CodeAtlas: No findings available.');
      return null;
    }
    const selected = await vscode.window.showQuickPick(
      currentFindings.map(formatQuickPickItem),
      { placeHolder: 'Select a finding for the fix proposal' }
    );
    return selected ? selected.finding : null;
  }

  async function toggleDismissFindingAction(finding: Finding): Promise<void> {
    if (!finding) return;
    if (dismissedFindingIds.has(finding.id)) {
      dismissedFindingIds.delete(finding.id);
      finding.dismissed = false;
      vscode.window.showInformationMessage(`CodeAtlas: Finding ${finding.id} restored.`);
    } else {
      dismissedFindingIds.add(finding.id);
      finding.dismissed = true;
      vscode.window.showInformationMessage(`CodeAtlas: Finding ${finding.id} dismissed locally.`);
    }

    findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
    updateDecorationsForEditor();

    if (
      activeFinding &&
      activeFinding.id === finding.id &&
      activeDetailPanel &&
      activeDetailPanel.visible
    ) {
      try {
        const detail = await client.getFindingDetail(currentRunId!, finding.id);
        detail.dismissed = finding.dismissed;
        activeDetailPanel.webview.html = getFindingWebviewHtml(detail);
      } catch (_) {}
    }
  }

  const ready = discoverService().then(() => serviceManager.currentUrl);
  startHealthTimer();
  if (vscode.workspace.onDidChangeConfiguration) {
    context.subscriptions.push(
      vscode.workspace.onDidChangeConfiguration((event) => {
        if (!event.affectsConfiguration('codeatlas')) return;
        configurationUpdate = configurationUpdate
          .then(async () => {
            if (disposed) return;
            config = vscode.workspace.getConfiguration('codeatlas');
            loadProfileSettings();
            const serviceChanged = [
              'serviceUrl',
              'serviceMode',
              'serviceHost',
              'servicePort',
              'serviceCommand',
              'autoStartService',
            ].some((key) => event.affectsConfiguration(`codeatlas.${key}`));
            if (serviceChanged) {
              client.enabled = false;
              await serviceManager.stopManagedService();
              await serviceManager.waitForStartup();
              if (disposed) return;
              resetReviewState();
              serviceManager.currentUrl = null;
              serviceManager.lastHealth = null;
              loadServiceSettings();
              await discoverService();
            }
            startHealthTimer();
            statusProvider.refresh();
          })
          .catch(() => {
            vscode.window.showErrorMessage(
              'CodeAtlas: Configuration update failed. Check service status and reload the extension.'
            );
          });
      })
    );
  }

  // Command: Start Local Service
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.startLocalService', async () => {
      try {
        vscode.window.showInformationMessage('CodeAtlas: Starting managed local service...');
        const res = await serviceManager.startManagedService();
        statusProvider.refresh();
        vscode.window.showInformationMessage(
          `CodeAtlas: ${res.started ? 'Started' : 'Using existing'} service on ${res.url}.`
        );
      } catch (err: any) {
        statusProvider.refresh();
        vscode.window.showErrorMessage(`CodeAtlas: Failed to start service: ${err.message}`);
      }
    })
  );

  // Command: Stop Local Service
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.stopLocalService', async () => {
      try {
        const res = await serviceManager.stopManagedService();
        statusProvider.refresh();
        vscode.window.showInformationMessage(
          res.stopped
            ? 'CodeAtlas: Managed service stopped.'
            : 'CodeAtlas: No owned service process is running.'
        );
      } catch (error: any) {
        vscode.window.showErrorMessage(`CodeAtlas: ${error.message}`);
      }
    })
  );

  // Command: Restart Local Service
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.restartLocalService', async () => {
      try {
        vscode.window.showInformationMessage('CodeAtlas: Restarting managed service...');
        const res = await serviceManager.restartManagedService();
        statusProvider.refresh();
        vscode.window.showInformationMessage(
          `CodeAtlas: ${res.started ? 'Restarted managed' : 'Using existing external'} service on ${
            res.url
          }.`
        );
      } catch (err: any) {
        statusProvider.refresh();
        vscode.window.showErrorMessage(`CodeAtlas: Restart failed: ${err.message}`);
      }
    })
  );

  // Command: Select Configuration Profile
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.selectConfigurationProfile', async () => {
      try {
        const profiles = profileManager.loadProfiles();
        const items: ProfileQuickPickItem[] = Object.keys(profiles).map((name) => ({
          label: name === profileManager.activeProfileName ? `✓ ${name}` : name,
          description: `Provider: ${profiles[name].provider || 'mock'}, Timeout: ${
            profiles[name].timeout || 30
          }s, Max Findings: ${profiles[name].maxFindings || 50}`,
          profileName: name,
        }));

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select active CodeAtlas configuration profile',
        });

        if (selected && selected.profileName) {
          const target = workspaceRoot
            ? (vscode.ConfigurationTarget?.Workspace ?? 2)
            : (vscode.ConfigurationTarget?.Global ?? 1);
          await config.update('activeProfile', selected.profileName, target);
          const profile = profileManager.select(selected.profileName, profiles);
          profileManager.error = null;
          client.timeoutMs = profile.timeout * 1000;
          statusProvider.refresh();
          vscode.window.showInformationMessage(
            `CodeAtlas: Active profile set to '${selected.profileName}'.`
          );
        }
      } catch (err: any) {
        vscode.window.showErrorMessage(`CodeAtlas: Profile selection failed: ${err.message}`);
      }
    })
  );

  // Command: Open Configuration
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.openConfiguration', () => {
      vscode.commands.executeCommand('workbench.action.openSettings', 'codeatlas');
    })
  );

  // Command: Check Service Health
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.checkServiceHealth', async () => {
      try {
        const h: any = await serviceManager.checkHealth();
        statusProvider.refresh();
        vscode.window.showInformationMessage(
          h.status === 'ok'
            ? `CodeAtlas Service Healthy: Version ${h.version}, Active Reviews: ${
                h.active_reviews ?? 'unknown'
              }, Provider: ${h.provider}`
            : 'CodeAtlas service is disabled or the health check was cancelled.'
        );
      } catch (err: any) {
        statusProvider.refresh();
        vscode.window.showWarningMessage(`CodeAtlas Service Unreachable: ${err.message}`);
      }
    })
  );

  // Command: Start Review
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.startReview', async () => {
      if (!workspaceFolders || !workspaceFolders.length) {
        vscode.window.showErrorMessage('CodeAtlas: Open a workspace folder first.');
        return;
      }

      const repoPath = workspaceFolders[0].uri.fsPath;
      try {
        await ready;
        await configurationUpdate;
        if (profileManager.error) throw new Error(profileManager.error);
        const activeProf = profileManager.getActiveProfile();
        if (activeProf.provider === 'live' && !activeProf.enableLiveReviewer) {
          throw new Error(
            'Live reviewer requires enableLiveReviewer=true in the active configuration.'
          );
        }
        if (serviceManager.serviceMode === 'disabled') {
          throw new Error('CodeAtlas service is disabled.');
        }
        const health: any = await serviceManager.checkHealth();
        if (health.status !== 'ok') throw new Error('CodeAtlas service is not ready.');
        client.timeoutMs = activeProf.timeout * 1000;

        vscode.window.showInformationMessage('CodeAtlas: Starting review...');
        // Read at Start Review time so setting changes apply to the next run.
        // Empty or whitespace-only values fall back to the default branch.
        const configuredBase = String(config.get('baseBranch') ?? 'main').trim();
        const baseBranch = configuredBase.length ? configuredBase : 'main';
        resetReviewState();
        const resp = await client.startReview(repoPath, {
          base: baseBranch,
          head: 'HEAD',
          review_provider: activeProf.provider,
          provider_model: activeProf.model || undefined,
          provider_timeout: activeProf.timeout,
          allow_patch_suggestions: activeProf.enablePatchSuggestions,
          max_findings: activeProf.maxFindings,
        });

        currentRunId = resp.run_id ?? null;
        statusProvider.setActiveRunId(currentRunId);
        statusProvider.lastReviewBase = baseBranch;
        statusProvider.refresh(resp);

        const runId = currentRunId;
        const pollInterval = setInterval(async () => {
          if (!runId || currentRunId !== runId) {
            clearInterval(pollInterval);
            return;
          }
          try {
            const status: ReviewStatus = await client.getReview(runId);
            if (currentRunId !== runId || status.run_id !== runId) return;
            statusProvider.refresh(status);

            if (
              status.status === 'completed' ||
              status.status === 'failed' ||
              status.status === 'cancelled'
            ) {
              clearInterval(pollInterval);
              reviewTimers.delete(pollInterval);
              const findingsResp = await client.getFindings(runId, {
                include_dismissed: true,
              });
              if (currentRunId !== runId) return;
              currentFindings = findingsResp.findings || [];
              applyDismissalState();
              findingsProvider.refresh(currentFindings);
              updateDecorationsForEditor();

              if (status.status === 'completed') {
                vscode.window.showInformationMessage(
                  `CodeAtlas: Review complete with ${currentFindings.length} findings.`
                );
              } else if (status.status === 'failed') {
                vscode.window.showWarningMessage(
                  `CodeAtlas: Review failed: ${(status.errors || []).join('; ')}`
                );
              }
            }
          } catch (pollErr: any) {
            clearInterval(pollInterval);
            reviewTimers.delete(pollInterval);
            if (currentRunId !== runId) return;
            if (pollErr.message && pollErr.message.includes('404')) {
              handleStaleRun();
            } else {
              vscode.window.showErrorMessage(
                `CodeAtlas: Review polling error: ${pollErr.message}`
              );
            }
          }
        }, 1000);
        reviewTimers.add(pollInterval);
      } catch (err: any) {
        vscode.window.showErrorMessage(`CodeAtlas: Start review failed: ${err.message}`);
      }
    })
  );

  // Command: Cancel Review
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.cancelReview', async () => {
      if (!currentRunId) {
        vscode.window.showInformationMessage('CodeAtlas: No review is currently running.');
        return;
      }
      const runId = currentRunId;
      try {
        await client.cancelReview(runId);
        if (currentRunId !== runId) return;
        statusProvider.clearShas();
        const status = await client.getReview(runId);
        if (currentRunId !== runId || status.run_id !== runId) return;
        statusProvider.refresh(status);
        vscode.window.showInformationMessage('CodeAtlas: Review cancelled.');
      } catch (err: any) {
        vscode.window.showErrorMessage(`CodeAtlas: Cancel failed: ${err.message}`);
      }
    })
  );

  // Command: Refresh
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.refresh', async () => {
      if (!currentRunId) {
        try {
          await serviceManager.checkHealth();
        } catch (_) {}
        statusProvider.refresh();
        vscode.window.showInformationMessage('CodeAtlas: Status refreshed.');
        return;
      }
      const runId = currentRunId;
      try {
        const status = await client.getReview(runId);
        if (currentRunId !== runId || status.run_id !== runId) return;
        statusProvider.refresh(status);
        const findingsResp = await client.getFindings(runId, {
          include_dismissed: true,
        });
        if (currentRunId !== runId) return;
        currentFindings = findingsResp.findings || [];
        applyDismissalState();
        findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
        updateDecorationsForEditor();
      } catch (err: any) {
        if (currentRunId !== runId) return;
        if (err.message && err.message.includes('404')) {
          handleStaleRun();
        } else {
          vscode.window.showErrorMessage(`CodeAtlas: Refresh failed: ${err.message}`);
        }
      }
    })
  );

  // Command: Open Finding Location
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.openFindingLocation', async (finding: Finding) => {
      await navigateToFinding(finding);
    })
  );

  // QuickPick Command 1: CodeAtlas: Find Finding
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.findFinding', async () => {
      if (!currentRunId) {
        vscode.window.showInformationMessage('CodeAtlas: No active review run.');
        return;
      }
      if (!currentFindings.length) {
        vscode.window.showInformationMessage('CodeAtlas: No findings available.');
        return;
      }
      const items: FindingQuickPickItem[] = currentFindings.map(formatQuickPickItem);
      const selected = await vscode.window.showQuickPick(items, {
        placeHolder: 'Select a finding to navigate and inspect',
        matchOnDescription: true,
        matchOnDetail: true,
      });
      if (selected && selected.finding) {
        await navigateToFinding(selected.finding);
      }
    })
  );

  // QuickPick Command 2: CodeAtlas: Explain Current Finding
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.explainCurrentFinding', async () => {
      if (!currentRunId) {
        vscode.window.showInformationMessage('CodeAtlas: No active review run.');
        return;
      }
      let target = activeFinding;
      if (!target) {
        const editor = vscode.window.activeTextEditor;
        if (editor) {
          const cursorLine = editor.selection.active.line + 1;
          const atCursor = findFindingsAtCursor(
            currentFindings,
            editor.document.uri.fsPath,
            cursorLine
          );
          if (atCursor.length > 0) {
            target = atCursor[0];
          }
        }
      }
      if (!target) {
        vscode.window.showInformationMessage(
          'CodeAtlas: No finding at current cursor position.'
        );
        return;
      }
      await explainFindingAction(target);
    })
  );

  // QuickPick Command 3: CodeAtlas: Show Context
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.showContext', async (item?: any) => {
      let finding = item ? item.finding || item : activeFinding;
      if (!finding) {
        if (!currentRunId) {
          vscode.window.showInformationMessage('CodeAtlas: No active review run.');
          return;
        }
        if (!currentFindings.length) {
          vscode.window.showInformationMessage('CodeAtlas: No findings available.');
          return;
        }
        const items: FindingQuickPickItem[] = currentFindings.map(formatQuickPickItem);
        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select a finding to inspect context',
        });
        if (!selected) return;
        finding = selected.finding;
      }
      await syncActiveFinding(finding);
    })
  );

  // QuickPick Command 4: CodeAtlas: Generate Draft Fix
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.generateDraftFix', async (item?: any) => {
      let finding = item ? item.finding || item : activeFinding;
      if (!finding) {
        if (!currentRunId) {
          vscode.window.showInformationMessage('CodeAtlas: No active review run.');
          return;
        }
        if (!currentFindings.length) {
          vscode.window.showInformationMessage('CodeAtlas: No findings available.');
          return;
        }
        const items: FindingQuickPickItem[] = currentFindings.map(formatQuickPickItem);
        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select a finding to generate draft fix proposal',
        });
        if (!selected) return;
        finding = selected.finding;
      }
      await generateDraftFixAction(finding);
    })
  );

  // Phase 11C-B Command: CodeAtlas: Generate Fix (bounded, preview-only proposal)
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.generateFix', async (item?: any) => {
      const finding = await resolveFixFinding(item);
      if (finding) await generateFixAction(finding, false);
    })
  );

  // Phase 11C-B Command: CodeAtlas: Preview Fix (read-only diff, never applied)
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.previewFix', async (item?: any) => {
      const finding = await resolveFixFinding(item);
      if (finding) await previewFixAction(finding);
    })
  );

  // Phase 11C-B Command: CodeAtlas: Reject Fix
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.rejectFix', async (item?: any) => {
      const finding = await resolveFixFinding(item);
      if (finding) await rejectFixAction(finding);
    })
  );

  // Phase 11C-B Command: CodeAtlas: Regenerate Fix
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.regenerateFix', async (item?: any) => {
      const finding = await resolveFixFinding(item);
      if (finding) await generateFixAction(finding, true);
    })
  );

  // QuickPick Command 5: CodeAtlas: Validate Approved Fix
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.validateApprovedFix', async (proposalItem?: any) => {
      let proposalId = proposalItem ? proposalItem.proposal_id || proposalItem.id : null;
      if (!proposalId) {
        proposalId = await vscode.window.showInputBox({
          prompt: 'Enter proposal ID to validate (e.g. prop-...)',
        });
      }
      if (!proposalId) return;

      const approvalToken = await vscode.window.showInputBox({
        prompt: `Enter scoped approval token for proposal ${proposalId}`,
        password: true,
      });

      if (!approvalToken) {
        vscode.window.showWarningMessage(
          'CodeAtlas: Approval token is required to validate patch in isolated sandbox.'
        );
        return;
      }

      try {
        if (profileManager.error) throw new Error(profileManager.error);
        const profile = profileManager.getActiveProfile();
        vscode.window.showInformationMessage(
          `CodeAtlas: Validating proposal ${proposalId} in detached sandbox...`
        );
        const result = await client.validateProposal(
          proposalId,
          approvalToken,
          profile.enableTestExecution,
          profile.enableFullSuiteExecution
        );
        if (result.valid && result.approval_verified) {
          vscode.window.showInformationMessage(
            `CodeAtlas: Patch validated cleanly in isolated sandbox! Status: ${result.status}`
          );
        } else {
          vscode.window.showWarningMessage(
            `CodeAtlas: Patch validation rejected: ${(result.errors || []).join('; ')}`
          );
        }
      } catch (err: any) {
        vscode.window.showErrorMessage(`CodeAtlas: Validation error: ${err.message}`);
      }
    })
  );

  // QuickPick Command 6: CodeAtlas: Dismiss Finding
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.dismissFinding', async (item?: any) => {
      let finding = item ? item.finding || item : activeFinding;
      if (!finding) {
        if (!currentRunId) {
          vscode.window.showInformationMessage('CodeAtlas: No active review run.');
          return;
        }
        if (!currentFindings.length) {
          vscode.window.showInformationMessage('CodeAtlas: No findings available.');
          return;
        }
        const items: FindingQuickPickItem[] = currentFindings.map(formatQuickPickItem);
        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select a finding to dismiss or restore locally',
        });
        if (!selected) return;
        finding = selected.finding;
      }
      await toggleDismissFindingAction(finding);
    })
  );

  // Command: Show Finding Details (Webview panel)
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.showFindingDetails', async (item?: any) => {
      const finding = item ? item.finding || item : activeFinding;
      if (!finding || !currentRunId) {
        vscode.window.showInformationMessage('CodeAtlas: Select a finding first.');
        return;
      }

      try {
        const detail: FindingDetail = await client.getFindingDetail(currentRunId, finding.id);
        detail.dismissed = dismissedFindingIds.has(finding.id);

        if (!activeDetailPanel) {
          activeDetailPanel = vscode.window.createWebviewPanel(
            'codeatlasFindingDetail',
            `CodeAtlas Finding: ${detail.id}`,
            vscode.ViewColumn.Beside,
            { enableScripts: true }
          );

          activeDetailPanel.onDidDispose(
            () => {
              activeDetailPanel = null;
              currentDetail = null;
            },
            null,
            context.subscriptions
          );

          activeDetailPanel.webview.onDidReceiveMessage(async (msg: any) => {
            // Route actions against the finding currently shown in the panel.
            const target = currentDetail;
            if (!target) return;
            if (msg.action === 'explain') {
              vscode.commands.executeCommand('codeatlas.explainFinding', target);
            } else if (msg.action === 'draftFix') {
              vscode.commands.executeCommand('codeatlas.generateDraftFix', target);
            } else if (msg.action === 'generateFix') {
              vscode.commands.executeCommand('codeatlas.generateFix', target);
            } else if (msg.action === 'previewFix') {
              vscode.commands.executeCommand('codeatlas.previewFix', target);
            } else if (msg.action === 'rejectFix') {
              vscode.commands.executeCommand('codeatlas.rejectFix', target);
            } else if (msg.action === 'regenerateFix') {
              vscode.commands.executeCommand('codeatlas.regenerateFix', target);
            } else if (msg.action === 'copy') {
              await vscode.env.clipboard.writeText(JSON.stringify(target, null, 2));
              vscode.window.showInformationMessage('CodeAtlas: Finding copied to clipboard.');
            } else if (msg.action === 'dismiss') {
              vscode.commands.executeCommand('codeatlas.dismissFinding', target);
            }
          });
        }

        currentDetail = detail;
        activeDetailPanel.title = `CodeAtlas Finding: ${detail.id}`;
        activeDetailPanel.webview.html = getFindingWebviewHtml(detail);
      } catch (err: any) {
        if (err.message && err.message.includes('404')) {
          handleStaleRun();
        } else {
          vscode.window.showErrorMessage(
            `CodeAtlas: Cannot fetch finding details: ${err.message}`
          );
        }
      }
    })
  );

  // Command: Explain Finding (direct)
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.explainFinding', async (item?: any) => {
      const finding = item ? item.finding || item : activeFinding;
      await explainFindingAction(finding);
    })
  );

  // Command: Copy Finding
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.copyFinding', async (item?: any) => {
      const finding = item ? item.finding || item : activeFinding;
      if (!finding) return;
      await vscode.env.clipboard.writeText(JSON.stringify(finding, null, 2));
      vscode.window.showInformationMessage('CodeAtlas: Finding copied to clipboard.');
    })
  );

  // Command: Copy Comparison Range
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.copyComparisonRange', async (item?: vscode.TreeItem) => {
      // Read only the current validated revisions; never serialize the item,
      // display label, or arbitrary service/command data into the clipboard.
      const comparison = statusProvider.getComparisonRange(currentRunId, item);
      if (!comparison) {
        vscode.window.showWarningMessage(
          'CodeAtlas: Comparison range unavailable or unresolved. Both full validated base and head SHAs must belong to the active review run; refresh the comparison item or start a new review.'
        );
        return;
      }
      try {
        await vscode.env.clipboard.writeText(comparison);
        vscode.window.showInformationMessage('CodeAtlas: Comparison range copied to clipboard.');
      } catch (_) {
        vscode.window.showErrorMessage('CodeAtlas: Could not copy comparison range to clipboard.');
      }
    })
  );

  // Command: Filter by Severity
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.filterSeverity', async () => {
      const pick = await vscode.window.showQuickPick(
        ['All', 'Blocker', 'High', 'Medium', 'Low', 'Info'],
        {
          placeHolder: 'Filter findings by severity',
        }
      );
      if (pick) {
        findingsProvider.filterSeverity = pick === 'All' ? null : pick;
        findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
      }
    })
  );

  // Command: Filter by Category
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.filterCategory', async () => {
      const categories = ['All', ...new Set(currentFindings.map((f) => f.category))];
      const pick = await vscode.window.showQuickPick(categories, {
        placeHolder: 'Filter findings by category',
      });
      if (pick) {
        findingsProvider.filterCategory = pick === 'All' ? null : pick;
        findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
      }
    })
  );

  return {
    ready,
    serviceManager,
    profileManager,
    statusProvider,
    client,
    dispose: () => session.dispose(),
  };
}

export async function deactivate(): Promise<void> {
  const session = activeSession;
  activeSession = null;
  if (session) await session.dispose();
  disposeDecorations();
}
