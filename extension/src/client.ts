import * as http from 'http';
import { URL } from 'url';
import { SETUP_MESSAGE, validateServiceUrl } from './service';
import {
  ExplanationResult,
  FindingDetail,
  FindingsResponse,
  PatchProposal,
  ReviewStatus,
  ServiceHealth,
  StartReviewOptions,
  ValidationResult,
} from './types';

export class CodeAtlasClient {
  serviceUrl: string | null;
  timeoutMs: number;
  enabled: boolean;
  pending: Set<http.ClientRequest>;

  constructor(serviceUrl?: string | null) {
    this.serviceUrl = serviceUrl ? validateServiceUrl(serviceUrl) : null;
    this.timeoutMs = 30000;
    this.enabled = true;
    this.pending = new Set();
  }

  request<T = any>(
    method: string,
    endpoint: string,
    body: any = null,
    timeoutMs: number = this.timeoutMs
  ): Promise<T> {
    return new Promise((resolve, reject) => {
      if (!this.enabled || !this.serviceUrl) {
        reject(new Error(this.enabled ? SETUP_MESSAGE : 'CodeAtlas service is disabled.'));
        return;
      }
      const parsed = new URL(validateServiceUrl(this.serviceUrl) + endpoint);
      const postData = body ? JSON.stringify(body) : null;

      const options: http.RequestOptions = {
        hostname: parsed.hostname.replace(/^\[|\]$/g, ''),
        port: parsed.port || 80,
        path: parsed.pathname + parsed.search,
        method: method,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': postData ? Buffer.byteLength(postData) : 0,
        },
        timeout: timeoutMs,
      };

      const req = http.request(options, (res: http.IncomingMessage) => {
        let responseBody = '';
        let responseBytes = 0;
        res.setEncoding('utf8');
        res.on('data', (chunk: string | Buffer) => {
          responseBytes += Buffer.byteLength(chunk);
          if (responseBytes > 2 * 1024 * 1024) {
            req.destroy();
            reject(new Error('CodeAtlas response exceeded the size limit.'));
            return;
          }
          responseBody += chunk;
        });
        res.on('error', () => reject(new Error('CodeAtlas response was interrupted.')));
        res.on('end', () => {
          try {
            const data = responseBody ? JSON.parse(responseBody) : {};
            if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
              resolve(data);
            } else {
              reject(new Error(`CodeAtlas request failed (HTTP ${res.statusCode}).`));
            }
          } catch (_) {
            reject(new Error('Malformed response from CodeAtlas service.'));
          }
        });
      });

      const timer = setTimeout(() => {
        req.destroy();
        reject(new Error('Request to CodeAtlas service timed out.'));
      }, timeoutMs);
      this.pending.add(req);
      req.on('close', () => {
        clearTimeout(timer);
        this.pending.delete(req);
      });

      req.on('error', () => {
        reject(
          new Error(
            'CodeAtlas service is unreachable. Check service health and configuration.'
          )
        );
      });

      if (postData) {
        req.write(postData);
      }
      req.end();
    });
  }

  dispose(): void {
    this.enabled = false;
    for (const request of this.pending) request.destroy();
    this.pending.clear();
  }

  getHealth(timeoutMs: number = 1000): Promise<ServiceHealth> {
    return this.request<ServiceHealth>('GET', '/health', null, timeoutMs);
  }

  startReview(repo: string, options: StartReviewOptions = {}): Promise<ReviewStatus> {
    return this.request<ReviewStatus>('POST', '/reviews', {
      repo,
      base: options.base || 'main',
      head: options.head || 'HEAD',
      review_provider: options.review_provider,
      provider_model: options.provider_model,
      provider_timeout: options.provider_timeout,
      allow_patch_suggestions: options.allow_patch_suggestions,
      max_findings: options.max_findings ?? 50,
    });
  }

  getReview(runId: string): Promise<ReviewStatus> {
    return this.request<ReviewStatus>('GET', `/reviews/${runId}`);
  }

  getFindings(
    runId: string,
    filters: {
      severity?: string;
      category?: string;
      status?: string;
      include_dismissed?: boolean;
    } = {}
  ): Promise<FindingsResponse> {
    const query: string[] = [];
    if (filters.severity) query.push(`severity=${encodeURIComponent(filters.severity)}`);
    if (filters.category) query.push(`category=${encodeURIComponent(filters.category)}`);
    if (filters.status) query.push(`status=${encodeURIComponent(filters.status)}`);
    if (filters.include_dismissed) query.push('include_dismissed=true');
    const qs = query.length ? '?' + query.join('&') : '';
    return this.request<FindingsResponse>('GET', `/reviews/${runId}/findings${qs}`);
  }

  getFindingDetail(runId: string, findingId: string): Promise<FindingDetail> {
    return this.request<FindingDetail>('GET', `/reviews/${runId}/findings/${findingId}`);
  }

  cancelReview(runId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>('POST', `/reviews/${runId}/cancel`);
  }

  explainFinding(findingId: string, runId: string | null): Promise<ExplanationResult> {
    return this.request<ExplanationResult>('POST', `/findings/${findingId}/explain`, {
      run_id: runId,
    });
  }

  proposePatch(findingId: string, runId: string | null): Promise<PatchProposal> {
    return this.request<PatchProposal>('POST', `/findings/${findingId}/patch-proposal`, {
      run_id: runId,
    });
  }

  validateProposal(
    proposalId: string,
    approvalToken: string,
    runTests: boolean = false,
    runFullSuite: boolean = false
  ): Promise<ValidationResult> {
    return this.request<ValidationResult>('POST', `/proposals/${proposalId}/validate`, {
      approval_token: approvalToken,
      run_tests: runTests,
      run_full_suite: runFullSuite,
    });
  }
}
