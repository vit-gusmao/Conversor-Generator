# Contribuindo

Obrigado pelo interesse em contribuir. Este guia descreve como preparar o
ambiente, organizar commits e Pull Requests e rodar os testes de cada pacote.

## Antes de começar

- Leia o [README](README.md) para entender a arquitetura e os dois modos de
  execução (`standalone` e `full`).
- Para mudanças de comportamento, consulte antes os documentos em `docs/`:
  [CONVERSION.md](docs/CONVERSION.md), [AUDIT.md](docs/AUDIT.md),
  [API.md](docs/API.md) e [SECURITY.md](docs/SECURITY.md).
- Vulnerabilidades não devem ser reportadas em issues públicas. Use o canal
  descrito em [SECURITY.md](SECURITY.md).

## Configuração do ambiente

Pré-requisitos: Node.js 22, Python 3.11 ou superior (a imagem do motor usa 3.12),
Git. Tesseract e Ollama são opcionais.

```bash
git clone https://github.com/OWNER/REPO.git
cd REPO

cp .env.example .env                       # Windows (cmd): copy .env.example .env

# Node: contracts, API e web
npm run install:all

# Python: motor com as dependências de teste
cd services/converter
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"     # Linux/macOS: .venv/bin/pip install -e ".[test]"
cd ../..
```

Para subir o sistema, veja a seção "Início rápido" do [README](README.md).

## Estrutura do repositório

| Caminho | Conteúdo |
| --- | --- |
| `apps/api` | API NestJS 11 + Prisma (identidade, ownership, SSE, proxy do motor) |
| `apps/web` | Interface React 19 + Vite 7 |
| `packages/contracts` | tipos zod e `openapi/public-api.yaml`. Mudanças de contrato começam aqui |
| `services/converter` | motor Python (FastAPI, pipeline, auditoria, exportadores) |
| `infra/` | docker-compose, nginx, PostgreSQL, observabilidade |
| `docs/` | documentação técnica |

Mudanças que afetam o contrato público (`packages/contracts`, `docs/API.md`)
devem atualizar os três pontos juntos: contrato, implementação nas duas APIs
(NestJS no modo `full` e FastAPI no modo `standalone`) e a interface.

## Testes por pacote

Rode os testes do pacote que você alterou. Antes de abrir um PR, rode os de todos
os pacotes afetados.

```bash
# Motor Python (com o venv ativo, ou .venv/Scripts/python)
cd services/converter && python -m pytest tests -q

# API NestJS
npm --prefix apps/api run typecheck
npm --prefix apps/api test
npm --prefix apps/api run test:e2e

# Interface
npm --prefix apps/web run typecheck
npm --prefix apps/web test
npm --prefix apps/web run build

# Contratos
npm --prefix packages/contracts run typecheck
```

Atalhos da raiz: `npm test` (API e interface) e `npm run test:py` (motor).

Os PDFs de teste são gerados por `services/converter/tests/fixtures/generate_pdfs.py`.
Ao adicionar um caso, siga o procedimento de [docs/TESTING.md](docs/TESTING.md#como-adicionar-um-caso).

Nunca faça commit de documentos reais.

## Estilo de código

Ferramentas configuradas no repositório:

- **Python**: `[tool.ruff]` em `services/converter/pyproject.toml` (comprimento de
  linha 110, Python 3.11, regras `E`, `F`, `I`, `UP`, `B`). O ruff não está entre as
  dependências do projeto; instale-o localmente (`pip install ruff`) e rode
  `ruff check services/converter`.
- **TypeScript**: os pacotes têm `tsconfig.json` próprio. A verificação de tipos é
  feita com os scripts `typecheck` listados acima.

O repositório **não** configura ESLint, Prettier nem `.editorconfig`. Não
introduza formatadores por conta própria sem alinhar antes na issue ou no PR.

## Commits

Recomendamos [Conventional Commits](https://www.conventionalcommits.org/pt-br/):

```
<tipo>(<escopo opcional>): <descrição no imperativo>

[corpo opcional, explicando o porquê]
```

Tipos usuais: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`,
`chore`. Exemplos:

```
fix(converter): preserva zeros à esquerda em identificadores
docs(deploy): descreve a ordem de publicação da Vercel
feat(api): adiciona filtro de status em GET /conversions
```

Mantenha cada commit focado em uma mudança lógica. Não misture refatoração e
mudança de comportamento no mesmo commit.

## Branches

- `main` é a branch principal e deve estar sempre verde.
- Crie branches curtas a partir de `main`, com prefixos como `feat/`, `fix/` e
  `docs/`. Exemplo: `fix/zeros-a-esquerda`.

## Pull Requests

Antes de pedir revisão, confirme o checklist:

- [ ] A mudança tem um objetivo claro, descrito no título e no corpo do PR.
- [ ] Os testes do pacote alterado passam localmente.
- [ ] Novos comportamentos têm teste. Correções de bug têm um teste que falharia
      antes da correção.
- [ ] A verificação de tipos (`typecheck`) passa nos pacotes afetados.
- [ ] Mudanças de contrato atualizam `packages/contracts`, `docs/API.md` e a
      implementação nas duas APIs.
- [ ] Mudanças de comportamento visíveis atualizam a documentação relevante em
      `docs/` e o `CHANGELOG.md` (seção "Não lançado").
- [ ] Nenhum segredo, `.env`, banco ou PDF real foi incluído.
- [ ] A mudança não introduz saída de documentos para serviços externos. O OCR e o
      assistente são locais por projeto (veja [docs/SECURITY.md](docs/SECURITY.md)).
- [ ] Se a mudança toca a auditoria ou a proveniência, a cadeia de hash e os
      testes de regressão foram verificados.

Ao revisar, verifique também se a mudança mantém as regras de saída: o XLSX não
contém imagens nem fórmulas, e texto iniciado por `=`, `+`, `-` ou `@` continua
sendo texto.

## Licença das contribuições

O projeto ainda não tem licença definida (veja a seção "Licença" do README).
Não envie código de terceiros sem indicar a origem e a licença compatível.
