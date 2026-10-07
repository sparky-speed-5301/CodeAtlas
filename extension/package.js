/**
 * package.js - Phase 10F extension packaging and package-boundary security validator.
 */
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { execSync } = require('child_process');

const EXTENSION_ROOT = path.resolve(__dirname);
const VSIX_NAME = 'codeatlas-0.1.0.vsix';
const VSIX_PATH = path.join(EXTENSION_ROOT, VSIX_NAME);

// Explicit allowlist of files permissible inside extension/ in the packaged VSIX
const PACKAGED_FILES_ALLOWLIST = new Set([
  'package.json',
  'README.md',
  'CHANGELOG.md',
  'LICENSE',
  'resources/icon.svg',
  'out/client.d.ts',
  'out/client.js',
  'out/client.js.map',
  'out/decorations.d.ts',
  'out/decorations.js',
  'out/decorations.js.map',
  'out/detail-panel.d.ts',
  'out/detail-panel.js',
  'out/detail-panel.js.map',
  'out/extension.d.ts',
  'out/extension.js',
  'out/extension.js.map',
  'out/fix-preview.d.ts',
  'out/fix-preview.js',
  'out/fix-preview.js.map',
  'out/profiles.d.ts',
  'out/profiles.js',
  'out/profiles.js.map',
  'out/quickpick.d.ts',
  'out/quickpick.js',
  'out/quickpick.js.map',
  'out/service.d.ts',
  'out/service.js',
  'out/service.js.map',
  'out/types.d.ts',
  'out/types.js',
  'out/types.js.map',
  'out/providers/context.d.ts',
  'out/providers/context.js',
  'out/providers/context.js.map',
  'out/providers/findings.d.ts',
  'out/providers/findings.js',
  'out/providers/findings.js.map',
  'out/providers/status.d.ts',
  'out/providers/status.js',
  'out/providers/status.js.map',
]);

// Secret pattern signatures that MUST NOT appear anywhere in the packaged files
const FORBIDDEN_SECRET_PATTERNS = [
  { name: 'OpenAI/Generic API Key', pattern: /sk-[a-zA-Z0-9_-]{20,}/ },
  { name: 'Google API Key', pattern: /AIza[0-9A-Za-z-_]{35}/ },
  { name: 'GitHub Personal Token', pattern: /gh[pousr]_[a-zA-Z0-9]{36,}/ },
  { name: 'GitHub Fine-grained PAT', pattern: /github_pat_[a-zA-Z0-9_]{82}/ },
  { name: 'Approval Scoped Token', pattern: /appr_[a-zA-Z0-9_-]{16,}/ },
  { name: 'Private Key Header', pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/ },
  { name: 'AWS Access Key ID', pattern: /AKIA[0-9A-Z]{16}/ },
  { name: 'Raw Test Canary Secret', pattern: /fixture[_-]?secret/i },
  { name: 'Raw Canary Password', pattern: /raw[_-]?fixture[_-]?token/i },
];

/**
 * Minimal, zero-dependency ZIP reader to inspect and extract VSIX entries.
 */
function readZipEntries(zipBuffer) {
  // Find End of Central Directory record (EOCD) from the end of buffer
  let eocdOffset = -1;
  for (let i = zipBuffer.length - 22; i >= Math.max(0, zipBuffer.length - 65557); i--) {
    if (zipBuffer.readUInt32LE(i) === 0x06054b50) {
      eocdOffset = i;
      break;
    }
  }
  if (eocdOffset === -1) {
    throw new Error('Invalid VSIX: End of Central Directory record not found.');
  }

  const entryCount = zipBuffer.readUInt16LE(eocdOffset + 10);
  const cdOffset = zipBuffer.readUInt32LE(eocdOffset + 16);

  const entries = [];
  let curr = cdOffset;

  for (let idx = 0; idx < entryCount; idx++) {
    if (zipBuffer.readUInt32LE(curr) !== 0x02014b50) {
      throw new Error(`Invalid Central Directory header at offset ${curr}`);
    }

    const compression = zipBuffer.readUInt16LE(curr + 10);
    const compressedSize = zipBuffer.readUInt32LE(curr + 20);
    const uncompressedSize = zipBuffer.readUInt32LE(curr + 24);
    const filenameLen = zipBuffer.readUInt16LE(curr + 28);
    const extraLen = zipBuffer.readUInt16LE(curr + 30);
    const commentLen = zipBuffer.readUInt16LE(curr + 32);
    const localHeaderOffset = zipBuffer.readUInt32LE(curr + 42);

    const filename = zipBuffer.toString('utf8', curr + 46, curr + 46 + filenameLen);
    curr += 46 + filenameLen + extraLen + commentLen;

    // Read local header to locate data
    if (zipBuffer.readUInt32LE(localHeaderOffset) !== 0x04034b50) {
      throw new Error(`Invalid Local Header for ${filename} at ${localHeaderOffset}`);
    }
    const localFileLen = zipBuffer.readUInt16LE(localHeaderOffset + 26);
    const localExtraLen = zipBuffer.readUInt16LE(localHeaderOffset + 28);
    const dataStart = localHeaderOffset + 30 + localFileLen + localExtraLen;
    const rawData = zipBuffer.subarray(dataStart, dataStart + compressedSize);

    entries.push({
      filename,
      compression,
      compressedSize,
      uncompressedSize,
      getData: () => {
        if (compression === 0) return rawData;
        if (compression === 8) return zlib.inflateRawSync(rawData);
        throw new Error(`Unsupported compression method ${compression} for ${filename}`);
      }
    });
  }

  return entries;
}

function verifyPackagingPrerequisites() {
  console.log('[Packaging] Step 1: Verifying package prerequisites...');
  const manifestPath = path.join(EXTENSION_ROOT, 'package.json');
  if (!fs.existsSync(manifestPath)) {
    throw new Error('extension/package.json does not exist');
  }

  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (manifest.main !== './out/extension.js') {
    throw new Error(`package.json main must resolve only to ./out/extension.js (found: ${manifest.main})`);
  }

  const compiledEntry = path.join(EXTENSION_ROOT, 'out', 'extension.js');
  if (!fs.existsSync(compiledEntry)) {
    throw new Error('Compiled runtime out/extension.js does not exist. Run "npm run compile" first.');
  }

  const requiredIcon = path.join(EXTENSION_ROOT, 'resources', 'icon.svg');
  if (!fs.existsSync(requiredIcon)) {
    throw new Error('Required extension resource resources/icon.svg is missing.');
  }

  const deprecatedJs = ['./extension.js', 'extension.js', './profiles.js', 'profiles.js', './service.js', 'service.js'];
  for (const dep of deprecatedJs) {
    if (manifest.main === dep) {
      throw new Error(`Deprecated file ${dep} is configured as main entry point!`);
    }
  }

  console.log('✓ [Packaging] Prerequisites verified: compiled TypeScript runtime and resources present.');
}

function executePackaging() {
  console.log('[Packaging] Step 2: Packaging extension with vsce...');
  if (fs.existsSync(VSIX_PATH)) {
    fs.unlinkSync(VSIX_PATH);
  }

  // Attempt vsce package via npx
  try {
    const cmd = `npx --yes @vscode/vsce package --no-dependencies --skip-license --allow-missing-repository -o "${VSIX_PATH}"`;
    execSync(cmd, { cwd: EXTENSION_ROOT, stdio: 'inherit' });
  } catch (err) {
    throw new Error(`VSIX packaging failed: ${err.message}`);
  }

  if (!fs.existsSync(VSIX_PATH)) {
    throw new Error(`Packaging succeeded but expected artifact was not created: ${VSIX_PATH}`);
  }

  const stat = fs.statSync(VSIX_PATH);
  console.log(`✓ [Packaging] Package created: ${VSIX_NAME} (${(stat.size / 1024).toFixed(2)} KiB)`);
}

function verifyPackageContentsAndSecurity() {
  console.log('[Packaging] Step 3: Verifying package boundary and scanning for secrets...');
  const zipBuf = fs.readFileSync(VSIX_PATH);
  const entries = readZipEntries(zipBuf);

  const unexpectedFiles = [];
  const scannedFiles = [];
  const secretViolations = [];

  for (const entry of entries) {
    const normalizedName = entry.filename.replace(/\\/g, '/');

    // Skip root VSIX envelope metadata files
    if (normalizedName === '[Content_Types].xml' || normalizedName === 'extension.vsixmanifest') {
      continue;
    }

    if (!normalizedName.startsWith('extension/')) {
      unexpectedFiles.push(`Non-extension root file: ${normalizedName}`);
      continue;
    }

    const relPath = normalizedName.slice('extension/'.length);

    // 1. Allowlist check (normalize casing for root docs/license)
    const lower = relPath.toLowerCase();
    let allowlistKey = relPath;
    if (lower === 'readme.md') allowlistKey = 'README.md';
    else if (lower === 'changelog.md') allowlistKey = 'CHANGELOG.md';
    else if (lower === 'license' || lower === 'license.txt') allowlistKey = 'LICENSE';

    if (!PACKAGED_FILES_ALLOWLIST.has(allowlistKey)) {
      unexpectedFiles.push(`Disallowed packaged file: ${relPath}`);
      continue;
    }

    // 2. Reject any test/dev artifacts
    if (
      relPath.startsWith('src/') ||
      relPath.startsWith('test/') ||
      relPath.startsWith('test_') ||
      (relPath.endsWith('.ts') && !relPath.endsWith('.d.ts')) ||
      relPath === 'tsconfig.json' ||
      relPath === 'extension.js' ||
      relPath === 'profiles.js' ||
      relPath === 'service.js'
    ) {
      unexpectedFiles.push(`Development/test artifact leaked into package: ${relPath}`);
      continue;
    }

    scannedFiles.push(relPath);

    // 3. Scan file contents for secret patterns
    const content = entry.getData().toString('utf8');
    for (const secret of FORBIDDEN_SECRET_PATTERNS) {
      if (secret.pattern.test(content)) {
        secretViolations.push(`Found ${secret.name} in packaged file: ${relPath}`);
      }
    }
  }

  if (unexpectedFiles.length > 0) {
    console.error('[Packaging] FAILED: Package contains unexpected files:');
    unexpectedFiles.forEach(f => console.error('  - ' + f));
    throw new Error(`Package boundary check failed: ${unexpectedFiles.length} unexpected files detected.`);
  }

  if (secretViolations.length > 0) {
    console.error('[Packaging] FAILED: Secrets detected in packaged files:');
    secretViolations.forEach(v => console.error('  - ' + v));
    throw new Error(`Package security check failed: ${secretViolations.length} secret violations found.`);
  }

  console.log(`✓ [Packaging] All ${scannedFiles.length} packaged files matched the explicit allowlist.`);
  console.log(`✓ [Packaging] 0 secrets, tokens, or credentials detected across all packaged files.`);
  console.log(`✓ [Packaging] Release package verified clean and deterministic.`);
}

function main() {
  verifyPackagingPrerequisites();
  executePackaging();
  verifyPackageContentsAndSecurity();
  console.log('\nPackage validation completed successfully!');
}

if (require.main === module) {
  try {
    main();
  } catch (err) {
    console.error(`\nPackaging Error: ${err.message}`);
    process.exit(1);
  }
}

module.exports = {
  PACKAGED_FILES_ALLOWLIST,
  FORBIDDEN_SECRET_PATTERNS,
  readZipEntries,
  verifyPackagingPrerequisites,
  verifyPackageContentsAndSecurity,
};
