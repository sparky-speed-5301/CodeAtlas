/**
 * test_packaging.js - Phase 10G Packaging and Installation Verification Tests.
 *
 * Covers all 11 required scenarios:
 * 1. Valid package passes verification
 * 2. Missing VSIX rejected
 * 3. Malformed VSIX rejected
 * 4. Version mismatch rejected
 * 5. Missing main entry rejected
 * 6. Missing icon/resource rejected
 * 7. Unexpected development file rejected
 * 8. Secret detected in package rejected
 * 9. Invalid package metadata rejected
 * 10. Installation CLI unavailable reports skipped
 * 11. Disposable profile cleanup
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { verifyPackageStatic, verifyPackageInstallation } = require('./verify_package');

const EXTENSION_ROOT = path.resolve(__dirname);
const VSIX_NAME = 'codeatlas-0.1.0.vsix';
const VSIX_PATH = path.join(EXTENSION_ROOT, VSIX_NAME);

/**
 * Minimal ZIP builder for synthetic test cases.
 */
function createSyntheticZip(entries) {
  const localHeaders = [];
  const cdHeaders = [];
  let offset = 0;

  for (const entry of entries) {
    const data = Buffer.isBuffer(entry.content)
      ? entry.content
      : Buffer.from(entry.content || '', 'utf8');
    const nameBuf = Buffer.from(entry.name, 'utf8');

    // Local file header (30 bytes + name + data)
    const lh = Buffer.alloc(30 + nameBuf.length + data.length);
    lh.writeUInt32LE(0x04034b50, 0); // signature
    lh.writeUInt16LE(20, 4); // version needed
    lh.writeUInt16LE(0, 6); // flags
    lh.writeUInt16LE(0, 8); // compression: 0 (store)
    lh.writeUInt16LE(0, 10); // time
    lh.writeUInt16LE(0, 12); // date
    lh.writeUInt32LE(0, 14); // crc32 (store)
    lh.writeUInt32LE(data.length, 18); // comp size
    lh.writeUInt32LE(data.length, 22); // uncomp size
    lh.writeUInt16LE(nameBuf.length, 26);
    lh.writeUInt16LE(0, 28);
    nameBuf.copy(lh, 30);
    data.copy(lh, 30 + nameBuf.length);

    // Central directory header (46 bytes + name)
    const cd = Buffer.alloc(46 + nameBuf.length);
    cd.writeUInt32LE(0x02014b50, 0); // signature
    cd.writeUInt16LE(20, 4);
    cd.writeUInt16LE(20, 6);
    cd.writeUInt16LE(0, 8);
    cd.writeUInt16LE(0, 10);
    cd.writeUInt16LE(0, 12);
    cd.writeUInt16LE(0, 14);
    cd.writeUInt32LE(0, 16);
    cd.writeUInt32LE(data.length, 20);
    cd.writeUInt32LE(data.length, 24);
    cd.writeUInt16LE(nameBuf.length, 28);
    cd.writeUInt16LE(0, 30);
    cd.writeUInt16LE(0, 32);
    cd.writeUInt16LE(0, 34);
    cd.writeUInt16LE(0, 36);
    cd.writeUInt32LE(0, 38);
    cd.writeUInt32LE(offset, 42);
    nameBuf.copy(cd, 46);

    localHeaders.push(lh);
    cdHeaders.push(cd);
    offset += lh.length;
  }

  const localBuf = Buffer.concat(localHeaders);
  const cdBuf = Buffer.concat(cdHeaders);

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0); // EOCD signature
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(cdBuf.length, 12);
  eocd.writeUInt32LE(localBuf.length, 16);
  eocd.writeUInt16LE(0, 20);

  return Buffer.concat([localBuf, cdBuf, eocd]);
}

/**
 * Creates a base set of minimal valid entries needed for synthetic VSIX tests.
 */
function createBaseSyntheticEntries(overrides = {}) {
  const rootPkg = JSON.parse(fs.readFileSync(path.join(EXTENSION_ROOT, 'package.json'), 'utf8'));
  const pkg = {
    name: 'codeatlas',
    displayName: 'CodeAtlas',
    description: 'Evidence-first, repository-aware code review for VS Code',
    version: '0.1.0',
    publisher: 'codeatlas',
    engines: { vscode: '^1.85.0' },
    main: './out/extension.js',
    license: 'Apache-2.0',
    repository: { type: 'git', url: 'https://github.com/sparky-speed-5301/CodeAtlas.git' },
    contributes: rootPkg.contributes,
    ...overrides.packageJson,
  };

  const files = [
    { name: '[Content_Types].xml', content: '<Types/>' },
    { name: 'extension.vsixmanifest', content: '<PackageManifest/>' },
    { name: 'extension/package.json', content: JSON.stringify(pkg, null, 2) },
    { name: 'extension/README.md', content: '# CodeAtlas\ncode --install-extension codeatlas-0.1.0.vsix\n' },
    { name: 'extension/CHANGELOG.md', content: '## [0.1.0] - Initial Release\n' },
    { name: 'extension/LICENSE', content: 'Apache License 2.0\n' },
    { name: 'extension/resources/icon.svg', content: '<svg></svg>' },
    { name: 'extension/out/extension.js', content: 'exports.activate=function(){};' },
    { name: 'extension/out/extension.d.ts', content: 'export declare function activate(): void;' },
    { name: 'extension/out/extension.js.map', content: '{"version":3}' },
  ];

  // Fill up with dummy out/ files to meet the 25+ entries bound
  const modules = [
    'client', 'service', 'profiles', 'types', 'decorations',
    'quickpick', 'detail-panel', 'providers/status', 'providers/findings', 'providers/context'
  ];
  for (const m of modules) {
    files.push({ name: `extension/out/${m}.js`, content: 'module.exports={};' });
    files.push({ name: `extension/out/${m}.d.ts`, content: 'export {};' });
    files.push({ name: `extension/out/${m}.js.map`, content: '{"version":3}' });
  }

  // Pad to reach at least 40 entries
  while (files.length < 38) {
    files.push({ name: `extension/out/dummy_${files.length}.js`, content: '' });
  }

  return files;
}

function runTests() {
  console.log('Running Phase 10G Packaging and Installation Verification Tests...\n');
  const tempFiles = [];

  function makeTempVsix(filename, zipBuffer) {
    const p = path.join(os.tmpdir(), filename);
    // Pad buffer if needed to meet minimum 30 KiB bound
    let finalBuf = zipBuffer;
    if (finalBuf.length < 32 * 1024) {
      // Append zero-padding comment at the end (valid in ZIP spec)
      const pad = Buffer.alloc(32 * 1024 - finalBuf.length, 0);
      finalBuf = Buffer.concat([finalBuf, pad]);
    }
    fs.writeFileSync(p, finalBuf);
    tempFiles.push(p);
    return p;
  }

  try {
    // 1. Valid package passes verification
    const res = verifyPackageStatic(VSIX_PATH);
    assert.strictEqual(res.errors.length, 0);
    assert.strictEqual(res.metadata.name, 'codeatlas');
    assert.strictEqual(res.metadata.version, '0.1.0');
    console.log('✓ [Packaging Test] 1. Valid package passes verification');

    // 2. Missing VSIX rejected
    assert.throws(
      () => verifyPackageStatic(path.join(EXTENSION_ROOT, 'nonexistent-missing.vsix')),
      /VSIX package not found/
    );
    console.log('✓ [Packaging Test] 2. Missing VSIX rejected');

    // 3. Malformed VSIX rejected
    const malformedPath = makeTempVsix('codeatlas-0.1.0.vsix', Buffer.from('NOT_A_VALID_ZIP_ARCHIVE_DATA'));
    assert.throws(
      () => verifyPackageStatic(malformedPath),
      /Malformed VSIX package/
    );
    console.log('✓ [Packaging Test] 3. Malformed VSIX rejected');

    // 4. Version mismatch rejected
    const mismatchPath = makeTempVsix('codeatlas-9.9.9.vsix', fs.readFileSync(VSIX_PATH));
    assert.throws(
      () => verifyPackageStatic(mismatchPath),
      /VSIX filename mismatch/
    );
    console.log('✓ [Packaging Test] 4. Version mismatch rejected');

    // 5. Missing main entry rejected
    const noMainEntries = createBaseSyntheticEntries().filter((e) => e.name !== 'extension/out/extension.js');
    const noMainVsix = makeTempVsix('codeatlas-0.1.0.vsix', createSyntheticZip(noMainEntries));
    assert.throws(
      () => verifyPackageStatic(noMainVsix),
      /Packaged runtime entry extension\/out\/extension\.js missing/
    );
    console.log('✓ [Packaging Test] 5. Missing main entry rejected');

    // 6. Missing icon/resource rejected
    const noIconEntries = createBaseSyntheticEntries().filter((e) => e.name !== 'extension/resources/icon.svg');
    const noIconVsix = makeTempVsix('codeatlas-0.1.0.vsix', createSyntheticZip(noIconEntries));
    assert.throws(
      () => verifyPackageStatic(noIconVsix),
      /Packaged icon resource extension\/resources\/icon\.svg missing/
    );
    console.log('✓ [Packaging Test] 6. Missing icon/resource rejected');

    // 7. Unexpected development file rejected
    const devEntries = createBaseSyntheticEntries();
    devEntries.push({ name: 'extension/src/leaked_source.ts', content: 'export const x = 1;' });
    const devVsix = makeTempVsix('codeatlas-0.1.0.vsix', createSyntheticZip(devEntries));
    assert.throws(
      () => verifyPackageStatic(devVsix),
      /Development\/test artifact leaked into package/
    );
    console.log('✓ [Packaging Test] 7. Unexpected development file rejected');

    // 8. Secret detected rejected
    const secretEntries = createBaseSyntheticEntries();
    secretEntries.push({
      name: 'extension/out/service.js',
      content: 'const key = "sk-1234567890abcdef1234567890abcdef";'
    });
    const secretVsix = makeTempVsix('codeatlas-0.1.0.vsix', createSyntheticZip(secretEntries));
    assert.throws(
      () => verifyPackageStatic(secretVsix),
      /Secret pattern.*detected/
    );
    console.log('✓ [Packaging Test] 8. Secret detected in package rejected');

    // 9. Invalid package metadata rejected
    const badMetaEntries = createBaseSyntheticEntries({
      packageJson: { publisher: 'unauthorized-publisher', license: 'GPL-3.0' }
    });
    const badMetaVsix = makeTempVsix('codeatlas-0.1.0.vsix', createSyntheticZip(badMetaEntries));
    assert.throws(
      () => verifyPackageStatic(badMetaVsix),
      /Expected publisher 'codeatlas'/
    );
    console.log('✓ [Packaging Test] 9. Invalid package metadata rejected');

    // 10. Installation CLI unavailable reports skipped
    const cliUnavailableRes = verifyPackageInstallation(VSIX_PATH, { codeCli: null });
    assert.strictEqual(cliUnavailableRes.skipped, true);
    assert.ok(cliUnavailableRes.reason.includes('unavailable'));
    console.log('✓ [Packaging Test] 10. Installation CLI unavailable reports skipped with reason');

    // 11. Disposable profile cleanup
    let observedTmpDir = null;
    const mockFailingCli = 'nonexistent_failing_code_cli_executable';
    try {
      verifyPackageInstallation(VSIX_PATH, { codeCli: mockFailingCli });
    } catch (_) {
      // Error expected during mock execution
    }
    // Verify no temporary directory leaking in os.tmpdir() with our prefix
    const leftoverDirs = fs.readdirSync(os.tmpdir()).filter((f) => f.startsWith('codeatlas-disposable-profile-'));
    assert.strictEqual(leftoverDirs.length, 0, 'No disposable profile directories should leak on disk');
    console.log('✓ [Packaging Test] 11. Disposable profile cleanup verified');

    console.log('\nAll 11 Phase 10G Packaging and Installation Verification Tests Passed!');
  } finally {
    // Cleanup temporary VSIX files
    for (const p of tempFiles) {
      try {
        fs.unlinkSync(p);
      } catch (_) {}
    }
  }
}

if (require.main === module) {
  runTests();
}

module.exports = { runTests };
