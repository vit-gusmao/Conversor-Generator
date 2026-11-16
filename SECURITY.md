# Política de segurança

## Reportando uma vulnerabilidade

Não abra issues públicas para vulnerabilidades. Relate de forma privada pelo
recurso **Security Advisories** do GitHub:

1. Acesse a aba **Security** do repositório.
2. Clique em **Report a vulnerability**.
3. Descreva o problema, os passos para reproduzi-lo, a versão ou o commit
   afetado e, se possível, o impacto esperado.

Não inclua documentos reais, dados pessoais ou segredos no relato. Se for
necessário demonstrar a falha, use um PDF gerado por
`services/converter/tests/fixtures/generate_pdfs.py`.

## O que esperar

- Resposta dos mantenedores assim que possível, com avaliação do impacto.
- Se a falha for confirmada, correção em uma branch privada e publicação com
  aviso de segurança.
- Crédito ao relator, se desejado.

O projeto não oferece prazo formal de resposta (SLA).

## Escopo

Consideram-se relevantes, por exemplo: vazamento de conversões entre
proprietários, execução de conteúdo de PDF, injeção de fórmula no XLSX,
contorno de quarentena ou de validação de download, acesso às rotas internas sem
token, path traversal no armazenamento e vazamento de segredos em logs.

O modelo de ameaças, as defesas implementadas e as limitações conhecidas estão em
[docs/SECURITY.md](docs/SECURITY.md).

## Segredos de implantação

Não há segredos reais no repositório. Os valores padrão do código servem apenas
para desenvolvimento. Antes de expor qualquer instância, troque `JWT_SECRET` e
`CONVERTER_INTERNAL_TOKEN`, conforme [docs/DEPLOY.md](docs/DEPLOY.md).
