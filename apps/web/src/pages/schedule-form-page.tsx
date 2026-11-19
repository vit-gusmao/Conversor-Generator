import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { CalendarClock, Eye } from 'lucide-react';
import type { ScheduleCreateRequest } from '@pdf2x/contracts';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock } from '@/components/layout/states';
import { useEnsureSession } from '@/hooks/use-session';
import {
  useCreateSchedule,
  usePreviewSchedule,
  useSchedule,
  useScheduleRuns,
  useUpdateSchedule,
} from '@/hooks/use-schedules';
import { useSources } from '@/hooks/use-sources';
import { explainError } from '@/lib/errors';
import { formatDateTime } from '@/lib/format';
import { toast } from '@/stores/ui';
import { Link } from 'react-router-dom';

const TIMEZONES = ['America/Sao_Paulo', 'UTC', 'America/Manaus', 'America/Belem'] as const;
const INTERVAL_UNITS = [
  { key: 'minutes', label: 'minutos', factor: 60 },
  { key: 'hours', label: 'horas', factor: 3600 },
  { key: 'days', label: 'dias', factor: 86_400 },
] as const;

const RUN_STATE_LABELS: Record<string, string> = {
  queued: 'na fila',
  running: 'executando',
  waiting_children: 'aguardando',
  cancelling: 'cancelando',
  succeeded: 'sucesso',
  partial: 'parcial',
  no_changes: 'sem novidades',
  failed: 'falhou',
  cancelled: 'cancelada',
  skipped: 'ignorada',
};

export function ScheduleFormPage() {
  useEnsureSession();
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const existing = useSchedule(id ?? null);
  const sources = useSources();
  const createSchedule = useCreateSchedule();
  const updateSchedule = useUpdateSchedule();
  const preview = usePreviewSchedule();
  const runs = useScheduleRuns(id ?? null);

  const [name, setName] = useState('');
  const [sourceId, setSourceId] = useState(searchParams.get('source') ?? '');
  const [frequency, setFrequency] = useState<'daily' | 'interval'>('daily');
  const [localTime, setLocalTime] = useState('08:00');
  const [intervalValue, setIntervalValue] = useState(1);
  const [intervalUnit, setIntervalUnit] = useState<'minutes' | 'hours' | 'days'>('hours');
  const [timezone, setTimezone] = useState<string>('America/Sao_Paulo');
  const [misfire, setMisfire] = useState<'run_once_latest' | 'skip'>('run_once_latest');
  const [overlap, setOverlap] = useState<'skip' | 'queue_one'>('skip');

  useEffect(() => {
    if (!existing.data) return;
    const schedule = existing.data;
    setName(schedule.name);
    setSourceId(schedule.source_id);
    setTimezone(schedule.timezone);
    setMisfire(schedule.misfire_policy);
    setOverlap(schedule.overlap_policy);
    if (schedule.rule.kind === 'daily') {
      setFrequency('daily');
      setLocalTime(schedule.rule.local_time);
    } else {
      setFrequency('interval');
      const seconds = Number(schedule.rule.every_seconds ?? 3600);
      if (seconds % 86_400 === 0) {
        setIntervalUnit('days');
        setIntervalValue(seconds / 86_400);
      } else if (seconds % 3600 === 0) {
        setIntervalUnit('hours');
        setIntervalValue(seconds / 3600);
      } else {
        setIntervalUnit('minutes');
        setIntervalValue(Math.max(1, Math.round(seconds / 60)));
      }
    }
  }, [existing.data]);

  const seconds = useMemo(() => {
    const factor = INTERVAL_UNITS.find((unit) => unit.key === intervalUnit)?.factor ?? 3600;
    return Math.round(intervalValue * factor);
  }, [intervalUnit, intervalValue]);

  const intervalValid = seconds >= 60 && seconds <= 31_536_000;

  const buildRequest = (): ScheduleCreateRequest | null => {
    if (!name.trim() || !sourceId) return null;
    if (frequency === 'interval' && !intervalValid) return null;
    return {
      name: name.trim(),
      source_id: sourceId,
      rule:
        frequency === 'daily'
          ? { kind: 'daily', local_time: localTime }
          : { kind: 'interval', every_seconds: seconds, anchor_at: null },
      timezone,
      misfire_policy: misfire,
      overlap_policy: overlap,
      options: { schema_version: 3 },
      index_collection_id: null,
    } as ScheduleCreateRequest;
  };

  const submit = () => {
    const request = buildRequest();
    if (!request) {
      toast({
        title: 'Dados inválidos',
        description: 'Informe nome, fonte e uma regra válida (intervalo entre 60 s e 365 dias).',
        variant: 'error',
      });
      return;
    }
    if (id && existing.data) {
      updateSchedule.mutate(
        {
          scheduleId: id,
          revision: existing.data.revision,
          changes: request as unknown as Record<string, unknown>,
        },
        {
          onSuccess: () => toast({ title: 'Agenda atualizada', variant: 'success' }),
          onError: (error) =>
            toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
        },
      );
      return;
    }
    createSchedule.mutate(request, {
      onSuccess: (schedule) => {
        toast({ title: 'Agenda criada', variant: 'success' });
        navigate(`/schedules/${schedule.id}`);
      },
      onError: (error) => toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
    });
  };

  const runPreview = () => {
    const request = buildRequest();
    if (!request) return;
    preview.mutate(request, {
      onError: (error) =>
        toast({ title: 'Prévia inválida', description: explainError(error), variant: 'error' }),
    });
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title={id ? 'Editar agenda' : 'Nova agenda'}
        description="A mesma regra calcula a prévia e o disparo real — nunca um timer no navegador."
        backTo={{ to: '/schedules', label: 'Agendamentos' }}
      />

      {sources.data && sources.data.length === 0 ? (
        <EmptyBlock
          title="Cadastre uma fonte primeiro"
          description="Uma agenda sempre referencia uma fonte autorizada."
          action={
            <Button asChild size="sm">
              <Link to="/sources">Ir para Fontes</Link>
            </Button>
          }
        />
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <CalendarClock className="h-4 w-4" aria-hidden /> Regra
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1">
            <span className="text-sm font-medium">Nome</span>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Importação diária" />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Fonte</span>
            <Select value={sourceId} onValueChange={setSourceId}>
              <SelectTrigger aria-label="Fonte da agenda">
                <SelectValue placeholder="Escolha a fonte" />
              </SelectTrigger>
              <SelectContent>
                {(sources.data ?? []).map((source) => (
                  <SelectItem key={source.id} value={source.id}>
                    {source.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Frequência</span>
            <Select value={frequency} onValueChange={(value) => setFrequency(value as 'daily' | 'interval')}>
              <SelectTrigger aria-label="Frequência">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="daily">Diária (horário local)</SelectItem>
                <SelectItem value="interval">Intervalo</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Fuso horário</span>
            <Select value={timezone} onValueChange={setTimezone}>
              <SelectTrigger aria-label="Fuso horário">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TIMEZONES.map((zone) => (
                  <SelectItem key={zone} value={zone}>
                    {zone}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {frequency === 'daily' ? (
            <div className="space-y-1">
              <span className="text-sm font-medium">Horário local</span>
              <Input
                type="time"
                value={localTime}
                onChange={(event) => setLocalTime(event.target.value)}
              />
            </div>
          ) : (
            <div className="space-y-1">
              <span className="text-sm font-medium">Intervalo</span>
              <div className="flex gap-2">
                <Input
                  type="number"
                  min={1}
                  value={intervalValue}
                  onChange={(event) => setIntervalValue(Number(event.target.value))}
                />
                <Select
                  value={intervalUnit}
                  onValueChange={(value) => setIntervalUnit(value as 'minutes' | 'hours' | 'days')}
                >
                  <SelectTrigger aria-label="Unidade do intervalo" className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {INTERVAL_UNITS.map((unit) => (
                      <SelectItem key={unit.key} value={unit.key}>
                        {unit.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <p className={intervalValid ? 'text-xs text-muted' : 'text-xs text-danger'}>
                Duração: {seconds} s {intervalValid ? '' : '(mínimo 60 s, máximo 365 dias)'}
              </p>
            </div>
          )}

          <div className="space-y-1">
            <span className="text-sm font-medium">Horários perdidos</span>
            <Select value={misfire} onValueChange={(value) => setMisfire(value as 'run_once_latest' | 'skip')}>
              <SelectTrigger aria-label="Política de horários perdidos">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="run_once_latest">Executar a última ocorrência</SelectItem>
                <SelectItem value="skip">Ignorar e seguir</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Sobreposição</span>
            <Select value={overlap} onValueChange={(value) => setOverlap(value as 'skip' | 'queue_one')}>
              <SelectTrigger aria-label="Política de sobreposição">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="skip">Ignorar se houver execução ativa</SelectItem>
                <SelectItem value="queue_one">Enfileirar no máximo uma</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
            <Button onClick={submit} disabled={createSchedule.isPending || updateSchedule.isPending}>
              {id ? 'Salvar alterações' : 'Criar agenda'}
            </Button>
            <Button variant="outline" onClick={runPreview} disabled={preview.isPending}>
              <Eye className="mr-1 h-4 w-4" aria-hidden /> Prévia (5 ocorrências)
            </Button>
            {existing.data ? <Badge variant="muted">revisão {existing.data.revision}</Badge> : null}
          </div>

          {preview.data ? (
            <div className="sm:col-span-2">
              <p className="mb-1 text-xs uppercase tracking-wide text-muted">
                Próximas ocorrências ({preview.data.timezone})
              </p>
              <ul className="grid gap-1 text-sm">
                {preview.data.next_occurrences.map((instant) => (
                  <li key={instant} className="text-foreground">
                    {formatDateTime(instant)}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {id ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Execuções</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {(runs.data ?? []).length === 0 ? (
              <p className="text-muted">Nenhuma execução registrada ainda.</p>
            ) : (
              <ul className="divide-y divide-border/60">
                {(runs.data ?? []).map((run) => (
                  <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <Link to={`/schedule-runs/${run.id}`} className="text-accent hover:underline">
                      {formatDateTime(run.scheduled_for)}
                    </Link>
                    <span className="flex items-center gap-2 text-xs text-muted">
                      <Badge variant={run.state === 'failed' ? 'danger' : 'muted'}>
                        {RUN_STATE_LABELS[run.state] ?? run.state}
                      </Badge>
                      <span>{run.trigger}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
