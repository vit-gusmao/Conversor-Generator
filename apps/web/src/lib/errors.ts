import { ApiError, isApiError } from '@/lib/api';

const CODE_MESSAGES: Record<string, string> = {
  INVALID_FILE: 'O arquivo enviado não é um PDF válido ou está corrompido.',
  FILE_TOO_LARGE: 'O arquivo excede o tamanho máximo permitido.',
  TOO_MANY_PAGES: 'O PDF excede o número máximo de páginas permitido.',
  UNAUTHENTICATED: 'Sua sessão expirou. Recarregue a página para continuar.',
  FORBIDDEN: 'Você não tem permissão para acessar esta conversão.',
  NOT_FOUND: 'Não encontramos o recurso solicitado.',
  ALREADY_TERMINAL: 'Esta conversão já foi finalizada e não pode ser alterada.',
  IDEMPOTENT_REPLAY: 'Este upload já foi processado anteriormente.',
  VALIDATION_ERROR: 'Alguns dados enviados são inválidos.',
  RATE_LIMITED: 'Muitas requisições. Aguarde um instante e tente novamente.',
  QUEUE_FULL: 'A fila de conversões do servidor está cheia. Tente novamente em instantes.',
  STORAGE_UNAVAILABLE: 'O servidor está sem espaço para receber novos arquivos.',
  INTERNAL: 'A API encontrou um erro interno. Tente novamente.',
  CONVERTER_UNAVAILABLE: 'O motor de conversão está indisponível no momento.',
  NETWORK_ERROR: 'Não foi possível contatar a API. Verifique sua conexão e tente novamente.',
  INVALID_RESPONSE: 'A API respondeu com um formato inesperado.',
  REVISION_CONFLICT:
    'Esta agenda foi alterada em outra aba ou sessão. Recarregue a versão mais recente e reaplique suas mudanças.',
  SCHEDULE_NOT_FOUND: 'Esta agenda não existe mais ou pertence a outra sessão.',
  RUN_NOT_FOUND: 'Esta execução não existe mais ou pertence a outra sessão.',
  SOURCE_NOT_FOUND: 'A fonte desta agenda não existe mais. Escolha outra fonte.',
  SOURCE_DISABLED: 'A fonte desta agenda está desativada. Reative-a para executar.',
};

/** A user-facing pt-BR message for any thrown value. */
export function explainError(error: unknown): string {
  if (isApiError(error)) {
    const mapped = CODE_MESSAGES[error.code];
    if (mapped) return mapped;
    return error.message || 'A requisição falhou.';
  }
  if (error instanceof Error && error.message) return error.message;
  return 'Ocorreu um erro inesperado.';
}

export function isRetryable(error: unknown): boolean {
  if (!isApiError(error)) return false;
  return error.status === 0 || error.status >= 500 || error.code === 'RATE_LIMITED';
}

export function apiErrorDetails(error: unknown): { code: string; message: string; details: unknown } | null {
  if (!(error instanceof ApiError)) return null;
  return { code: error.code, message: error.message, details: error.details };
}
