# Changelog

Todas as mudanças relevantes deste projeto são documentadas aqui.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/), e o
projeto adota [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Não lançado]

### Alterado

- Licença: projeto proprietário, com todos os direitos reservados (antes, a licença não estava definida).

### Corrigido

- CORS da API passa a liberar o cabeçalho `Cache-Control`, enviado pelo stream
  SSE do frontend, para que o progresso funcione quando web e API estão em
  origens diferentes (Vercel + backend).

### Removido

- Arquivos de contribuição, canal de segurança e templates de issue/PR.

## [1.0.0] - 2026-11-16

### Adicionado

- Primeira versão pública da plataforma Conversor Generator, uma conversão
  documental auditável.
- **Motor de conversão** (`services/converter`, FastAPI): inspeção de PDF
  (criptografia, conteúdo ativo, limites), extração de texto nativo, OCR por
  página, layout e ordem de leitura, extração de campos `Rótulo: valor`, tabelas
  (lattice, stream, híbrida, Docling opcional), reconciliação multipágina,
  normalização de tipos, geração de XLSX e auditor do arquivo gerado.
- **Entradas e saídas**: PDF nativo, digitalizado e híbrido; imagens (PNG, JPEG,
  BMP, WebP, TIFF, GIF e outros codecs suportados). Saídas XLSX, TXT e CSV, geradas
  a partir do mesmo modelo intermediário.
- **Auditoria**: eventos encadeados por hash, manifesto com SHA-256, cobertura por
  página, proveniência célula a célula, quarentena de artefatos reprovados.
- **OCR**: GLM-OCR via Ollama (opcional, local), Tesseract e RapidOCR, com
  degradação explícita quando nenhum motor está disponível.
- **Assistente local (opcional)**: RAG sobre conversões do próprio usuário, com
  citações validadas pelo backend, usando Qwen3.5-4B via Ollama. Desativado por
  padrão.
- **API pública** (`/api/v1`) com dois modos de execução: `standalone` (FastAPI,
  SQLite, disco local, fila em threads) e `full` (NestJS 11 + Prisma com
  PostgreSQL ou SQLite, proxy para o motor, filas Celery/Redis opcionais, S3/MinIO
  opcional).
- **Interface** (`apps/web`, React 19 + Vite 7): upload com validação, progresso
  em tempo real por SSE com retomada, visualização do resultado e auditoria em 12
  abas.
- **Sessão local por navegador**: identidade por dispositivo, sem cadastro e sem
  senha, com isolamento de proprietário em todos os endpoints.
- **Pacote de contratos** (`packages/contracts`) com tipos zod e especificação
  OpenAPI.
- **Infraestrutura**: `infra/docker-compose.yml` (PostgreSQL, Redis, MinIO, motor,
  worker Celery, API e nginx), imagens Docker da API, do motor e da interface.
- **Documentação** em `docs/`: arquitetura, conversão, auditoria, banco,
  API e estratégia de testes.
- **Publicação**: `vercel.json` para a interface como SPA, workflow de integração
  contínua (`.github/workflows/ci.yml`) e guia de publicação em `docs/DEPLOY.md`.

### Conhecido e não implementado

- Estado `REVIEW_REQUIRED` e endpoint de revisão humana: ausentes. Veja
  [docs/API.md](docs/API.md#fora-do-escopo-atual-lacunas-declaradas).
- OCR por célula ou linha existe no componente, mas ainda não é acionado pelo
  pipeline.
- A execução com Docker Compose não foi verificada no ambiente de construção.
