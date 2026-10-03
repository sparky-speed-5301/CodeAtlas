/**
 * @deprecated Legacy Phase 10C JavaScript implementation.
 * Retained temporarily for comparison.
 * The packaged extension loads the TypeScript-compiled output from ./out/profiles.js.
 */
// Configuration is data only. Never scan source files or persist profile contents.
const fs = require('fs');
const path = require('path');

const DEFAULT_PROFILE = Object.freeze({
  provider: 'mock', model: '', timeout: 30, maxFindings: 50,
  enableLiveReviewer: false, enablePatchSuggestions: false,
  enableTestExecution: false, enableFullSuiteExecution: false,
  githubDryRun: true, commentMode: 'summary',
});
const ALIASES = {
  max_findings: 'maxFindings', enable_live_reviewer: 'enableLiveReviewer',
  enable_patch_suggestions: 'enablePatchSuggestions', enable_test_execution: 'enableTestExecution',
  enable_full_suite: 'enableFullSuiteExecution', github_dry_run: 'githubDryRun', comment_mode: 'commentMode',
};
const ALLOWED_PROFILE_KEYS = new Set([...Object.keys(DEFAULT_PROFILE), ...Object.keys(ALIASES)]);
const FORBIDDEN_PROFILE_KEYS = new Set(['autoapply', 'allowapply', 'apply', 'merge', 'allowmerge', 'automerge']);
const SECRET_FIELD = /key|token|secret|cred|password|prompt|auth|output/i;
const SECRET_VALUE = /(?:sk-|gh[pousr]_|github_pat_|CAT-APP-|Bearer\s|AKIA|-----BEGIN)/i;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function validateProfileName(name) {
  if (typeof name !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(name) ||
      SECRET_VALUE.test(name) || ['__proto__', 'constructor', 'prototype'].includes(name)) {
    throw new Error('Invalid configuration profile name. Use a short, non-secret identifier.');
  }
  return name;
}

function validateProfile(name, rawProfile) {
  validateProfileName(name);
  if (!object(rawProfile)) throw new Error('Profile must be a configuration object.');
  const normalized = {};
  for (const [key, value] of Object.entries(rawProfile)) {
    if (FORBIDDEN_PROFILE_KEYS.has(key.replace(/[_-]/g, '').toLowerCase())) {
      throw new Error('Profile automatic patch application or merge is not permitted.');
    }
    if (SECRET_FIELD.test(key)) throw new Error('Profile secret-like field is prohibited.');
    if (!ALLOWED_PROFILE_KEYS.has(key)) throw new Error('Profile contains an unknown configuration field.');
    const field = ALIASES[key] || key;
    if (Object.hasOwn(normalized, field)) throw new Error('Profile contains duplicate field aliases.');
    let valid = false;
    if (field === 'provider') valid = ['mock', 'live'].includes(value);
    else if (field === 'commentMode') valid = ['summary', 'inline', 'both'].includes(value);
    else if (field === 'timeout') valid = typeof value === 'number' && Number.isFinite(value) && value >= 0.1 && value <= 300;
    else if (field === 'maxFindings') valid = Number.isInteger(value) && value >= 1 && value <= 200;
    else if (field === 'model') valid = typeof value === 'string' && value.length <= 128 &&
      /^(?:[a-zA-Z0-9][a-zA-Z0-9_./:-]*)?$/.test(value) && !SECRET_VALUE.test(value);
    else valid = typeof value === 'boolean';
    // Never include untrusted names, keys, values, or parser errors in diagnostics.
    if (!valid) throw new Error('Invalid profile field value or secret-like credential value.');
    normalized[field] = value;
  }
  return normalized;
}

function configurationPath(root, relativePath) {
  if (typeof relativePath !== 'string' || !/^\.codeatlas\/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*\.json$/.test(relativePath)) {
    throw new Error('Configuration path must name a JSON file directly inside .codeatlas/.');
  }
  return path.join(root, relativePath);
}

function readWorkspaceConfiguration(root, relativePath) {
  if (!root) return undefined;
  const filename = configurationPath(root, relativePath);
  let fd;
  try {
    const dir = fs.lstatSync(path.join(root, '.codeatlas'));
    if (dir.isSymbolicLink() || !dir.isDirectory()) throw new Error();
    const stat = fs.lstatSync(filename);
    if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 65536) throw new Error();
    fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    const opened = fs.fstatSync(fd);
    if (!opened.isFile() || opened.ino !== stat.ino || opened.dev !== stat.dev) throw new Error();
    const buffer = Buffer.alloc(65537);
    const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
    if (size > 65536) throw new Error();
    return JSON.parse(buffer.subarray(0, size).toString('utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return undefined;
    throw new Error('Cannot load CodeAtlas configuration: expected bounded JSON in a regular, non-symlink file.');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

class ProfileManager {
  constructor(workspaceRoot = null, userProfiles = {}, baseSettings = {}) {
    this.workspaceRoot = workspaceRoot;
    this.userProfiles = userProfiles;
    this.baseSettings = baseSettings;
    this.profilePath = '.codeatlas/profiles.json';
    this.activeProfileName = 'default';
  }

  loadProfiles(customPath = this.profilePath) {
    configurationPath(this.workspaceRoot || '.', customPath);
    const defaults = { ...DEFAULT_PROFILE, ...validateProfile('default', this.baseSettings) };
    const profiles = Object.assign(Object.create(null), { default: defaults });
    const workspace = readWorkspaceConfiguration(this.workspaceRoot, customPath);
    for (const source of [this.userProfiles, workspace === undefined ? {} : workspace]) {
      if (!object(source) || Object.keys(source).length > 100) throw new Error('Profiles must be an object with at most 100 named profiles.');
      for (const [name, value] of Object.entries(source)) {
        profiles[validateProfileName(name)] = { ...defaults, ...validateProfile(name, value) };
      }
    }
    return profiles;
  }

  getActiveProfile(profiles = this.loadProfiles()) {
    validateProfileName(this.activeProfileName);
    if (!Object.hasOwn(profiles, this.activeProfileName)) throw new Error('Active configuration profile was not found. Select an existing profile.');
    return profiles[this.activeProfileName];
  }

  select(name, profiles = this.loadProfiles()) {
    validateProfileName(name);
    if (!Object.hasOwn(profiles, name)) throw new Error('Configuration profile was not found.');
    this.activeProfileName = name;
    return profiles[name];
  }
}

module.exports = { ProfileManager, validateProfile, validateProfileName, DEFAULT_PROFILE,
  ALLOWED_PROFILE_KEYS, FORBIDDEN_PROFILE_KEYS, readWorkspaceConfiguration, configurationPath };
