# Segurança, privacidade e retenção

PDF é arquivo não confiável por definição. O sistema trata cada upload como
potencialmente hostil.

## Superfície de ataque e defesas

| Ameaça | Defesa implementada |
| --- | --- |
| Extensão falsa | validação por assinatura `%PDF-`, não por MIME/nome |
| Arquivo gigante | limite de bytes no gateway **e** no motor |
| Bomba de páginas | limite de páginas antes de abrir o conteúdo |
| PDF criptografado | detectado; sem senha → erro explícito |
| JavaScript / `/Launch` / `/OpenAction` | detectado e **nunca executado** |
| Arquivos embutidos / XFA | ignorados; presença registrada na auditoria |
| Zip bomb no XLSX | o auditor abre com limite de memória do processo |
| Path traversal na storage | chave validada em Node e Python (raiz confinada) |
| Injeção de fórmula no Excel | texto iniciado por `=`/`+`/`-`/`@` é gravado como **texto literal** (`data_type="s"`), nunca como fórmula; o auditor reabre o arquivo e reprova qualquer célula de tipo `f` |
| Acesso a conversão alheia | `owner_id` verificado em **todos** os endpoints (403) |
| Reenvio duplicado | `Idempotency-Key` por proprietário |
| Enumeração de IDs | UUID v4 + 404 para inexistente, 403 para alheio |
| Força bruta de login | não se aplica: não existe cadastro, login nem senha |
| Sessão perdida ao limpar cache | identidade local também fica em cookie (`pdf2x_device_id`, HttpOnly) e em cookie legível de fallback; limpar só o localStorage não troca o proprietário |
| Vazamento em logs | logs não incluem conteúdo de célula nem valores pessoais |
| Credencial na query string | `access_token`, `device_id`, ticket, `token`, `api_key` e `secret` são redigidos antes de qualquer linha de log (inclusive no proxy) |
| PDF compartilhado apagado | chaves de storage têm prefixo do proprietário; a exclusão só remove o objeto quando nenhum outro documento o referencia |
| Artefato reprovado baixado | falha fatal de auditoria coloca o artefato em quarentena e bloqueia o download (409 `ARTIFACT_QUARANTINED`) |
| Hash de artefato adulterado | download compara o SHA-256 do arquivo com o registrado na auditoria |
| Prompt injection no RAG | texto recuperado é dado; o prompt instrui a ignorar comandos e o assistente não tem ferramentas, shell ou acesso a arquivos |
| Vazamento entre proprietários no RAG | `owner_id` e coleções autorizadas filtram os chunks **antes** do ranking/top-k; citações só aceitam IDs da recuperação da requisição |
| Índice de fontes privadas | indexação apenas de conversões do próprio usuário; nunca varredura automática de diretório, `.env`, bancos ou binários |
| Imagem maliciosa | detecção por assinatura, limites de bytes/quadros/megapixels antes do decode e erro explícito para codec ausente |
| SVG ativo | declarado indisponível sem rasterizador isolado; não há execução de script/rede |

## Segredos de desenvolvimento

`CONVERTER_INTERNAL_TOKEN` e `JWT_SECRET` têm valores padrão para o modo
`standalone` funcionar sem configuração. Ao iniciar, o motor registra um aviso
explícito quando eles continuam com o valor de fábrica do motor
(`dev-internal-token-change-me` e `dev-jwt-secret-change-me`). Em qualquer
implantação exposta, defina ambos (ver `.env.example`); o gateway valida o token
interno em todas as rotas `/internal/v1`.

Atenção: o gateway NestJS usa outro padrão para `JWT_SECRET` quando a variável não
está definida (`dev-only-insecure-secret-change-me`, em
`apps/api/src/config/configuration.ts`). O aviso do motor não cobre esse valor.
Defina `JWT_SECRET` explicitamente na API.

## Isolamento de processamento

- O motor roda como usuário sem privilégios (uid 10001) no container.
- Sem porta pública: apenas `api` e `web` são expostos; `converter` e `worker`
  vivem na rede interna.
- Timeouts por tarefa (`CONVERTER_TASK_TIMEOUT_SECONDS`) e limite de tentativas
  de fallback impedem consumo indefinido.
- O worker Celery usa `--max-tasks-per-child` para conter vazamentos de memória.
- Nenhuma chamada externa é feita com o conteúdo do documento. O OCR local
  (GLM-OCR via Ollama) é usado por padrão para páginas sem texto; o assistente
  Qwen é opcional. Ambos apontam para loopback (`GLMOCR_HOST`,
  `ASSISTANT_HOST`) e não enviam documentos para serviços remotos.

## Sessão local (sem conta)

- Não existe cadastro, login nem senha. Cada navegador recebe uma identidade
  determinística por `x-device-id` + cookie HttpOnly `pdf2x_device_id`.
- JWT HS256 (`JWT_SECRET`) de curta duração é emitido para essa identidade e
  renovado quando expira; não há troca de identidade silenciosa.
- `AUTH_MODE=optional` mantém as conversões privadas por navegador sem exigir
  conta. `AUTH_MODE=required` aceita JWTs emitidos por um emissor confiável que
  compartilhe o `JWT_SECRET`; não há tela de login nesta aplicação.
- Limpar cookies **e** cache cria um novo proprietário local e um workspace
  vazio; o anterior deixa de ser acessível por esse navegador.
- Quando o frontend e a API ficam em domínios diferentes (por exemplo, Vercel e
  um host de containers), o cookie `pdf2x_device_id` (SameSite=Lax) não é enviado
  nas requisições cross-site. A identidade passa então pelo cabeçalho
  `x-device-id`, com o valor guardado no `localStorage` do domínio do frontend.
  Limpar o `localStorage` nesse caso também cria um novo proprietário.

## Armazenamento

- Chaves opacas e escopadas por proprietário:
  `documents/<owner_id>/ab/<sha256>.pdf`, `runs/<run_id>/output.xlsx` e
  `runs/<run_id>/exports/<export_id>.<ext>`.
- Bucket privado; o download passa pelo gateway autenticado, nunca por URL
  pública. (S3 suporta URLs assinadas, mas o padrão é proxy.)
- PDFs originais e XLSX têm prazos distintos de retenção.

## Retenção e LGPD

| Dado | Padrão | Configuração |
| --- | --- | --- |
| PDF original | 7 dias | `RETENTION_PDF_DAYS` |
| XLSX gerado | 30 dias | `RETENTION_XLSX_DAYS` |
| Auditoria (eventos + manifesto) | 90 dias | `RETENTION_AUDIT_DAYS` |

- Finalidade: converter o documento enviado e permitir auditoria da conversão.
- Base legal e prazo devem ser definidos pelo operador conforme o tipo de
  documento (trabalhista, financeiro, pessoal).
- `DELETE /conversions/:id` faz exclusão lógica e remove os artefatos do
  storage.
- Os prazos acima são gravados como metadado de retenção nos artefatos e
  informados em `/settings`. No código atual não há rotina agendada que aplique
  esses prazos automaticamente. Defina essa rotina na operação antes de tratar
  dados reais (veja `docs/DEPLOY.md`).
- A cópia temporária criada para normalizar `/Rotate` (necessária porque o
  pdfplumber não aplica a rotação) é **apagada ao final de cada execução**. Se a
  remoção falhar, um issue `WORK_COPY_CLEANUP_FAILED` é registrado na auditoria.
- Direitos do titular: a exclusão remove conversão, artefatos e referências;
  eventos de auditoria podem ser mantidos de forma pseudonimizada pelo prazo
  legal aplicável.

## Segredos

- `.env` nunca é versionado (`.gitignore`).
- `CONVERTER_INTERNAL_TOKEN` protege todas as rotas internas; `/health` é a
  única exceção (necessária para readiness).
- Em produção, injete segredos por variável de ambiente ou gerenciador de
  segredos; o compose exige `CONVERTER_INTERNAL_TOKEN` e `JWT_SECRET`.

## Licenças de dependências

| Biblioteca | Licença | Uso |
| --- | --- | --- |
| pdfplumber / pdfminer.six | MIT | extração de texto e linhas |
| pypdf | BSD | inspeção, rotação, criptografia |
| pypdfium2 | Apache-2.0 / BSD | rasterização |
| openpyxl | MIT | XLSX |
| **GLM-OCR** (modelo + SDK `glmocr`) | Apache-2.0 | OCR principal (VLM local via Ollama) |
| Ollama | MIT | servidor local do modelo (GPU) |
| Tesseract | Apache-2.0 | OCR (fallback) |
| RapidOCR | Apache-2.0 | OCR (fallback) |
| Docling | MIT | estrutura de tabelas (opcional) |
| Camelot | MIT | tabelas (opcional, requer Ghostscript) |
| **PyMuPDF** | **AGPL / comercial** | **não utilizado pelo motor** |

O motor foi desenhado para funcionar sem PyMuPDF justamente para não impor
obrigações AGPL a um produto proprietário.

> Nota sobre o GLM-OCR: o SDK oficial (`glmocr`) opera por padrão em modo
> **MaaS**, enviando documentos ao serviço em nuvem da Zhipu, e o `GlmOcr()`
> seleciona esse modo automaticamente se `ZHIPU_API_KEY` estiver no ambiente.
> Este projeto **não usa o cliente MaaS**: `engine/ocr/glmocr.py` fala
> diretamente com o Ollama em `127.0.0.1`, sem chave de API e sem saída de
> dados pela rede. Nenhum documento sai da máquina.

## O que a auditoria não promete

- Não prova que o reconhecimento está correto; prova que **toda transformação
  instrumentada foi registrada** e que o XLSX corresponde ao plano.
- A cadeia de hash detecta alteração, mas um administrador com acesso total ao
  banco pode reconstruí-la. Checkpoints assinados externamente são a mitigação.
