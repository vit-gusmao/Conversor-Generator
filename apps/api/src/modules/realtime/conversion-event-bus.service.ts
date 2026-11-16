import { Injectable } from '@nestjs/common';
import { Subject, type Observable } from 'rxjs';

import type { SseEvent } from './realtime.types';

/**
 * In-process fan-out for live SSE deltas.
 *
 * Persistence (ConversionEvent rows) is the source of truth; this bus only
 * carries updates to clients that are currently connected.
 */
@Injectable()
export class ConversionEventBus {
  private readonly subjects = new Map<string, Subject<SseEvent>>();

  stream(conversionId: string): Observable<SseEvent> {
    let subject = this.subjects.get(conversionId);
    if (!subject) {
      subject = new Subject<SseEvent>();
      this.subjects.set(conversionId, subject);
    }
    return subject.asObservable();
  }

  publish(conversionId: string, event: SseEvent): void {
    this.subjects.get(conversionId)?.next(event);
  }

  /** Drops the subject once the last subscriber unsubscribes. */
  release(conversionId: string): void {
    const subject = this.subjects.get(conversionId);
    if (!subject) return;
    if (subject.observers.length === 0) {
      subject.complete();
      this.subjects.delete(conversionId);
    }
  }

  get activeStreams(): number {
    return this.subjects.size;
  }
}
