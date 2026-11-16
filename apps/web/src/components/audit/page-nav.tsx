import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { formatNumber } from '@/lib/format';

export function PageNav({
  page,
  total,
  onChange,
  busy,
}: {
  page: number;
  total: number;
  onChange: (page: number) => void;
  busy?: boolean;
}) {
  const boundedTotal = Math.max(1, total);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        onClick={() => onChange(Math.max(1, page - 1))}
        disabled={page <= 1 || busy}
        aria-label="Página anterior"
      >
        <ChevronLeft aria-hidden="true" />
      </Button>
      <div className="flex items-center gap-1.5 text-xs text-muted">
        <span>Página</span>
        <Input
          type="number"
          min={1}
          max={boundedTotal}
          value={page}
          onChange={(event) => {
            const next = Number.parseInt(event.target.value, 10);
            if (Number.isFinite(next) && next >= 1 && next <= boundedTotal) onChange(next);
          }}
          aria-label="Número da página"
          className="h-8 w-16 text-center font-mono"
        />
        <span>de {formatNumber(boundedTotal)}</span>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={() => onChange(Math.min(boundedTotal, page + 1))}
        disabled={page >= boundedTotal || busy}
        aria-label="Próxima página"
      >
        <ChevronRight aria-hidden="true" />
      </Button>
    </div>
  );
}
