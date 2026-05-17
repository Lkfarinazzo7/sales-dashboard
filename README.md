# Sales Dashboard

Dashboard comercial Flask integrado ao Meetime e Agendor.

## Estado do projeto

- Flask
- Chart.js
- Render ready
- Tokens por variáveis de ambiente
- Correção aplicada para buscar vendas do Agendor via `/deals`

## Rodar localmente

```bash
pip install -r requirements.txt
python dashboard.py
```

Acesse:

```txt
http://localhost:5000
```

## Variáveis de ambiente

Crie um `.env` local com:

```env
MEETIME_TOKEN=seu_token_meetime
AGENDOR_TOKEN=seu_token_agendor
```

## Deploy no Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn dashboard:app --bind 0.0.0.0:$PORT
```

No Render, cadastre as variáveis:

- `MEETIME_TOKEN`
- `AGENDOR_TOKEN`
