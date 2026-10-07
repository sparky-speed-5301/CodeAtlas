/**
 * Type definitions for CodeAtlas VS Code Extension.
 * Defines types for service status, configuration profiles, lifecycle states,
 * findings, locations, evidence, review status, patch proposals, validation results,
 * API errors, and command contexts.
 */

import * as vscode from 'vscode';

// ---------------------------------------------------------------------------
// Service & Lifecycle Types
// ---------------------------------------------------------------------------

export type ServiceMode = 'external' | 'managed' | 'disabled';

export type HealthStatus =
  | 'unknown'
  | 'healthy'
  | 'unreachable'
  | 'starting'
  | 'stopping'
  | 'stopped'
  | 'crashed'
  | 'disabled'
  | 'not_configured'
  | 'error'
  | 'stale';

export interface ServiceHealth {
  status: 'ok';
  service: 'codeatlas-service';
  version: string;
  pid: number | null;
  active_reviews: number | null;
  provider: 'mock' | 'live' | 'deterministic' | 'mixed' | 'unknown' | string;
}

export interface ServiceLifecycleOptions {
  serviceMode?: ServiceMode;
  serviceHost?: string;
  servicePort?: number;
  serviceCommand?: string;
  serviceUrl?: string | null;
  autoStartService?: boolean;
  workspaceRoot?: string | null;
  trusted?: boolean;
  clientFactory?: (url: string) => any;
  onChange?: () => void;
  onCrash?: (message: string) => void;
}

export interface ServiceStartResult {
  started: boolean;
  alreadyRunning?: boolean;
  pid?: number | null;
  url: string;
  health: ServiceHealth;
}

export interface ServiceStopResult {
  stopped: boolean;
  pid?: number | null;
  message?: string;
}

export interface ServiceCommandParseResult {
  executable: string;
  args: string[];
}

// ---------------------------------------------------------------------------
// Configuration Profile Types
// ---------------------------------------------------------------------------

export type ProfileProvider = 'mock' | 'live';
export type ProfileCommentMode = 'summary' | 'inline' | 'both';

export interface Profile {
  provider: ProfileProvider;
  model: string;
  timeout: number;
  maxFindings: number;
  enableLiveReviewer: boolean;
  enablePatchSuggestions: boolean;
  enableTestExecution: boolean;
  enableFullSuiteExecution: boolean;
  githubDryRun: boolean;
  commentMode: ProfileCommentMode;
}

export type RawProfile = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Finding & Location Types
// ---------------------------------------------------------------------------

export type FindingSeverity = 'blocker' | 'high' | 'medium' | 'low' | 'info';

export interface FindingLocation {
  file: string;
  line?: number;
  start_line?: number;
  end_line?: number;
}

export interface SymbolContext {
  name?: string;
  kind?: string;
  start_line?: number;
  end_line?: number;
}

export interface FindingContext {
  changed_lines?: number[];
  containing_symbol?: SymbolContext | null;
  relevant_imports?: string[];
  relevant_references?: string[];
  related_tests?: string[];
  retrieved_context_candidates?: unknown[];
  truncation_status?: boolean;
  evidence_sources?: string[];
}

export interface Finding extends FindingLocation {
  id: string;
  category: string;
  severity: FindingSeverity | string;
  claim: string;
  line: number;
  confidence?: number | string;
  evidence_strength?: string;
  status?: string;
  start_line?: number;
  end_line?: number;
  dismissed?: boolean;
  impact?: string;
  policy_decision?: string;
  test_result?: string;
  limitations?: string[];
  deterministic_evidence?: unknown;
  context?: FindingContext;
  quality_version?: string;
  evidence_sources?: string[];
  deterministic_support?: number;
  reviewer_support?: number;
  changed_line_support?: number;
  repository_context_support?: number;
  test_support?: number;
  ambiguity_score?: number;
  truncation_penalty?: number;
  unsupported_flow?: boolean;
  abstention_reason?: string | null;
  duplicate_group_id?: string | null;
  suppressed_finding_ids?: string[];
  quality_decision?: string;
  quality_score?: number;
  score_components?: Record<string, number>;
  quality_limitations?: string[];
  feedback?: string | null;
}

export interface FindingCounts {
  total?: number;
  blocker?: number;
  high?: number;
  medium?: number;
  low?: number;
  info?: number;
}

export interface FindingDetail extends Finding {
  deterministic_evidence?: unknown;
  limitations?: string[];
  context?: FindingContext;
}

// ---------------------------------------------------------------------------
// Review Status Types
// ---------------------------------------------------------------------------

export type ReviewRunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface ReviewStatus {
  run_id?: string;
  status: ReviewRunStatus | string;
  progress_text?: string;
  policy_decision?: string;
  finding_counts?: FindingCounts;
  test_status?: string;
  patch_validation_status?: string;
  errors?: string[];
  /** Resolved revisions, present only after the service resolved them. */
  base_commit?: string | null;
  head_commit?: string | null;
}

export interface StartReviewOptions {
  base?: string;
  head?: string;
  review_provider?: string;
  provider_model?: string;
  provider_timeout?: number;
  allow_patch_suggestions?: boolean;
  max_findings?: number;
}

export interface FindingsResponse {
  findings: Finding[];
  count?: number;
}

// ---------------------------------------------------------------------------
// Patch Proposals & Validation Types
// ---------------------------------------------------------------------------

export interface PatchProposal {
  proposal_id: string;
  finding_id: string;
  diff?: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface ValidationResult {
  valid: boolean;
  approval_verified: boolean;
  status: string;
  proposal_id?: string;
  test_status?: string;
  full_suite_status?: string;
  errors?: string[];
}

// ---------------------------------------------------------------------------
// Phase 11C-B: FixProposal Types (bounded, preview-only)
// ---------------------------------------------------------------------------

export type FixProposalLifecycle =
  | 'not_eligible'
  | 'generating'
  | 'generation_failed'
  | 'rejected_by_policy'
  | 'draft_ready'
  | 'rejected'
  | 'regeneration_requested';

export type FixValidationLifecycle =
  | 'not_requested'
  | 'approval_required'
  | 'approved_for_validation'
  | 'validating'
  | 'applied_in_isolated_worktree'
  | 'tests_running'
  | 'validated'
  | 'applied'
  | 'reverted'
  | 'validation_failed'
  | 'cleanup_failed';

export interface FixProposal {
  proposal_id: string;
  finding_id: string;
  run_id: string;
  repository: string;
  base_commit: string;
  head_commit: string;
  target_files: string[];
  patch_text: string;
  patch_hash: string;
  diagnosis: string;
  explanation: string;
  expected_behavior_change: string;
  assumptions: string[];
  risk_level: string;
  confidence: number;
  evidence_strength: string;
  evidence_sources: string[];
  quality_decision: string;
  policy_decision: Record<string, unknown>;
  generation_status: FixProposalLifecycle;
  approval_required: boolean;
  limitations: string[];
  rejection_reason?: string | null;
  rejection_explanation?: string | null;
  created_at: string;
  schema_version: string;
  validation_operation: 'validate';
  validation_status: FixValidationLifecycle;
  validation_result?: Record<string, unknown> | null;
  review_packet?: Record<string, unknown> | null;
  human_approval_manifest?: Record<string, unknown> | null;
  validation_history: string[];
}

export interface FixValidationApprovalResult {
  proposal_id: string;
  finding_id: string;
  run_id: string;
  operation: 'validate';
  approval_verified: boolean;
  validation_status: 'approval_required' | 'approved_for_validation';
  errors: string[];
}

export interface FixValidationResult {
  proposal_id: string;
  finding_id: string;
  run_id: string;
  operation: 'validate';
  status: string;
  validation_status: FixValidationLifecycle;
  approval_verified: boolean;
  valid: boolean;
  applies_cleanly: boolean;
  syntax_valid?: boolean | null;
  tests_status: string;
  full_suite_status: string;
  sandbox_id?: string | null;
  cleanup_status?: string | null;
  resulting_diff_hash?: string | null;
  commands_run: string[];
  tests_run: string[];
  test_plan?: Record<string, unknown> | null;
  test_result?: Record<string, unknown> | null;
  full_suite_result?: Record<string, unknown> | null;
  validation_result: Record<string, unknown>;
  observed_test_evidence?: Record<string, unknown> | null;
  errors: string[];
  warnings: string[];
  limitations: string[];
  review_packet?: Record<string, unknown> | null;
  human_approval_manifest?: Record<string, unknown> | null;
  validation_history: string[];
}

// ---------------------------------------------------------------------------
// Phase 11C-D/E: explicit apply of a validated FixProposal, revert, history
// ---------------------------------------------------------------------------

export type FixApplyStatus = 'not_applied' | 'applied' | 'reverted';

export interface FixApplyEvent {
  event: 'applied' | 'reverted' | 'apply_rejected' | 'revert_failed';
  proposal_id: string;
  finding_id: string;
  run_id: string;
  patch_hash: string;
  resulting_diff_hash?: string | null;
  head_commit: string;
  branch_ref: string;
  files: string[];
  reason?: string | null;
  at: string;
}

export interface FixApplyResult {
  proposal_id: string;
  finding_id: string;
  run_id: string;
  operation: 'apply' | 'revert';
  apply_status: FixApplyStatus;
  approval_verified: boolean;
  head_commit: string;
  branch_ref: string;
  patch_hash: string;
  resulting_diff_hash?: string | null;
  validated_resulting_diff_hash?: string | null;
  files_changed: string[];
  files_restored: string[];
  apply_history: FixApplyEvent[];
  errors: string[];
}

export interface FixApplyHistory {
  proposal_id: string;
  finding_id: string;
  run_id: string;
  apply_status: FixApplyStatus;
  revert_available: boolean;
  head_commit_at_apply: string;
  branch_ref_at_apply: string;
  patch_hash: string;
  resulting_diff_hash?: string | null;
  files_changed: string[];
  events: FixApplyEvent[];
}

export interface FixEligibility {
  run_id: string;
  finding_id: string;
  eligible: boolean;
  reasons: string[];
  explanations: string[];
}

export interface ExplanationResult {
  finding_id: string;
  category: string;
  explanation: string;
  remediation_advice: string;
}

// ---------------------------------------------------------------------------
// API Error & Command Context Types
// ---------------------------------------------------------------------------

export interface ApiError {
  message: string;
  statusCode?: number;
  code?: string;
}

export interface FindingQuickPickItem extends vscode.QuickPickItem {
  finding: Finding;
}

export interface ProfileQuickPickItem extends vscode.QuickPickItem {
  profileName: string;
}

export interface ExtensionSession {
  dispose(): Promise<void>;
}

export interface ExtensionActivationResult {
  ready: Promise<string | null>;
  serviceManager: any;
  profileManager: any;
  statusProvider: any;
  client: any;
  dispose: () => Promise<void>;
}
