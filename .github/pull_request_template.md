## Resumo

<!-- O que muda e por quê, em poucas linhas. -->

## Tipo de mudança

- [ ] Correção de bug
- [ ] Nova funcionalidade
- [ ] Refatoração / manutenção
- [ ] Documentação
- [ ] Dependências / CI

## Checklist

- [ ] `npm --prefix apps/web test` e `npm --prefix apps/api test` passam
- [ ] `pytest tests -q` em `services/converter` passa (quando aplicável)
- [ ] Contrato em `packages/contracts` atualizado e espelhado no converter (quando aplicável)
- [ ] Nenhum segredo, `.env`, banco ou documento pessoal incluído no diff
