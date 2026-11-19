import { Injectable } from '@nestjs/common';

import { ApiError } from '../../common/errors/api-error';
import { ERROR_CODES } from '../../common/errors/error-codes';
import type { AuthenticatedUser } from '../../common/request-context';
import { EngineProxyService } from '../engine-proxy/engine-proxy.service';

/**
 * Full-mode automation facade (T6, doc 02 §7).
 *
 * Public source/schedule management proxies to the engine's internal API with
 * the authenticated owner id. The engine remains the single writer of
 * sources/imports/schedules; no Prisma duplication of those domains.
 */
@Injectable()
export class AutomationService {
  constructor(private readonly proxy: EngineProxyService) {}

  listRoots(ownerId: string): Promise<unknown> {
    return this.proxy.json('/internal/v1/source-roots', { ownerId });
  }

  listSources(ownerId: string): Promise<unknown> {
    return this.proxy.json('/internal/v1/sources', { ownerId });
  }

  async createSource(owner: AuthenticatedUser, body: unknown): Promise<unknown> {
    return this.proxy.json('/internal/v1/sources', {
      method: 'POST',
      body: stripOwner(body),
      ownerId: owner.id,
    });
  }

  async getSource(ownerId: string, sourceId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/sources/${encodeURIComponent(sourceId)}`, { ownerId });
  }

  async patchSource(owner: AuthenticatedUser, sourceId: string, body: unknown): Promise<unknown> {
    return this.proxy.json(`/internal/v1/sources/${encodeURIComponent(sourceId)}`, {
      method: 'PATCH',
      body: stripOwner(body),
      ownerId: owner.id,
    });
  }

  async deleteSource(ownerId: string, sourceId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/sources/${encodeURIComponent(sourceId)}`, {
      method: 'DELETE',
      ownerId,
    });
  }

  async validateSource(ownerId: string, sourceId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/sources/${encodeURIComponent(sourceId)}/validate`, {
      method: 'POST',
      ownerId,
    });
  }

  async startImport(
    owner: AuthenticatedUser,
    sourceId: string,
    body: unknown,
    idempotencyKey: string | undefined,
  ): Promise<unknown> {
    if (!idempotencyKey) {
      throw ApiError.badRequest(ERROR_CODES.VALIDATION_ERROR, {
        reason: 'idempotency_key_required',
      });
    }
    return this.proxy.json(`/internal/v1/sources/${encodeURIComponent(sourceId)}/imports`, {
      method: 'POST',
      body: stripOwner(body),
      ownerId: owner.id,
      headers: { 'Idempotency-Key': idempotencyKey },
    });
  }

  getImport(ownerId: string, importId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/imports/${encodeURIComponent(importId)}`, { ownerId });
  }

  listImportItems(ownerId: string, importId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/imports/${encodeURIComponent(importId)}/items`, {
      ownerId,
    });
  }

  cancelImport(ownerId: string, importId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/imports/${encodeURIComponent(importId)}/cancel`, {
      method: 'POST',
      ownerId,
    });
  }

  listSchedules(ownerId: string): Promise<unknown> {
    return this.proxy.json('/internal/v1/schedules', { ownerId });
  }

  async createSchedule(owner: AuthenticatedUser, body: unknown): Promise<unknown> {
    return this.proxy.json('/internal/v1/schedules', {
      method: 'POST',
      body: stripOwner(body),
      ownerId: owner.id,
    });
  }

  getSchedule(ownerId: string, scheduleId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/schedules/${encodeURIComponent(scheduleId)}`, { ownerId });
  }

  async patchSchedule(owner: AuthenticatedUser, scheduleId: string, body: unknown): Promise<unknown> {
    return this.proxy.json(`/internal/v1/schedules/${encodeURIComponent(scheduleId)}`, {
      method: 'PATCH',
      body: stripOwner(body),
      ownerId: owner.id,
    });
  }

  async deleteSchedule(ownerId: string, scheduleId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/schedules/${encodeURIComponent(scheduleId)}`, {
      method: 'DELETE',
      ownerId,
    });
  }

  scheduleAction(
    ownerId: string,
    scheduleId: string,
    action: 'pause' | 'resume' | 'run-now',
  ): Promise<unknown> {
    return this.proxy.json(
      `/internal/v1/schedules/${encodeURIComponent(scheduleId)}/${action}`,
      { method: 'POST', ownerId },
    );
  }

  scheduleRuns(ownerId: string, scheduleId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/schedules/${encodeURIComponent(scheduleId)}/runs`, {
      ownerId,
    });
  }

  getRun(ownerId: string, runId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/schedule-runs/${encodeURIComponent(runId)}`, { ownerId });
  }

  /**
   * Run events after `after`. The engine resumes from the `Last-Event-ID`
   * header; `after` in the query is forwarded too, so both engine forms agree.
   */
  runEvents(ownerId: string, runId: string, after: number | null = null): Promise<unknown> {
    const query = after === null ? '' : `?after=${after}`;
    return this.proxy.json(
      `/internal/v1/schedule-runs/${encodeURIComponent(runId)}/events${query}`,
      {
        ownerId,
        ...(after === null ? {} : { headers: { 'Last-Event-ID': String(after) } }),
      },
    );
  }

  cancelRun(ownerId: string, runId: string): Promise<unknown> {
    return this.proxy.json(`/internal/v1/schedule-runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      ownerId,
    });
  }
}

function stripOwner(body: unknown): unknown {
  if (typeof body !== 'object' || body === null) return body;
  const clone = { ...(body as Record<string, unknown>) };
  // owner_id is never client input in any direction.
  delete clone.owner_id;
  delete clone.ownerId;
  return clone;
}
