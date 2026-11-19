# Plano de evolução do Conversor Generator

**Data:** 18/11/2026. **Base analisada:** commit `ca3c08a` e árvore de trabalho disponível nesta sessão. **Status:** proposta técnica; nenhuma funcionalidade deste diretório foi implementada.

Este conjunto descreve como acrescentar ao projeto atual:

1. Aplicativo Windows em **Electron**, com executável `conversor-generator.exe`, reutilizando a interface e o motor existentes.
2. Configuração visual dos caminhos e modelos locais de GLM-OCR e do LLM usado pelo assistente com RAG.
3. Cadastro de arquivos e pastas para importação por caminho, incluindo entrada real de planilhas XLSX e CSV.
4. Agendamento diário ou por intervalo, com administração no site e no desktop, histórico e recuperação após interrupções.

## Ordem de leitura

| Documento | Conteúdo |
| --- | --- |
| [01 — Diagnóstico](01-diagnostico-projeto-atual.md) | O que existe, como funciona, como o histórico registra sua evolução e onde integrar as mudanças |
| [02 — Arquitetura e contratos](02-arquitetura-e-contratos.md) | Decisões propostas, ambientes, responsabilidades, dados, APIs e isolamento |
| [03 — Desktop e modelos](03-desktop-electron-e-modelos.md) | Inicialização, empacotamento, configuração de IA, segurança, atualização e encerramento |
| [04 — Caminhos e planilhas](04-importacao-por-caminhos-e-planilhas.md) | Seleção de pastas, leitura, upload, deduplicação, interpretação de XLSX/CSV e RAG |
| [05 — Agendador](05-agendador-site-e-desktop.md) | Calendário, estados, concorrência, falhas, retomada e telas |
| [06 — Implementação e validação](06-plano-de-implementacao-e-validacao.md) | Dependências, tarefas por arquivo, testes, entregas e critérios de aceite |

## Premissas propostas, não decisões já aprovadas

- Windows x64 é o primeiro alvo. Outras plataformas exigem seu próprio empacotamento e validação.
- “Planilhas” é interpretado literalmente: XLSX e CSV de entrada, além dos PDFs e imagens já aceitos. Se a intenção era apenas selecionar PDFs que viram planilhas, a etapa de novos leitores pode ser retirada sem alterar fontes e agendamento.
- O site atual é local. A primeira entrega compartilha funcionalidades entre navegador local e desktop; não transforma o produto em um serviço público. O comportamento em servidor remoto é delimitado e uma extensão de conector é descrita separadamente.
- Não haverá cadastro/login obrigatório: preservar identidade por dispositivo/sessão. O desktop terá identidade persistente própria; seus dados não serão automaticamente os mesmos do navegador.
- A automação padrão será: ler fonte → importar arquivos novos/alterados → converter/exportar → opcionalmente indexar para RAG → registrar resultado. Executar prompts arbitrários, comandos de terminal e macros não faz parte dessa tarefa.
- Agendamentos só executam com o runtime responsável ligado. No desktop, manter na bandeja permite fechar a janela. Com o processo encerrado ou computador desligado, a ocorrência é tratada na retomada.
- “LLM para ser o RAG” significa selecionar o modelo que responde usando trechos recuperados. Modelo de linguagem, modelo de embeddings e armazenamento de conhecimento são componentes distintos.

## Resultado esperado

Uma mesma interface React terá **Fontes**, **Agendamentos** e configurações de **IA local**. O desktop acrescenta seletor nativo e gerenciamento dos processos locais. O backend continua sendo a autoridade para uploads, conversões, resultados, identidade e execução de tarefas.

O primeiro pacote deverá instalar e abrir sem Python, Node ou Docker previamente instalados. Ollama e pesos de modelos são dependências locais configuráveis; não estarão embutidos automaticamente no instalador. PDFs com texto nativo e importação de planilhas devem continuar utilizáveis sem modelos disponíveis.

## Limites desta análise

Foi feita análise estática da estrutura, documentação, histórico Git, contratos, pontos de entrada e fluxos dos subsistemas. Não é uma auditoria de cada linha nem certificação de ausência de bugs. Não foram executadas conversões, testes de produto, instalações ou builds de Electron. Os testes descritos são trabalho futuro. Referências externas oficiais aparecem nos documentos correspondentes; versões exatas de novas dependências serão fixadas e verificadas na implementação.
