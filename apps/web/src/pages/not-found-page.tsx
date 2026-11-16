import { Link } from 'react-router-dom';
import { ArrowLeft, FileQuestion } from 'lucide-react';
import { Button } from '@/components/ui/button';

export function NotFoundPage() {
  return (
    <div className="mx-auto flex max-w-lg flex-col items-center gap-5 py-16 text-center">
      <span className="grid size-16 place-items-center rounded-2xl bg-surface-2 text-muted-2">
        <FileQuestion className="size-7" aria-hidden="true" />
      </span>
      <div className="space-y-2">
        <p className="text-xs font-semibold uppercase tracking-widest text-accent">Erro 404</p>
        <h1 className="text-3xl font-semibold text-foreground">Página não encontrada</h1>
        <p className="text-sm text-muted">
          O endereço acessado não existe. Volte para a página inicial para converter um PDF ou consultar o
          histórico.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <Button asChild>
          <Link to="/">
            <ArrowLeft aria-hidden="true" />
            Voltar ao início
          </Link>
        </Button>
        <Button asChild variant="secondary">
          <Link to="/history">Ver histórico</Link>
        </Button>
      </div>
    </div>
  );
}
