# PILOTO CONTROLADO — VALIDAÇÃO REAL DE MERCADO
MULTIVERSO GROWTH STUDIO / ATLAS / ORION
Preparado em 2026-10-10 (America/Maceio). Status: PLANO, NÃO EXECUTADO NO MERCADO.

## 1. Identidade e fronteiras
Canal piloto já definido pelo Owner: Capital Decifrado (educação financeira).
Objetivo: comprovar demanda real de audiência, correspondência problema-conteúdo-oferta e atribuição observável.
Não confundir finanças com ofertas de cursos de IA; cada produto precisa de aderência de público verificada.
Modo inicial: pesquisa pública somente leitura, sem publicação, venda, acesso indevido ou gastos não aprovados.

## 2. Fontes externas REAL_VERIFIED
- Banco Central do Brasil, Comunicado nº 45.963 de 16/09/2026: Copom fixou meta Selic de 13,75% a.a. a partir de 17/09/2026. Fonte: https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?numero=45963&tipo=Comunicado
- Banco Central do Brasil, Ata do Copom de 16/09/2026, explicação contextual da decisão: https://www.bcb.gov.br/publicacoes/atascopom/16092026
- Hotmart, Termos de Uso, regras de divulgação de afiliados e vedações a spam e informações falsas: https://hotmart.com/pt-br/legal/termos-de-uso
- Hotmart, ajuda oficial sobre pesquisa no Mercado de Afiliação: https://help.hotmart.com/pt-BR/article/115006334868/como-pesquisar-produtos-no-mercado-de-afiliacao-da-hotmart

NOTA DE PROVENIÊNCIA: URLs oficiais verificadas por pesquisa em 10/10/2026. A taxa Copom é dado factual publicado, NÃO é prova de demanda comercial, CTR ou conversão. Confirmar atualização/vigência antes de produzir.
Não atribuir à Hotmart aprovação específica de oferta, comissão, elegibilidade ou venda sem consulta autenticada à conta e documentação correspondente.

## 3. Hipóteses para TESTAR, ainda NÃO verificadas
H1: dúvidas sobre rendimento de renda fixa com juros básicos elevados geram interesse do público de finanças.
H2: comparativos didáticos, claros e sem promessa de rentabilidade atraem atenção e engajamento qualificado.
H3: existe ao menos uma oferta legal e aderente disponível para afiliação sem conflito com o perfil do canal.
Toda hipótese começa como INFERRED/HYPOTHESIS; jamais é reclassificada como REAL_VERIFIED sem observação auditável.

## 4. Coleta real que o ORION deve executar após gates
1. Consultar fontes públicas e permitidas para termos de busca, conteúdos concorrentes, dúvidas observáveis e público; registrar URL exata, timestamp, plataforma e data do evento.
2. Comparar 3 hipóteses de conteúdo em indicadores observáveis; registrar ausência de dados como UNKNOWN, nunca zero.
3. Checar programa de afiliação e permissão do produtor antes de associar uma oferta; nunca inferir comissão ou preço.
4. Selecionar uma única hipótese de conteúdo e um único canal na primeira rodada; elaborar roteiro com checagem factual e compliance financeiro.
5. Solicitar aprovação do Owner para ação externa com custos, postagem, conta vinculada ou efeitos irreversíveis.
6. Após publicação autorizada, medir dados reais por plataforma: impressões, CTR, retenção, cliques rastreáveis, conversão e reembolso. Não inventar números.
7. Auditoria independente dos resultados, aprendizado documentado e decisão CONTINUAR/REVISAR/ABANDONAR.

## 5. Gates obrigatórios para GO
- CORE: API estável em concorrência; Worker heartbeat persistido com amostras sucessivas; Scheduler ticks SUCCESS sem janelas paradas; SQLite quick_check ok.
- AUDITORIA: regressões passam; backup diário local com SHA-256 válido; nenhuma fila presa; integridade dos logs.
- ATLAS: MCP initialize + atlas_health + leitura de job verdadeiro PASS (já confirmado); despacho, processamento e retorno de NOVA missão real ainda pendentes.
- CUSTO: verificar CostGuard e limite para provider PAID; 0 gasto até autorização Owner específica.
- EXTERNO: publicação e afiliado somente com permissão e aprovação; não confundir PUBLIC_EXPOSURE_READY=false com capacidade local de pesquisa somente leitura.
- MERCADO: dados reais com proveniência e datas; nenhum mock representa mercado.
- CONCLUSÃO: só declarar PRODUÇÃO APROVADA depois de E2E real autorizado e auditoria final.

## 6. Status constatado em 10/10/2026
- Máquina Windows: Scheduler restaurado e funcionando em amostras; Worker com heartbeat real, sujeito a histórico de intermitência.
- API: ThreadingHTTPServer aplicado e 17 testes HTTP PASS; liveness preservado quando readiness ficou lento.
- Readiness operacional pontual: True; técnico: DEGRADED por última falha local histórica em 02/10; exposição pública: False.
- Integridade backup: local novo, integrity_check=ok, SHA256 PASS; rotina diária MGS-Day0-Backup resultado Windows=0.
- MCP: atlas_health e atlas_get_task de ATLAS_TASK anterior REAL, COMPLETED, PASS.
- Aprovação Owner pendente no banco: navigator_controlled_noop_test (HIGH); não aprovar nem executar sem escopo explícito.
- Ainda não existe comprovação de scan de mercado NOVO em 10/10 via ORION nesta intervenção.
- Status final: NO-GO produção pública; preparar GO condicionado para piloto real mínimo somente após aprovação e reparos remanescentes.

## 7. Proteção de escopo
Não alterar kill switch, credenciais, custos, contas externas, publicação ou autorizações sem escopo específico. Não enviar a mercado sinais simulados. Não estabelecer metas como resultados já obtidos.
