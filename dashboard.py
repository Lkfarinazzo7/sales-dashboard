#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║   Dashboard de Vendas — Meetime + Agendor            ║
║   Como usar:                                         ║
║     1. Configure seus tokens abaixo (ou via .env)    ║
║     2. pip install flask requests python-dotenv      ║
║     3. python dashboard.py                           ║
║     4. Abra http://localhost:5000 no navegador       ║
╚══════════════════════════════════════════════════════╝
"""

import os, json, time, threading, random
from datetime import datetime, timedelta, date
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests
except ImportError:
    print("❌ Instale as dependências: pip install flask requests")
    exit(1)

try:
    from flask import Flask, jsonify, render_template_string
except ImportError:
    print("❌ Instale as dependências: pip install flask requests")
    exit(1)

# ============================================================
# ⚙️  CONFIGURAÇÃO — coloque seus tokens aqui
# ============================================================
MEETIME_TOKEN  = os.environ.get("MEETIME_TOKEN",  "1202a929057d950c0dd8ddf4c6a47e4d")
AGENDOR_TOKEN  = os.environ.get("AGENDOR_TOKEN",  "9dfb5f7d-ca2f-4d1b-b98a-72beaba702f1")

MEETIME_BASE   = "https://api.meetime.com.br/v2"
AGENDOR_BASE   = "https://api.agendor.com.br/v3"

REFRESH_INTERVAL = 300   # segundos entre atualizações automáticas (5 min)
DAYS_RANGE       = 30    # janela de dias para os gráficos

# ============================================================
app   = Flask(__name__)
cache = {}
_mt_auth_ok = False  # imprime mensagem de auth apenas uma vez

def _mt_request(method, url, **kwargs):
    """
    Meetime usa apiKey direto no header Authorization (sem prefixo).
    Ref: OpenAPI spec - securitySchemes.ApiKeyAuth.type = apiKey
    Tenta: token puro -> Bearer -> Token -> query param
    """
    global _mt_auth_ok
    for auth_value in (
        MEETIME_TOKEN,                   # formato correto: token puro
        f"Bearer {MEETIME_TOKEN}",
        f"Token {MEETIME_TOKEN}",
    ):
        try:
            headers = {"Authorization": auth_value, "Content-Type": "application/json"}
            r = requests.request(method, url, headers=headers, **kwargs)
            if r.status_code != 401:
                if auth_value == MEETIME_TOKEN and not _mt_auth_ok:
                    print("  [Meetime] Auth OK: token puro no header Authorization")
                    _mt_auth_ok = True
                return r
        except Exception:
            raise
    # ultima tentativa: query param
    params = dict(kwargs.pop("params", {}) or {})
    params["api_token"] = MEETIME_TOKEN
    r = requests.request(method, url, params=params, **kwargs)
    return r

def ag_headers():
    return {"Authorization": f"Token {AGENDOR_TOKEN}",  "Content-Type": "application/json"}

# ── Meetime ──────────────────────────────────────────────────

def fetch_meetime_won_leads():
    # Datas em UTC conforme exigido pela API Meetime
    since_dt = datetime.now() - timedelta(days=DAYS_RANGE)
    until_dt = datetime.now()
    since    = since_dt.strftime("%Y-%m-%dT00:00:00")
    until    = until_dt.strftime("%Y-%m-%dT23:59:59")

    all_items, start = [], 0
    total_api    = None
    sample_shown = False
    pages_loaded = 0

    while True:
        try:
            r = _mt_request(
                "GET", f"{MEETIME_BASE}/prospections",
                params={
                    "status":     "WON",
                    "end_after":  since,
                    "end_before": until,
                    "limit":      100,
                    "start":      start,
                },
                timeout=15
            )
            r.raise_for_status()
            raw  = r.json()
            rows = raw.get("data", []) if isinstance(raw, dict) else raw
            pages_loaded += 1

            if total_api is None:
                total_api = raw.get("totalItems", "?") if isinstance(raw, dict) else "?"
                print(f"  [Meetime] Total WON retornado pela API: {total_api}")

            if not rows:
                break

            # Debug: mostra TODOS os campos do 1o item para descobrir campo de data
            if not sample_shown:
                first = rows[0]
                print(f"  [Meetime] Todos os campos do 1o item WON:")
                for k, v in first.items():
                    print(f"    {k} = {repr(v)}")
                sample_shown = True

            # Filtro local pela end_date (campo correto no Meetime e snake_case)
            filtered = []
            for p in rows:
                # Campo de data WON no Meetime e "end_date" (snake_case)
                date_raw = (p.get("end_date") or p.get("endDate") or
                            p.get("last_activity_date") or p.get("created_date") or "")
                if date_raw:
                    try:
                        end_dt = datetime.fromisoformat(
                            date_raw.replace("Z", "").replace("+00:00", ""))
                        if since_dt <= end_dt <= until_dt:
                            filtered.append(p)
                        continue
                    except Exception:
                        pass
                # Se nenhuma data disponivel, confia no filtro da API
                filtered.append(p)

            all_items.extend(filtered)
            print(f"  [Meetime] Pagina {pages_loaded}: {len(rows)} itens, {len(filtered)} aceitos")

            # Paginacao via campo "next" da resposta
            next_url = raw.get("next") if isinstance(raw, dict) else None
            if not next_url or len(rows) < 100:
                break
            start += 100
        except Exception as e:
            print(f"  Meetime prospections start={start}: {e}")
            break

    print(f"  [Meetime] TOTAL leads ganhos no periodo: {len(all_items)} (em {pages_loaded} paginas)")
    return all_items

def fetch_meetime_users():
    try:
        r = _mt_request("GET", f"{MEETIME_BASE}/users", timeout=15)
        r.raise_for_status()
        raw = r.json()
        return raw.get("data", raw) if isinstance(raw, dict) else raw
    except Exception as e:
        print(f"  ⚠️  Meetime users: {e}")
        return []

# ── Agendor ──────────────────────────────────────────────────

def fetch_agendor_won_deals():
    """
    Busca negócios ganhos no Agendor com mais tolerância.

    Correções principais:
    - Usa /deals diretamente em vez de depender de /deals/stream.
    - Pagina até não haver mais linhas, sem depender apenas de totalPages.
    - Considera status que contenha "ganho" ou "won", além de wonAt/isWon/won.
    - Mantém filtro local pela data de ganho/encerramento/atualização dentro de DAYS_RANGE.
    """
    since_dt = datetime.now() - timedelta(days=DAYS_RANGE)
    all_items = []
    page = 1
    per_page = 100
    debug_done = False
    unique_statuses = set()

    while True:
        try:
            r = requests.get(
                f"{AGENDOR_BASE}/deals",
                headers=ag_headers(),
                params={
                    "per_page": per_page,
                    "page": page,
                },
                timeout=20
            )
            r.raise_for_status()
            raw = r.json()
            rows = raw.get("data", []) if isinstance(raw, dict) else []

            print(f"  [Agendor] Página {page}: {len(rows)} negócios retornados")

            if not rows:
                break

            for d in rows:
                status = d.get("dealStatus") or {}
                status_name = ""
                status_id = ""

                if isinstance(status, dict):
                    status_name = str(status.get("name", "")).strip().lower()
                    status_id = str(status.get("id", ""))
                    unique_statuses.add(f"id={status_id} name='{status_name}'")
                else:
                    status_name = str(status).strip().lower()
                    unique_statuses.add(status_name)

                if not debug_done:
                    print("  [DEBUG Agendor] Primeiro negócio retornado:")
                    print(json.dumps(d, indent=2, ensure_ascii=False)[:3000])
                    debug_done = True

                is_won = (
                    "ganho" in status_name or
                    "won" in status_name or
                    d.get("won") is True or
                    d.get("isWon") is True or
                    bool(d.get("wonAt"))
                )

                if not is_won:
                    continue

                won_raw = (
                    d.get("wonAt") or
                    d.get("endTime") or
                    d.get("updatedAt") or
                    d.get("createdAt") or
                    ""
                )

                if won_raw:
                    try:
                        won_dt = datetime.fromisoformat(
                            won_raw.replace("Z", "").replace("+00:00", "")
                        )
                        if won_dt >= since_dt:
                            all_items.append(d)
                    except Exception:
                        # Se a API vier com formato de data inesperado, não descarta venda ganha.
                        all_items.append(d)
                else:
                    # Se não houver data, não descarta venda ganha.
                    all_items.append(d)

            if len(rows) < per_page:
                break

            page += 1

        except Exception as e:
            print(f"  [Agendor] Erro p{page}: {e}")
            break

    print(f"  [DEBUG Agendor] Status encontrados: {unique_statuses}")
    print(f"  [DEBUG Agendor] Total ganhos no período: {len(all_items)}")
    return all_items

def fetch_agendor_users():
    try:
        r = requests.get(f"{AGENDOR_BASE}/users", headers=ag_headers(), timeout=15)
        r.raise_for_status()
        raw = r.json()
        return raw.get("data", [])
    except Exception as e:
        print(f"  ⚠️  Agendor users: {e}")
        return []

# ── Agregação ────────────────────────────────────────────────

def parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ",     "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s.replace("+00:00", "Z"), fmt.replace("%z", "Z")
                                     if "%z" in fmt else fmt)
        except Exception:
            continue
    return None

def build_date_labels():
    today = date.today()
    return [(today - timedelta(days=i)).strftime("%d/%m") for i in range(DAYS_RANGE - 1, -1, -1)]

def aggregate(won_leads, won_deals, mt_users, ag_users):
    mt_map = {str(u.get("id")): u.get("name", "Desconhecido") for u in mt_users}
    ag_map = {str(u.get("id")): u.get("name", "Desconhecido") for u in ag_users}

    labels        = build_date_labels()
    leads_by_day  = defaultdict(int)
    deals_by_day  = defaultdict(int)
    value_by_day  = defaultdict(float)
    seller_leads  = defaultdict(int)
    seller_deals  = defaultdict(int)
    seller_value  = defaultdict(float)

    for lead in won_leads:
        # Meetime usa snake_case: end_date e o campo da data WON
        raw_dt = (lead.get("end_date") or lead.get("last_activity_date") or
                  lead.get("created_date") or "")
        dt = parse_date(raw_dt)
        if dt:
            leads_by_day[dt.strftime("%d/%m")] += 1
        # Meetime: vendedor esta em owner_name (string direta)
        name = (lead.get("owner_name") or
                lead.get("salesman_name") or
                str(lead.get("owner_id") or "Sem usuario"))
        seller_leads[name] += 1

    for deal in won_deals:
        # Agendor: wonAt e o campo correto para data de fechamento
        raw_dt = (deal.get("wonAt") or deal.get("updatedAt") or
                  deal.get("createdAt") or "")
        dt = parse_date(raw_dt)
        val = float(deal.get("value") or deal.get("dealValue") or 0)
        if dt:
            deals_by_day[dt.strftime("%d/%m")] += 1
            value_by_day[dt.strftime("%d/%m")] += val
        # Agendor: responsavel pelo negocio fica em "owner"
        owner = deal.get("owner") or deal.get("user") or {}
        if isinstance(owner, dict):
            uid = str(owner.get("id") or "")
            name = owner.get("name") or ag_map.get(uid, "Sem usuário")
        else:
            uid = str(owner or "")
            name = ag_map.get(uid, "Sem usuário")
        seller_deals[name] += 1
        seller_value[name] += val

    total_leads = len(won_leads)
    total_deals = len(won_deals)
    total_value = sum(float(d.get("value") or d.get("dealValue") or 0) for d in won_deals)
    conv_rate   = round(total_deals / total_leads * 100, 1) if total_leads else 0

    all_sellers = set(list(seller_leads) + list(seller_deals))
    ranking = sorted(
        [{"name": s,
          "leads": seller_leads.get(s, 0),
          "deals": seller_deals.get(s, 0),
          "value": seller_value.get(s, 0)}
         for s in all_sellers],
        key=lambda x: x["deals"] * 2 + x["leads"],
        reverse=True
    )[:10]

    return {
        "kpis": {
            "total_leads":     total_leads,
            "total_deals":     total_deals,
            "total_value":     total_value,
            "conv_rate":       conv_rate,
        },
        "time_series": {
            "labels":      labels,
            "leads":       [leads_by_day.get(d, 0) for d in labels],
            "deals":       [deals_by_day.get(d, 0) for d in labels],
            "deals_value": [value_by_day.get(d, 0)  for d in labels],
        },
        "funnel": {
            "labels": ["Leads Prospectados", "Leads Ganhos\n(Meetime)", "Contratos Fechados\n(Agendor)"],
            "values": [max(total_leads * 3, 1), max(total_leads, 1), max(total_deals, 1)],
        },
        "ranking":     ranking,
        "last_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "demo":        False,
    }

# ── Demo data ────────────────────────────────────────────────

def demo_data():
    rng    = random.Random(42)
    labels = build_date_labels()
    leads  = [rng.randint(3, 18) for _ in labels]
    deals  = [max(0, l - rng.randint(0, 8)) for l in leads]
    values = [d * rng.uniform(2500, 18000) for d in deals]
    tl, td = sum(leads), sum(deals)
    tv     = sum(values)
    return {
        "kpis":  {"total_leads": tl, "total_deals": td,
                  "total_value": tv, "conv_rate": round(td/tl*100,1) if tl else 0},
        "time_series": {"labels": labels, "leads": leads, "deals": deals, "deals_value": values},
        "funnel": {"labels": ["Leads Prospectados","Leads Ganhos\n(Meetime)","Contratos Fechados\n(Agendor)"],
                   "values": [tl*3, tl, td]},
        "ranking": [
            {"name":"Ana Silva",      "leads":52,"deals":14,"value":182000},
            {"name":"Carlos Mendes",  "leads":44,"deals":11,"value":143000},
            {"name":"Juliana Costa",  "leads":36,"deals": 9,"value":118000},
            {"name":"Roberto Lima",   "leads":29,"deals": 7,"value": 91000},
            {"name":"Fernanda Souza", "leads":22,"deals": 5,"value": 65000},
        ],
        "last_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S") + "  ⚠️ Dados de demonstração",
        "demo": True,
    }

# ── Refresh ──────────────────────────────────────────────────

def refresh():
    configured = (MEETIME_TOKEN != "SEU_TOKEN_MEETIME_AQUI" and
                  AGENDOR_TOKEN  != "SEU_TOKEN_AGENDOR_AQUI")
    if not configured:
        print("  ⚠️  Tokens não configurados — usando dados de demonstração")
        cache["data"] = demo_data()
        return
    try:
        print("  🔄 Buscando dados das APIs...")
        wl = fetch_meetime_won_leads()
        wd = fetch_agendor_won_deals()
        mu = fetch_meetime_users()
        au = fetch_agendor_users()
        print(f"  📊 Meetime: {len(wl)} leads ganhos | {len(mu)} usuários")
        print(f"  📊 Agendor: {len(wd)} contratos fechados | {len(au)} usuários")
        if len(wl) == 0 and len(wd) == 0:
            print("  ℹ️  Nenhum dado nos últimos 30 dias — verifique o período no Meetime/Agendor")
        cache["data"] = aggregate(wl, wd, mu, au)
        print(f"  ✅ Dashboard atualizado com sucesso")
    except Exception as e:
        print(f"  ❌ Erro: {e} — usando dados de demonstração")
        cache["data"] = demo_data()

def auto_refresh():
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh()

# ── HTML Template ─────────────────────────────────────────────

HTML = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard de Vendas · Meetime + Agendor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0e1117;--surface:#161b27;--surface2:#1e2535;--border:#2a3347;
    --text:#e2e8f0;--text2:#94a3b8;--accent:#6366f1;--accent2:#22d3ee;
    --green:#10b981;--orange:#f59e0b;--red:#ef4444;--purple:#a855f7;
    --card-r:12px;--shadow:0 4px 24px rgba(0,0,0,.4);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;
       min-height:100vh;overflow-x:hidden}

  /* ── TOP BAR ── */
  .topbar{background:var(--surface);border-bottom:1px solid var(--border);
          padding:0 28px;height:60px;display:flex;align-items:center;
          justify-content:space-between;position:sticky;top:0;z-index:100;
          backdrop-filter:blur(8px)}
  .logo{display:flex;align-items:center;gap:12px}
  .logo-icon{width:36px;height:36px;background:linear-gradient(135deg,var(--accent),var(--accent2));
             border-radius:8px;display:grid;place-items:center;font-size:18px}
  .logo-text{font-size:16px;font-weight:700;letter-spacing:-.3px}
  .logo-sub{font-size:11px;color:var(--text2);margin-top:1px}
  .topbar-right{display:flex;align-items:center;gap:16px}
  .badge{background:var(--surface2);border:1px solid var(--border);border-radius:20px;
         padding:4px 12px;font-size:12px;color:var(--text2)}
  .badge.demo{background:#3b1c0820;border-color:#f59e0b60;color:var(--orange)}
  .refresh-btn{background:var(--accent);border:none;color:#fff;padding:7px 16px;
               border-radius:8px;font-size:13px;cursor:pointer;transition:.2s;font-weight:600}
  .refresh-btn:hover{opacity:.85;transform:translateY(-1px)}
  .refresh-btn:active{transform:scale(.97)}

  /* ── MAIN ── */
  main{padding:24px 28px;max-width:1600px;margin:0 auto}

  /* ── KPI CARDS ── */
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
  @media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}}
  .kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--card-r);
       padding:20px 22px;display:flex;flex-direction:column;gap:8px;
       box-shadow:var(--shadow);transition:.2s;position:relative;overflow:hidden}
  .kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
               background:var(--kpi-color,var(--accent))}
  .kpi:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.5)}
  .kpi-icon{font-size:24px}
  .kpi-label{font-size:12px;color:var(--text2);font-weight:500;text-transform:uppercase;
             letter-spacing:.5px}
  .kpi-value{font-size:32px;font-weight:800;letter-spacing:-1px}
  .kpi-sub{font-size:12px;color:var(--text2)}

  /* ── CHART GRID ── */
  .grid{display:grid;gap:16px;margin-bottom:16px}
  .grid-2{grid-template-columns:1fr 1fr}
  .grid-3{grid-template-columns:2fr 1fr}
  @media(max-width:1100px){.grid-2,.grid-3{grid-template-columns:1fr}}

  /* ── CHART CARD ── */
  .card{background:var(--surface);border:1px solid var(--border);
        border-radius:var(--card-r);padding:20px 22px;box-shadow:var(--shadow)}
  .card-header{display:flex;align-items:center;justify-content:space-between;
               margin-bottom:16px}
  .card-title{font-size:14px;font-weight:700;color:var(--text)}
  .card-subtitle{font-size:12px;color:var(--text2);margin-top:2px}
  .chart-wrap{position:relative;height:220px}
  .chart-wrap.tall{height:280px}
  .chart-wrap.short{height:180px}

  /* ── RANKING TABLE ── */
  .ranking-table{width:100%;border-collapse:collapse}
  .ranking-table th{font-size:11px;color:var(--text2);text-transform:uppercase;
                    letter-spacing:.5px;padding:8px 10px;border-bottom:1px solid var(--border);
                    text-align:left;font-weight:600}
  .ranking-table td{padding:10px 10px;font-size:13px;border-bottom:1px solid var(--border)20}
  .ranking-table tr:last-child td{border:none}
  .rank-num{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;
            font-size:11px;font-weight:800;margin-right:8px;flex-shrink:0}
  .rank-1{background:#f59e0b30;color:var(--orange)}
  .rank-2{background:#94a3b830;color:var(--text2)}
  .rank-3{background:#f97316 20;color:#f97316}
  .rank-n{background:var(--surface2);color:var(--text2)}
  .rank-name{display:flex;align-items:center}
  .bar-mini{height:6px;border-radius:3px;background:var(--accent);margin-top:4px;
            transition:width .6s ease}
  .value-chip{background:var(--green)20;color:var(--green);border-radius:6px;
              padding:2px 8px;font-size:12px;font-weight:700}

  /* ── FOOTER ── */
  footer{text-align:center;padding:16px;font-size:12px;color:var(--text2);
         border-top:1px solid var(--border);margin-top:8px}

  /* ── SPINNER ── */
  .spinner{display:none;position:fixed;inset:0;background:#0e111780;
           place-items:center;z-index:999;font-size:48px}
  .spinner.show{display:grid}
  @keyframes spin{to{transform:rotate(360deg)}}
  .spin-icon{animation:spin 1s linear infinite;display:inline-block}
</style>
</head>
<body>

<div class="spinner" id="spinner"><span class="spin-icon">⟳</span></div>

<!-- TOP BAR -->
<div class="topbar">
  <div class="logo">
    <div class="logo-icon">📊</div>
    <div>
      <div class="logo-text">Dashboard de Vendas</div>
      <div class="logo-sub">Meetime · Agendor · Tempo real</div>
    </div>
  </div>
  <div class="topbar-right">
    <span class="badge" id="update-badge">Carregando...</span>
    <button class="refresh-btn" onclick="loadData(true)">⟳ Atualizar</button>
  </div>
</div>

<!-- MAIN -->
<main>

  <!-- KPIs -->
  <div class="kpis">
    <div class="kpi" style="--kpi-color:#6366f1">
      <span class="kpi-icon">🏆</span>
      <div class="kpi-label">Leads Ganhos</div>
      <div class="kpi-value" id="kpi-leads">—</div>
      <div class="kpi-sub">Meetime · últimos 30 dias</div>
    </div>
    <div class="kpi" style="--kpi-color:#10b981">
      <span class="kpi-icon">📝</span>
      <div class="kpi-label">Contratos Fechados</div>
      <div class="kpi-value" id="kpi-deals">—</div>
      <div class="kpi-sub">Agendor · últimos 30 dias</div>
    </div>
    <div class="kpi" style="--kpi-color:#22d3ee">
      <span class="kpi-icon">💰</span>
      <div class="kpi-label">Valor Total</div>
      <div class="kpi-value" id="kpi-value">—</div>
      <div class="kpi-sub">Receita acumulada</div>
    </div>
    <div class="kpi" style="--kpi-color:#a855f7">
      <span class="kpi-icon">🎯</span>
      <div class="kpi-label">Taxa de Conversão</div>
      <div class="kpi-value" id="kpi-conv">—</div>
      <div class="kpi-sub">Leads → Contratos</div>
    </div>
  </div>

  <!-- CHART ROW 1 -->
  <div class="grid grid-2">
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">📈 Leads Ganhos — Meetime</div>
          <div class="card-subtitle">Prospecções com status WON nos últimos 30 dias</div>
        </div>
      </div>
      <div class="chart-wrap tall"><canvas id="chartLeads"></canvas></div>
    </div>
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">📊 Contratos Fechados — Agendor</div>
          <div class="card-subtitle">Negócios marcados como ganhos</div>
        </div>
      </div>
      <div class="chart-wrap tall"><canvas id="chartDeals"></canvas></div>
    </div>
  </div>

  <!-- CHART ROW 2 -->
  <div class="grid grid-3">
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">💵 Valor Acumulado por Dia</div>
          <div class="card-subtitle">Receita de contratos fechados (R$)</div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chartValue"></canvas></div>
    </div>
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">🔽 Funil de Conversão</div>
          <div class="card-subtitle">Da prospecção ao contrato</div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chartFunnel"></canvas></div>
    </div>
  </div>

  <!-- RANKING -->
  <div class="card">
    <div class="card-header">
      <div>
        <div class="card-title">🏅 Ranking de Vendedores</div>
        <div class="card-subtitle">Top 10 · Leads ganhos (Meetime) + Contratos fechados (Agendor)</div>
      </div>
    </div>
    <div id="ranking-container">
      <table class="ranking-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Vendedor</th>
            <th style="text-align:center">Leads Ganhos</th>
            <th style="text-align:center">Contratos</th>
            <th style="text-align:right">Valor (R$)</th>
          </tr>
        </thead>
        <tbody id="ranking-body"></tbody>
      </table>
    </div>
  </div>

</main>

<footer>
  Dashboard de Vendas &middot; Meetime + Agendor &middot;
  Atualização automática a cada 5 minutos
</footer>

<script>
const fmt_num  = n => Number(n).toLocaleString('pt-BR');
const fmt_brl  = n => 'R$ ' + Number(n).toLocaleString('pt-BR',{minimumFractionDigits:0,maximumFractionDigits:0});
const fmt_pct  = n => n + '%';

const COLORS = {
  accent:  '#6366f1', accent2: '#22d3ee',
  green:   '#10b981', orange:  '#f59e0b',
  purple:  '#a855f7', red:     '#ef4444',
  bg:      '#161b27', border:  '#2a3347', text: '#94a3b8'
};

Chart.defaults.color       = COLORS.text;
Chart.defaults.borderColor = COLORS.border;
Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";

let charts = {};

function destroyChart(id){ if(charts[id]){ charts[id].destroy(); delete charts[id]; } }

function mkLineChart(id, labels, datasets){
  destroyChart(id);
  const ctx = document.getElementById(id).getContext('2d');
  charts[id] = new Chart(ctx, {
    type:'line',
    data:{ labels, datasets },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ display: datasets.length > 1,
        labels:{ boxWidth:10, padding:16, usePointStyle:true, pointStyleWidth:8 }}},
      scales:{
        x:{ grid:{ color:'#2a334740' }, ticks:{ maxTicksLimit:10, font:{size:11} }},
        y:{ grid:{ color:'#2a334740' }, ticks:{ font:{size:11} }, beginAtZero:true }
      },
      elements:{ line:{ tension:.4, borderWidth:2.5 }, point:{ radius:0, hoverRadius:5 }},
      interaction:{ mode:'index', intersect:false }
    }
  });
}

function mkBarChart(id, labels, datasets, opts={}){
  destroyChart(id);
  const ctx = document.getElementById(id).getContext('2d');
  charts[id] = new Chart(ctx, {
    type:'bar',
    data:{ labels, datasets },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ display: datasets.length > 1,
        labels:{ boxWidth:10, padding:16, usePointStyle:true }}},
      scales:{
        x:{ grid:{ display:false }, ticks:{ maxTicksLimit:10, font:{size:11} }},
        y:{ grid:{ color:'#2a334740' }, ticks:{ font:{size:11}, ...opts.yTicks }, beginAtZero:true }
      },
      interaction:{ mode:'index', intersect:false },
      ...opts.extra
    }
  });
}

function mkFunnelChart(id, labels, values){
  destroyChart(id);
  const max = Math.max(...values);
  const ctx = document.getElementById(id).getContext('2d');
  const colors = [COLORS.accent, COLORS.accent2, COLORS.green];
  charts[id] = new Chart(ctx, {
    type:'bar',
    data:{
      labels: labels.map(l => l.replace('\n',' ')),
      datasets:[{
        data: values,
        backgroundColor: colors.map(c => c + '99'),
        borderColor: colors,
        borderWidth: 2,
        borderRadius: 6,
      }]
    },
    options:{
      indexAxis:'y',
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ display:false },
        tooltip:{ callbacks:{ label: ctx => '  ' + fmt_num(ctx.raw) + ' registros' }}},
      scales:{
        x:{ grid:{ color:'#2a334740' }, max: max * 1.15, ticks:{ font:{size:11} }},
        y:{ grid:{ display:false }, ticks:{ font:{ size:12, weight:'600' }}}
      }
    }
  });
}

function renderKpis(d){
  document.getElementById('kpi-leads').textContent = fmt_num(d.total_leads);
  document.getElementById('kpi-deals').textContent = fmt_num(d.total_deals);
  document.getElementById('kpi-value').textContent = fmt_brl(d.total_value);
  document.getElementById('kpi-conv').textContent  = fmt_pct(d.conv_rate);
}

function renderRanking(rows){
  const body = document.getElementById('ranking-body');
  const maxL = Math.max(...rows.map(r => r.leads), 1);
  const maxD = Math.max(...rows.map(r => r.deals), 1);
  body.innerHTML = rows.map((r, i) => {
    const cls  = ['rank-1','rank-2','rank-3'][i] || 'rank-n';
    const pctL = Math.round(r.leads / maxL * 100);
    const pctD = Math.round(r.deals / maxD * 100);
    return `<tr>
      <td><div class="rank-num ${cls}">${i+1}</div></td>
      <td class="rank-name" style="font-weight:600">${r.name}</td>
      <td style="text-align:center">
        <div>${fmt_num(r.leads)}</div>
        <div class="bar-mini" style="width:${pctL}%;background:${COLORS.accent}"></div>
      </td>
      <td style="text-align:center">
        <div>${fmt_num(r.deals)}</div>
        <div class="bar-mini" style="width:${pctD}%;background:${COLORS.green}"></div>
      </td>
      <td style="text-align:right"><span class="value-chip">${fmt_brl(r.value)}</span></td>
    </tr>`;
  }).join('');
}

function gradLine(ctx, color1, color2){
  const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height);
  g.addColorStop(0, color1 + '55');
  g.addColorStop(1, color1 + '00');
  return g;
}

async function loadData(forceRefresh=false){
  document.getElementById('spinner').classList.add('show');
  try {
    if(forceRefresh) await fetch('/api/refresh');
    const res  = await fetch('/api/data');
    const data = await res.json();

    renderKpis(data.kpis);
    renderRanking(data.ranking);

    const ts = data.time_series;

    // Leads chart
    mkLineChart('chartLeads', ts.labels, [{
      label: 'Leads Ganhos',
      data: ts.leads,
      borderColor: COLORS.accent,
      backgroundColor: (ctx) => gradLine(ctx, COLORS.accent),
      fill: true,
    }]);

    // Deals chart
    mkLineChart('chartDeals', ts.labels, [{
      label: 'Contratos Fechados',
      data: ts.deals,
      borderColor: COLORS.green,
      backgroundColor: (ctx) => gradLine(ctx, COLORS.green),
      fill: true,
    }]);

    // Value bar chart
    mkBarChart('chartValue', ts.labels, [{
      label: 'Valor (R$)',
      data: ts.deals_value,
      backgroundColor: COLORS.accent2 + '80',
      borderColor: COLORS.accent2,
      borderWidth: 1.5,
      borderRadius: 4,
    }], {
      yTicks: { callback: v => v >= 1000 ? 'R$' + (v/1000).toFixed(0) + 'k' : 'R$' + v }
    });

    // Funnel
    mkFunnelChart('chartFunnel', data.funnel.labels, data.funnel.values);

    // Badge
    const badge = document.getElementById('update-badge');
    badge.textContent = '⏱ ' + data.last_update;
    badge.className   = 'badge' + (data.demo ? ' demo' : '');

  } catch(e) {
    console.error(e);
  } finally {
    document.getElementById('spinner').classList.remove('show');
  }
}

// Auto-refresh a cada 5 minutos
loadData();
setInterval(() => loadData(false), 300_000);
</script>
</body>
</html>
"""

# ── Routes ───────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/data")
def api_data():
    if "data" not in cache:
        refresh()
    return jsonify(cache.get("data", demo_data()))

@app.route("/api/refresh")
def api_refresh():
    refresh()
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

if __name__ == "__main__":
    mt_ok = MEETIME_TOKEN != "SEU_TOKEN_MEETIME_AQUI"
    ag_ok = AGENDOR_TOKEN != "SEU_TOKEN_AGENDOR_AQUI"
    print("")
    print("=" * 55)
    print("  Dashboard de Vendas - Meetime + Agendor")
    print("=" * 55)
    print("  Meetime Token : " + ("OK" if mt_ok else "NAO configurado"))
    print("  Agendor Token : " + ("OK" if ag_ok else "NAO configurado"))
    print("=" * 55)
    refresh()
    t = threading.Thread(target=auto_refresh, daemon=True)
    t.start()
    print("")
    print("  Dashboard disponivel em: http://localhost:5000")
    print("")
    app.run(debug=False, port=5000, host="0.0.0.0")
