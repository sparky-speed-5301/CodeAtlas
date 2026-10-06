/**
 * CodeAtlas VS Code Extension
 * Evidence-first, repository-aware code review for VS Code.
 * Phase 10D: TypeScript migration of the extension runtime.
 */
import * as vscode from 'vscode';
import { CodeAtlasClient } from './client';
import { clampLine, decorationTypes, findFindingsAtCursor, findingLineBounds, normalizePath, pathMatches, SEVERITY_ORDER, sortFindingsBySeverity } from './decorations';
import { getFindingWebviewHtml, getNoFindingWebviewHtml } from './detail-panel';
import { FixPreviewDocumentProvider, FIX_PREVIEW_SCHEME, PatchPreviewError, renderProposalPreview, applyPatchToContent, parseUnifiedDiff } from './fix-preview';
import { ALLOWED_PROFILE_KEYS, configurationPath, DEFAULT_PROFILE, FORBIDDEN_PROFILE_KEYS, ProfileManager, readWorkspaceConfiguration, validateProfile, validateProfileName } from './profiles';
import { ContextTreeDataProvider } from './providers/context';
import { FindingsTreeDataProvider } from './providers/findings';
import { StatusTreeDataProvider } from './providers/status';
import { formatQuickPickItem } from './quickpick';
import { parseServiceCommand, safeHealth, ServiceLifecycleManager, SETUP_MESSAGE, validateServiceUrl } from './service';
import { ExtensionActivationResult } from './types';
export { CodeAtlasClient, StatusTreeDataProvider, FindingsTreeDataProvider, ContextTreeDataProvider, ProfileManager, ServiceLifecycleManager, validateProfile, validateProfileName, DEFAULT_PROFILE, ALLOWED_PROFILE_KEYS, FORBIDDEN_PROFILE_KEYS, readWorkspaceConfiguration, configurationPath, validateServiceUrl, parseServiceCommand, safeHealth, SETUP_MESSAGE, findFindingsAtCursor, findingLineBounds, formatQuickPickItem, clampLine, sortFindingsBySeverity, normalizePath, pathMatches, getFindingWebviewHtml, getNoFindingWebviewHtml, SEVERITY_ORDER, decorationTypes, FixPreviewDocumentProvider, FIX_PREVIEW_SCHEME, PatchPreviewError, parseUnifiedDiff, applyPatchToContent, renderProposalPreview, };
export declare function activate(context: vscode.ExtensionContext): ExtensionActivationResult;
export declare function deactivate(): Promise<void>;
