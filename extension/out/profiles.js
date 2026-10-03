"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.ProfileManager = exports.FORBIDDEN_PROFILE_KEYS = exports.ALLOWED_PROFILE_KEYS = exports.ALIASES = exports.DEFAULT_PROFILE = void 0;
exports.validateProfileName = validateProfileName;
exports.validateProfile = validateProfile;
exports.configurationPath = configurationPath;
exports.readWorkspaceConfiguration = readWorkspaceConfiguration;
/**
 * Configuration is data only. Never scan source files or persist profile contents.
 */
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
exports.DEFAULT_PROFILE = Object.freeze({
    provider: 'mock',
    model: '',
    timeout: 30,
    maxFindings: 50,
    enableLiveReviewer: false,
    enablePatchSuggestions: false,
    enableTestExecution: false,
    enableFullSuiteExecution: false,
    githubDryRun: true,
    commentMode: 'summary',
});
exports.ALIASES = {
    max_findings: 'maxFindings',
    enable_live_reviewer: 'enableLiveReviewer',
    enable_patch_suggestions: 'enablePatchSuggestions',
    enable_test_execution: 'enableTestExecution',
    enable_full_suite: 'enableFullSuiteExecution',
    github_dry_run: 'githubDryRun',
    comment_mode: 'commentMode',
};
exports.ALLOWED_PROFILE_KEYS = new Set([
    ...Object.keys(exports.DEFAULT_PROFILE),
    ...Object.keys(exports.ALIASES),
]);
exports.FORBIDDEN_PROFILE_KEYS = new Set([
    'autoapply',
    'allowapply',
    'apply',
    'merge',
    'allowmerge',
    'automerge',
]);
const SECRET_FIELD = /key|token|secret|cred|password|prompt|auth|output/i;
const SECRET_VALUE = /(?:sk-|gh[pousr]_|github_pat_|CAT-APP-|Bearer\s|AKIA|-----BEGIN)/i;
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
function validateProfileName(name) {
    if (typeof name !== 'string' ||
        !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(name) ||
        SECRET_VALUE.test(name) ||
        ['__proto__', 'constructor', 'prototype'].includes(name)) {
        throw new Error('Invalid configuration profile name. Use a short, non-secret identifier.');
    }
    return name;
}
function validateProfile(name, rawProfile) {
    validateProfileName(name);
    if (!isObject(rawProfile))
        throw new Error('Profile must be a configuration object.');
    const normalized = {};
    for (const [key, value] of Object.entries(rawProfile)) {
        if (exports.FORBIDDEN_PROFILE_KEYS.has(key.replace(/[_-]/g, '').toLowerCase())) {
            throw new Error('Profile automatic patch application or merge is not permitted.');
        }
        if (SECRET_FIELD.test(key))
            throw new Error('Profile secret-like field is prohibited.');
        if (!exports.ALLOWED_PROFILE_KEYS.has(key))
            throw new Error('Profile contains an unknown configuration field.');
        const field = (exports.ALIASES[key] || key);
        if (Object.hasOwn(normalized, field))
            throw new Error('Profile contains duplicate field aliases.');
        let valid = false;
        if (field === 'provider') {
            valid = typeof value === 'string' && ['mock', 'live'].includes(value);
        }
        else if (field === 'commentMode') {
            valid = typeof value === 'string' && ['summary', 'inline', 'both'].includes(value);
        }
        else if (field === 'timeout') {
            valid = typeof value === 'number' && Number.isFinite(value) && value >= 0.1 && value <= 300;
        }
        else if (field === 'maxFindings') {
            valid = typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 200;
        }
        else if (field === 'model') {
            valid =
                typeof value === 'string' &&
                    value.length <= 128 &&
                    /^(?:[a-zA-Z0-9][a-zA-Z0-9_./:-]*)?$/.test(value) &&
                    !SECRET_VALUE.test(value);
        }
        else {
            valid = typeof value === 'boolean';
        }
        // Never include untrusted names, keys, values, or parser errors in diagnostics.
        if (!valid)
            throw new Error('Invalid profile field value or secret-like credential value.');
        normalized[field] = value;
    }
    return normalized;
}
function configurationPath(root, relativePath) {
    if (typeof relativePath !== 'string' ||
        !/^\.codeatlas\/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*\.json$/.test(relativePath)) {
        throw new Error('Configuration path must name a JSON file directly inside .codeatlas/.');
    }
    return path.join(root, relativePath);
}
function readWorkspaceConfiguration(root, relativePath) {
    if (!root)
        return undefined;
    const filename = configurationPath(root, relativePath);
    let fd;
    try {
        const dir = fs.lstatSync(path.join(root, '.codeatlas'));
        if (dir.isSymbolicLink() || !dir.isDirectory())
            throw new Error();
        const stat = fs.lstatSync(filename);
        if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 65536)
            throw new Error();
        fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
        const opened = fs.fstatSync(fd);
        if (!opened.isFile() || opened.ino !== stat.ino || opened.dev !== stat.dev)
            throw new Error();
        const buffer = Buffer.alloc(65537);
        const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
        if (size > 65536)
            throw new Error();
        return JSON.parse(buffer.subarray(0, size).toString('utf8'));
    }
    catch (error) {
        if (error && error.code === 'ENOENT')
            return undefined;
        throw new Error('Cannot load CodeAtlas configuration: expected bounded JSON in a regular, non-symlink file.');
    }
    finally {
        if (fd !== undefined)
            fs.closeSync(fd);
    }
}
class ProfileManager {
    workspaceRoot;
    userProfiles;
    baseSettings;
    profilePath;
    activeProfileName;
    error;
    constructor(workspaceRoot = null, userProfiles = {}, baseSettings = {}) {
        this.workspaceRoot = workspaceRoot;
        this.userProfiles = userProfiles;
        this.baseSettings = baseSettings;
        this.profilePath = '.codeatlas/profiles.json';
        this.activeProfileName = 'default';
        this.error = null;
    }
    loadProfiles(customPath = this.profilePath) {
        configurationPath(this.workspaceRoot || '.', customPath);
        const defaults = {
            ...exports.DEFAULT_PROFILE,
            ...validateProfile('default', this.baseSettings),
        };
        const profiles = Object.assign(Object.create(null), {
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
    getActiveProfile(profiles = this.loadProfiles()) {
        validateProfileName(this.activeProfileName);
        if (!Object.hasOwn(profiles, this.activeProfileName)) {
            throw new Error('Active configuration profile was not found. Select an existing profile.');
        }
        return profiles[this.activeProfileName];
    }
    select(name, profiles = this.loadProfiles()) {
        validateProfileName(name);
        if (!Object.hasOwn(profiles, name)) {
            throw new Error('Configuration profile was not found.');
        }
        this.activeProfileName = name;
        return profiles[name];
    }
}
exports.ProfileManager = ProfileManager;
//# sourceMappingURL=profiles.js.map