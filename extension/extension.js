/**
 * @deprecated Legacy Phase 10C JavaScript implementation.
 * Retained temporarily for comparison.
 * The packaged extension loads the TypeScript-compiled output from ./out/extension.js.
 *
 * CodeAtlas VS Code Extension
 * Evidence-first, repository-aware code review for VS Code.
 * Phase 10C: Service discovery, configuration profiles, and lifecycle management.
 */

const vscode = require('vscode');
const http = require('http');
const { ProfileManager, validateProfile, validateProfileName, DEFAULT_PROFILE,
  ALLOWED_PROFILE_KEYS, FORBIDDEN_PROFILE_KEYS } = require('./profiles');
const { ServiceLifecycleManager, validateServiceUrl, SETUP_MESSAGE } = require('./service');

// Severity ordering for ranking multiple findings on one line
const SEVERITY_ORDER = {
  blocker: 5,
  high: 4,
  medium: 3,
  low: 2,
  info: 1,
};

function sortFindingsBySeverity(findings) {
  return [...findings].sort((a, b) => {
    const sa = SEVERITY_ORDER[(a.severity || 'info').toLowerCase()] || 0;
    const sb = SEVERITY_ORDER[(b.severity || 'info').toLowerCase()] || 0;
    return sb - sa;
  });
}

function normalizePath(p) {
  return (p || '').replace(/\\/g, '/').toLowerCase();
}

function pathMatches(docPath, filePath) {
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

function clampLine(line, maxLines) {
  const num = parseInt(line, 10);
  if (isNaN(num) || num < 1) return 1;
  if (maxLines && num > maxLines) return maxLines;
  return num;
}

function findFindingsAtCursor(findings, docPath, cursorLine) {
  if (!findings || !docPath || !cursorLine) return [];
  const matches = findings.filter(f => {
    if (f.dismissed) return false;
    if (!pathMatches(docPath, f.file)) return false;
    const start = f.start_line || f.line || 1;
    const end = f.end_line || f.line || 1;
    return cursorLine >= start && cursorLine <= end;
  });
  return sortFindingsBySeverity(matches);
}

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

// Decoration types by severity with visible text badges (not relying on color alone)
const decorationTypes = {
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

class CodeAtlasClient {
  constructor(serviceUrl) {
    this.serviceUrl = serviceUrl ? validateServiceUrl(serviceUrl) : null;
    this.timeoutMs = 30000;
    this.enabled = true;
    this.pending = new Set();
  }

  request(method, endpoint, body, timeoutMs = this.timeoutMs) {
    return new Promise((resolve, reject) => {
      if (!this.enabled || !this.serviceUrl) {
        reject(new Error(this.enabled ? SETUP_MESSAGE : 'CodeAtlas service is disabled.'));
        return;
      }
      const parsed = new URL(validateServiceUrl(this.serviceUrl) + endpoint);
      const postData = body ? JSON.stringify(body) : null;

      const options = {
        hostname: parsed.hostname.replace(/^\[|\]$/g, ''),
        port: parsed.port || 80,
        path: parsed.pathname + parsed.search,
        method: method,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': postData ? Buffer.byteLength(postData) : 0,
        },
        timeout: timeoutMs,
      };

      const req = http.request(options, (res) => {
        let responseBody = '';
        let responseBytes = 0;
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          responseBytes += Buffer.byteLength(chunk);
          if (responseBytes > 2 * 1024 * 1024) {
            req.destroy();
            reject(new Error('CodeAtlas response exceeded the size limit.'));
            return;
          }
          responseBody += chunk;
        });
        res.on('error', () => reject(new Error('CodeAtlas response was interrupted.')));
        res.on('end', () => {
          try {
            const data = responseBody ? JSON.parse(responseBody) : {};
            if (res.statusCode >= 200 && res.statusCode < 300) {
              resolve(data);
            } else {
              reject(new Error(`CodeAtlas request failed (HTTP ${res.statusCode}).`));
            }
          } catch (err) {
            reject(new Error('Malformed response from CodeAtlas service.'));
          }
        });
      });

      const timer = setTimeout(() => {
        req.destroy();
        reject(new Error('Request to CodeAtlas service timed out.'));
      }, timeoutMs);
      this.pending.add(req);
      req.on('close', () => { clearTimeout(timer); this.pending.delete(req); });

      req.on('error', () => {
        reject(new Error('CodeAtlas service is unreachable. Check service health and configuration.'));
      });

      if (postData) {
        req.write(postData);
      }
      req.end();
    });
  }

  dispose() {
    this.enabled = false;
    for (const request of this.pending) request.destroy();
    this.pending.clear();
  }

  getHealth(timeoutMs = 1000) {
    return this.request('GET', '/health', null, timeoutMs);
  }

  startReview(repo, options = {}) {
    return this.request('POST', '/reviews', {
      repo,
      base: options.base || 'main',
      head: options.head || 'HEAD',
      review_provider: options.review_provider,
      provider_model: options.provider_model,
      provider_timeout: options.provider_timeout,
      allow_patch_suggestions: options.allow_patch_suggestions,
      max_findings: options.max_findings ?? 50,
    });
  }

  getReview(runId) {
    return this.request('GET', `/reviews/${runId}`);
  }

  getFindings(runId, filters = {}) {
    let query = [];
    if (filters.severity) query.push(`severity=${encodeURIComponent(filters.severity)}`);
    if (filters.category) query.push(`category=${encodeURIComponent(filters.category)}`);
    if (filters.status) query.push(`status=${encodeURIComponent(filters.status)}`);
    if (filters.include_dismissed) query.push('include_dismissed=true');
    const qs = query.length ? '?' + query.join('&') : '';
    return this.request('GET', `/reviews/${runId}/findings${qs}`);
  }

  getFindingDetail(runId, findingId) {
    return this.request('GET', `/reviews/${runId}/findings/${findingId}`);
  }

  cancelReview(runId) {
    return this.request('POST', `/reviews/${runId}/cancel`);
  }

  explainFinding(findingId, runId) {
    return this.request('POST', `/findings/${findingId}/explain`, { run_id: runId });
  }

  proposePatch(findingId, runId) {
    return this.request('POST', `/findings/${findingId}/patch-proposal`, { run_id: runId });
  }

  validateProposal(proposalId, approvalToken, runTests = false, runFullSuite = false) {
    return this.request('POST', `/proposals/${proposalId}/validate`, {
      approval_token: approvalToken,
      run_tests: runTests,
      run_full_suite: runFullSuite,
    });
  }
}

class StatusTreeDataProvider {
  constructor(serviceManager = null, profileManager = null) {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.status = null;
    this.serviceManager = serviceManager;
    this.profileManager = profileManager;
  }

  refresh(status) {
    if (status !== undefined) {
      this.status = status;
    }
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element) {
    return element;
  }

  getChildren(element) {
    if (element) return [];

    const items = [];

    // Service & Discovery Status
    const configuredMode = this.serviceManager?.serviceMode ?? 'external';
    const mode = ['external', 'managed', 'disabled'].includes(configuredMode) ? configuredMode : 'invalid';
    const sUrl = this.serviceManager ? this.serviceManager.currentUrl : null;
    const ownership = this.serviceManager && this.serviceManager.owned ? `managed (PID: ${this.serviceManager.managedPid})` : 'external / unowned';
    const healthStatus = this.serviceManager ? this.serviceManager.lastHealthStatus : 'unknown';
    const activeProf = this.profileManager ? this.profileManager.activeProfileName : 'default';

    items.push(new vscode.TreeItem(`Service Mode: ${mode} (${ownership})`));
    items.push(new vscode.TreeItem(`Service URL: ${sUrl || 'none'}`));
    items.push(new vscode.TreeItem(`Service Health: ${healthStatus}`));
    items.push(new vscode.TreeItem(`Active Profile: ${activeProf}`));
    const h = this.serviceManager?.lastHealth;
    items.push(new vscode.TreeItem(`Service Version: ${h?.version ?? 'unknown'}`));
    items.push(new vscode.TreeItem(`Active Reviews: ${h?.active_reviews ?? 'unknown'}`));
    items.push(new vscode.TreeItem(`Service Provider: ${h?.provider ?? 'unknown'}`));
    items.push(new vscode.TreeItem(`Last Health Check: ${this.serviceManager?.lastHealthCheck ?? 'not checked'}`));
    if (this.serviceManager?.message) items.push(new vscode.TreeItem(this.serviceManager.message));
    if (this.profileManager?.error) items.push(new vscode.TreeItem(`Profile Error: ${this.profileManager.error}`));

    if (this.serviceManager && this.serviceManager.lastErrorMessage) {
      items.push(new vscode.TreeItem(`Service Error: ${this.serviceManager.lastErrorMessage}`));
    }

    // Review Run Status
    if (!this.status) {
      items.push(new vscode.TreeItem('--- No active review run ---'));
      return items;
    }

    items.push(new vscode.TreeItem('--- Active Review ---'));
    const itemStatus = new vscode.TreeItem(`Status: ${this.status.status}`);
    itemStatus.description = this.status.progress_text;
    items.push(itemStatus);

    if (this.status.run_id) {
      items.push(new vscode.TreeItem(`Run ID: ${this.status.run_id}`));
    }
    if (this.status.policy_decision) {
      items.push(new vscode.TreeItem(`Policy Decision: ${this.status.policy_decision}`));
    }
    if (this.status.finding_counts) {
      const fc = this.status.finding_counts;
      items.push(new vscode.TreeItem(`Findings: ${fc.total || 0} (Blocker: ${fc.blocker || 0}, High: ${fc.high || 0}, Med: ${fc.medium || 0}, Low: ${fc.low || 0})`));
    }
    items.push(new vscode.TreeItem(`Tests Status: ${this.status.test_status || 'not_run'}`));
    items.push(new vscode.TreeItem(`Patch Validation: ${this.status.patch_validation_status || 'not_run'}`));

    return items;
  }
}

class FindingsTreeDataProvider {
  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.findings = [];
    this.activeFindingId = null;
    this.filterSeverity = null;
    this.filterCategory = null;
  }

  refresh(findings, activeFindingId = null) {
    this.findings = findings || [];
    if (activeFindingId !== undefined) {
      this.activeFindingId = activeFindingId;
    }
    this._onDidChangeTreeData.fire();
  }

  setActiveFinding(id) {
    this.activeFindingId = id;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element) {
    return element;
  }

  getChildren(element) {
    if (element) return [];

    let filtered = this.findings;
    if (this.filterSeverity) {
      filtered = filtered.filter(f => f.severity.toLowerCase() === this.filterSeverity.toLowerCase());
    }
    if (this.filterCategory) {
      filtered = filtered.filter(f => f.category.toLowerCase() === this.filterCategory.toLowerCase());
    }

    if (!filtered.length) {
      return [new vscode.TreeItem(this.findings.length ? 'No findings matching filter' : 'No findings found')];
    }

    return filtered.map(f => {
      const badge = `[${f.severity.toUpperCase()}]`;
      const isActive = this.activeFindingId && f.id === this.activeFindingId;
      const activeMark = isActive ? ' ◀ ACTIVE' : '';
      const dismissedMark = f.dismissed ? ' (Dismissed)' : '';
      const label = `${badge} ${f.category}: ${f.file}:${f.line}${activeMark}${dismissedMark}`;
      const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
      item.description = f.claim;
      item.tooltip = `Claim: ${f.claim}\nConfidence: ${f.confidence}\nEvidence: ${f.evidence_strength}\nStatus: ${f.status}`;
      item.command = {
        command: 'codeatlas.openFindingLocation',
        title: 'Open Finding',
        arguments: [f],
      };
      item.contextValue = 'findingItem';
      item.finding = f;
      return item;
    });
  }
}

class ContextTreeDataProvider {
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
    } else {
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

function getFindingWebviewHtml(detail) {
  const safeStr = (s) => (s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : '');

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
  <p><span class="badge">[${safeStr((detail.severity || '').toUpperCase())}]</span> <code>${safeStr(detail.category)}</code> - ${safeStr(detail.file)}:${detail.line}</p>

  <table>
    <tr><th>Confidence</th><td>${detail.confidence}</td></tr>
    <tr><th>Evidence Strength</th><td>${safeStr(detail.evidence_strength)}</td></tr>
    <tr><th>Status</th><td>${safeStr(detail.status)}</td></tr>
    <tr><th>Impact</th><td>${safeStr(detail.impact)}</td></tr>
    <tr><th>Policy Decision</th><td>${safeStr(detail.policy_decision)}</td></tr>
    <tr><th>Test Status</th><td>${safeStr(detail.test_result)}</td></tr>
  </table>

  <div>
    <button onclick="vscode.postMessage({action: 'explain'})">Explain Finding</button>
    <button onclick="vscode.postMessage({action: 'draftFix'})">Generate Draft Fix</button>
    <button onclick="vscode.postMessage({action: 'copy'})">Copy Finding</button>
    <button onclick="vscode.postMessage({action: 'dismiss'})">${detail.dismissed ? 'Restore Finding' : 'Dismiss Locally'}</button>
  </div>

  <h3>Evidence</h3>
  <pre>${safeStr(JSON.stringify(detail.deterministic_evidence, null, 2))}</pre>

  <h3>Limitations</h3>
  <ul>
    ${(detail.limitations || []).map(l => `<li>${safeStr(l)}</li>`).join('')}
  </ul>

  <script>
    const vscode = acquireVsCodeApi();
  </script>
</body>
</html>`;
}

function getNoFindingWebviewHtml(cursorLine) {
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

let activeSession = null;

function activate(context) {
  let config = vscode.workspace.getConfiguration('codeatlas');
  const workspaceFolders = vscode.workspace.workspaceFolders;
  const workspaceRoot = workspaceFolders && workspaceFolders.length ? workspaceFolders[0].uri.fsPath : null;

  const profileManager = new ProfileManager(workspaceRoot);
  const serviceManager = new ServiceLifecycleManager({ workspaceRoot });
  const client = new CodeAtlasClient(null);

  const statusProvider = new StatusTreeDataProvider(serviceManager, profileManager);
  const findingsProvider = new FindingsTreeDataProvider();
  const contextProvider = new ContextTreeDataProvider();

  function loadProfileSettings() {
    profileManager.error = null;
    try {
      const inspected = config.inspect?.('profiles');
      profileManager.userProfiles = inspected ? (inspected.globalValue ?? {}) : (config.get('profiles') ?? {});
      profileManager.baseSettings = Object.fromEntries(Object.keys(DEFAULT_PROFILE)
        .map(key => [key, config.get(key)]).filter(([, value]) => value !== undefined));
      profileManager.profilePath = config.get('profilePath') ?? '.codeatlas/profiles.json';
      profileManager.activeProfileName = validateProfileName(config.get('activeProfile') ?? 'default');
      client.timeoutMs = profileManager.getActiveProfile().timeout * 1000;
    } catch (error) {
      profileManager.error = error.message;
      // Do not surface an invalid, potentially secret-bearing profile name.
      profileManager.activeProfileName = 'invalid';
      vscode.window.showErrorMessage(`CodeAtlas: ${error.message}`);
    }
  }

  function loadServiceSettings() {
    const inspected = config.inspect?.('serviceUrl');
    // The contributed localhost default is a hint, not an explicit override.
    serviceManager.explicitServiceUrl = inspected
      ? (inspected.workspaceFolderValue ?? inspected.workspaceValue ?? inspected.globalValue ?? null)
      : (config.get('serviceUrl') ?? null);
    serviceManager.serviceMode = config.get('serviceMode') ?? 'external';
    serviceManager.serviceHost = config.get('serviceHost') ?? '127.0.0.1';
    serviceManager.servicePort = config.get('servicePort') ?? 8765;
    serviceManager.serviceCommand = config.get('serviceCommand') ?? 'codeatlas serve';
    serviceManager.autoStartService = config.get('autoStartService') === true;
    serviceManager.trusted = vscode.workspace.isTrusted !== false;
    client.enabled = serviceManager.serviceMode !== 'disabled';
  }

  serviceManager.onChange = () => {
    if (client.serviceUrl && client.serviceUrl !== serviceManager.currentUrl) resetReviewState();
    client.serviceUrl = serviceManager.currentUrl;
    statusProvider.refresh();
  };
  serviceManager.onCrash = message => vscode.window.showErrorMessage(`CodeAtlas: ${message}`);
  loadProfileSettings();
  loadServiceSettings();

  vscode.window.registerTreeDataProvider('codeatlas.statusView', statusProvider);
  vscode.window.registerTreeDataProvider('codeatlas.findingsView', findingsProvider);
  vscode.window.registerTreeDataProvider('codeatlas.contextView', contextProvider);

  let currentRunId = null;
  let currentFindings = [];
  let activeFinding = null;
  let activeDetailPanel = null;
  const dismissedFindingIds = new Set();
  const reviewTimers = new Set();
  let healthTimer = null;
  let disposed = false;
  let checkingHealth = false;
  let configurationUpdate = Promise.resolve();
  let disposal = null;
  const session = {
    dispose() {
      if (disposal) return disposal;
      disposed = true;
      clearInterval(healthTimer);
      for (const timer of reviewTimers) clearInterval(timer);
      client.dispose();
      disposal = serviceManager.dispose();
      return disposal;
    },
  };
  activeSession = session;
  context.subscriptions.push({ dispose: () => { session.dispose().catch(() => {}); } });

  function resetReviewState() {
    currentRunId = null;
    currentFindings = [];
    activeFinding = null;
    for (const timer of reviewTimers) clearInterval(timer);
    reviewTimers.clear();
    dismissedFindingIds.clear();
    findingsProvider.refresh([]);
    contextProvider.refresh(null);
    statusProvider.refresh(null);
    if (activeDetailPanel) activeDetailPanel.webview.html = getNoFindingWebviewHtml();
    updateDecorations();
  }

  function startHealthTimer() {
    clearInterval(healthTimer);
    const seconds = config.get('healthCheckInterval') ?? 30;
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 86400) {
      serviceManager.lastErrorMessage = 'healthCheckInterval must be between 0 and 86400 seconds.';
      statusProvider.refresh();
      return;
    }
    if (serviceManager.serviceMode === 'disabled' || seconds === 0) return;
    healthTimer = setInterval(async () => {
      if (disposed || checkingHealth || serviceManager.lastHealthStatus === 'starting' || !serviceManager.currentUrl) return;
      checkingHealth = true;
      try { await serviceManager.checkHealth(); } catch (_) { /* Status view reports failure. */ }
      finally { checkingHealth = false; }
    }, Math.max(1, seconds) * 1000);
  }

  async function discoverService() {
    try {
      await serviceManager.discover();
      if (serviceManager.lastHealthStatus === 'not_configured') vscode.window.showInformationMessage(`CodeAtlas: ${SETUP_MESSAGE}`);
    } catch (error) { vscode.window.showWarningMessage(`CodeAtlas: ${error.message}`); }
  }

  function applyDismissalState() {
    for (const f of currentFindings) {
      f.dismissed = dismissedFindingIds.has(f.id);
    }
  }

  function updateDecorations() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;

    const docPath = editor.document.uri.fsPath;
    const buckets = { blocker: [], high: [], medium: [], low: [], info: [] };
    const activeLineDecs = [];

    for (const f of currentFindings) {
      if (f.dismissed) continue;
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

  async function syncActiveFinding(finding, cursorLine = null, multipleCount = 0) {
    activeFinding = finding;
    findingsProvider.setActiveFinding(finding ? finding.id : null);
    updateDecorations();

    if (finding && currentRunId) {
      try {
        const runId = currentRunId;
        const detail = await client.getFindingDetail(runId, finding.id);
        if (currentRunId !== runId || activeFinding?.id !== finding.id) return;
        detail.dismissed = dismissedFindingIds.has(finding.id);
        contextProvider.refresh(detail.context, finding, cursorLine, multipleCount);
        if (activeDetailPanel && activeDetailPanel.visible) {
          activeDetailPanel.webview.html = getFindingWebviewHtml(detail);
        }
      } catch (err) {
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

  function handleStaleRun() {
    vscode.window.showWarningMessage('CodeAtlas: Current review run expired or was not found on service. Please start a new review.');
    currentRunId = null;
    currentFindings = [];
    activeFinding = null;
    statusProvider.refresh(null);
    findingsProvider.refresh([]);
    contextProvider.refresh(null);
    updateDecorations();
  }

  async function handleCursorSelectionChange(editor) {
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

  vscode.window.onDidChangeActiveTextEditor(editor => {
    updateDecorations();
    if (editor) {
      handleCursorSelectionChange(editor);
    }
  }, null, context.subscriptions);

  vscode.window.onDidChangeTextEditorSelection(e => {
    if (e.textEditor === vscode.window.activeTextEditor) {
      handleCursorSelectionChange(e.textEditor);
    }
  }, null, context.subscriptions);

  vscode.workspace.onDidChangeTextDocument(e => {
    if (vscode.window.activeTextEditor && e.document === vscode.window.activeTextEditor.document) {
      updateDecorations();
    }
  }, null, context.subscriptions);

  async function navigateToFinding(finding) {
    if (!finding) return;

    const file = typeof finding.file === 'string' ? finding.file.replace(/\\/g, '/') : '';
    if (!file || file.startsWith('/') || file.includes(':') || file.includes('\0') || file.split('/').includes('..')) {
      vscode.window.showWarningMessage('CodeAtlas: Finding location must be a workspace-relative file.');
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
      const targetLine = clampLine(finding.start_line || finding.line || 1, doc.lineCount) - 1;
      const range = new vscode.Range(targetLine, 0, targetLine, 0);
      editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      editor.selection = new vscode.Selection(range.start, range.end);
    } catch (err) {
      vscode.window.showWarningMessage(`CodeAtlas: File not found or moved: ${finding.file}`);
    }

    await syncActiveFinding(finding);
  }

  async function explainFindingAction(finding) {
    if (!finding) return;
    try {
      const explanation = await client.explainFinding(finding.id, currentRunId);
      vscode.window.showInformationMessage(
        `CodeAtlas Explanation (${explanation.category}): ${explanation.explanation}\n\nRemediation: ${explanation.remediation_advice}`,
        { modal: true }
      );
    } catch (err) {
      vscode.window.showErrorMessage(`CodeAtlas: Explanation failed: ${err.message}`);
    }
  }

  async function generateDraftFixAction(finding) {
    if (!finding) return;
    try {
      const proposal = await client.proposePatch(finding.id, currentRunId);
      vscode.window.showInformationMessage(
        `CodeAtlas: Draft fix proposal created (${proposal.proposal_id}). Requires approval token to validate in sandbox.`,
        'Validate Approved Fix'
      ).then(selection => {
        if (selection === 'Validate Approved Fix') {
          vscode.commands.executeCommand('codeatlas.validateApprovedFix', proposal);
        }
      });
    } catch (err) {
      vscode.window.showErrorMessage(`CodeAtlas: Draft fix generation failed: ${err.message}`);
    }
  }

  async function toggleDismissFindingAction(finding) {
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
    updateDecorations();

    if (activeFinding && activeFinding.id === finding.id && activeDetailPanel && activeDetailPanel.visible) {
      try {
        const detail = await client.getFindingDetail(currentRunId, finding.id);
        detail.dismissed = finding.dismissed;
        activeDetailPanel.webview.html = getFindingWebviewHtml(detail);
      } catch (err) {}
    }
  }

  const ready = discoverService();
  startHealthTimer();
  if (vscode.workspace.onDidChangeConfiguration) {
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(event => {
      if (!event.affectsConfiguration('codeatlas')) return;
      configurationUpdate = configurationUpdate.then(async () => {
        if (disposed) return;
        config = vscode.workspace.getConfiguration('codeatlas');
        loadProfileSettings();
        const serviceChanged = ['serviceUrl', 'serviceMode', 'serviceHost', 'servicePort', 'serviceCommand', 'autoStartService']
          .some(key => event.affectsConfiguration(`codeatlas.${key}`));
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
      }).catch(() => vscode.window.showErrorMessage('CodeAtlas: Configuration update failed. Check service status and reload the extension.'));
    }));
  }

  // Command: Start Local Service
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.startLocalService', async () => {
      try {
        vscode.window.showInformationMessage('CodeAtlas: Starting managed local service...');
        const res = await serviceManager.startManagedService();
        statusProvider.refresh();
        vscode.window.showInformationMessage(`CodeAtlas: ${res.started ? 'Started' : 'Using existing'} service on ${res.url}.`);
      } catch (err) {
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
        vscode.window.showInformationMessage(res.stopped
          ? 'CodeAtlas: Managed service stopped.' : 'CodeAtlas: No owned service process is running.');
      } catch (error) { vscode.window.showErrorMessage(`CodeAtlas: ${error.message}`); }
    })
  );

  // Command: Restart Local Service
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.restartLocalService', async () => {
      try {
        vscode.window.showInformationMessage('CodeAtlas: Restarting managed service...');
        const res = await serviceManager.restartManagedService();
        statusProvider.refresh();
        vscode.window.showInformationMessage(`CodeAtlas: ${res.started ? 'Restarted managed' : 'Using existing external'} service on ${res.url}.`);
      } catch (err) {
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
        const items = Object.keys(profiles).map(name => ({
          label: name === profileManager.activeProfileName ? `✓ ${name}` : name,
          description: `Provider: ${profiles[name].provider || 'mock'}, Timeout: ${profiles[name].timeout || 30}s, Max Findings: ${profiles[name].maxFindings || 50}`,
          profileName: name,
        }));

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select active CodeAtlas configuration profile',
        });

        if (selected && selected.profileName) {
          const target = workspaceRoot ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
          await config.update('activeProfile', selected.profileName, target);
          const profile = profileManager.select(selected.profileName, profiles);
          profileManager.error = null;
          client.timeoutMs = profile.timeout * 1000;
          statusProvider.refresh();
          vscode.window.showInformationMessage(`CodeAtlas: Active profile set to '${selected.profileName}'.`);
        }
      } catch (err) {
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
        const h = await serviceManager.checkHealth();
        statusProvider.refresh();
        vscode.window.showInformationMessage(
          h.status === 'ok'
            ? `CodeAtlas Service Healthy: Version ${h.version}, Active Reviews: ${h.active_reviews ?? 'unknown'}, Provider: ${h.provider}`
            : 'CodeAtlas service is disabled or the health check was cancelled.'
        );
      } catch (err) {
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
          throw new Error('Live reviewer requires enableLiveReviewer=true in the active configuration.');
        }
        if (serviceManager.serviceMode === 'disabled') throw new Error('CodeAtlas service is disabled.');
        const health = await serviceManager.checkHealth();
        if (health.status !== 'ok') throw new Error('CodeAtlas service is not ready.');
        client.timeoutMs = activeProf.timeout * 1000;
        vscode.window.showInformationMessage('CodeAtlas: Starting review...');
        const resp = await client.startReview(repoPath, {
          base: 'main',
          head: 'HEAD',
          review_provider: activeProf.provider,
          provider_model: activeProf.model || undefined,
          provider_timeout: activeProf.timeout,
          allow_patch_suggestions: activeProf.enablePatchSuggestions,
          max_findings: activeProf.maxFindings,
        });

        currentRunId = resp.run_id;
        statusProvider.refresh(resp);

        const pollInterval = setInterval(async () => {
          if (!currentRunId) {
            clearInterval(pollInterval);
            return;
          }
          try {
            const status = await client.getReview(currentRunId);
            statusProvider.refresh(status);

            if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled') {
              clearInterval(pollInterval);
              const findingsResp = await client.getFindings(currentRunId, { include_dismissed: true });
              currentFindings = findingsResp.findings || [];
              applyDismissalState();
              findingsProvider.refresh(currentFindings);
              updateDecorations();

              if (status.status === 'completed') {
                vscode.window.showInformationMessage(`CodeAtlas: Review complete with ${currentFindings.length} findings.`);
              } else if (status.status === 'failed') {
                vscode.window.showWarningMessage(`CodeAtlas: Review failed: ${(status.errors || []).join('; ')}`);
              }
            }
          } catch (pollErr) {
            clearInterval(pollInterval);
            if (pollErr.message && pollErr.message.includes('404')) {
              handleStaleRun();
            } else {
              vscode.window.showErrorMessage(`CodeAtlas: Review polling error: ${pollErr.message}`);
            }
          }
        }, 1000);
        reviewTimers.add(pollInterval);

      } catch (err) {
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
      try {
        await client.cancelReview(currentRunId);
        const status = await client.getReview(currentRunId);
        statusProvider.refresh(status);
        vscode.window.showInformationMessage('CodeAtlas: Review cancelled.');
      } catch (err) {
        vscode.window.showErrorMessage(`CodeAtlas: Cancel failed: ${err.message}`);
      }
    })
  );

  // Command: Refresh
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.refresh', async () => {
      if (!currentRunId) {
        // Refresh service health and status view
        try {
          await serviceManager.checkHealth();
        } catch (err) {}
        statusProvider.refresh();
        vscode.window.showInformationMessage('CodeAtlas: Status refreshed.');
        return;
      }
      try {
        const status = await client.getReview(currentRunId);
        statusProvider.refresh(status);
        const findingsResp = await client.getFindings(currentRunId, { include_dismissed: true });
        currentFindings = findingsResp.findings || [];
        applyDismissalState();
        findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
        updateDecorations();
      } catch (err) {
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
    vscode.commands.registerCommand('codeatlas.openFindingLocation', async (finding) => {
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
      const items = currentFindings.map(formatQuickPickItem);
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
          const atCursor = findFindingsAtCursor(currentFindings, editor.document.uri.fsPath, cursorLine);
          if (atCursor.length > 0) {
            target = atCursor[0];
          }
        }
      }
      if (!target) {
        vscode.window.showInformationMessage('CodeAtlas: No finding at current cursor position.');
        return;
      }
      await explainFindingAction(target);
    })
  );

  // QuickPick Command 3: CodeAtlas: Show Context
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.showContext', async (item) => {
      let finding = item ? (item.finding || item) : activeFinding;
      if (!finding) {
        if (!currentRunId) {
          vscode.window.showInformationMessage('CodeAtlas: No active review run.');
          return;
        }
        if (!currentFindings.length) {
          vscode.window.showInformationMessage('CodeAtlas: No findings available.');
          return;
        }
        const items = currentFindings.map(formatQuickPickItem);
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
    vscode.commands.registerCommand('codeatlas.generateDraftFix', async (item) => {
      let finding = item ? (item.finding || item) : activeFinding;
      if (!finding) {
        if (!currentRunId) {
          vscode.window.showInformationMessage('CodeAtlas: No active review run.');
          return;
        }
        if (!currentFindings.length) {
          vscode.window.showInformationMessage('CodeAtlas: No findings available.');
          return;
        }
        const items = currentFindings.map(formatQuickPickItem);
        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select a finding to generate draft fix proposal',
        });
        if (!selected) return;
        finding = selected.finding;
      }
      await generateDraftFixAction(finding);
    })
  );

  // QuickPick Command 5: CodeAtlas: Validate Approved Fix
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.validateApprovedFix', async (proposalItem) => {
      let proposalId = proposalItem ? (proposalItem.proposal_id || proposalItem.id) : null;
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
        vscode.window.showWarningMessage('CodeAtlas: Approval token is required to validate patch in isolated sandbox.');
        return;
      }

      try {
        if (profileManager.error) throw new Error(profileManager.error);
        const profile = profileManager.getActiveProfile();
        vscode.window.showInformationMessage(`CodeAtlas: Validating proposal ${proposalId} in detached sandbox...`);
        const result = await client.validateProposal(proposalId, approvalToken, profile.enableTestExecution, profile.enableFullSuiteExecution);
        if (result.valid && result.approval_verified) {
          vscode.window.showInformationMessage(`CodeAtlas: Patch validated cleanly in isolated sandbox! Status: ${result.status}`);
        } else {
          vscode.window.showWarningMessage(`CodeAtlas: Patch validation rejected: ${(result.errors || []).join('; ')}`);
        }
      } catch (err) {
        vscode.window.showErrorMessage(`CodeAtlas: Validation error: ${err.message}`);
      }
    })
  );

  // QuickPick Command 6: CodeAtlas: Dismiss Finding
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.dismissFinding', async (item) => {
      let finding = item ? (item.finding || item) : activeFinding;
      if (!finding) {
        if (!currentRunId) {
          vscode.window.showInformationMessage('CodeAtlas: No active review run.');
          return;
        }
        if (!currentFindings.length) {
          vscode.window.showInformationMessage('CodeAtlas: No findings available.');
          return;
        }
        const items = currentFindings.map(formatQuickPickItem);
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
    vscode.commands.registerCommand('codeatlas.showFindingDetails', async (item) => {
      const finding = item ? (item.finding || item) : activeFinding;
      if (!finding || !currentRunId) {
        vscode.window.showInformationMessage('CodeAtlas: Select a finding first.');
        return;
      }

      try {
        const detail = await client.getFindingDetail(currentRunId, finding.id);
        detail.dismissed = dismissedFindingIds.has(finding.id);

        if (!activeDetailPanel) {
          activeDetailPanel = vscode.window.createWebviewPanel(
            'codeatlasFindingDetail',
            `CodeAtlas Finding: ${detail.id}`,
            vscode.ViewColumn.Beside,
            { enableScripts: true }
          );

          activeDetailPanel.onDidDispose(() => {
            activeDetailPanel = null;
          }, null, context.subscriptions);

          activeDetailPanel.webview.onDidReceiveMessage(async (msg) => {
            if (msg.action === 'explain') {
              vscode.commands.executeCommand('codeatlas.explainFinding', detail);
            } else if (msg.action === 'draftFix') {
              vscode.commands.executeCommand('codeatlas.generateDraftFix', detail);
            } else if (msg.action === 'copy') {
              await vscode.env.clipboard.writeText(JSON.stringify(detail, null, 2));
              vscode.window.showInformationMessage('CodeAtlas: Finding copied to clipboard.');
            } else if (msg.action === 'dismiss') {
              vscode.commands.executeCommand('codeatlas.dismissFinding', detail);
            }
          });
        }

        activeDetailPanel.title = `CodeAtlas Finding: ${detail.id}`;
        activeDetailPanel.webview.html = getFindingWebviewHtml(detail);
      } catch (err) {
        if (err.message && err.message.includes('404')) {
          handleStaleRun();
        } else {
          vscode.window.showErrorMessage(`CodeAtlas: Cannot fetch finding details: ${err.message}`);
        }
      }
    })
  );

  // Command: Explain Finding (direct)
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.explainFinding', async (item) => {
      const finding = item ? (item.finding || item) : activeFinding;
      await explainFindingAction(finding);
    })
  );

  // Command: Copy Finding
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.copyFinding', async (item) => {
      const finding = item ? (item.finding || item) : activeFinding;
      if (!finding) return;
      await vscode.env.clipboard.writeText(JSON.stringify(finding, null, 2));
      vscode.window.showInformationMessage('CodeAtlas: Finding copied to clipboard.');
    })
  );

  // Command: Filter by Severity
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.filterSeverity', async () => {
      const pick = await vscode.window.showQuickPick(['All', 'Blocker', 'High', 'Medium', 'Low', 'Info'], {
        placeHolder: 'Filter findings by severity',
      });
      if (pick) {
        findingsProvider.filterSeverity = pick === 'All' ? null : pick;
        findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
      }
    })
  );

  // Command: Filter by Category
  context.subscriptions.push(
    vscode.commands.registerCommand('codeatlas.filterCategory', async () => {
      const categories = ['All', ...new Set(currentFindings.map(f => f.category))];
      const pick = await vscode.window.showQuickPick(categories, {
        placeHolder: 'Filter findings by category',
      });
      if (pick) {
        findingsProvider.filterCategory = pick === 'All' ? null : pick;
        findingsProvider.refresh(currentFindings, activeFinding ? activeFinding.id : null);
      }
    })
  );
  return { ready, serviceManager, profileManager, statusProvider, client, dispose: () => session.dispose() };
}

async function deactivate() {
  const session = activeSession;
  activeSession = null;
  if (session) await session.dispose();
  for (const type of Object.values(decorationTypes)) {
    if (type && typeof type.dispose === 'function') {
      type.dispose();
    }
  }
}

module.exports = {
  activate,
  deactivate,
  CodeAtlasClient,
  StatusTreeDataProvider,
  FindingsTreeDataProvider,
  ContextTreeDataProvider,
  ProfileManager,
  ServiceLifecycleManager,
  validateProfile,
  findFindingsAtCursor,
  formatQuickPickItem,
  clampLine,
  sortFindingsBySeverity,
  normalizePath,
  pathMatches,
  getFindingWebviewHtml,
  getNoFindingWebviewHtml,
  SEVERITY_ORDER,
  ALLOWED_PROFILE_KEYS,
  FORBIDDEN_PROFILE_KEYS,
};
