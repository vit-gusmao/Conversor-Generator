import { createHash } from 'node:crypto';

import { Body, Controller, HttpCode, HttpStatus, Inject, Param, Post, Put, Req } from '@nestjs/common';
import type { Request } from 'express';

import { InternalTokenGuard } from '../realtime/internal-token.guard';
import { UseGuards } from '@nestjs/common';
import type { AuthenticatedUser } from '../../common/request-context';
import { BatchesService } from '../batches/batches.service';
import { STORAGE_SERVICE, type StorageService } from '../../upload/storage.service';
import { ConversionsService } from '../conversions/conversions.service';

/**
 * Engine → gateway automation submission (T6, doc 02 §7).
 *
 * Guarded by `x-internal-token`. The engine's GatewaySubmissionPort calls
 * these endpoints with stable client keys; every effect is idempotent, so a
 * lost response is recovered by replaying the identical request.
 */
@Controller('internal/automation')
@UseGuards(InternalTokenGuard)
export class AutomationInternalController {
  constructor(
    private readonly batches: BatchesService,
    @Inject(STORAGE_SERVICE) private readonly storage: StorageService,
    private readonly conversions: ConversionsService,
  ) {}

  @Post('batches')
  @HttpCode(HttpStatus.CREATED)
  async ensureBatch(
    @Body() body: { owner_id?: string; import_id?: string },
  ): Promise<unknown> {
    const owner = this.ownerFrom(body?.owner_id);
    const batch = await this.batches.create(owner, `source-import:${body?.import_id ?? 'unknown'}`);
    return { batch_id: batch.batch_id };
  }

  @Post('batches/:batchId/items')
  @HttpCode(HttpStatus.CREATED)
  async registerItem(
    @Param('batchId') batchId: string,
    @Body()
    body: {
      owner_id?: string;
      client_item_id?: string;
      filename?: string;
      size_bytes?: number;
      sha256?: string;
      options?: unknown;
      import_id?: string;
    },
  ): Promise<unknown> {
    const owner = this.ownerFrom(body?.owner_id);
    const registered = await this.batches.registerItem(owner, batchId, {
      client_item_id: String(body?.client_item_id ?? ''),
      filename: String(body?.filename ?? 'documento'),
      size_bytes: Number(body?.size_bytes ?? 0),
      options: (body?.options as Record<string, unknown> | null) ?? null,
    });
    const item = registered.item as unknown as Record<string, unknown>;
    return {
      item_id: item.item_id,
      upload_id: registered.upload_id,
      conversion_id: item.conversion_id ?? null,
      replayed: registered.replayed ?? false,
    };
  }

  /**
   * Snapshot content from the engine. Identity travels in headers set by the
   * trusted port; the conversion id is recovered idempotently by the stable
   * `source-import:{import}:{item}` key.
   */
  @Put('batches/:batchId/items/:clientItemId/content')
  @HttpCode(HttpStatus.CREATED)
  async putContent(
    @Param('batchId') batchId: string,
    @Param('clientItemId') clientItemId: string,
    @Req() request: Request,
  ): Promise<unknown> {
    const owner = this.ownerFrom(headerValue(request, 'x-owner-id'));
    const importId = headerValue(request, 'x-import-id') ?? 'unknown';
    const declaredSha = headerValue(request, 'x-content-sha256') ?? '';
    const content = Buffer.from((request.body as Buffer) ?? Buffer.alloc(0));
    const sha256 = createHash('sha256').update(content).digest('hex');
    if (declaredSha && declaredSha !== sha256) {
      return { error: { code: 'UPLOAD_CHECKSUM_MISMATCH', message: 'Hash divergente.' } };
    }
    const extension = clientItemId.toLowerCase().endsWith('.csv') ? '.csv' : '.xlsx';
    const storageKey = `documents/${owner.id}/${sha256}${extension}`;
    if (!(await this.storage.exists(storageKey))) {
      await this.storage.put(storageKey, content, mimeFor(extension));
    }
    const conversion = await this.conversions.createStoredConversion(
      owner,
      {
        storageKey,
        sha256,
        sizeBytes: content.length,
        filename: fileNameFrom(clientItemId),
        mimeType: mimeFor(extension),
        pageCount: null,
        validation: {
          ok: true,
          is_pdf: false,
          pdf_version: null,
          page_count: 0,
          encrypted: false,
          needs_password: false,
          has_forms: false,
          has_javascript: false,
          has_embedded_files: false,
          has_launch_actions: false,
          rotation_pages: [],
          warnings: [],
          errors: [],
        },
      },
      optionsFor(extension) as unknown as Parameters<typeof this.conversions.createStoredConversion>[2],
      { idempotencyKey: `source-import:${importId}:${clientItemId}` },
    );
    return {
      item_id: clientItemId,
      batch_id: batchId,
      conversion_id: conversion.conversion_id,
    };
  }

  @Post('batches/:batchId/seal')
  async seal(
    @Param('batchId') batchId: string,
    @Body() body: { owner_id?: string },
  ): Promise<unknown> {
    const owner = this.ownerFrom(body?.owner_id);
    await this.batches.seal(owner, batchId);
    return { sealed: true };
  }

  @Post('batches/:batchId/reconcile')
  async reconcile(
    @Param('batchId') batchId: string,
    @Body() body: { owner_id?: string },
  ): Promise<unknown> {
    const owner = this.ownerFrom(body?.owner_id);
    return this.batches.get(owner, batchId);
  }

  @Post('batches/:batchId/cancel')
  async cancel(
    @Param('batchId') batchId: string,
    @Body() body: { owner_id?: string },
  ): Promise<unknown> {
    const owner = this.ownerFrom(body?.owner_id);
    await this.batches.cancel(owner, batchId);
    return { cancelled: true };
  }

  private ownerFrom(ownerId?: string): AuthenticatedUser {
    // The engine is trusted (internal token); it always sends the persisted
    // import owner — never a client-supplied identity.
    return {
      id: String(ownerId ?? ''),
      email: null,
      name: null,
      role: 'owner',
      isAnonymous: true,
      createdAt: new Date().toISOString(),
      deviceId: null,
    };
  }
}

function headerValue(request: Request, name: string): string | undefined {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function fileNameFrom(clientItemId: string): string {
  const marker = 'srcimp:';
  const index = clientItemId.indexOf(marker);
  const tail = index >= 0 ? clientItemId.slice(index + marker.length) : clientItemId;
  return `${tail.replace(/[^A-Za-z0-9._-]/g, '_') || 'documento'}.xlsx`.replace(/\.xlsx\.xlsx$/, '.xlsx');
}

function mimeFor(extension: string): string {
  return extension === '.csv'
    ? 'text/csv'
    : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
}

function optionsFor(extension: string): Record<string, unknown> {
  // The registered item already carries the normalized v3 options; replays
  // reuse them through the stable idempotency key.
  return extension === '.csv'
    ? { schema_version: 3, spreadsheet: { csv_encoding: null, csv_delimiter: null } }
    : { schema_version: 3 };
}
