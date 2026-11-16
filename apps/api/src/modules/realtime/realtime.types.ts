import type { ConversionEvent, ConversionStatus } from '@pdf2x/contracts';

/** Exactly the contract shape written to the SSE stream. */
export type SseEvent = ConversionEvent;

export interface RecordEventInput {
  type: ConversionEvent['type'];
  status: ConversionStatus;
  stage: ConversionEvent['stage'];
  progress: number;
  message: string;
  indeterminate?: boolean;
  runId?: string | null;
  payload?: Record<string, unknown>;
}

/** Payload persisted in `ConversionEvent.payloadJson`. */
export interface StoredEventPayload {
  run_id: string | null;
  progress_indeterminate: boolean;
  data: Record<string, unknown>;
}

export interface ConversionEventRow {
  id: string;
  conversionId: string;
  sequence: number;
  type: string;
  status: string;
  stage: string;
  progress: number;
  message: string;
  payloadJson: string | null;
  createdAt: Date;
}

export const HEARTBEAT_INTERVAL_MS = 15000;
