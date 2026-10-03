/**
 * Configuration is data only. Never scan source files or persist profile contents.
 */
import * as fs from 'fs';
import * as path from 'path';
import { Profile, ProfileCommentMode, ProfileProvider } from './types';

export const DEFAULT_PROFILE: Readonly<Profile> = Object.freeze({
  provider: 'mock' as ProfileProvider,
  model: '',
  timeout: 30,
  maxFindings: 50,
  enableLiveReviewer: false,
  enablePatchSuggestions: false,
  enableTestExecution: false,
  enableFullSuiteExecution: false,
  githubDryRun: true,
  commentMode: 'summary' as ProfileCommentMode,
});

export const ALIASES: Record<string, keyof Profile> = {
  max_findings: 'maxFindings',
  enable_live_reviewer: 'enableLiveReviewer',
  enable_patch_suggestions: 'enablePatchSuggestions',
  enable_test_execution: 'enableTestExecution',
  enable_full_suite: 'enableFullSuiteExecution',
  github_dry_run: 'githubDryRun',
  comment_mode: 'commentMode',
};

export const ALLOWED_PROFILE_KEYS = new Set<string>([
  ...Object.keys(DEFAULT_PROFILE),
  ...Object.keys(ALIASES),
]);

export const FORBIDDEN_PROFILE_KEYS = new Set<string>([
  'autoapply',
  'allowapply',
  'apply',
  'merge',
  'allowmerge',
  'automerge',
]);

const SECRET_FIELD = /key|token|secret|cred|password|prompt|auth|output/i;
const SECRET_VALUE = /(?:sk-|gh[pousr]_|github_pat_|CAT-APP-|Bearer\s|AKIA|-----BEGIN)/i;

const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

export function validateProfileName(name: unknown): string {
  if (
    typeof name !== 'string' ||
    !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(name) ||
    SECRET_VALUE.test(name) ||
    ['__proto__', 'constructor', 'prototype'].includes(name)
  ) {
    throw new Error('Invalid configuration profile name. Use a short, non-secret identifier.');
  }
  return name;
}

export function validateProfile(name: unknown, rawProfile: unknown): Profile {
  validateProfileName(name);
  if (!isObject(rawProfile)) throw new Error('Profile must be a configuration object.');
  const normalized: Partial<Profile> = {};

  for (const [key, value] of Object.entries(rawProfile)) {
    if (FORBIDDEN_PROFILE_KEYS.has(key.replace(/[_-]/g, '').toLowerCase())) {
      throw new Error('Profile automatic patch application or merge is not permitted.');
    }
    if (SECRET_FIELD.test(key)) throw new Error('Profile secret-like field is prohibited.');
    if (!ALLOWED_PROFILE_KEYS.has(key)) throw new Error('Profile contains an unknown configuration field.');

    const field = (ALIASES[key] || key) as keyof Profile;
    if (Object.hasOwn(normalized, field)) throw new Error('Profile contains duplicate field aliases.');

    let valid = false;
    if (field === 'provider') {
      valid = typeof value === 'string' && ['mock', 'live'].includes(value);
    } else if (field === 'commentMode') {
      valid = typeof value === 'string' && ['summary', 'inline', 'both'].includes(value);
    } else if (field === 'timeout') {
      valid = typeof value === 'number' && Number.isFinite(value) && value >= 0.1 && value <= 300;
    } else if (field === 'maxFindings') {
      valid = typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 200;
    } else if (field === 'model') {
      valid =
        typeof value === 'string' &&
        value.length <= 128 &&
        /^(?:[a-zA-Z0-9][a-zA-Z0-9_./:-]*)?$/.test(value) &&
        !SECRET_VALUE.test(value);
    } else {
      valid = typeof value === 'boolean';
    }

    // Never include untrusted names, keys, values, or parser errors in diagnostics.
    if (!valid) throw new Error('Invalid profile field value or secret-like credential value.');
    (normalized as any)[field] = value;
  }

  return normalized as Profile;
}

export function configurationPath(root: string, relativePath: unknown): string {
  if (
    typeof relativePath !== 'string' ||
    !/^\.codeatlas\/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*\.json$/.test(relativePath)
  ) {
    throw new Error('Configuration path must name a JSON file directly inside .codeatlas/.');
  }
  return path.join(root, relativePath);
}

export function readWorkspaceConfiguration(
  root: string | null,
  relativePath: string
): Record<string, unknown> | undefined {
  if (!root) return undefined;
  const filename = configurationPath(root, relativePath);
  let fd: number | undefined;
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
  } catch (error: any) {
    if (error && error.code === 'ENOENT') return undefined;
    throw new Error('Cannot load CodeAtlas configuration: expected bounded JSON in a regular, non-symlink file.');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

export class ProfileManager {
  workspaceRoot: string | null;
  userProfiles: Record<string, unknown>;
  baseSettings: Record<string, unknown>;
  profilePath: string;
  activeProfileName: string;
  error: string | null;

  constructor(
    workspaceRoot: string | null = null,
    userProfiles: Record<string, unknown> = {},
    baseSettings: Record<string, unknown> = {}
  ) {
    this.workspaceRoot = workspaceRoot;
    this.userProfiles = userProfiles;
    this.baseSettings = baseSettings;
    this.profilePath = '.codeatlas/profiles.json';
    this.activeProfileName = 'default';
    this.error = null;
  }

  loadProfiles(customPath: string = this.profilePath): Record<string, Profile> {
    configurationPath(this.workspaceRoot || '.', customPath);
    const defaults: Profile = {
      ...DEFAULT_PROFILE,
      ...validateProfile('default', this.baseSettings),
    };
    const profiles: Record<string, Profile> = Object.assign(Object.create(null), {
      default: defaults,
    });
    const workspace = readWorkspaceConfiguration(this.workspaceRoot, customPath);

    for (const source of [this.userProfiles, workspace === undefined ? {} : workspace]) {
      if (!isObject(source) || Object.keys(source).length > 100) {
        throw new Error('Profiles must be an object with at most 100 named profiles.');
      }
      for (const [name, value] of Object.entries(source)) {
        profiles[validateProfileName(name)] = {
          ...defaults,
          ...validateProfile(name, value),
        };
      }
    }
    return profiles;
  }

  getActiveProfile(profiles: Record<string, Profile> = this.loadProfiles()): Profile {
    validateProfileName(this.activeProfileName);
    if (!Object.hasOwn(profiles, this.activeProfileName)) {
      throw new Error('Active configuration profile was not found. Select an existing profile.');
    }
    return profiles[this.activeProfileName];
  }

  select(name: string, profiles: Record<string, Profile> = this.loadProfiles()): Profile {
    validateProfileName(name);
    if (!Object.hasOwn(profiles, name)) {
      throw new Error('Configuration profile was not found.');
    }
    this.activeProfileName = name;
    return profiles[name];
  }
}
