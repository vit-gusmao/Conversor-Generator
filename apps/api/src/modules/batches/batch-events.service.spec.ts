import { FakePrisma } from '../../../test/helpers/fake-prisma';

import { BatchEventsService } from './batch-events.service';

describe('BatchEventsService', () => {
  let prisma: FakePrisma;
  let events: BatchEventsService;

  beforeEach(() => {
    prisma = new FakePrisma();
    events = new BatchEventsService(prisma as never);
  });

  const subjects = (): Map<string, unknown> => (events as unknown as { streams: Map<string, unknown> }).streams;

  it('removes a batch subject when its last subscriber leaves', () => {
    const first = events.stream('batch-a').subscribe();
    const second = events.stream('batch-a').subscribe();
    expect(subjects().size).toBe(1);

    first.unsubscribe();
    expect(subjects().size).toBe(1);

    second.unsubscribe();
    expect(subjects().size).toBe(0);
  });

  it('keeps no subject for a batch nobody is watching', async () => {
    await events.record('batch-idle', { status: 'accepting', message: 'Lote criado.' });

    expect(subjects().size).toBe(0);
  });

  it('still delivers recorded events to a live subscriber', async () => {
    const received: string[] = [];
    const subscription = events.stream('batch-live').subscribe((event) => received.push(event.status));

    await events.record('batch-live', { status: 'processing', message: 'Processando.' });
    subscription.unsubscribe();

    expect(received).toEqual(['processing']);
  });
});
