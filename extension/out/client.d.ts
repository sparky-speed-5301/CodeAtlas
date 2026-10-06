import * as http from 'http';
import { ExplanationResult, FindingDetail, FindingsResponse, FixEligibility, FixProposal, PatchProposal, ReviewStatus, ServiceHealth, StartReviewOptions, ValidationResult } from './types';
export declare class CodeAtlasClient {
    serviceUrl: string | null;
    timeoutMs: number;
    enabled: boolean;
    pending: Set<http.ClientRequest>;
    constructor(serviceUrl?: string | null);
    request<T = any>(method: string, endpoint: string, body?: any, timeoutMs?: number): Promise<T>;
    dispose(): void;
    getHealth(timeoutMs?: number): Promise<ServiceHealth>;
    startReview(repo: string, options?: StartReviewOptions): Promise<ReviewStatus>;
    getReview(runId: string): Promise<ReviewStatus>;
    getFindings(runId: string, filters?: {
        severity?: string;
        category?: string;
        status?: string;
        include_dismissed?: boolean;
    }): Promise<FindingsResponse>;
    getFindingDetail(runId: string, findingId: string): Promise<FindingDetail>;
    cancelReview(runId: string): Promise<{
        status: string;
    }>;
    explainFinding(findingId: string, runId: string | null): Promise<ExplanationResult>;
    proposePatch(findingId: string, runId: string | null): Promise<PatchProposal>;
    validateProposal(proposalId: string, approvalToken: string, runTests?: boolean, runFullSuite?: boolean): Promise<ValidationResult>;
    getFixEligibility(runId: string, findingId: string): Promise<FixEligibility>;
    requestFixProposal(runId: string, findingId: string): Promise<FixProposal>;
    getFixProposal(runId: string, findingId: string, proposalId?: string): Promise<FixProposal>;
    rejectFixProposal(runId: string, findingId: string, proposalId: string): Promise<FixProposal>;
    regenerateFixProposal(runId: string, findingId: string): Promise<FixProposal>;
}
