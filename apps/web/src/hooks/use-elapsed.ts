import { useEffect, useRef, useState } from 'react';

/** Ticking clock for elapsed-time displays, paused once `endIso` is known. */
export function useElapsed(startIso: string | null | undefined, endIso?: string | null): number {
  const [now, setNow] = useState(() => Date.now());
  const intervalRef = useRef<number | null>(null);

  useEffect(() => {
    if (!startIso || endIso) {
      if (intervalRef.current !== null) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }
    setNow(Date.now());
    intervalRef.current = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      if (intervalRef.current !== null) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [startIso, endIso]);

  if (!startIso) return 0;
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) return 0;
  const end = endIso ? Date.parse(endIso) : now;
  return Math.max(0, (Number.isNaN(end) ? now : end) - start);
}
