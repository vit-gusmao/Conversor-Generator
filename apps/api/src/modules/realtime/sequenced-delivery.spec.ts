import { SequencedDelivery } from './sequenced-delivery';

interface Ev {
  sequence: number;
  label: string;
}

const ev = (sequence: number, label = `e${sequence}`): Ev => ({ sequence, label });

describe('SequencedDelivery (replay then live, A-23)', () => {
  it('delivers replayed events in order and then the buffered live events', () => {
    const out: number[] = [];
    const delivery = new SequencedDelivery<Ev>(0, (event) => out.push(event.sequence));

    delivery.replay(ev(1));
    delivery.replay(ev(2));
    delivery.flush();
    delivery.receive(ev(3));

    expect(out).toEqual([1, 2, 3]);
  });

  it('holds live events that arrive during the replay and flushes them after it', () => {
    const out: number[] = [];
    const delivery = new SequencedDelivery<Ev>(0, (event) => out.push(event.sequence));

    // Live events committed while the replay query was running.
    delivery.receive(ev(4));
    delivery.receive(ev(3));
    expect(out).toEqual([]);

    delivery.replay(ev(1));
    delivery.replay(ev(2));
    delivery.flush();

    expect(out).toEqual([1, 2, 3, 4]);
  });

  it('drops the overlap between replay and the live buffer', () => {
    const out: number[] = [];
    const delivery = new SequencedDelivery<Ev>(0, (event) => out.push(event.sequence));

    // Event 2 is both in the replay page and on the bus while the replay runs.
    delivery.receive(ev(2));
    delivery.replay(ev(1));
    delivery.replay(ev(2));
    delivery.flush();
    delivery.receive(ev(2));
    delivery.receive(ev(3));

    expect(out).toEqual([1, 2, 3]);
  });

  it('skips everything at or below the resume cursor', () => {
    const out: number[] = [];
    const delivery = new SequencedDelivery<Ev>(5, (event) => out.push(event.sequence));

    delivery.replay(ev(4));
    delivery.replay(ev(5));
    delivery.replay(ev(6));
    delivery.flush();

    expect(out).toEqual([6]);
    expect(delivery.cursor).toBe(6);
  });

  it('sorts buffered live events before delivering them', () => {
    const out: number[] = [];
    const delivery = new SequencedDelivery<Ev>(0, (event) => out.push(event.sequence));

    delivery.receive(ev(3));
    delivery.receive(ev(2));
    delivery.flush();

    expect(out).toEqual([2, 3]);
  });
});
