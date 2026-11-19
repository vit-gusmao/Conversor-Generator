/**
 * Orders an SSE stream that replays the durable log and then goes live.
 *
 * The live subscription must be attached BEFORE the replay query. Otherwise an
 * event committed between the query and the subscription is lost. The price is
 * an overlap: an event can be both replayed and buffered. This class holds the
 * live events until the replay finishes, then delivers them sorted by
 * `sequence`, dropping anything already delivered (A-23).
 */
export class SequencedDelivery<T extends { sequence: number }> {
  private buffering = true;
  private pending: T[] = [];
  private lastSequence: number;

  constructor(
    cursor: number,
    private readonly deliver: (event: T) => void,
  ) {
    this.lastSequence = cursor;
  }

  /** A live event from the bus. Held while the replay is still running. */
  receive(event: T): void {
    if (this.buffering) {
      this.pending.push(event);
      return;
    }
    this.emit(event);
  }

  /** An event read from the durable log, in ascending sequence order. */
  replay(event: T): void {
    this.emit(event);
  }

  /** Ends the replay phase and delivers the buffered live events in order. */
  flush(): void {
    this.buffering = false;
    const buffered = [...this.pending].sort((left, right) => left.sequence - right.sequence);
    this.pending = [];
    for (const event of buffered) this.emit(event);
  }

  /** Highest sequence delivered or skipped so far. */
  get cursor(): number {
    return this.lastSequence;
  }

  private emit(event: T): void {
    if (event.sequence <= this.lastSequence) return;
    this.lastSequence = event.sequence;
    this.deliver(event);
  }
}
