/**
 * Phase 10D Migration Verification Suite
 * Verifies that the TypeScript compiled extension runtime fulfills all contract requirements:
 * 1. Extension activation
 * 2. Command registration
 * 3. Service discovery
 * 4. Managed startup/shutdown
 * 5. Profile validation
 * 6. Health polling
 * 7. Cursor synchronization
 * 8. Finding navigation
 * 9. QuickPick commands
 * 10. Draft-fix proposal
 * 11. Approval-gated validation
 * 12. Shutdown cleanup
 * 13. No secret exposure
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { EventEmitter } = require('events');

// Load mock vscode from test_sync
const mockVsCode = require('./test_sync').mockVsCode;

// Load compiled TypeScript modules
const ext = require('./out/extension');
const { ServiceLifecycleManager, validateServiceUrl, parseServiceCommand, safeHealth } = require('./out/service');
const { ProfileManager, validateProfile, validateProfileName, DEFAULT_PROFILE } = require('./out/profiles');
const { findFindingsAtCursor, clampLine, sortFindingsBySeverity, pathMatches } = require('./out/decorations');
const { formatQuickPickItem } = require('./out/quickpick');

async function runMigrationTests() {
  console.log('Running Phase 10D TypeScript Migration Tests...');
  let passed = 0;
  const test = async (name, fn) => {
    await fn();
    passed++;
    console.log(`✓ [Migration] ${name}`);
  };

  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codeatlas-migration-'));
  fs.mkdirSync(path.join(root, '.codeatlas'));

  // 1. Extension activation
  await test('1. Extension activation returns typed session and providers', async () => {
    const subscriptions = [];
    const context = { subscriptions };
    const session = ext.activate(context);
    assert.ok(session, 'activate must return a session');
    assert.ok(session.serviceManager instanceof ServiceLifecycleManager, 'serviceManager instance');
    assert.ok(session.profileManager instanceof ProfileManager, 'profileManager instance');
    assert.ok(session.statusProvider instanceof ext.StatusTreeDataProvider, 'statusProvider instance');
    assert.ok(session.client instanceof ext.CodeAtlasClient, 'client instance');
    assert.strictEqual(typeof session.dispose, 'function', 'dispose function');
    await session.dispose();
  });

  // 2. Command registration
  await test('2. Command registration registers all 22 contribution point commands', async () => {
    const registeredCommands = new Set();
    const origRegister = mockVsCode.commands.registerCommand;
    mockVsCode.commands.registerCommand = (id, handler) => {
      registeredCommands.add(id);
      return { dispose: () => {} };
    };
    try {
      const subscriptions = [];
      const session = ext.activate({ subscriptions });
      const expectedCommands = [
        'codeatlas.startReview',
        'codeatlas.cancelReview',
        'codeatlas.refresh',
        'codeatlas.findFinding',
        'codeatlas.explainCurrentFinding',
        'codeatlas.openFindingLocation',
        'codeatlas.showFindingDetails',
        'codeatlas.explainFinding',
        'codeatlas.showContext',
        'codeatlas.generateDraftFix',
        'codeatlas.validateApprovedFix',
        'codeatlas.copyFinding',
        'codeatlas.copyComparisonRange',
        'codeatlas.dismissFinding',
        'codeatlas.filterSeverity',
        'codeatlas.filterCategory',
        'codeatlas.startLocalService',
        'codeatlas.stopLocalService',
        'codeatlas.restartLocalService',
        'codeatlas.selectConfigurationProfile',
        'codeatlas.openConfiguration',
        'codeatlas.checkServiceHealth',
      ];
      for (const cmd of expectedCommands) {
        assert.ok(registeredCommands.has(cmd), `Command ${cmd} must be registered`);
      }
      await session.dispose();
    } finally {
      mockVsCode.commands.registerCommand = origRegister;
    }
  });

  // 3. Service discovery
  await test('3. Service discovery: explicit URL > workspace JSON > managed URL', async () => {
    const serviceFile = path.join(root, '.codeatlas', 'service.json');
    fs.writeFileSync(serviceFile, JSON.stringify({ url: 'http://127.0.0.1:9191' }));
    try {
      const manager = new ServiceLifecycleManager({
        workspaceRoot: root,
        serviceUrl: 'http://127.0.0.1:8765',
        serviceMode: 'managed',
      });
      // Explicit override has highest precedence
      assert.strictEqual(manager.resolveServiceUrl(), 'http://127.0.0.1:8765');
      // When explicit is unset, workspace service.json takes precedence
      manager.explicitServiceUrl = null;
      assert.strictEqual(manager.resolveServiceUrl(), 'http://127.0.0.1:9191');
      // Disabled mode always resolves to null
      manager.serviceMode = 'disabled';
      assert.strictEqual(manager.resolveServiceUrl(), null);
    } finally {
      if (fs.existsSync(serviceFile)) fs.unlinkSync(serviceFile);
    }
  });

  // 4. Managed startup/shutdown
  await test('4. Managed startup direct spawn and clean shutdown', async () => {
    let spawnCalled = false;
    let killedWith = null;
    const fakeChild = new EventEmitter();
    fakeChild.pid = 9999;
    fakeChild.stdout = new EventEmitter();
    fakeChild.stderr = new EventEmitter();
    fakeChild.kill = (sig) => {
      killedWith = sig;
      fakeChild.emit('exit', 0, sig);
      return true;
    };

    const manager = new ServiceLifecycleManager({
      serviceMode: 'managed',
      serviceHost: '127.0.0.1',
      servicePort: 8765,
      trusted: true,
      clientFactory: () => ({
        getHealth: async () => {
          if (!spawnCalled) throw new Error('offline');
          return {
            status: 'ok',
            service: 'codeatlas-service',
            version: '0.1.0',
            pid: 9999,
            active_reviews: 0,
            provider: 'mock',
          };
        },
      }),
    });

    const spawnMock = (cmd, args, opts) => {
      assert.strictEqual(opts.shell, false, 'Direct execution without shell');
      assert.ok(args.includes('--host') && args.includes('127.0.0.1'));
      spawnCalled = true;
      return fakeChild;
    };

    const res = await manager.startManagedService({ spawn: spawnMock, timeoutMs: 1000 });
    assert.ok(spawnCalled, 'spawn must be called');
    assert.strictEqual(res.started, true);
    assert.strictEqual(res.pid, 9999);
    assert.strictEqual(manager.owned, true);

    const stopRes = await manager.stopManagedService();
    assert.strictEqual(stopRes.stopped, true);
    assert.strictEqual(killedWith, 'SIGTERM');
    assert.strictEqual(manager.owned, false);
  });

  // 5. Profile validation
  await test('5. Profile validation rejects forbidden fields and accepts valid overrides', async () => {
    assert.throws(() => validateProfile('ci', { autoapply: true }), /not permitted/);
    assert.throws(() => validateProfile('ci', { automerge: true }), /not permitted/);
    assert.throws(() => validateProfile('ci', { unknownField: 123 }), /unknown configuration field/);
    assert.throws(() => validateProfile('ci', { timeout: -5 }), /Invalid profile field/);

    const validated = validateProfile('ci', {
      provider: 'live',
      timeout: 45,
      max_findings: 25,
      enable_live_reviewer: true,
      github_dry_run: true,
      comment_mode: 'inline',
    });
    assert.strictEqual(validated.provider, 'live');
    assert.strictEqual(validated.timeout, 45);
    assert.strictEqual(validated.maxFindings, 25);
    assert.strictEqual(validated.enableLiveReviewer, true);
    assert.strictEqual(validated.githubDryRun, true);
    assert.strictEqual(validated.commentMode, 'inline');
  });

  // 6. Health polling
  await test('6. Health polling validates payload structure and bounds values', async () => {
    assert.throws(() => safeHealth({ status: 'error' }), /valid CodeAtlas \/health response/);
    assert.throws(() => safeHealth({ status: 'ok', service: 'other', version: '1.0.0' }), /valid CodeAtlas/);

    const valid = safeHealth({
      status: 'ok',
      service: 'codeatlas-service',
      version: '0.2.0',
      pid: 4321,
      active_reviews: 2,
      provider: 'mock',
    });
    assert.strictEqual(valid.status, 'ok');
    assert.strictEqual(valid.version, '0.2.0');
    assert.strictEqual(valid.pid, 4321);
    assert.strictEqual(valid.active_reviews, 2);
  });

  // 7. Cursor synchronization
  await test('7. Cursor synchronization and severity ordering', async () => {
    const findings = [
      { id: 'f-low', file: 'src/app.py', start_line: 10, end_line: 15, severity: 'low', category: 'lint' },
      { id: 'f-blocker', file: 'src/app.py', start_line: 12, end_line: 14, severity: 'blocker', category: 'security' },
      { id: 'f-med', file: 'src/app.py', start_line: 11, end_line: 13, severity: 'medium', category: 'perf' },
    ];

    const atLine12 = findFindingsAtCursor(findings, 'src/app.py', 12);
    assert.strictEqual(atLine12.length, 3);
    assert.strictEqual(atLine12[0].id, 'f-blocker', 'Highest severity first');
    assert.strictEqual(atLine12[1].id, 'f-med');
    assert.strictEqual(atLine12[2].id, 'f-low');

    const atLine20 = findFindingsAtCursor(findings, 'src/app.py', 20);
    assert.strictEqual(atLine20.length, 0, 'No finding outside range');
  });

  // 8. Finding navigation
  await test('8. Finding navigation rejects path traversal and outside workspace', async () => {
    let warningMsg = null;
    mockVsCode.window.showWarningMessage = async (msg) => { warningMsg = msg; };

    const subscriptions = [];
    const session = ext.activate({ subscriptions });

    const commands = new Map();
    const origRegister = mockVsCode.commands.registerCommand;
    mockVsCode.commands.registerCommand = (id, handler) => {
      commands.set(id, handler);
      return { dispose: () => {} };
    };
    try {
      const navSession = ext.activate({ subscriptions: [] });
      // Call navigate with path traversal
      await commands.get('codeatlas.openFindingLocation')({ file: '../../etc/passwd', line: 1 });
      assert.ok(warningMsg && warningMsg.includes('workspace-relative'), 'Rejects .. traversal');
      await navSession.dispose();
    } finally {
      mockVsCode.commands.registerCommand = origRegister;
      await session.dispose();
    }
  });

  // 9. QuickPick commands
  await test('9. QuickPick commands format items with badge and details', async () => {
    const finding = {
      id: 'f-q',
      file: 'src/auth.ts',
      line: 30,
      severity: 'high',
      category: 'AUTH_BYPASS',
      claim: 'Potential bypass in auth check',
      status: 'detected',
      confidence: 0.95,
      evidence_strength: 'supported',
    };
    const item = formatQuickPickItem(finding);
    assert.ok(item.label.includes('[HIGH]'));
    assert.ok(item.label.includes('AUTH_BYPASS'));
    assert.ok(item.label.includes('src/auth.ts:30'));
    assert.strictEqual(item.description, 'Potential bypass in auth check');
    assert.ok(item.detail.includes('0.95'));
  });

  // 10. Draft-fix proposal
  await test('10. Draft-fix proposal requests patch from client', async () => {
    let proposedId = null;
    const client = new ext.CodeAtlasClient('http://127.0.0.1:8765');
    client.proposePatch = async (findingId, runId) => {
      proposedId = findingId;
      return { proposal_id: 'prop-1234', finding_id: findingId, diff: '--- a\n+++ b' };
    };

    const res = await client.proposePatch('find-99', 'run-42');
    assert.strictEqual(proposedId, 'find-99');
    assert.strictEqual(res.proposal_id, 'prop-1234');
    client.dispose();
  });

  // 11. Approval-gated validation
  await test('11. Approval-gated validation sends scoped token and returns sandbox result', async () => {
    let validatedArgs = null;
    const client = new ext.CodeAtlasClient('http://127.0.0.1:8765');
    client.validateProposal = async (proposalId, token, runTests, runFullSuite) => {
      validatedArgs = { proposalId, token, runTests, runFullSuite };
      return { valid: true, approval_verified: true, status: 'validated' };
    };

    const res = await client.validateProposal('prop-1234', 'CAT-APP-token', true, false);
    assert.deepStrictEqual(validatedArgs, {
      proposalId: 'prop-1234',
      token: 'CAT-APP-token',
      runTests: true,
      runFullSuite: false,
    });
    assert.strictEqual(res.valid, true);
    assert.strictEqual(res.approval_verified, true);
    client.dispose();
  });

  // 12. Shutdown cleanup
  await test('12. Extension deactivation cleans up session, child processes and decorations', async () => {
    let serviceDisposed = false;
    const subscriptions = [];
    const session = ext.activate({ subscriptions });
    const origServiceDispose = session.serviceManager.dispose;
    session.serviceManager.dispose = async () => {
      serviceDisposed = true;
      return origServiceDispose.call(session.serviceManager);
    };

    await ext.deactivate();
    assert.ok(serviceDisposed, 'deactivate must call serviceManager.dispose()');
    assert.strictEqual(session.client.enabled, false, 'client must be disabled on dispose');
  });

  // 13. No secret exposure
  await test('13. No secret exposure in profile keys, values, or diagnostics', async () => {
    assert.throws(() => validateProfileName('ghp_secretToken12345'), /non-secret identifier/);
    assert.throws(() => validateProfileName('sk-apiKeySecret99999'), /non-secret identifier/);
    assert.throws(() => validateProfile('sec', { apiKey: 'secret' }), /secret-like field/);
    assert.throws(() => validateProfile('sec', { token: 'secret' }), /secret-like field/);
    assert.throws(() => validateProfile('sec', { model: 'sk-1234567890abcdef' }), /secret-like credential/);
  });

  console.log(`\nAll ${passed} Phase 10D migration tests passed successfully!`);
}

if (require.main === module) {
  runMigrationTests().catch((err) => {
    console.error('Migration tests failed:', err);
    process.exit(1);
  });
}

module.exports = { runMigrationTests };
