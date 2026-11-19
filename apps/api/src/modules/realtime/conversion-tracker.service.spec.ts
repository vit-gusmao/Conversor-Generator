import type { AppConfigService } from '../../config/app-config.service';
import type { PrismaService } from '../../database/prisma.service';
import type { BatchesService } from '../batches/batches.service';
import type { ConverterClient } from '../conversions/converter.client';

import type { ConversionEventsService } from './conversion-events.service';
import { ConversionTrackerService } from './conversion-tracker.service';

describe('ConversionTrackerService batch notification (A-09)', () => {
  let row: Record<string, unknown>;
  let prisma: {
    conversion: { findUnique: jest.Mock; update: jest.Mock };
    conversionRun: { updateMany: jest.Mock };
  };
  let events: { record: jest.Mock };
  let batches: { onConversionTerminal: jest.Mock };
  let tracker: ConversionTrackerService;

  const push = (status: string) =>
    tracker.applyPush({ conversion_id: 'conv-1', run_id: 'run-1', status, progress: 1 });

  beforeEach(() => {
    row = {
      id: 'conv-1',
      status: 'extracting',
      stage: 'extract',
      progress: 0.5,
      quality: 'pending',
      activeRunId: 'run-1',
      deletedAt: null,
    };
    prisma = {
      conversion: {
        findUnique: jest.fn(async () => ({ ...row })),
        update: jest.fn(async ({ data }: { data: Record<string, unknown> }) => {
          Object.assign(row, data);
          return { ...row };
        }),
      },
      conversionRun: { updateMany: jest.fn().mockResolvedValue({ count: 1 }) },
    };
    events = { record: jest.fn().mockResolvedValue(null) };
    batches = { onConversionTerminal: jest.fn().mockResolvedValue(undefined) };
    tracker = new ConversionTrackerService(
      prisma as unknown as PrismaService,
      {} as ConverterClient,
      events as unknown as ConversionEventsService,
      { converterPollIntervalMs: 800 } as AppConfigService,
      batches as unknown as BatchesService,
    );
  });

  it('notifies the batch when the conversion reaches a terminal status', async () => {
    await expect(push('completed')).resolves.toBe(true);

    expect(batches.onConversionTerminal).toHaveBeenCalledTimes(1);
    expect(batches.onConversionTerminal).toHaveBeenCalledWith('conv-1');
  });

  it('notifies the batch for failed and cancelled terminal transitions', async () => {
    await push('failed');
    row['status'] = 'extracting';
    await push('cancelled');

    expect(batches.onConversionTerminal.mock.calls).toEqual([['conv-1'], ['conv-1']]);
  });

  it('does not notify the batch for an active transition', async () => {
    await push('generating');

    expect(prisma.conversion.update).toHaveBeenCalled();
    expect(batches.onConversionTerminal).not.toHaveBeenCalled();
  });

  it('does not notify again when the stored status was already terminal', async () => {
    row['status'] = 'completed';

    await push('completed');

    expect(batches.onConversionTerminal).not.toHaveBeenCalled();
  });

  it('keeps the push successful when the batch roll-up fails after the state is saved', async () => {
    batches.onConversionTerminal.mockRejectedValue(new Error('db down'));

    await expect(push('completed')).resolves.toBe(true);

    expect(row['status']).toBe('completed');
  });
});
