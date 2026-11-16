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
  INTERNAL: 'INTERNAL',
  CONVERTER_UNAVAILABLE: 'CONVERTER_UNAVAILABLE',
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
  INTERNAL: 'Erro interno inesperado. A ocorrência foi registrada.',
  CONVERTER_UNAVAILABLE: 'O motor de conversão está indisponível no momento.',
};
