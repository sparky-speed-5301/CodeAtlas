/** Phase 11A: IDE dogfooding regression tests.
 *
 * Reuses the Phase 10E workflow harness (compiled runtime -> real loopback
 * HTTP -> deterministic fixture service). Each test covers one dogfooding bug
 * fix; no live LLM/GitHub credentials or network access are required.
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

const entry = compiledEntry(__dirname);
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
    GIT_AUTHOR_NAME: 'Dogfood Fixture', GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
    GIT_COMMITTER_NAME: 'Dogfood Fixture', GIT_COMMITTER_EMAIL: 'fixture@example.invalid',
    GIT_AUTHOR_DATE: '2026-01-01T00:00:00Z', GIT_COMMITTER_DATE: '2026-01-01T00:00:00Z',
  } });
}

async function runTests() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codeatlas-dogfood-'));
  const children = [];
  let host, ext, session, count = 0;
  const originalHttp = http.request, originalHttps = https.request;
  const test = async (name, fn) => { await fn(); count++; console.log(`✓ [Dogfood] ${name}`); };
  try {
    fs.mkdirSync(path.join(root, 'src')); fs.mkdirSync(path.join(root, 'tests'));
    fs.mkdirSync(path.join(root, '.codeatlas'));
    fs.writeFileSync(path.join(root, 'src/app.py'), 'def run(value):\n    return value\n');
    fs.writeFileSync(path.join(root, 'tests/test_app.py'), 'from src.app import run\n\ndef test_run():\n    assert run(2) == 2\n');
    fs.writeFileSync(path.join(root, 'pyproject.toml'), '[tool.pytest.ini_options]\npythonpath = ["."]\n');
    git(root, 'init', '-q', '-b', 'main'); git(root, 'add', '.');
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture base');
    git(root, 'checkout', '-qb', 'dogfood');
    fs.appendFileSync(path.join(root, 'src/app.py'), '# CodeAtlas finding: Fixture requires operator review\n\n# end\n');
    git(root, 'add', '.');
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture change');
    const port = await freePort();
    const settings = { serviceMode: 'managed', servicePort: port, healthCheckInterval: 0,
      activeProfile: 'default', serviceCommand: 'codeatlas serve', autoStartService: false };
    host = createHost(root, settings);
    host.install();
    http.request = function (options, ...args) {
      const hostname = typeof options === 'string' ? new URL(options).hostname : options.hostname;
      assert.ok(['127.0.0.1', 'localhost', '::1'].includes(hostname), 'Dogfood attempted external network access');
      return originalHttp.call(this, options, ...args);
    };
    https.request = () => assert.fail('Dogfood attempted external HTTPS access');
    cp.spawn = (executable, args, options) => {
      assert.strictEqual(executable, 'codeatlas');
      assert.deepStrictEqual(args, ['serve', '--host', '127.0.0.1', '--port', String(port)]);
      assert.strictEqual(options.shell, false);
      const child = childFixture(port, options); children.push(child); return child;
    };
    ext = require(entry);
    session = ext.activate({ subscriptions: [] });
    await session.ready;
    const status = session.statusProvider;
    const findings = host.views.get('codeatlas.findingsView');
    const editor = await host.vscode.window.showTextDocument(
      await host.vscode.workspace.openTextDocument({ fsPath: path.join(root, 'src/app.py') }));
    const decorations = kind => editor.decorations.get(ext.decorationTypes[kind]) || [];
    let child, finding;
    const review = async scenario => {
      await child.control('scenario', { value: scenario });
      await host.command('startReview');
      for (let i = 1; i < STATES.length; i++) await host.poll();
      assert.strictEqual(status.status.status, 'completed');
    };

    await test('service starts and health is healthy before the dogfood checks', async () => {
      await host.command('startLocalService');
      child = children[0];
      assert.ok(child, 'Start Local Service must spawn the fixture');
      assert.strictEqual(session.serviceManager.lastHealth.pid, child.pid);
    });

    await test('inverted finding range renders decorations without crashing', async () => {
      await review('inverted_range');
      finding = findings.getChildren()[0].finding;
      assert.strictEqual(finding.start_line, 3);
      assert.strictEqual(finding.end_line, 2);
      // A real vscode.Range throws when end < start; the normalized bounds
      // must keep every decoration ordered and the whole editor annotated.
      assert.strictEqual(decorations('high').length, 1);
      for (const d of decorations('high')) {
        assert.ok(d.range.end.line >= d.range.start.line, 'Decoration range end before start');
      }
      assert.deepStrictEqual(decorations('high')[0].range, new host.vscode.Range(1, 0, 2, 999));
      // Cursor detection uses the same normalized bounds.
      host.cursor(editor, 2);
      await eventually(() => findings.activeFindingId === finding.id, 'Cursor missed inverted-range finding');
      host.cursor(editor, 4);
      assert.strictEqual(findings.activeFindingId, null);
    });

    await test('detail webview actions target the currently displayed finding', async () => {
      await host.command('showFindingDetails', findings.getChildren()[0].finding);
      const panel = host.panels[0];
      await panel.message('copy');
      const copyId = () => JSON.parse(host.clipboard.at(-1)).id;
      assert.strictEqual(copyId(), findings.getChildren()[0].finding.id);
      // A later review replaces the finding objects; re-opening the detail in
      // the same panel must retarget every webview action to the new finding.
      await review('success');
      const nextFinding = findings.getChildren()[0].finding;
      await host.command('showFindingDetails', nextFinding);
      await panel.message('copy');
      assert.strictEqual(copyId(), nextFinding.id, 'Webview acted on a stale finding');
      await panel.message('explain');
      await panel.message('draftFix');
      assert.strictEqual(host.inputs.length, 0, 'Draft fix must not auto-validate');
      panel.dispose();
    });

    await test('explain and draft fix without an active run show a clear message and send nothing', async () => {
      await child.control('expire', { run_id: status.status.run_id });
      await host.command('refresh');
      assert.ok(host.messages.some(m => /expired or was not found/.test(m.text)));
      const auditBefore = (await child.control('inspect')).audit.length;
      const messageCount = host.messages.length;
      await host.command('explainFinding', { id: 'finding-x' });
      await host.command('generateDraftFix', { id: 'finding-x' });
      assert.ok(host.messages.slice(messageCount).some(m => /No active review run/.test(m.text)),
        'Explain/draft must show a clear no-run message');
      assert.strictEqual((await child.control('inspect')).audit.length, auditBefore,
        'No service request may be sent without an active run');
    });

    await test('detail webview escapes hostile detail fields', async () => {
      const { getFindingWebviewHtml } = require(path.join(path.dirname(entry), 'detail-panel.js'));
      const hostile = {
        id: 'CA-X', claim: '<script>alert(1)</script>', severity: 'high', category: 'CODE_QUALITY',
        file: 'src/app.py', line: '<img src=x onerror=alert(2)>', confidence: '<script>h()</script>',
        status: 'review_only', impact: 'ok', deterministic_evidence: null, limitations: [],
      };
      const html = getFindingWebviewHtml(hostile);
      assert.ok(!html.includes('<script>alert(1)'), 'Claim must be escaped');
      assert.ok(!html.includes('<img src=x'), 'Line must be escaped');
      assert.ok(!html.includes('<script>h()'), 'Confidence must be escaped');
      assert.ok(html.includes('&lt;script&gt;'));
    });

    // ------------------------------------------------------------------
    // Phase 11A follow-up: configurable review base branch.
    // ------------------------------------------------------------------
    const reviewBase = async () => {
      await child.control('scenario', { value: 'success' });
      await host.command('startReview');
      for (let i = 1; i < STATES.length; i++) await host.poll();
      const audits = (await child.control('inspect')).audit.filter(a => a.path === '/reviews');
      return { body: audits.at(-1).body, labels: host.labels('status') };
    };

    await test('default base branch is main and is shown in the status view', async () => {
      // No baseBranch setting: the contributed default applies.
      delete settings.baseBranch;
      const { body, labels } = await reviewBase();
      assert.strictEqual(body.base, 'main');
      assert.ok(labels.some(l => l.startsWith('Base Branch: main')), labels.join(' | '));
    });

    await test('custom base branch is transmitted and displayed', async () => {
      settings.baseBranch = 'develop';
      const { body, labels } = await reviewBase();
      assert.strictEqual(body.base, 'develop');
      assert.ok(labels.some(l => l.startsWith('Base Branch: develop')));
    });

    await test('whitespace-only values fall back to main; surrounding whitespace is trimmed', async () => {
      settings.baseBranch = '   ';
      assert.strictEqual((await reviewBase()).body.base, 'main');
      settings.baseBranch = '  release/1.2  ';
      const { body } = await reviewBase();
      assert.strictEqual(body.base, 'release/1.2');
    });

    await test('empty value falls back to main and no-run behavior is unchanged', async () => {
      settings.baseBranch = '';
      const { body } = await reviewBase();
      assert.strictEqual(body.base, 'main');
      // No active review run: explain still shows the clear message and sends nothing.
      await child.control('expire', { run_id: status.status.run_id });
      await host.command('refresh');
      assert.ok(host.messages.some(m => /expired or was not found/.test(m.text)));
      const auditBefore = (await child.control('inspect')).audit.length;
      const messageCount = host.messages.length;
      await host.command('explainFinding', { id: 'finding-x' });
      assert.ok(host.messages.slice(messageCount).some(m => /No active review run/.test(m.text)));
      assert.strictEqual((await child.control('inspect')).audit.length, auditBefore);
      delete settings.baseBranch;
    });

    // ------------------------------------------------------------------
    // Phase 11A follow-up: resolved base/head SHAs in the Status view.
    // ------------------------------------------------------------------
    const statusLabels = () => host.labels('status');
    const shaLabel = prefix => statusLabels().find(l => l.startsWith(prefix));
    const comparisonItem = () => status.getChildren().find(item => item.label.startsWith('Comparison: '));
    const clipboardCount = () => host.clipboard.length;
    const copyComparison = () => host.command('copyComparisonRange', comparisonItem());

    await test('comparison copy command and Status context-menu registration are exposed', async () => {
      const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8'));
      assert.ok(host.commands.has('codeatlas.copyComparisonRange'));
      const menu = manifest.contributes.menus['view/item/context'].find(
        entry => entry.command === 'codeatlas.copyComparisonRange'
      );
      assert.ok(menu, 'Comparison copy command must have a context-menu contribution');
      assert.strictEqual(
        menu.when,
        'view == codeatlas.statusView && viewItem == codeatlas.comparisonRange'
      );
    });

    await test('resolved base/head SHAs display abbreviated with full-SHA tooltips', async () => {
      await review('success');
      const baseLabel = shaLabel('Base SHA: ');
      const headLabel = shaLabel('Head SHA: ');
      const comparison = shaLabel('Comparison: ');
      assert.ok(baseLabel && headLabel && comparison, statusLabels().join(' | '));
      // Abbreviated: exactly 12 hex characters.
      assert.match(baseLabel, /^Base SHA: [0-9a-f]{12}$/);
      assert.match(headLabel, /^Head SHA: [0-9a-f]{12}$/);
      assert.match(comparison, /^Comparison: [0-9a-f]{12}\.\.\.[0-9a-f]{12}$/);
      // Distinct revisions: the fixture HEAD differs from the base branch.
      assert.notStrictEqual(baseLabel, headLabel);
      // The resolved status payload carries the full SHAs (tooltip source);
      // verify the provider retained them for the hover tooltips.
      assert.match(status.lastReviewShas.base, /^[0-9a-f]{40}$/);
      assert.match(status.lastReviewShas.head, /^[0-9a-f]{40}$/);
    });

    await test('comparison copy uses the full active range, not abbreviated labels', async () => {
      const item = comparisonItem();
      assert.ok(item, 'Resolved comparison item must be present');
      assert.strictEqual(item.contextValue, 'codeatlas.comparisonRange');
      const expected = `${status.lastReviewShas.base}...${status.lastReviewShas.head}`;
      const before = clipboardCount();
      await copyComparison();
      assert.strictEqual(host.clipboard.length, before + 1);
      assert.strictEqual(host.clipboard.at(-1), expected);
      assert.ok(host.messages.at(-1).text.includes('Comparison range copied'));
      assert.notStrictEqual(host.clipboard.at(-1), item.label.replace('Comparison: ', ''));
    });

    await test('active-run identity prevents an older status from changing the copy range', async () => {
      const expected = `${status.lastReviewShas.base}...${status.lastReviewShas.head}`;
      status.refresh({
        run_id: 'rev-old-run',
        status: 'completed',
        base_commit: 'a'.repeat(40),
        head_commit: 'b'.repeat(40),
      });
      assert.strictEqual(status.lastReviewShas.base, expected.split('...')[0]);
      const before = clipboardCount();
      await copyComparison();
      assert.strictEqual(host.clipboard.at(-1), expected);
      assert.strictEqual(host.clipboard.length, before + 1);
    });

    await test('missing SHA fields render unresolved and are never invented', async () => {
      await review('unresolved_shas');
      assert.strictEqual(shaLabel('Base SHA: '), 'Base SHA: unresolved');
      assert.strictEqual(shaLabel('Head SHA: '), 'Head SHA: unresolved');
      assert.strictEqual(shaLabel('Comparison: '), undefined, 'No comparison without both SHAs');
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
      assert.match(host.messages.at(-1).text, /base and head SHAs/i);
    });

    await test('missing base SHA is rejected without changing the clipboard', async () => {
      await review('success');
      const resolved = status.status;
      status.refresh({ ...resolved, base_commit: null });
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
      assert.strictEqual(status.lastReviewShas, null);
      status.refresh(resolved);
    });

    await test('missing head SHA is rejected without changing the clipboard', async () => {
      const resolved = status.status;
      status.refresh({ ...resolved, head_commit: null });
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
      assert.strictEqual(status.lastReviewShas, null);
      status.refresh(resolved);
    });

    await test('starting a new review clears the previous run SHAs', async () => {
      await review('success');
      assert.match(shaLabel('Base SHA: '), /^Base SHA: [0-9a-f]{12}$/);
      // The next run's SHAs are not yet resolved: the old ones must not show.
      await child.control('scenario', { value: 'unresolved_shas' });
      await host.command('startReview');
      assert.strictEqual(status.status.status, 'preparing');
      assert.strictEqual(shaLabel('Base SHA: '), 'Base SHA: unresolved');
      assert.strictEqual(shaLabel('Comparison: '), undefined);
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
      for (let i = 1; i < STATES.length; i++) await host.poll();
    });

    await test('cancelling a review clears the displayed SHAs', async () => {
      await review('success');
      assert.match(shaLabel('Base SHA: '), /^Base SHA: [0-9a-f]{12}$/);
      await child.control('scenario', { value: 'success' });
      await host.command('startReview');
      await host.command('cancelReview');
      assert.strictEqual(status.status.status, 'cancelled');
      assert.strictEqual(shaLabel('Base SHA: '), 'Base SHA: unresolved');
      assert.strictEqual(shaLabel('Comparison: '), undefined);
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
    });

    await test('stale run removes the SHA section entirely', async () => {
      await review('success');
      assert.ok(shaLabel('Base SHA: '));
      await child.control('expire', { run_id: status.status.run_id });
      await host.command('refresh');
      assert.ok(host.messages.some(m => /expired or was not found/.test(m.text)));
      assert.strictEqual(status.status, null);
      assert.strictEqual(shaLabel('Base SHA: '), undefined);
      assert.strictEqual(shaLabel('Head SHA: '), undefined);
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
    });

    await test('service crash clears displayed SHAs while the status stays readable', async () => {
      // Restart the managed service (the previous fixture died in the crash test below).
      await host.command('startLocalService');
      child = children[children.length - 1];
      assert.strictEqual(session.serviceManager.owned, true);
      await review('success');
      assert.match(shaLabel('Base SHA: '), /^Base SHA: [0-9a-f]{12}$/);
      child.control('crash').catch(() => {});
      await eventually(() => session.serviceManager.lastHealthStatus === 'crashed', 'Crash not detected');
      assert.strictEqual(status.lastReviewShas, null);
      assert.strictEqual(shaLabel('Base SHA: '), 'Base SHA: unresolved');
      const before = clipboardCount();
      await host.command('copyComparisonRange');
      assert.strictEqual(clipboardCount(), before);
    });

    await test('managed service stops cleanly on session dispose (reload recovery)', async () => {
      // The previous test crashed the fixture; restart so an owned child exists.
      await host.command('startLocalService');
      child = children[children.length - 1];
      assert.strictEqual(session.serviceManager.owned, true);
      await session.dispose();
      assert.strictEqual(session.serviceManager.owned, false);
      assert.ok(child.signals.includes('SIGTERM'));
      await child.closed;
      // Reactivation disposes safely even when no service was ever started:
      // the extension never stops a process it does not own.
      const second = createHost(root, { serviceMode: 'external', healthCheckInterval: 0 });
      second.install();
      const ext2 = require(entry);
      const session2 = ext2.activate({ subscriptions: [] });
      await session2.ready.catch(() => {});
      assert.strictEqual(session2.serviceManager.owned, false);
      await session2.dispose();
      assert.ok(children.length >= 1, 'Disposal must not spawn or stop foreign processes');
    });
  } finally {
    http.request = originalHttp;
    https.request = originalHttps;
    cp.spawn = realSpawn;
    try { if (session && session.serviceManager.owned) await session.dispose(); } catch (_) {}
    for (const child of children) { try { child.kill(); } catch (_) {} }
    try { fs.rmSync(root, { recursive: true, force: true }); } catch (_) {}
    host?.restore?.();
  }
  console.log(`\nAll ${count} Phase 11A Dogfooding Tests Passed!`);
}

runTests().then(() => process.exit(0), error => {
  console.error(error);
  process.exit(1);
});
