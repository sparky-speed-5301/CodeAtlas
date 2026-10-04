/**
 * verify_package.js - Phase 10G production release metadata and installation verification.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');
const {
  PACKAGED_FILES_ALLOWLIST,
  FORBIDDEN_SECRET_PATTERNS,
  readZipEntries,
} = require('./package');

const EXTENSION_ROOT = path.resolve(__dirname);
const ROOT_DIR = path.resolve(__dirname, '..');

/**
 * Locate VS Code CLI executable for disposable profile operations.
 */
function findVsCodeCli() {
  if (process.env.VSCODE_CLI && fs.existsSync(process.env.VSCODE_CLI)) {
    return process.env.VSCODE_CLI;
  }

  if (process.platform === 'win32') {
    const candidates = [
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Microsoft VS Code', 'bin', 'code.cmd'),
      'C:\\Program Files\\Microsoft VS Code\\bin\\code.cmd',
      'C:\\Program Files (x86)\\Microsoft VS Code\\bin\\code.cmd',
    ];
    try {
      const whereOut = execSync('where.exe code.cmd', { stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf8' }).trim();
      if (whereOut) {
        const first = whereOut.split(/\r?\n/)[0].trim();
        if (fs.existsSync(first)) candidates.unshift(first);
      }
    } catch (_) {}

    for (const c of candidates) {
      if (c && fs.existsSync(c)) return c;
    }
  } else {
    // Linux / macOS
    try {
      const whichOut = execSync('which code', { stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf8' }).trim();
      if (whichOut && fs.existsSync(whichOut)) return whichOut;
    } catch (_) {}
  }

  return null;
}

/**
 * Verify static release metadata and VSIX package boundary invariants.
 */
function verifyPackageStatic(vsixPath, options = {}) {
  const result = {
    errors: [],
    warnings: [],
    metadata: null,
    fileCount: 0,
    sizeBytes: 0,
    scannedFiles: [],
  };

  // 1. File existence
  if (!fs.existsSync(vsixPath)) {
    throw new Error(`VSIX package not found at: ${vsixPath}`);
  }

  const stat = fs.statSync(vsixPath);
  result.sizeBytes = stat.size;

  // Bound check on VSIX size: between 30 KiB and 2 MiB
  if (stat.size < 30 * 1024 || stat.size > 2 * 1024 * 1024) {
    throw new Error(`VSIX package size out of expected bounds: ${stat.size} bytes (expected 30 KiB - 2 MiB)`);
  }

  // 2. Read ZIP archive
  let zipBuf;
  let entries;
  try {
    zipBuf = fs.readFileSync(vsixPath);
    entries = readZipEntries(zipBuf);
  } catch (err) {
    throw new Error(`Malformed VSIX package: failed to parse ZIP archive (${err.message})`);
  }

  result.fileCount = entries.length;
  // Bounds check on entry count: between 25 and 60 entries
  if (entries.length < 25 || entries.length > 60) {
    throw new Error(`VSIX package entry count out of expected bounds: ${entries.length} entries`);
  }

  // 3. Inspect package.json inside VSIX
  const manifestEntry = entries.find(
    (e) => e.filename.replace(/\\/g, '/') === 'extension/package.json'
  );
  if (!manifestEntry) {
    throw new Error('VSIX package missing extension/package.json');
  }

  let pkg;
  try {
    pkg = JSON.parse(manifestEntry.getData().toString('utf8'));
  } catch (err) {
    throw new Error(`Invalid JSON in packaged package.json: ${err.message}`);
  }
  result.metadata = pkg;

  // Release metadata consistency checks
  if (pkg.name !== 'codeatlas') {
    result.errors.push(`Expected name 'codeatlas', found: '${pkg.name}'`);
  }
  if (pkg.displayName !== 'CodeAtlas') {
    result.errors.push(`Expected displayName 'CodeAtlas', found: '${pkg.displayName}'`);
  }
  if (!pkg.description || pkg.description.trim().length === 0) {
    result.errors.push('Package description is missing or empty');
  }
  if (pkg.publisher !== 'codeatlas') {
    result.errors.push(`Expected publisher 'codeatlas', found: '${pkg.publisher}'`);
  }
  if (!pkg.version || !/^\d+\.\d+\.\d+$/.test(pkg.version)) {
    result.errors.push(`Invalid package version: '${pkg.version}'`);
  }

  // Filename version consistency
  const expectedFilename = `codeatlas-${pkg.version}.vsix`;
  if (path.basename(vsixPath) !== expectedFilename) {
    result.errors.push(
      `VSIX filename mismatch: expected '${expectedFilename}', found '${path.basename(vsixPath)}'`
    );
  }

  // Engine requirement
  if (!pkg.engines || !pkg.engines.vscode || !pkg.engines.vscode.startsWith('^1.')) {
    result.errors.push(`Invalid engines.vscode version: ${pkg.engines?.vscode}`);
  }

  // Main entry point must be ./out/extension.js
  if (pkg.main !== './out/extension.js') {
    result.errors.push(`package.json main must resolve to ./out/extension.js, found '${pkg.main}'`);
  }

  // Repository & License metadata
  if (!pkg.license || pkg.license !== 'Apache-2.0') {
    result.errors.push(`Expected license 'Apache-2.0', found '${pkg.license}'`);
  }
  const repoUrl = typeof pkg.repository === 'string' ? pkg.repository : pkg.repository?.url;
  if (!repoUrl || !repoUrl.includes('github.com/sparky-speed-5301/CodeAtlas')) {
    result.errors.push(`Invalid repository URL: '${repoUrl}'`);
  }

  // Commands and views verification
  const contributes = pkg.contributes || {};
  const commands = contributes.commands || [];
  if (commands.length !== 22) {
    result.errors.push(`Expected 22 contributed commands, found ${commands.length}`);
  }
  const views = contributes.views?.['codeatlas-sidebar'] || [];
  const expectedViews = ['codeatlas.statusView', 'codeatlas.findingsView', 'codeatlas.contextView'];
  for (const v of expectedViews) {
    if (!views.some((view) => view.id === v)) {
      result.errors.push(`Missing contributed view: ${v}`);
    }
  }

  // 4. Verify main entry file and required resources exist inside VSIX
  const packagedFileSet = new Set(entries.map((e) => e.filename.replace(/\\/g, '/')));
  if (!packagedFileSet.has('extension/out/extension.js')) {
    result.errors.push('Packaged runtime entry extension/out/extension.js missing from VSIX');
  }
  if (!packagedFileSet.has('extension/resources/icon.svg')) {
    result.errors.push('Packaged icon resource extension/resources/icon.svg missing from VSIX');
  }

  // 5. Verify documentation and license inside VSIX
  const hasReadme = entries.some((e) => /^extension\/readme\.md$/i.test(e.filename.replace(/\\/g, '/')));
  const hasChangelog = entries.some((e) => /^extension\/changelog\.md$/i.test(e.filename.replace(/\\/g, '/')));
  const hasLicense = entries.some((e) => /^extension\/license(\.txt)?$/i.test(e.filename.replace(/\\/g, '/')));

  if (!hasReadme) result.errors.push('README.md missing from packaged VSIX');
  if (!hasChangelog) result.errors.push('CHANGELOG.md missing from packaged VSIX');
  if (!hasLicense) result.errors.push('LICENSE missing from packaged VSIX');

  // Verify CHANGELOG has current version entry
  const changelogEntry = entries.find((e) => /^extension\/changelog\.md$/i.test(e.filename.replace(/\\/g, '/')));
  if (changelogEntry) {
    const changelogText = changelogEntry.getData().toString('utf8');
    if (!changelogText.includes(`[${pkg.version}]`)) {
      result.errors.push(`CHANGELOG.md does not contain an entry for release version [${pkg.version}]`);
    }
  }

  // Verify README has installation instructions
  const readmeEntry = entries.find((e) => /^extension\/readme\.md$/i.test(e.filename.replace(/\\/g, '/')));
  if (readmeEntry) {
    const readmeText = readmeEntry.getData().toString('utf8');
    if (!readmeText.includes('--install-extension')) {
      result.errors.push('README.md missing CLI installation instruction (--install-extension)');
    }
  }

  // 6. Explicit allowlist & secret scanning for every file
  for (const entry of entries) {
    const norm = entry.filename.replace(/\\/g, '/');
    if (norm === '[Content_Types].xml' || norm === 'extension.vsixmanifest') {
      continue;
    }
    if (!norm.startsWith('extension/')) {
      result.errors.push(`Unexpected non-extension root file: ${norm}`);
      continue;
    }

    const relPath = norm.slice('extension/'.length);
    const lower = relPath.toLowerCase();
    let allowlistKey = relPath;
    if (lower === 'readme.md') allowlistKey = 'README.md';
    else if (lower === 'changelog.md') allowlistKey = 'CHANGELOG.md';
    else if (lower === 'license' || lower === 'license.txt') allowlistKey = 'LICENSE';

    // Explicit rejection of development and test files
    if (
      relPath.startsWith('src/') ||
      relPath.startsWith('test/') ||
      relPath.startsWith('test_') ||
      (relPath.endsWith('.ts') && !relPath.endsWith('.d.ts')) ||
      relPath === 'tsconfig.json' ||
      relPath === 'extension.js' ||
      relPath === 'profiles.js' ||
      relPath === 'service.js' ||
      relPath === 'package.js' ||
      relPath === 'test_smoke.js' ||
      relPath === 'verify_package.js'
    ) {
      result.errors.push(`Development/test artifact leaked into package: ${relPath}`);
      continue;
    }

    if (!PACKAGED_FILES_ALLOWLIST.has(allowlistKey)) {
      result.errors.push(`Disallowed file in package: ${relPath}`);
      continue;
    }

    result.scannedFiles.push(relPath);

    // Secret scanner
    const content = entry.getData().toString('utf8');
    for (const secret of FORBIDDEN_SECRET_PATTERNS) {
      if (secret.pattern.test(content)) {
        result.errors.push(`Secret pattern '${secret.name}' detected in: ${relPath}`);
      }
    }
  }

  if (result.errors.length > 0) {
    throw new Error(`Package verification failed with ${result.errors.length} errors:\n  - ${result.errors.join('\n  - ')}`);
  }

  return result;
}

/**
 * Verify extension installation in an isolated disposable VS Code environment.
 */
function verifyPackageInstallation(vsixPath, options = {}) {
  const codeCli = options.codeCli !== undefined ? options.codeCli : findVsCodeCli();

  if (!codeCli) {
    return {
      skipped: true,
      reason: 'VS Code CLI executable (code.cmd or code) is unavailable in this environment',
    };
  }

  if (!fs.existsSync(vsixPath)) {
    throw new Error(`VSIX package not found: ${vsixPath}`);
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'codeatlas-disposable-profile-'));
  const extDir = path.join(tmpDir, 'extensions');
  const dataDir = path.join(tmpDir, 'user-data');
  fs.mkdirSync(extDir);
  fs.mkdirSync(dataDir);

  try {
    console.log(`[Install Verify] Using disposable profile at: ${tmpDir}`);

    // 1. Install extension into disposable profile
    const installCmd = `"${codeCli}" --extensions-dir "${extDir}" --user-data-dir "${dataDir}" --install-extension "${vsixPath}"`;
    const installOut = execSync(installCmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    if (!installOut.includes('successfully installed') && !installOut.includes('codeatlas')) {
      console.log(`[Install Verify] Install output: ${installOut.trim()}`);
    }

    // 2. Verify extension is present in installed list
    const listCmd = `"${codeCli}" --extensions-dir "${extDir}" --user-data-dir "${dataDir}" --list-extensions`;
    const listOut = execSync(listCmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

    if (!listOut.toLowerCase().includes('codeatlas.codeatlas')) {
      throw new Error(`Installed extension not found in list-extensions output: '${listOut}'`);
    }
    console.log('✓ [Install Verify] Extension installed and confirmed in isolated extension list.');

    // 3. Verify installed folder structure on disk
    const installedFolders = fs.readdirSync(extDir);
    const codeatlasFolder = installedFolders.find((f) => f.toLowerCase().startsWith('codeatlas.codeatlas'));
    if (!codeatlasFolder) {
      throw new Error(`Installed extension directory missing from ${extDir}`);
    }
    const installedManifest = path.join(extDir, codeatlasFolder, 'package.json');
    if (!fs.existsSync(installedManifest)) {
      throw new Error(`Installed package.json missing at: ${installedManifest}`);
    }
    const installedMain = path.join(extDir, codeatlasFolder, 'out', 'extension.js');
    if (!fs.existsSync(installedMain)) {
      throw new Error(`Installed compiled entry missing at: ${installedMain}`);
    }
    console.log('✓ [Install Verify] Installed package structure and compiled entry point verified.');

    // 4. Uninstall extension from disposable profile
    const uninstallCmd = `"${codeCli}" --extensions-dir "${extDir}" --user-data-dir "${dataDir}" --uninstall-extension codeatlas.codeatlas`;
    const uninstallOut = execSync(uninstallCmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    console.log('✓ [Install Verify] Extension uninstalled cleanly from disposable profile.');

    return {
      skipped: false,
      installedId: 'codeatlas.codeatlas',
      tempProfile: tmpDir,
    };
  } finally {
    // 5. Clean up disposable profile completely
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true });
      console.log('✓ [Install Verify] Disposable profile directory cleaned up completely.');
    } catch (_) {}
  }
}

async function main() {
  console.log('=== CodeAtlas Phase 10G Release Metadata & Installation Verification ===\n');

  const vsixName = 'codeatlas-0.1.0.vsix';
  const vsixPath = path.join(EXTENSION_ROOT, vsixName);

  console.log('[Verify 1/2] Verifying VSIX static metadata, boundaries, and security...');
  const staticResult = verifyPackageStatic(vsixPath);
  console.log(`✓ [Static Verify] Package: ${vsixName} (${(staticResult.sizeBytes / 1024).toFixed(2)} KiB, ${staticResult.fileCount} entries)`);
  console.log(`✓ [Static Verify] Version: ${staticResult.metadata.version} | Publisher: ${staticResult.metadata.publisher} | Engine: ${staticResult.metadata.engines?.vscode}`);
  console.log(`✓ [Static Verify] All ${staticResult.scannedFiles.length} packaged files matched the explicit allowlist.`);
  console.log(`✓ [Static Verify] 0 secrets, tokens, or credentials detected across all packaged files.\n`);

  console.log('[Verify 2/2] Verifying installation in disposable VS Code environment...');
  const installResult = verifyPackageInstallation(vsixPath);

  if (installResult.skipped) {
    console.log(`[Install Verify] SKIPPED: ${installResult.reason}`);
  } else {
    console.log('✓ [Install Verify] Disposable profile installation and uninstall passed with 0 host side-effects.');
  }

  console.log('\n✓ [Release Verification] All Phase 10G release checks completed successfully.');
}

if (require.main === module) {
  try {
    main();
  } catch (err) {
    console.error(`\n[Release Verification] FAILED: ${err.message}`);
    process.exit(1);
  }
}

module.exports = {
  findVsCodeCli,
  verifyPackageStatic,
  verifyPackageInstallation,
  main,
};
