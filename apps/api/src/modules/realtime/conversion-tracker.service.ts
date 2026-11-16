import {
  Injectable,
  Logger,
  type OnModuleDestroy,
  type OnModuleInit,
} from '@nestjs/common';
import type { ConversionStatus } from '@pdf2x/contracts';

import {
  asConversionStatus,
  asQualityState,
  clampProgress,
  isTerminal,
  progressFloorForStatus,
  stageKeyForStatus,
  TERMINAL_STATUSES,
} from '../../common/status';
import { stringifyJson } from '../../database/json.util';
import { PrismaService } from '../../database/prisma.service';
import { AppConfigService } from '../../config/app-config.service';
import {
  ConverterClient,
  type ConverterErrorInfo,
  type ConverterJobState,
} from '../conversions/converter.client';
import { ConversionEventsService } from './conversion-events.service';
import type { RecordEventInput } from './realtime.types';

export interface ConverterPushPayload {
  conversion_id?: string;
  run_id?: string;
  status?: string;
  stage?: string;
  progress?: number;
  progress_indeterminate?: boolean;
  quality?: string;
  message?: string;
  event_type?: string;
  payload?: Record<string, unknown>;
  stats?: Record<string, unknown>;
  validation?: Record<string, unknown>;
  error?: ConverterErrorInfo;
}

interface TrackedConversion {
  id: string;
  status: string;
  stage: string;
  progress: number;
  quality: string;
  activeRunId: string | null;
}

const PROGRESS_EVENT_THRESHOLD = 0.005;

/**
 * Single global poller (default 800 ms) that reconciles active conversions
 * against the engine. It is the fallback for engines that do not push; the
 * `POST /internal/hooks/converter` route feeds the exact same state machine.
 */
@Injectable()
export class ConversionTrackerService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(ConversionTrackerService.name);
  private timer: NodeJS.Timeout | null = null;
  private ticking = false;

  constructor(
    private readonly prisma: PrismaService,
    private readonly converter: ConverterClient,
    private readonly events: ConversionEventsService,
    private readonly config: AppConfigService,
  ) {}

  onModuleInit(): void {
    const interval = this.config.converterPollIntervalMs;
    this.timer = setInterval(() => {
      void this.tick();
    }, interval);
    this.timer.unref();
  }

  onModuleDestroy(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** One polling pass. Public so tests and manual reconciliation can call it. */
  async tick(): Promise<void> {
    if (this.ticking) return;
    this.ticking = true;
    try {
      const rows = (await this.prisma.conversion.findMany({
        where: {
          deletedAt: null,
          activeRunId: { not: null },
          status: { notIn: [...TERMINAL_STATUSES] },
        },
        orderBy: { updatedAt: 'asc' },
        take: 50,
        select: {
          id: true,
          status: true,
          stage: true,
          progress: true,
          quality: true,
          activeRunId: true,
        },
      })) as TrackedConversion[];

      await Promise.all(
        rows.map(async (row) => {
          if (!row.activeRunId) return;
          try {
            const state = await this.converter.status(row.activeRunId);
            await this.applyState(row.id, state, 'poll');
          } catch (error) {
            // The engine may be down or may have forgotten the run (restart).
            // Never fail the pass: the outbox/poller will catch up later.
            this.logger.debug(
              `Não foi possível consultar a execução ${row.activeRunId}: ${
                error instanceof Error ? error.message : String(error)
              }`,
            );
          }
        }),
      );
    } catch (error) {
      this.logger.warn(`Falha na varredura de conversões: ${String(error)}`);
    } finally {
      this.ticking = false;
    }
  }

  /** Applies a pushed update from the engine (internal hook). */
  async applyPush(payload: ConverterPushPayload): Promise<boolean> {
    const conversionId = await this.resolveConversionId(payload);
    if (!conversionId) return false;

    const state: ConverterJobState = {
      run_id: payload.run_id ?? '',
      conversion_id: payload.conversion_id ?? conversionId,
      status: payload.status ?? 'unknown',
      stage: payload.stage ?? null,
      progress: typeof payload.progress === 'number' ? payload.progress : null,
      progress_indeterminate:
        typeof payload.progress_indeterminate === 'boolean'
          ? payload.progress_indeterminate
          : null,
      quality: payload.quality ?? null,
      message: payload.message ?? null,
      stats: (payload.stats ?? null) as ConverterJobState['stats'],
      validation: (payload.validation ?? null) as ConverterJobState['validation'],
      error: payload.error ?? null,
    };

    await this.applyState(conversionId, state, 'push');
    return true;
  }

  private async resolveConversionId(payload: ConverterPushPayload): Promise<string | null> {
    if (payload.conversion_id) {
      const found = await this.prisma.conversion.findUnique({
        where: { id: payload.conversion_id },
        select: { id: true },
      });
      if (found) return found.id;
    }
    if (payload.run_id) {
      const run = await this.prisma.conversionRun.findFirst({
        where: { id: payload.run_id },
        select: { conversionId: true },
      });
      if (run) return run.conversionId;
    }
    return null;
  }

  private async applyState(
    conversionId: string,
    state: ConverterJobState,
    source: 'poll' | 'push',
  ): Promise<void> {
    const conversion = await this.prisma.conversion.findUnique({
      where: { id: conversionId },
    });
    if (!conversion || conversion.deletedAt) return;

    const previousStatus = asConversionStatus(conversion.status) ?? 'received';
    const status = ConverterClient.asStatus(state.status) ?? previousStatus;

    // A poll that started before a terminal transition must never move the
    // conversion back to an active state.
    if (isTerminal(previousStatus) && !isTerminal(status)) return;
    // A late answer from a previous run must not overwrite the active run.
    if (state.run_id && conversion.activeRunId && state.run_id !== conversion.activeRunId) return;

    const stage = ConverterClient.asStage(state.stage) ?? stageKeyForStatus(status);
    const progress = clampProgress(
      state.progress ?? Math.max(conversion.progress, progressFloorForStatus(status)),
    );
    const quality = ConverterClient.asQuality(state.quality) ?? asQualityState(conversion.quality) ?? 'pending';
    const terminal = isTerminal(status);

    const statusChanged = status !== previousStatus;
    const progressDelta = Math.abs(progress - conversion.progress);
    const stageChanged = stage !== conversion.stage;
    const qualityChanged = quality !== conversion.quality;

    if (!statusChanged && !stageChanged && !qualityChanged && progressDelta < PROGRESS_EVENT_THRESHOLD) {
      return;
    }

    const errorData: { errorCode: string | null; errorMessage: string | null } = state.error
      ? { errorCode: state.error.code, errorMessage: state.error.message }
      : { errorCode: null, errorMessage: null };

    await this.prisma.conversion.update({
      where: { id: conversionId },
      data: {
        status,
        stage,
        progress,
        quality,
        ...errorData,
        ...(state.stats ? { statsJson: stringifyJson(state.stats) } : {}),
        ...(state.validation ? { validationJson: stringifyJson(state.validation) } : {}),
      },
    });

    if (conversion.activeRunId) {
      await this.prisma.conversionRun.updateMany({
        where: { id: conversion.activeRunId },
        data: {
          status,
          stage,
          progress,
          ...(state.engine_version ? { engineVersion: state.engine_version } : {}),
          ...(state.stats ? { statsJson: stringifyJson(state.stats) } : {}),
          ...(state.error
            ? { errorCode: state.error.code, errorMessage: state.error.message }
            : {}),
          ...(terminal ? { finishedAt: new Date() } : {}),
        },
      });
    }

    if (statusChanged || progressDelta >= PROGRESS_EVENT_THRESHOLD) {
      await this.events.record(conversionId, {
        type: this.eventTypeFor(status, statusChanged),
        status,
        stage,
        progress,
        message: state.message ?? this.defaultMessage(status),
        indeterminate: Boolean(state.progress_indeterminate),
        runId: conversion.activeRunId,
        payload: {
          source,
          ...(state.error ? { error: state.error } : {}),
        },
      });
    }
  }

  private eventTypeFor(status: ConversionStatus, statusChanged: boolean): RecordEventInput['type'] {
    if (status === 'failed' || status === 'cancelled') return 'failed';
    if (status === 'completed' || status === 'completed_with_warnings') return 'completed';
    return statusChanged ? 'status' : 'progress';
  }

  private defaultMessage(status: ConversionStatus): string {
    switch (status) {
      case 'completed':
        return 'Conversão concluída.';
      case 'completed_with_warnings':
        return 'Conversão concluída com avisos.';
      case 'failed':
        return 'A conversão falhou.';
      case 'cancelled':
        return 'Conversão cancelada.';
      case 'generating':
        return 'Gerando o arquivo Excel.';
      case 'auditing':
        return 'Executando auditoria final.';
      default:
        return 'Processando o documento.';
    }
  }
}
