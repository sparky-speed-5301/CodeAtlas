/** Phase 11C-F: compiled extension commands -> REAL production service -> full autofix chain.
 *
 * Unlike Phase 10E, nothing on the service side is scripted: the real review
 * pipeline, repair orchestrator, isolated sandbox validator, and the explicit
 * apply/revert operations run against a real Git working tree. The fixture
 * process only mints operator approval tokens over the private control pipe.
 * Automatic application must be impossible; the workspace must change only
 * through the explicit Apply Validated Fix command and restore exactly on revert.
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
const fixture = path.join(__dirname, 'test', 'fixtures', 'autofix_service.py');
const python = process.env.CODEATLAS_TEST_PYTHON || 'python';
const realSpawn = cp.spawn;
const realHttp = http.request, realHttps = https.request;

const BASE_SOURCE = 'def run(x):\n    return x\n';
const HEAD_SOURCE = 'def render(user):\n    print(user.password)\n';

async function eventually(predicate, message) {
  const deadline = Date.now() + 15000;
  while (!(await predicate())) {
    assert.ok(Date.now() < deadline, message);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}

async function freePort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function git(root, ...args) {
  return cp.execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
}

function getJson(port, pathname) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port, path: pathname, method: 'GET' }, res => {
      let body = ''; res.on('data', c => { body += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(body || '{}') }));
    });
    req.on('error', reject); req.end();
  });
}

function postJson(port, pathname, data) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(data);
    const req = http.request({ hostname: '127.0.0.1', port, path: pathname, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } }, res => {
      let body = ''; res.on('data', c => { body += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(body || '{}') }));
    });
    req.on('error', reject); req.write(payload); req.end();
  });
}

function childFixture(port) {
  const env = Object.fromEntries(['PATH', 'HOME', 'SYSTEMROOT', 'WINDIR', 'TMPDIR']
    .filter(key => process.env[key]).map(key => [key, process.env[key]]));
  env.PYTHONUNBUFFERED = '1';
  const child = realSpawn(python, [fixture, '--host', '127.0.0.1', '--port', String(port)],
    { shell: false, env, stdio: ['ignore', 'pipe', 'pipe', 'pipe'] });
  const pending = new Map();
  let serial = 0, buffer = '';
  child.kill = child.kill.bind(child);
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
  child.control = (op, fields = {}) => new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Fixture control timeout: ${op}`)); }, 30000);
    pending.set(id, { resolve, reject, timer });
    child.stdio[3].write(JSON.stringify({ id, op, ...fields }) + '\n');
  });
  return child;
}

async function runTests() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codeatlas-autofix-'));
  const children = [];
  let host, ext, session;
  try {
    // Real Git working tree with a deterministic sensitive-data finding.
    fs.mkdirSync(path.join(root, 'src'));
    fs.mkdirSync(path.join(root, '.codeatlas'));
    fs.writeFileSync(path.join(root, 'src/app.py'), BASE_SOURCE);
    git(root, 'init', '-q');
    git(root, 'config', 'core.autocrlf', 'false');
    git(root, 'config', 'user.email', 'autofix@example.invalid');
    git(root, 'config', 'user.name', 'Autofix Fixture');
    git(root, 'add', '.');
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture base');
    const branch = git(root, 'rev-parse', '--abbrev-ref', 'HEAD');
    git(root, 'checkout', '-qb', 'review');
    fs.writeFileSync(path.join(root, 'src/app.py'), HEAD_SOURCE);
    git(root, 'add', '.');
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture change');
    const headBefore = git(root, 'rev-parse', 'HEAD');

    const port = await freePort();
    const child = childFixture(port); children.push(child);
    await eventually(async () => {
      try { return (await getJson(port, '/health')).body.pid === child.pid; } catch { return false; }
    }, 'fixture service must become healthy');

    host = createHost(root, { serviceMode: 'external', serviceUrl: `http://127.0.0.1:${port}`,
      autoStartService: false, healthCheckInterval: 0, baseBranch: branch });
    host.install();
    http.request = function (options, ...args) {
      const hostname = typeof options === 'string' ? new URL(options).hostname : options.hostname;
      assert.ok(['127.0.0.1', 'localhost', '::1'].includes(hostname), 'Autofix workflow attempted external network access');
      return realHttp.call(this, options, ...args);
    };
    https.request = () => assert.fail('Autofix workflow attempted external HTTPS access');

    ext = require(entry);
    const subscriptions = [];
    session = ext.activate({ subscriptions });
    await session.ready;
    const status = session.statusProvider;
    const findings = host.views.get('codeatlas.findingsView');

    let count = 0;
    const test = async (name, fn) => { await fn(); count++; console.log(`✓ [Autofix] ${name}`); };

    await test('real review run completes and yields one deterministic finding', async () => {
      await host.command('startReview');
      assert.ok(status.status && status.status.run_id, 'startReview must register the run');
      await eventually(async () => {
        await host.poll();
        return Boolean(status.status) && status.status.status === 'completed';
      }, `review must complete; provider=${JSON.stringify(status.status)} messages=${JSON.stringify(host.messages)}`);
      assert.ok(findings.findings.length >= 1, 'expected a deterministic finding');
      assert.strictEqual(findings.findings[0].file, 'src/app.py');
    });
    const finding = findings.findings[0];

    await test('Generate Fix produces a draft that requires human approval', async () => {
      await host.command('generateFix', finding);
      const { body: proposal } = await getJson(port, `/reviews/${status.status.run_id}/findings/${finding.id}/fix-proposal`);
      assert.strictEqual(proposal.generation_status, 'draft_ready');
      assert.strictEqual(proposal.approval_required, true);
      assert.strictEqual(proposal.target_files[0], 'src/app.py');
      assert.ok(!fs.readFileSync(path.join(root, 'src/app.py'), 'utf8').includes('TODO(codeatlas)'),
        'generation must never modify the workspace');
    });
    const proposalId = (await getJson(port, `/reviews/${status.status.run_id}/findings/${finding.id}/fix-proposal`)).body.proposal_id;

    await test('Apply is refused while the proposal is not validated', async () => {
      await host.command('applyFix', finding);
      assert.ok(host.messages.some(m => /only a FixProposal that passed isolated sandbox validation/i.test(m.text)),
        `unexpected messages: ${JSON.stringify(host.messages.slice(-3))}`);
      assert.strictEqual(git(root, 'status', '--porcelain'), '');
    });

    await test('approve and validate run the real isolated sandbox; nothing is applied', async () => {
      const validateToken = await child.control('mint_fix_token', { proposal_id: proposalId, operation: 'validate' });
      host.input(validateToken);
      await host.command('approveFixForValidation', finding);
      assert.ok(host.messages.some(m => /approved for isolated sandbox validation/.test(m.text)));
      host.input(validateToken);
      await host.command('validateFixProposal', finding);
      assert.ok(host.messages.some(m => /validated in an isolated sandbox/.test(m.text)));
      assert.strictEqual(git(root, 'status', '--porcelain'), '');
    });

    await test('a validate token is rejected at apply time and nothing changes', async () => {
      const validateToken = await child.control('mint_fix_token', { proposal_id: proposalId, operation: 'validate' });
      host.choice('Apply Fix');
      host.input(validateToken);
      await host.command('applyFix', finding);
      assert.ok(host.messages.some(m => /Apply rejected/.test(m.text)));
      assert.ok(!fs.readFileSync(path.join(root, 'src/app.py'), 'utf8').includes('TODO(codeatlas)'));
    });

    await test('Apply Validated Fix requires modal confirmation and an apply-scoped token', async () => {
      // Without the modal choice the flow must abort before any token prompt.
      const inputsBefore = host.inputs.length;
      await host.command('applyFix', finding);
      assert.ok(!host.inputs.slice(inputsBefore).some(i => /apply-scoped/.test(i?.prompt || '')),
        'token prompt must not appear without explicit confirmation');
      const applyToken = await child.control('mint_fix_token', { proposal_id: proposalId, operation: 'apply' });
      host.choice('Apply Fix');
      host.input(applyToken);
      await host.command('applyFix', finding);
      assert.ok(host.messages.some(m => /Applied validated fix to src\/app\.py/.test(m.text)),
        `unexpected messages: ${JSON.stringify(host.messages.slice(-3))}`);
      assert.ok(fs.readFileSync(path.join(root, 'src/app.py'), 'utf8').includes('TODO(codeatlas)'));
      assert.ok(git(root, 'status', '--porcelain').includes('M src/app.py'));
      assert.strictEqual(git(root, 'rev-parse', 'HEAD'), headBefore);
    });

    await test('apply history shows the apply event with bounded metadata', async () => {
      host.pick(items => items[0] && { chosen: items[0] });
      await host.command('showApplyHistory', finding);
      assert.ok(host.picks.some(p => (p.items || []).some(item => /^applied - /.test(item.label))));
      assert.ok(host.picks.every(p => (p.items || []).every(item => !item.detail.includes('CAT-APP'))));
    });

    await test('Revert restores the exact pre-apply bytes and blocks a second revert', async () => {
      host.choice('Revert Fix');
      await host.command('revertAppliedFix', finding);
      assert.ok(!fs.readFileSync(path.join(root, 'src/app.py'), 'utf8').includes('TODO(codeatlas)'));
      assert.strictEqual(git(root, 'status', '--porcelain'), '');
      assert.strictEqual(git(root, 'rev-parse', 'HEAD'), headBefore);
      await host.command('revertAppliedFix', finding);
      assert.ok(host.messages.some(m => /no applied fix available to revert/.test(m.text)));
    });

    await test('apply history records the full applied->reverted chain', async () => {
      host.pick(items => items[items.length - 1]);
      await host.command('showApplyHistory', finding);
      const historyPick = host.picks.at(-1);
      assert.deepStrictEqual((historyPick.items || []).map(item => item.label.split(' - ')[0]), ['applied', 'reverted']);
    });

    await test('tokens never reach any observable UI surface', async () => {
      const surfaces = JSON.stringify({ messages: host.messages, inputs: host.inputs, picks: host.picks,
        updates: host.updates, clipboard: host.clipboard });
      assert.ok(!surfaces.includes('CAT-APP-'), 'an approval token leaked to a UI surface');
      const { audit } = await child.control('audit');
      assert.ok(audit.some(a => a.path.endsWith('/fix-proposal/apply')));
      assert.ok(audit.some(a => a.path.endsWith('/fix-proposal/revert')));
      assert.ok(audit.every(a => a.status < 500));
    });

    console.log(`All ${count} Phase 11C-F compiled-runtime autofix workflow tests passed.`);
  } finally {
    try { if (ext) await ext.deactivate(); } finally {
      http.request = realHttp; https.request = realHttps;
      host?.restore();
      for (const child of children) {
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
        await child.closed;
      }
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
}

runTests().catch(error => { console.error('Phase 11C-F autofix workflow failed:', error); process.exitCode = 1; });
