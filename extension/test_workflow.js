/** Phase 10E: compiled commands -> real loopback HTTP -> deterministic local provider.
 * No imports from the legacy suites (which permit a JavaScript fallback).
 */
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const http = require('http');
const https = require('https');
const cp = require('child_process');
const { compiledEntry, createHost } = require('./test/workflow_host');

// Run before installing mocks or creating any child process. Missing output is always fatal.
const entry = compiledEntry(__dirname);
const SECRET = 'sk-phase10e-synthetic-secret-canary';
const STATES = ['preparing', 'indexing', 'analyzing', 'reviewing', 'findings_ready', 'completed'];
const fixture = path.join(__dirname, 'test', 'fixtures', 'workflow_service.py');
const python = process.env.CODEATLAS_TEST_PYTHON || 'python';
const realSpawn = cp.spawn;

async function eventually(predicate, message) {
  const deadline = Date.now() + 5000;
  while (!await predicate()) {
    assert.ok(Date.now() < deadline, message);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

async function freePort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

function childFixture(port, options = {}) {
  // Whitelist the test child's environment; live credentials are neither required nor inherited.
  const env = Object.fromEntries(['PATH', 'HOME', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'VIRTUAL_ENV']
    .filter(key => process.env[key]).map(key => [key, process.env[key]]));
  Object.assign(env, { PYTHONUNBUFFERED: '1', PYTHONSAFEPATH: '1', PYTHONDONTWRITEBYTECODE: '1' });
  const child = realSpawn(python, [fixture, '--host', '127.0.0.1', '--port', String(port)],
    { ...options, shell: false, env, stdio: ['ignore', 'pipe', 'pipe', 'pipe'] });
  const pending = new Map();
  let serial = 0, buffer = '';
  child.signals = [];
  const kill = child.kill.bind(child);
  child.kill = signal => { child.signals.push(signal); return kill(signal); };
  child.stdio[3].setEncoding('utf8');
  child.stdio[3].on('data', chunk => {
    buffer += chunk;
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const reply = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1);
      const waiter = pending.get(reply.id);
      if (waiter) { pending.delete(reply.id); clearTimeout(waiter.timer);
        if (reply.error) waiter.reject(new Error(reply.error)); else waiter.resolve(reply.result); }
    }
  });
  child.stdio[3].on('error', () => {});
  child.closed = new Promise(resolve => child.once('close', resolve));
  child.on('exit', () => {
    for (const waiter of pending.values()) { clearTimeout(waiter.timer); waiter.reject(new Error('Fixture exited')); }
    pending.clear();
  });
  child.control = (op, fields = {}) => new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Fixture control timeout: ${op}`)); }, 5000);
    pending.set(id, { resolve, reject, timer });
    child.stdio[3].write(JSON.stringify({ id, op, ...fields }) + '\n');
  });
  return child;
}

function git(root, ...args) {
  return cp.execFileSync('git', args, { cwd: root, encoding: 'utf8', env: { ...process.env,
    GIT_AUTHOR_NAME: 'Workflow Fixture', GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
    GIT_COMMITTER_NAME: 'Workflow Fixture', GIT_COMMITTER_EMAIL: 'fixture@example.invalid',
    GIT_AUTHOR_DATE: '2026-01-01T00:00:00Z', GIT_COMMITTER_DATE: '2026-01-01T00:00:00Z',
  } });
}

function snapshot(root) {
  const files = {};
  function visit(dir, prefix = '') {
    for (const name of fs.readdirSync(dir).sort()) {
      if (name === '.git') continue;
      const full = path.join(dir, name), rel = prefix + name;
      if (fs.statSync(full).isDirectory()) visit(full, rel + '/');
      else files[rel] = fs.readFileSync(full).toString('base64');
    }
  }
  visit(root);
  return { files, head: git(root, 'rev-parse', 'HEAD'), refs: git(root, 'show-ref'),
    status: git(root, 'status', '--porcelain=v1', '-z'), diff: git(root, 'diff', '--binary'),
    staged: git(root, 'diff', '--cached', '--binary'), worktrees: git(root, 'worktree', 'list', '--porcelain'),
    index: fs.readFileSync(path.join(root, '.git', 'index')).toString('base64') };
}

async function runTests() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codeatlas-workflow-'));
  const children = [], tokens = [];
  let host, ext, session, count = 0;
  const originalHttp = http.request, originalHttps = https.request;
  const test = async (name, fn) => { await fn(); count++; console.log(`✓ [Workflow] ${name}`); };
  try {
    await test('compiled-runtime guard rejects legacy main and missing output', async () => {
      const guard = path.join(root, 'guard'); fs.mkdirSync(guard);
      fs.writeFileSync(path.join(guard, 'package.json'), JSON.stringify({ main: './extension.js' }));
      fs.writeFileSync(path.join(guard, 'extension.js'), 'throw new Error("Legacy must never load");');
      assert.throws(() => compiledEntry(guard), /main.*out\/extension.js/);
      fs.writeFileSync(path.join(guard, 'package.json'), JSON.stringify({ main: './out/extension.js' }));
      assert.throws(() => compiledEntry(guard), /compiled output missing/);
      fs.rmSync(guard, { recursive: true });
    });

    fs.mkdirSync(path.join(root, 'src')); fs.mkdirSync(path.join(root, 'tests'));
    fs.mkdirSync(path.join(root, '.codeatlas'));
    fs.writeFileSync(path.join(root, 'src/app.py'), 'def run(value):\n    return value\n');
    fs.writeFileSync(path.join(root, 'tests/test_app.py'), 'from src.app import run\n\ndef test_run():\n    assert run(2) == 2\n');
    fs.writeFileSync(path.join(root, 'pyproject.toml'), '[tool.pytest.ini_options]\npythonpath = ["."]\n');
    git(root, 'init', '-q', '-b', 'main'); git(root, 'add', '.');
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture base');
    git(root, 'checkout', '-qb', 'workflow');
    fs.appendFileSync(path.join(root, 'src/app.py'), '# CodeAtlas finding: Fixture requires operator review\n\n# end\n');
    git(root, 'add', '.');
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture change');
    fs.writeFileSync(path.join(root, 'operator-notes.txt'), 'staged operator notes\n');
    git(root, 'add', 'operator-notes.txt');
    fs.appendFileSync(path.join(root, 'operator-notes.txt'), 'unstaged operator notes\n');
    fs.writeFileSync(path.join(root, '.env'), `API_KEY=${SECRET}\n`);
    fs.writeFileSync(path.join(root, '.codeatlas/profiles.json'), JSON.stringify({ workflow: {
      provider: 'mock', timeout: 10, maxFindings: 7, enablePatchSuggestions: true, enableTestExecution: true,
    } }));
    const before = snapshot(root);
    const port = await freePort();
    host = createHost(root, { serviceMode: 'managed', servicePort: port, healthCheckInterval: 0,
      activeProfile: 'default', serviceCommand: 'codeatlas serve', autoStartService: false });
    host.install();
    http.request = function (options, ...args) {
      const hostname = typeof options === 'string' ? new URL(options).hostname : options.hostname;
      assert.ok(['127.0.0.1', 'localhost', '::1'].includes(hostname), 'Workflow attempted external network access');
      return originalHttp.call(this, options, ...args);
    };
    https.request = () => assert.fail('Workflow attempted external HTTPS access');
    cp.spawn = (executable, args, options) => {
      assert.strictEqual(executable, 'codeatlas');
      assert.deepStrictEqual(args, ['serve', '--host', '127.0.0.1', '--port', String(port)]);
      assert.strictEqual(options.shell, false);
      assert.notStrictEqual(options.cwd, root);
      const child = childFixture(port, options); children.push(child); return child;
    };
    ext = require(entry);
    const subscriptions = [];
    session = ext.activate({ subscriptions });
    await session.ready;
    const status = session.statusProvider;
    const findings = host.views.get('codeatlas.findingsView');
    const context = host.views.get('codeatlas.contextView');
    const observedStates = [];
    status.onDidChangeTreeData(() => { if (status.status) observedStates.push(status.status.status); });
    const editor = await host.vscode.window.showTextDocument(
      await host.vscode.workspace.openTextDocument({ fsPath: path.join(root, 'src/app.py') }));
    const decorations = kind => editor.decorations.get(ext.decorationTypes[kind]) || [];
    let child, finding, proposal;
    const inspect = () => child.control('inspect');
    const approve = async (proposalId, wrong_scope) => {
      const token = await child.control('approve', { proposal_id: proposalId, wrong_scope });
      tokens.push(token); return token;
    };
    const latestProposal = async () => (await inspect()).audit.filter(a => a.path.endsWith('/patch-proposal')).at(-1).response;
    const review = async scenario => {
      await child.control('scenario', { value: scenario });
      await host.command('startReview');
      assert.strictEqual(status.status.status, 'preparing');
      for (let i = 1; i < STATES.length; i++) await host.poll();
      assert.strictEqual(host.intervals.size, 0);
    };

    await test('workspace activation, service unavailable, profile selection, explicit startup and health', async () => {
      assert.strictEqual(session.serviceManager.lastHealthStatus, 'not_configured');
      assert.strictEqual(children.length, 0);
      await host.command('startReview');
      assert.ok(host.messages.some(m => /Start review failed/.test(m.text)));
      assert.strictEqual(status.status, null);
      host.pick(items => items.find(i => i.profileName === 'workflow'));
      await host.command('selectConfigurationProfile');
      assert.deepStrictEqual(host.updates, [['activeProfile', 'workflow']]);
      assert.strictEqual(session.profileManager.getActiveProfile().githubDryRun, true);
      await host.command('startLocalService');
      child = children[0];
      assert.ok(child, 'Start Local Service must spawn the fixture');
      assert.strictEqual(session.serviceManager.owned, true, host.messages.at(-1)?.text);
      assert.strictEqual(session.serviceManager.managedPid, child.pid);
      await host.command('checkServiceHealth');
      assert.strictEqual(session.serviceManager.lastHealth.pid, child.pid);
      assert.ok(host.labels('status').includes('Service Health: healthy'));
      assert.strictEqual((await inspect()).audit.filter(a => a.path === '/reviews').length, 0);
    });

    await test('start review sends schema-valid profile options and displays every lifecycle state', async () => {
      await host.command('startReview');
      assert.strictEqual(status.status.status, 'preparing');
      for (const state of STATES.slice(1)) {
        await host.poll();
        assert.strictEqual(status.status.status, state);
        assert.ok(host.labels('status').includes(`Status: ${state}`));
      }
      assert.deepStrictEqual(observedStates, STATES);
      assert.strictEqual(host.intervals.size, 0);
      const request = (await inspect()).audit.find(a => a.path === '/reviews');
      assert.deepStrictEqual(request.body, { repo: root, base: 'main', head: 'HEAD', review_provider: 'mock',
        provider_timeout: 10, allow_patch_suggestions: true, max_findings: 7 });
      assert.deepStrictEqual(status.status.errors, []);
      assert.match(status.status.limitations[0], /Deterministic workflow fixture/);
    });

    await test('sidebar, exact editor decoration, QuickPick navigation, and cursor synchronization', async () => {
      assert.strictEqual(findings.findings.length, 1);
      finding = findings.getChildren()[0].finding;
      assert.match(host.labels('findings')[0], /\[HIGH\].*src\/app.py:3/);
      assert.strictEqual(decorations('high').length, 1);
      assert.deepStrictEqual(decorations('high')[0].range, new host.vscode.Range(2, 0, 2, 999));
      for (const value of ['0.95', 'supported', 'review_only', finding.claim])
        assert.ok(decorations('high')[0].hoverMessage.value.includes(value));
      host.cursor(editor, 2);
      await eventually(() => context.context?.changed_lines?.[0] === 3, 'Cursor context did not load');
      assert.strictEqual(findings.activeFindingId, finding.id);
      assert.strictEqual(decorations('activeCursor').length, 1);
      assert.ok(host.labels('findings')[0].includes('◀ ACTIVE'));
      host.cursor(editor, 4);
      assert.strictEqual(findings.activeFindingId, null);
      assert.strictEqual(decorations('activeCursor').length, 0);
      assert.ok(host.labels('context').includes('No finding at cursor (line 5)'));
      host.pick(items => items[0]);
      await host.command('findFinding');
      assert.strictEqual(editor.revealed.start.line, 2);
      assert.strictEqual(editor.selection.active.line, 2);
      assert.ok(host.picks.at(-1).items[0].detail.includes('0.95'));
    });

    await test('detail panel, evidence/context/confidence/policy/limitations and Explain Finding', async () => {
      await host.command('showFindingDetails', finding);
      const panel = host.panels[0];
      for (const value of [finding.claim, '0.95', 'supported', 'review_only', 'Exact changed line: src/app.py:3',
        'Deterministic workflow fixture', 'not_run', '[REDACTED']) assert.ok(panel.webview.html.includes(value), value);
      assert.ok(host.labels('context').includes('Changed lines: 3'));
      assert.ok(host.labels('context').some(s => s.includes('function run')));
      assert.ok(host.labels('context').includes('Related tests: tests/test_app.py'));
      await host.command('explainFinding', finding);
      assert.ok(host.messages.some(m => /CodeAtlas Explanation.*code_quality/.test(m.text) && /Remediation:/.test(m.text)));
      await panel.message('copy');
      assert.strictEqual(JSON.parse(host.clipboard.at(-1)).id, finding.id);
      host.cursor(editor, 4);
      assert.match(panel.webview.html, /No finding at cursor position \(line 5\)/);
      host.cursor(editor, 2);
      await eventually(() => panel.webview.html.includes(finding.claim), 'Detail panel did not synchronize');
    });

    await test('draft proposal requires human approval and leaves dirty/staged/untracked worktree unchanged', async () => {
      await host.command('generateDraftFix', finding);
      proposal = await latestProposal();
      assert.strictEqual(proposal.finding_id, finding.id);
      assert.strictEqual(proposal.status, 'requires_human_approval');
      assert.strictEqual(proposal.approval_required, true);
      assert.deepStrictEqual(proposal.target_files, ['src/app.py']);
      assert.match(proposal.unified_diff, /TODO\(operator\)/);
      assert.strictEqual(host.inputs.length, 0, 'Proposal notification must not automatically validate');
      assert.strictEqual((await inspect()).validations.length, 0);
      assert.deepStrictEqual(snapshot(root), before);
    });

    await test('missing/cancelled approval fails closed in the command and service', async () => {
      for (const input of ['', undefined]) {
        host.input(input);
        await host.command('validateApprovedFix', proposal);
      }
      assert.strictEqual((await inspect()).validations.length, 0, 'Missing approval must send no validation request');
      assert.ok(host.messages.some(m => /Approval token is required/.test(m.text)));
      const rejected = await session.client.validateProposal(proposal.proposal_id, '', true, false);
      assert.strictEqual(rejected.valid, false);
      assert.strictEqual(rejected.approval_verified, false);
      assert.strictEqual(rejected.applies_cleanly, false);
      assert.strictEqual(rejected.tests_status, 'not_run');
      assert.ok(rejected.errors.length);
      assert.strictEqual((await inspect()).test_calls.length, 0);
    });

    await test('tokens are scoped to proposal, base commit, patch hash, allowed paths and run', async () => {
      for (const scope of ['proposal_id', 'base_commit', 'patch_hash', 'allowed_paths', 'run_id']) {
        host.input(await approve(proposal.proposal_id, scope));
        await host.command('validateApprovedFix', proposal);
        const report = (await inspect()).validations.at(-1);
        assert.strictEqual(report.approval_verified, false, scope);
        assert.strictEqual(report.applies_cleanly, false, scope);
        assert.strictEqual(report.test_execution_attempted, false, scope);
      }
      assert.match(host.messages.at(-1).text, /Patch validation rejected/);
      assert.deepStrictEqual(snapshot(root), before);
    });

    await test('explicit approval validates in a detached sandbox with test status and evidence', async () => {
      host.input(await approve(proposal.proposal_id));
      await host.command('validateApprovedFix', proposal);
      assert.strictEqual(host.inputs.at(-1).password, true);
      assert.match(host.messages.at(-1).text, /validated cleanly in isolated sandbox.*validated/);
      const trace = await inspect(), report = trace.validations.at(-1);
      const wire = trace.audit.filter(a => a.path.endsWith('/validate')).at(-1).response;
      assert.strictEqual(wire.status, 'validated');
      assert.strictEqual(wire.valid, true); assert.strictEqual(wire.approval_verified, true);
      assert.strictEqual(wire.applies_cleanly, true); assert.strictEqual(wire.syntax_valid, true);
      assert.strictEqual(wire.tests_status, 'passed'); assert.strictEqual(wire.full_suite_status, 'not_run');
      assert.deepStrictEqual(wire.errors, []);
      assert.deepStrictEqual(trace.audit.filter(a => a.path.endsWith('/validate')).at(-1).body,
        { approval_token: '[REDACTED]', run_tests: true, run_full_suite: false });
      assert.strictEqual(report.patch_applied_in_isolated_sandbox, true);
      assert.strictEqual(report.cleanup_status, 'completed');
      assert.strictEqual(report.test_result.tests_passed, 1);
      assert.deepStrictEqual(report.tests_run, ['tests/test_app.py']);
      assert.strictEqual(report.network_isolation_verified, false);
      assert.match(report.diagnostic_limitations[0], /Synthetic test executor/);
      for (const event of ['sandbox_created', 'patch_check_started', 'patch_validation_completed', 'fixture_test_outcome'])
        assert.ok(trace.events.some(e => e.event === event), event);
      assert.strictEqual(trace.sandboxes_removed, true);
      // Validation is a separate response in the current contract; the initial
      // review/detail status is not automatically rewritten with test outcomes.
      assert.ok(host.labels('status').includes('Tests Status: not_run'));
      assert.deepStrictEqual(snapshot(root), before);
    });

    await test('local dismissal survives a service refresh and remains reversible', async () => {
      await host.command('dismissFinding', finding);
      assert.ok(host.labels('findings')[0].includes('(Dismissed)'));
      assert.strictEqual(decorations('high').length, 0);
      assert.match(host.panels[0].webview.html, /Restore Finding/);
      await host.command('refresh');
      assert.notStrictEqual(findings.findings[0], finding, 'Refresh must replace the HTTP finding objects');
      assert.strictEqual(findings.findings[0].dismissed, true);
      assert.ok(host.labels('findings')[0].includes('(Dismissed)'));
      assert.strictEqual(decorations('high').length, 0);
      const trace = await inspect();
      assert.ok(!trace.audit.some(a => a.path.endsWith('/dismiss')), 'Dismissal must stay local');
      assert.strictEqual(trace.audit.filter(a => a.path.includes('/findings?')).at(-1).response.findings[0].dismissed, false);
      await host.command('dismissFinding', findings.findings[0]);
      assert.strictEqual(decorations('high').length, 1);
    });

    await test('malformed JSON, malformed health schema and hostile error bodies are safely reported', async () => {
      await child.control('fault', { value: 'malformed' });
      await host.command('refresh');
      assert.match(host.messages.at(-1).text, /Malformed response/);
      await child.control('fault', { value: 'bad_health' });
      await host.command('checkServiceHealth');
      assert.strictEqual(session.serviceManager.lastHealth, null);
      assert.match(host.messages.at(-1).text, /Service Unreachable: CodeAtlas \/health failed/);
      await child.control('fault', { value: 'secret_error' });
      await host.command('refresh');
      assert.match(host.messages.at(-1).text, /HTTP 503/);
      await host.command('checkServiceHealth');
      assert.strictEqual(session.serviceManager.lastHealthStatus, 'healthy');
    });

    await test('stale run clears findings, context and decorations', async () => {
      await child.control('expire', { run_id: status.status.run_id });
      await host.command('refresh');
      assert.strictEqual(status.status, null);
      assert.deepStrictEqual(findings.findings, []);
      assert.strictEqual(context.context, null);
      assert.strictEqual(decorations('high').length, 0);
      assert.match(host.messages.at(-1).text, /expired or was not found/);
      await host.command('findFinding');
      assert.match(host.messages.at(-1).text, /No active review run/);
    });

    await test('invalid finding range clamps to document bounds; missing file warns without opening it', async () => {
      await review('invalid_range');
      assert.deepStrictEqual(decorations('high')[0].range, new host.vscode.Range(0, 0, editor.document.lineCount - 1, 999));
      await host.command('openFindingLocation', findings.findings[0]);
      assert.strictEqual(editor.revealed.start.line, 0);
      await review('missing_file');
      assert.strictEqual(decorations('high').length, 0);
      await host.command('openFindingLocation', findings.findings[0]);
      assert.ok(host.messages.some(m => /File not found or moved: src\/missing.py/.test(m.text)));
      assert.strictEqual(host.vscode.window.activeTextEditor, editor);
    });

    await test('review cancellation stops polling and provider failure retains safe errors/limitations', async () => {
      await child.control('scenario', { value: 'success' });
      await host.command('startReview');
      await host.poll();
      await host.command('cancelReview');
      assert.strictEqual(status.status.status, 'cancelled');
      await host.poll();
      assert.strictEqual(host.intervals.size, 0);
      assert.deepStrictEqual(findings.findings, []);
      await review('provider_failure');
      assert.strictEqual(status.status.status, 'failed');
      assert.deepStrictEqual(findings.findings, []);
      assert.match(status.status.errors[0], /Deterministic provider failed/);
      assert.ok(status.status.limitations.length);
      assert.ok(host.messages.some(m => /Review failed: Deterministic provider failed/.test(m.text)));
    });

    await test('approved test failure rejects validation and cleans the sandbox', async () => {
      await review('test_failure');
      await host.command('generateDraftFix', findings.findings[0]);
      const p = await latestProposal();
      host.input(await approve(p.proposal_id));
      await host.command('validateApprovedFix', p);
      assert.match(host.messages.at(-1).text, /Patch validation rejected: Deterministic fixture assertion failed/);
      const trace = await inspect(), report = trace.validations.at(-1);
      assert.strictEqual(report.approval_verified, true); assert.strictEqual(report.valid, false);
      assert.strictEqual(report.tests_status, 'failed'); assert.strictEqual(report.test_result.tests_failed, 1);
      assert.strictEqual(report.cleanup_status, 'completed'); assert.strictEqual(trace.sandboxes_removed, true);
      assert.deepStrictEqual(snapshot(root), before);
    });

    await test('production router has no arbitrary shell/file-read/apply/approval endpoints', async () => {
      for (const [method, endpoint, body] of [
        ['POST', '/exec', { cmd: 'whoami' }], ['GET', '/files/etc/passwd', null],
        ['POST', `/proposals/${proposal.proposal_id}/apply`, {}],
        ['POST', `/proposals/${proposal.proposal_id}/approve`, {}],
      ]) await assert.rejects(session.client.request(method, endpoint, body), /HTTP 404/);
      // Malformed requests exercise the real service's request model, not a fixture imitation.
      const response = await new Promise((resolve, reject) => {
        const data = JSON.stringify({ repo: root, max_findings: 0 });
        const req = http.request({ hostname: '127.0.0.1', port, path: '/reviews', method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data), 'X-Workflow-Invalid': 'true' } }, res => {
          let body = ''; res.on('data', c => { body += c; }); res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(body) }));
        });
        req.on('error', reject); req.end(data);
      });
      assert.strictEqual(response.status, 400); assert.strictEqual(response.body.error, 'bad_request');
    });

    await test('secrets and approval tokens never enter UI, settings, clipboard, logs or response evidence', async () => {
      await child.control('noise');
      await eventually(() => session.serviceManager.stderrBuffer.includes('suppressed'), 'Process output not observed');
      const trace = await inspect();
      assert.deepStrictEqual(trace.contract_errors, []);
      const surfaces = JSON.stringify({ trace, messages: host.messages, inputs: host.inputs, picks: host.picks,
        updates: host.updates, clipboard: host.clipboard, panels: host.panels.map(p => p.webview.html),
        trees: ['status', 'findings', 'context'].map(v => host.labels(v)),
        stdout: session.serviceManager.stdoutBuffer, stderr: session.serviceManager.stderrBuffer });
      for (const secret of [SECRET, 'CAT-APP-synthetic-log-canary', ...tokens])
        assert.ok(!surfaces.includes(secret), 'Sensitive value leaked to an observable surface');
      assert.ok(trace.audit.some(a => a.body?.approval_token === '[REDACTED]'));
      assert.ok(trace.audit.every(a => !a.body?.approval_token || /^\/proposals\/[^/]+\/validate$/.test(a.path)));
      assert.ok(!Object.keys(require.cache).some(file => ['extension.js', 'service.js', 'profiles.js'].some(name => file === path.join(__dirname, name))),
        'Phase 10E loaded a legacy runtime module');
    });

    await test('stop/deactivation affect only the owned child, preserving an external service', async () => {
      const externalPort = await freePort();
      const external = childFixture(externalPort); children.push(external);
      const externalClient = new ext.CodeAtlasClient(`http://127.0.0.1:${externalPort}`);
      await eventually(async () => { try { return (await externalClient.getHealth()).pid === external.pid; } catch { return false; } }, 'External fixture startup');
      const manager = new ext.ServiceLifecycleManager({ serviceMode: 'managed', servicePort: externalPort });
      try {
        assert.strictEqual((await manager.startManagedService()).started, false);
        assert.strictEqual(manager.owned, false);
        assert.strictEqual((await manager.stopManagedService()).stopped, false);
        await manager.dispose();
        await host.command('stopLocalService');
        await child.closed;
        assert.deepStrictEqual(child.signals, ['SIGTERM']);
        assert.strictEqual(session.serviceManager.owned, false);
        assert.strictEqual(session.serviceManager.lastHealthStatus, 'stopped');
        assert.strictEqual((await externalClient.getHealth()).pid, external.pid);
        assert.deepStrictEqual(external.signals, []);
        await host.command('checkServiceHealth');
        assert.match(host.messages.at(-1).text, /Service Unreachable/);
      } finally { externalClient.dispose(); await manager.dispose(); }
    });

    await test('service crash reports safely, does not auto-restart, and deactivation clears polling', async () => {
      await host.command('startLocalService');
      child = children.at(-1);
      assert.strictEqual(session.serviceManager.owned, true);
      await host.command('startReview');
      assert.strictEqual(host.intervals.size, 1);
      const childCount = children.length;
      await assert.rejects(child.control('crash'), /Fixture exited/);
      await child.closed;
      assert.strictEqual(session.serviceManager.lastHealthStatus, 'crashed');
      assert.strictEqual(session.serviceManager.owned, false);
      assert.ok(host.messages.some(m => /exited unexpectedly.*17/.test(m.text)));
      await host.poll();
      assert.match(host.messages.at(-1).text, /Review polling error/);
      assert.strictEqual(children.length, childCount);
      assert.strictEqual(host.intervals.size, 0);
      await ext.deactivate();
      for (const sub of subscriptions) sub.dispose();
      assert.strictEqual(session.client.enabled, false);
      assert.strictEqual(session.client.pending.size, 0);
      assert.deepStrictEqual(snapshot(root), before);
      const external = children[1];
      assert.deepStrictEqual(external.signals, []);
      assert.ok((await external.control('inspect')).audit.length > 0, 'External process survives extension deactivation');
    });
    console.log(`All ${count} Phase 10E compiled-runtime workflow tests passed.`);
  } finally {
    try { if (ext) await ext.deactivate(); } finally {
      cp.spawn = realSpawn; http.request = originalHttp; https.request = originalHttps;
      host?.restore();
      for (const child of children) {
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
        await child.closed;
      }
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
}

if (!process.argv.includes('--guard-only')) {
  runTests().catch(error => { console.error('Phase 10E workflow failed:', error); process.exitCode = 1; });
}
