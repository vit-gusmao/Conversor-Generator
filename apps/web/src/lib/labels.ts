import type { ConversionStatus, QualityState, Severity, ValueType } from '@pdf2x/contracts';

export const STATUS_LABELS: Record<ConversionStatus, string> = {
  received: 'Recebido',
  validating: 'Validando',
  queued: 'Na fila',
  inspecting: 'Inspecionando',
  extracting: 'Extraindo',
  structuring: 'Estruturando',
  normalizing: 'Normalizando',
  reconciling: 'Conciliando',
  generating: 'Gerando Excel',
  auditing: 'Auditando',
  completed: 'Concluído',
  completed_with_warnings: 'Concluído com avisos',
  failed: 'Falhou',
  cancelled: 'Cancelado',
};

export const QUALITY_LABELS: Record<QualityState, string> = {
  validated: 'Validado',
  partial: 'Parcial',
  failed: 'Falhou',
  pending: 'Pendente',
};

export const SEVERITY_LABELS: Record<Severity, string> = {
  info: 'Informação',
  warning: 'Aviso',
  error: 'Erro',
  critical: 'Crítico',
};

export const VALUE_TYPE_LABELS: Record<ValueType, string> = {
  text: 'Texto',
  integer: 'Inteiro',
  decimal: 'Decimal',
  money: 'Moeda',
  date: 'Data',
  datetime: 'Data/hora',
  percentage: 'Percentual',
  identifier: 'Identificador',
  code: 'Código',
  competencia: 'Competência',
  boolean: 'Booleano',
  empty: 'Vazio',
  unknown: 'Desconhecido',
};

export const SHEET_KIND_LABELS: Record<string, string> = {
  consolidated: 'Todos os dados',
  records: 'Registros',
  table: 'Tabela',
  text: 'Texto',
  unclassified: 'Não classificado',
  metadata: 'Metadados',
};

export const ORIGIN_LABELS: Record<string, string> = {
  native: 'Nativo',
  ocr: 'OCR',
  form_field: 'Campo de formulário',
  mixed: 'Misto',
  none: 'Nenhum',
};

export const DESTINATION_LABELS: Record<string, string> = {
  field: 'Campo',
  table: 'Tabela',
  text: 'Texto',
  metadata: 'Metadado',
  header_footer: 'Cabeçalho/rodapé',
  unclassified: 'Não classificado',
  dropped: 'Descartado',
};

export const CLASSIFICATION_LABELS: Record<string, string> = {
  native: 'Nativo',
  scan: 'Digitalizado',
  hybrid: 'Híbrido',
  image: 'Imagem',
  mixed: 'Misto',
  empty: 'Vazio',
};

export const STRATEGY_LABELS: Record<string, string> = {
  lattice: 'Lattice (bordas)',
  stream: 'Stream (alinhamento)',
  network: 'Network',
  hybrid: 'Híbrida',
  docling: 'Docling',
  transposed: 'Transposta',
  manual: 'Manual',
};

export const STAGE_LABELS: Record<string, string> = {
  receive: 'Recebimento',
  validate: 'Validação',
  inspect: 'Inspeção',
  extract: 'Extração',
  ocr: 'OCR',
  structure: 'Estruturação',
  field_resolution: 'Resolução de campos',
  table_detection: 'Detecção de tabelas',
  normalize: 'Normalização',
  reconcile: 'Conciliação',
  generate: 'Geração do Excel',
  audit: 'Auditoria',
  done: 'Concluído',
  failed: 'Falha',
};
