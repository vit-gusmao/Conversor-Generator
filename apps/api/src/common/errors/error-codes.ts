/**
 * Public error codes. The set mirrors docs/API.md plus a few additive codes
 * needed for flows the table does not enumerate (login, download readiness).
 */
export const ERROR_CODES = {
  INVALID_FILE: 'INVALID_FILE',
  FILE_TOO_LARGE: 'FILE_TOO_LARGE',
  TOO_MANY_PAGES: 'TOO_MANY_PAGES',
  UNAUTHENTICATED: 'UNAUTHENTICATED',
  INVALID_CREDENTIALS: 'INVALID_CREDENTIALS',
  EMAIL_ALREADY_EXISTS: 'EMAIL_ALREADY_EXISTS',
  FORBIDDEN: 'FORBIDDEN',
  NOT_FOUND: 'NOT_FOUND',
  ALREADY_TERMINAL: 'ALREADY_TERMINAL',
  CONVERSION_BUSY: 'CONVERSION_BUSY',
  IDEMPOTENT_REPLAY: 'IDEMPOTENT_REPLAY',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  RATE_LIMITED: 'RATE_LIMITED',
  NOT_READY: 'NOT_READY',
  STORAGE_UNAVAILABLE: 'STORAGE_UNAVAILABLE',
  QUEUE_FULL: 'QUEUE_FULL',
  INTERNAL: 'INTERNAL',
  CONVERTER_UNAVAILABLE: 'CONVERTER_UNAVAILABLE',
  // -- batches -----------------------------------------------------------
  BATCH_NOT_OPEN: 'BATCH_NOT_OPEN',
  BATCH_HAS_PENDING_UPLOADS: 'BATCH_HAS_PENDING_UPLOADS',
  BATCH_ITEM_MISMATCH: 'BATCH_ITEM_MISMATCH',
  BATCH_CANCELLED: 'BATCH_CANCELLED',
  // -- resumable uploads -------------------------------------------------
  UPLOAD_NOT_FOUND: 'UPLOAD_NOT_FOUND',
  UPLOAD_STATE_CONFLICT: 'UPLOAD_STATE_CONFLICT',
  UPLOAD_PART_MISMATCH: 'UPLOAD_PART_MISMATCH',
  UPLOAD_INCOMPLETE: 'UPLOAD_INCOMPLETE',
  UPLOAD_EXPIRED: 'UPLOAD_EXPIRED',
  UPLOAD_CHECKSUM_MISMATCH: 'UPLOAD_CHECKSUM_MISMATCH',
  // -- options / delivery ------------------------------------------------
  INVALID_OPTION: 'INVALID_OPTION',
  UNSUPPORTED_TARGET_FORMAT: 'UNSUPPORTED_TARGET_FORMAT',
  INCOMPATIBLE_OUTPUT_FORMAT: 'INCOMPATIBLE_OUTPUT_FORMAT',
  QUEUE_DELIVERY_FAILED: 'QUEUE_DELIVERY_FAILED',
  INSUFFICIENT_STORAGE: 'INSUFFICIENT_STORAGE',
  RATE_LIMIT_PRESSURE: 'RATE_LIMIT_PRESSURE',
} as const;

export type ErrorCode = (typeof ERROR_CODES)[keyof typeof ERROR_CODES];

/** Every user-facing message is pt-BR (see docs/API.md error table). */
export const ERROR_MESSAGES: Record<ErrorCode, string> = {
  INVALID_FILE: 'O arquivo enviado não é um PDF válido.',
  FILE_TOO_LARGE: 'O arquivo excede o tamanho máximo permitido.',
  TOO_MANY_PAGES: 'O PDF excede o número máximo de páginas permitido.',
  UNAUTHENTICATED: 'Autenticação necessária para acessar este recurso.',
  INVALID_CREDENTIALS: 'E-mail ou senha inválidos.',
  EMAIL_ALREADY_EXISTS: 'Este e-mail já está cadastrado.',
  FORBIDDEN: 'Você não tem permissão para acessar esta conversão.',
  NOT_FOUND: 'Recurso não encontrado.',
  ALREADY_TERMINAL: 'A conversão já foi finalizada e não pode ser alterada.',
  CONVERSION_BUSY: 'Já existe uma execução em andamento para esta conversão.',
  IDEMPOTENT_REPLAY: 'Chave de idempotência já utilizada com outro conteúdo.',
  VALIDATION_ERROR: 'Dados inválidos na requisição.',
  RATE_LIMITED: 'Muitas requisições. Tente novamente em instantes.',
  NOT_READY: 'O resultado ainda não está disponível para download.',
  STORAGE_UNAVAILABLE: 'O armazenamento de arquivos não está disponível.',
  QUEUE_FULL:
    'A fila de conversões está cheia. Tente novamente em alguns instantes.',
  INTERNAL: 'Erro interno inesperado. A ocorrência foi registrada.',
  CONVERTER_UNAVAILABLE: 'O motor de conversão está indisponível no momento.',
  BATCH_NOT_OPEN: 'Este lote não aceita mais novos itens.',
  BATCH_HAS_PENDING_UPLOADS:
    'O lote ainda possui uploads pendentes e não pode ser selado.',
  BATCH_ITEM_MISMATCH: 'O item informado não pertence a este lote.',
  BATCH_CANCELLED: 'O lote foi cancelado e não aceita novas operações.',
  UPLOAD_NOT_FOUND: 'Sessão de upload não encontrada.',
  UPLOAD_STATE_CONFLICT: 'A sessão de upload não está no estado esperado.',
  UPLOAD_PART_MISMATCH:
    'A parte enviada não confere com o tamanho ou hash esperado para a posição.',
  UPLOAD_INCOMPLETE: 'Ainda faltam partes para concluir o upload.',
  UPLOAD_EXPIRED: 'A sessão de upload expirou. Inicie o envio novamente.',
  UPLOAD_CHECKSUM_MISMATCH: 'O hash do arquivo não confere com o conteúdo recebido.',
  INVALID_OPTION: 'Uma das opções enviadas não é suportada.',
  UNSUPPORTED_TARGET_FORMAT: 'O formato de saída solicitado não é suportado.',
  INCOMPATIBLE_OUTPUT_FORMAT:
    'O formato de saída escolhido não é compatível com este tipo de arquivo.',
  QUEUE_DELIVERY_FAILED:
    'A tarefa não pôde ser entregue ao motor de conversão. Tente novamente.',
  INSUFFICIENT_STORAGE: 'Não há espaço de armazenamento suficiente para receber o arquivo.',
  RATE_LIMIT_PRESSURE: 'O serviço está sob carga. Tente novamente em instantes.',
};

