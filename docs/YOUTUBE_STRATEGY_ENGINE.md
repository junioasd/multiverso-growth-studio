# YouTube Strategy Engine

`youtube_strategy/` — Seções 22-25.

- `strategy_engine.py`: `request_growth_action()` recusa explicitamente qualquer tática da lista proibida da Seção 24 (fake views/likes/comments, bot farms, fake subscribers, spam, engagement manipulation, account farming, clickbait enganoso, metadata stuffing, mass duplicate content, policy bypass) — levanta `ProhibitedTacticRequested`. `recommend_focus()` trabalha apenas com sinais legítimos (retenção, watch time, CTR, satisfação, intenção de busca, relevância de audiência, histórico do canal).
- `alignment_checker.py`: checklist da Seção 23 antes de publicar — qualquer resposta ausente (`None`) é tratada como pendência (`unresolved_questions`), não como aprovação.
- `change_radar.py`: registro local de mudanças de política reportadas por fonte autorizada (Owner ou boletim oficial) — não faz scraping.
