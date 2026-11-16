import type { Readable } from 'node:stream';

import { Injectable } from '@nestjs/common';
import type { AuditSummary, PageAudit } from '@pdf2x/contracts';

import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { PrismaService } from '../../database/prisma.service';
import { ConverterClient, type QueryParams } from '../conversions/converter.client';

export interface AuditExportPayload {
  stream: Readable;
  contentType: string;
  filename: string;
}

/**
 * Query values arrive as strings; only scalars are forwarded so nothing can
 * smuggle nested structures into the engine URL.
 */
export function forwardableQuery(query: Record<string, unknown>): QueryParams {
  const forwarded: QueryParams = {};
  for (const [key, value] of Object.entries(query)) {
    if (typeof value === 'string') {
      if (value.length > 0) forwarded[key] = value;
      continue;
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      forwarded[key] = value;
      continue;
    }
    if (Array.isArray(value) && value.length > 0) {
      const first = value[0];
      if (typeof first === 'string' || typeof first === 'number' || typeof first === 'boolean') {
        forwarded[key] = first;
      }
    }
  }
  return forwarded;
}

@Injectable()
export class AuditService {
  constructor(
    private readonly prisma: PrismaService,
    private readonly converter: ConverterClient,
  ) {}

  async summary(user: AuthenticatedUser, conversionId: string): Promise<AuditSummary> {
    const runId = await this.resolveRunId(user, conversionId);
    return this.converter.auditSummary(runId);
  }

  async events(
    user: AuthenticatedUser,
    conversionId: string,
    query: Record<string, unknown>,
  ): Promise<{ items: unknown[]; total: number }> {
    const runId = await this.resolveRunId(user, conversionId);
    return this.converter.auditEvents(runId, forwardableQuery(query));
  }

  async page(
    user: AuthenticatedUser,
    conversionId: string,
    page: number,
  ): Promise<PageAudit> {
    const runId = await this.resolveRunId(user, conversionId);
    return this.converter.auditPage(runId, page);
  }

  async cells(
    user: AuthenticatedUser,
    conversionId: string,
    query: Record<string, unknown>,
  ): Promise<{ items: unknown[]; total: number }> {
    const runId = await this.resolveRunId(user, conversionId);
    return this.converter.auditCells(runId, forwardableQuery(query));
  }

  async issues(
    user: AuthenticatedUser,
    conversionId: string,
    query: Record<string, unknown>,
  ): Promise<{ items: unknown[]; total: number }> {
    const runId = await this.resolveRunId(user, conversionId);
    return this.converter.auditIssues(runId, forwardableQuery(query));
  }

  async export(
    user: AuthenticatedUser,
    conversionId: string,
    query: Record<string, unknown>,
  ): Promise<AuditExportPayload> {
    const runId = await this.resolveRunId(user, conversionId);
    const forwarded = forwardableQuery(query);
    const format = forwarded['format'] === 'txt' ? 'txt' : 'json';
    const payload = await this.converter.auditExport(runId, { ...forwarded, format });
    return {
      stream: payload.stream,
      contentType: payload.contentType,
      filename: payload.filename || `auditoria-${conversionId}.${format}`,
    };
  }

  /** Resolves the run that owns the audit, enforcing ownership first. */
  private async resolveRunId(user: AuthenticatedUser, conversionId: string): Promise<string> {
    const conversion = await this.prisma.conversion.findUnique({
      where: { id: conversionId },
      include: { runs: { orderBy: { attempt: 'desc' }, take: 1 } },
    });

    if (!conversion || conversion.deletedAt) throw ApiError.notFound({ resource: 'conversion' });
    if (conversion.ownerId !== user.id) throw ApiError.forbidden({ resource: 'conversion' });

    const runId = conversion.activeRunId ?? conversion.runs[0]?.id;
    if (!runId) throw ApiError.notFound({ resource: 'conversion_run' });
    return runId;
  }
}
