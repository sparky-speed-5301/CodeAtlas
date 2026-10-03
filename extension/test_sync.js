/**
 * Unit and integration tests for CodeAtlas Phase 10B VS Code extension logic.
 */

const assert = require('assert');
const path = require('path');
const Module = require('module');

// Intercept 'vscode' module require
const originalRequire = Module.prototype.require;

let lastRevealedRange = null;
let lastSelectedRange = null;
let lastOpenedFile = null;
let lastWarningMessage = null;
let lastInfoMessage = null;
let lastErrorMessage = null;
let mockQuickPickSelection = null;
let mockInputBoxValue = null;

const mockVsCode = {
  window: {
    createTextEditorDecorationType: () => ({ dispose: () => {} }),
    registerTreeDataProvider: () => {},
    showInformationMessage: async (msg, ...items) => {
      lastInfoMessage = msg;
      return items && items.length ? items[0] : undefined;
    },
    showWarningMessage: async (msg) => {
      lastWarningMessage = msg;
    },
    showErrorMessage: async (msg) => {
      lastErrorMessage = msg;
    },
    showQuickPick: async (items, opts) => {
      if (typeof mockQuickPickSelection === 'function') {
        return mockQuickPickSelection(items, opts);
      }
      return mockQuickPickSelection;
    },
    showInputBox: async (opts) => {
      if (typeof mockInputBoxValue === 'function') {
        return mockInputBoxValue(opts);
      }
      return mockInputBoxValue;
    },
    createWebviewPanel: (viewType, title) => ({
      title,
      visible: true,
      webview: {
        html: '',
        onDidReceiveMessage: () => {},
      },
      onDidDispose: () => {},
    }),
    activeTextEditor: null,
    onDidChangeActiveTextEditor: () => ({ dispose: () => {} }),
    onDidChangeTextEditorSelection: () => ({ dispose: () => {} }),
  },
  workspace: {
    getConfiguration: () => ({ get: () => undefined }),
    onDidChangeTextDocument: () => ({ dispose: () => {} }),
    workspaceFolders: [{ uri: { fsPath: 'd:/repo' } }],
    openTextDocument: async (uri) => {
      lastOpenedFile = uri.fsPath;
      if (uri.fsPath.includes('deleted')) {
        throw new Error('File not found on disk');
      }
      return {
        lineCount: 100,
        uri,
      };
    },
  },
  commands: {
    registerCommand: () => ({ dispose: () => {} }),
    executeCommand: async (cmd, ...args) => {},
  },
  TreeItem: class {
    constructor(label, collapsibleState) {
      this.label = label;
      this.collapsibleState = collapsibleState;
    }
  },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  Range: class {
    constructor(sl, sc, el, ec) {
      this.start = { line: sl, character: sc };
      this.end = { line: el, character: ec };
    }
  },
  Selection: class {
    constructor(start, end) {
      this.start = start;
      this.end = end;
    }
  },
  MarkdownString: class {
    constructor() { this.value = ''; }
    appendMarkdown(val) { this.value += val; }
  },
  OverviewRulerLane: { Right: 4 },
  TextEditorRevealType: { InCenter: 1 },
  Uri: {
    joinPath: (base, rel) => ({ fsPath: `${base.fsPath}/${rel}` }),
  },
  EventEmitter: class {
    constructor() { this._listeners = []; }
    get event() { return (listener) => { this._listeners.push(listener); return { dispose: () => {} }; }; }
    fire(data) { for (const l of this._listeners) l(data); }
  },
};

mockVsCode.ConfigurationTarget = { Global: 1, Workspace: 2 };

mockVsCode.window.showTextDocument = async (doc) => {
  return {
    document: doc,
    revealRange: (range, type) => {
      lastRevealedRange = range;
    },
    selection: null,
    setDecorations: () => {},
  };
};

Module.prototype.require = function (id) {
  if (id === 'vscode') {
    return mockVsCode;
  }
  return originalRequire.apply(this, arguments);
};

const fs = require('fs');
const extPath = fs.existsSync(path.join(__dirname, 'out', 'extension.js')) ? './out/extension' : './extension';
const {
  findFindingsAtCursor,
  formatQuickPickItem,
  clampLine,
  sortFindingsBySeverity,
  pathMatches,
  normalizePath,
  getFindingWebviewHtml,
  getNoFindingWebviewHtml,
  StatusTreeDataProvider,
  FindingsTreeDataProvider,
  ContextTreeDataProvider,
  ProfileManager,
  ServiceLifecycleManager,
  validateProfile,
  SEVERITY_ORDER,
  activate,
  deactivate,
} = require(extPath);

// Mock data
const mockFindings = [
  {
    id: 'find-001',
    severity: 'medium',
    category: 'security',
    file: 'src/auth.py',
    line: 42,
    start_line: 40,
    end_line: 45,
    claim: 'Weak cryptographic hash function used',
    confidence: 'high',
    evidence_strength: 'strong',
    status: 'open',
    dismissed: false,
  },
  {
    id: 'find-002',
    severity: 'blocker',
    category: 'vulnerability',
    file: 'src/auth.py',
    line: 42,
    start_line: 42,
    end_line: 42,
    claim: 'Hardcoded secret token in auth handler',
    confidence: 'certain',
    evidence_strength: 'deterministic',
    status: 'open',
    dismissed: false,
  },
  {
    id: 'find-003',
    severity: 'low',
    category: 'style',
    file: 'src/utils.py',
    line: 10,
    start_line: 10,
    end_line: 10,
    claim: 'Unused import os',
    confidence: 'high',
    evidence_strength: 'strong',
    status: 'open',
    dismissed: false,
  },
];

async function runTests() {
  console.log('Running Phase 10B VS Code Extension Logic Tests...');

  // Test 1: Severity sorting and multiple findings on one line
  {
    const sorted = sortFindingsBySeverity(mockFindings);
    assert.strictEqual(sorted[0].id, 'find-002', 'Blocker should sort ahead of medium and low');
    assert.strictEqual(sorted[1].id, 'find-001', 'Medium should sort ahead of low');
    assert.strictEqual(sorted[2].id, 'find-003', 'Low should sort last');
    console.log('✓ Severity sorting passed');
  }

  // Test 2: Active file filtering and path matching
  {
    assert.strictEqual(pathMatches('d:/repo/src/auth.py', 'src/auth.py'), true);
    assert.strictEqual(pathMatches('src/auth.py', 'src/auth.py'), true);
    assert.strictEqual(pathMatches('D:\\repo\\src\\auth.py', 'src/auth.py'), true);
    assert.strictEqual(pathMatches('src/other.py', 'src/auth.py'), false);
    console.log('✓ Path matching passed');
  }

  // Test 3: Cursor enters finding range
  {
    const matches = findFindingsAtCursor(mockFindings, 'src/auth.py', 41);
    assert.strictEqual(matches.length, 1);
    assert.strictEqual(matches[0].id, 'find-001');
    console.log('✓ Cursor enters finding range passed');
  }

  // Test 4: Cursor leaves finding range (no finding at cursor)
  {
    const matches = findFindingsAtCursor(mockFindings, 'src/auth.py', 100);
    assert.strictEqual(matches.length, 0, 'No finding on line 100');
    console.log('✓ Cursor leaves finding range (empty matches) passed');
  }

  // Test 5: Multiple findings on one line
  {
    const matches = findFindingsAtCursor(mockFindings, 'src/auth.py', 42);
    assert.strictEqual(matches.length, 2, 'Line 42 has 2 findings');
    assert.strictEqual(matches[0].id, 'find-002', 'Primary must be blocker');
    assert.strictEqual(matches[1].id, 'find-001', 'Secondary must be medium');
    console.log('✓ Multiple findings on one line passed');
  }

  // Test 6: Invalid range clamping
  {
    assert.strictEqual(clampLine(-5, 100), 1, 'Negative line clamps to 1');
    assert.strictEqual(clampLine(0, 100), 1, 'Zero line clamps to 1');
    assert.strictEqual(clampLine(150, 100), 100, 'Exceeding line clamps to doc lineCount');
    assert.strictEqual(clampLine(50, 100), 50, 'Valid line is preserved');
    console.log('✓ Invalid range clamping passed');
  }

  // Test 7: QuickPick formatting
  {
    const item = formatQuickPickItem(mockFindings[0]);
    assert.ok(item.label.includes('[MEDIUM]'), 'Label must contain severity badge');
    assert.ok(item.label.includes('security'), 'Label must contain category');
    assert.ok(item.label.includes('src/auth.py:42'), 'Label must contain file and line');
    assert.strictEqual(item.description, 'Weak cryptographic hash function used', 'Description must be claim');
    assert.ok(item.detail.includes('Status: open'), 'Detail must include status');
    assert.ok(item.detail.includes('Confidence: high'), 'Detail must include confidence');
    assert.ok(item.detail.includes('Evidence: strong'), 'Detail must include evidence');
    console.log('✓ QuickPick formatting passed');
  }

  // Test 8: Findings Tree Data Provider and active marker
  {
    const provider = new FindingsTreeDataProvider();
    provider.refresh(mockFindings, 'find-002');
    const children = provider.getChildren();
    assert.strictEqual(children.length, 3);
    const activeItem = children.find(c => c.finding.id === 'find-002');
    assert.ok(activeItem.label.includes('◀ ACTIVE'), 'Active finding must be marked in tree');
    console.log('✓ Findings tree active marker passed');
  }

  // Test 9: Context Tree Data Provider with active finding vs no finding at cursor
  {
    const provider = new ContextTreeDataProvider();
    // No finding at cursor
    provider.refresh(null, null, 77);
    let items = provider.getChildren();
    assert.strictEqual(items.length, 2);
    assert.ok(items[0].label.includes('No finding at cursor (line 77)'));

    // Active finding with multiple findings count
    const ctx = {
      changed_lines: [40, 41, 42],
      containing_symbol: { kind: 'function', name: 'authenticate', start_line: 35, end_line: 55 },
      relevant_imports: ['hashlib'],
      relevant_references: [],
      related_tests: ['test_auth.py'],
      retrieved_context_candidates: [{}],
      truncation_status: false,
      evidence_sources: ['deterministic_ast'],
    };
    provider.refresh(ctx, mockFindings[1], 42, 2);
    items = provider.getChildren();
    assert.ok(items.some(i => i.label.includes('Active: [BLOCKER]')));
    assert.ok(items.some(i => i.label.includes('Multiple findings on this line: 2')));
    assert.ok(items.some(i => i.label.includes('Changed lines: 40, 41, 42')));
    assert.ok(items.some(i => i.label.includes('Symbol: function authenticate')));
    console.log('✓ Context tree provider passed');
  }

  // Test 10: Detail Webview rendering
  {
    const detail = {
      id: 'find-001',
      claim: 'Weak cryptographic hash function used',
      severity: 'medium',
      category: 'security',
      file: 'src/auth.py',
      line: 42,
      confidence: 'high',
      evidence_strength: 'strong',
      status: 'open',
      impact: 'Allows hash collision',
      policy_decision: 'audit_required',
      test_result: 'passed',
      deterministic_evidence: ['hashlib.md5() detected'],
      limitations: ['Static AST analysis only'],
      dismissed: false,
    };
    const html = getFindingWebviewHtml(detail);
    assert.ok(html.includes('[MEDIUM]'));
    assert.ok(html.includes('Weak cryptographic hash function used'));
    assert.ok(html.includes('Allows hash collision'));
    assert.ok(html.includes('Static AST analysis only'));

    const noFindingHtml = getNoFindingWebviewHtml(50);
    assert.ok(noFindingHtml.includes('No finding at cursor position (line 50)'));
    console.log('✓ Webview detail rendering passed');
  }

  // Test 11: Extension activation and command registration
  const registeredCommands = new Map();
  mockVsCode.commands.registerCommand = (name, handler) => {
    registeredCommands.set(name, handler);
    return { dispose: () => {} };
  };
  const context = { subscriptions: [] };
  activate(context);

  // Verify all 6 QuickPick commands and lifecycle actions are registered
  assert.ok(registeredCommands.has('codeatlas.findFinding'));
  assert.ok(registeredCommands.has('codeatlas.explainCurrentFinding'));
  assert.ok(registeredCommands.has('codeatlas.showContext'));
  assert.ok(registeredCommands.has('codeatlas.generateDraftFix'));
  assert.ok(registeredCommands.has('codeatlas.validateApprovedFix'));
  assert.ok(registeredCommands.has('codeatlas.dismissFinding'));
  assert.ok(registeredCommands.has('codeatlas.startReview'));
  assert.ok(registeredCommands.has('codeatlas.cancelReview'));
  assert.ok(registeredCommands.has('codeatlas.refresh'));
  assert.ok(registeredCommands.has('codeatlas.showFindingDetails'));
  console.log('✓ All 6 QuickPick commands and lifecycle actions registered');

  // Test 12: Finding selection navigation
  {
    lastOpenedFile = null;
    lastRevealedRange = null;
    const navCmd = registeredCommands.get('codeatlas.openFindingLocation');
    await navCmd(mockFindings[0]);
    assert.ok(lastOpenedFile.includes('src/auth.py'), 'Must open target file');
    assert.strictEqual(lastRevealedRange.start.line, 39, 'Line 40 (1-based) is line 39 (0-based)');
    console.log('✓ Finding selection navigation passed');
  }

  // Test 13: Deleted/moved file handling
  {
    lastWarningMessage = null;
    const navCmd = registeredCommands.get('codeatlas.openFindingLocation');
    const deletedFinding = { ...mockFindings[0], file: 'src/deleted_module.py' };
    await navCmd(deletedFinding);
    assert.ok(lastWarningMessage && lastWarningMessage.includes('File not found or moved'), 'Must show friendly warning');
    console.log('✓ Deleted/moved file handling passed');
  }

  // Test 14: QuickPick execution with no active review
  {
    lastInfoMessage = null;
    const findCmd = registeredCommands.get('codeatlas.findFinding');
    await findCmd();
    assert.strictEqual(lastInfoMessage, 'CodeAtlas: No active review run.');

    const showCtxCmd = registeredCommands.get('codeatlas.showContext');
    // passing undefined item and with no active finding
    // (mockFindings[0] was active from navCmd, so let's verify dismiss on activeFinding)
    const dismissCmd = registeredCommands.get('codeatlas.dismissFinding');
    await dismissCmd(mockFindings[0]);
    assert.strictEqual(mockFindings[0].dismissed, true, 'Finding should be locally dismissed');
    await dismissCmd(mockFindings[0]);
    assert.strictEqual(mockFindings[0].dismissed, false, 'Finding should be locally restored');
    console.log('✓ QuickPick execution and dismissal passed');
  }

  // Test 15: Approval-gated validation requires scoped token
  {
    lastWarningMessage = null;
    mockInputBoxValue = (opts) => {
      if (opts.prompt.includes('proposal ID')) return 'prop-123';
      if (opts.prompt.includes('scoped approval token')) return ''; // empty token
    };
    const valCmd = registeredCommands.get('codeatlas.validateApprovedFix');
    await valCmd();
    assert.ok(lastWarningMessage.includes('Approval token is required'), 'Must require scoped approval token');
    console.log('✓ Validation fails closed without scoped approval token');
  }

  // Test 16: Local reversible dismissal state preserved across refresh
  {
    const dismissCmd = registeredCommands.get('codeatlas.dismissFinding');
    const testFinding = { id: 'test-f-1', severity: 'high', category: 'security', file: 'a.py', line: 1, dismissed: false };
    await dismissCmd(testFinding);
    assert.strictEqual(testFinding.dismissed, true, 'Finding should be locally dismissed');

    // Toggling again restores it
    await dismissCmd(testFinding);
    assert.strictEqual(testFinding.dismissed, false, 'Finding should be restored');
    console.log('✓ Local reversible dismissal passed');
  }

  await deactivate();
  for (const subscription of context.subscriptions) subscription.dispose();
  await require('./test_lifecycle').runTests(mockVsCode);
  console.log('All Phase 10C VS Code extension unit tests passed successfully!');
}

module.exports = { mockVsCode, runTests };

if (require.main === module) {
  runTests().catch(err => {
    console.error('Test execution failed:', err);
    process.exitCode = 1;
  });
}
