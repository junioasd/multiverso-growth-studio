# Video Model Router

`core/adapters/video/router.py` — Seção 14.

- Perfis declarativos para Seedance, Kling, Veo, Sora (`KNOWN_PROVIDERS`), com qualidade/custo/latência de referência.
- `choose_provider()` filtra por requisitos da cena (3D, consistência de personagem, custo máximo, latência máxima) e escolhe o melhor candidato configurado.
- Sem API key configurada (`VIDEO_PROVIDER_*_KEY`) → cai automaticamente para `MockVideoProvider`, que nunca afirma execução real (`AdapterResult.mode == MOCK`).
- Adicionar um novo provider = criar um novo `VideoProviderProfile` + adapter; nenhuma mudança estrutural necessária (sem lock-in).
