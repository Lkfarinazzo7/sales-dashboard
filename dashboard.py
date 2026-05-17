#!/usr/bin/env python3
import os
import time
import random
import threading
from datetime import datetime, timedelta, date
from collections import defaultdict

from dotenv import load_dotenv
import requests
from flask import Flask, jsonify, render_template_string

load_dotenv()

MEETIME_TOKEN = os.getenv("MEETIME_TOKEN", "")
AGENDOR_TOKEN = os.getenv("AGENDOR_TOKEN", "")
MEETIME_BASE = "https://api.meetime.com.br/v2"
AGENDOR_BASE = "https://api.agendor.com.br/v3"
DAYS_RANGE = int(os.getenv("DAYS_RANGE", "30"))
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "300"))

app = Flask(__name__)
cache = {}


def parse_dt(value):
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00"), text.replace("Z", ""), text.replace("+00:00", "")):
        try:
            return datetime.fromisoformat(candidate).replace(tzinfo=None)
        except Exception:
            pass
    return None


def to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("R$", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return 0.0


def labels():
    today = date.today()
    return [(today - timedelta(days=i)).strftime("%d/%m") for i in range(DAYS_RANGE - 1, -1, -1)]


def mt_request(path, params=None):
    if not MEETIME_TOKEN:
        return None
    url = f"{MEETIME_BASE}{path}"
    for auth in (MEETIME_TOKEN, f"Bearer {MEETIME_TOKEN}", f"Token {MEETIME_TOKEN}"):
        r = requests.get(url, headers={"Authorization": auth}, params=params or {}, timeout=25)
        if r.status_code != 401:
            r.raise_for_status()
            return r.json()
    r = requests.get(url, params={**(params or {}), "api_token": MEETIME_TOKEN}, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_meetime_won_leads():
    since_dt = datetime.now() - timedelta(days=DAYS_RANGE)
    since = since_dt.strftime("%Y-%m-%dT00:00:00")
    until = datetime.now().strftime("%Y-%m-%dT23:59:59")
    items = []
    start = 0
    while True:
        raw = mt_request("/prospections", {"status": "WON", "end_after": since, "end_before": until, "limit": 100, "start": start})
        rows = raw.get("data", []) if isinstance(raw, dict) else raw or []
        if not rows:
            break
        for row in rows:
            dt = parse_dt(row.get("end_date") or row.get("endDate") or row.get("last_activity_date") or row.get("created_date"))
            if not dt or dt >= since_dt:
                items.append(row)
        if not isinstance(raw, dict) or not raw.get("next") or len(rows) < 100:
            break
        start += 100
    print(f"[Meetime] leads ganhos: {len(items)}")
    return items


def fetch_meetime_users():
    raw = mt_request("/users")
    return raw.get("data", raw) if isinstance(raw, dict) else raw or []


def ag_headers():
    return {"Authorization": f"Token {AGENDOR_TOKEN}", "Content-Type": "application/json"}


def is_won(deal):
    status = deal.get("dealStatus") or deal.get("status") or {}
    name = str(status.get("name") if isinstance(status, dict) else status).lower()
    return "ganho" in name or "won" in name or bool(deal.get("wonAt")) or deal.get("won") is True or deal.get("isWon") is True


def fetch_agendor_won_deals():
    if not AGENDOR_TOKEN:
        return []
    since_dt = datetime.now() - timedelta(days=DAYS_RANGE)
    items, seen = [], set()
    page, per_page = 1, 100
    statuses = set()
    while True:
        r = requests.get(f"{AGENDOR_BASE}/deals", headers=ag_headers(), params={"page": page, "per_page": per_page}, timeout=30)
        r.raise_for_status()
        raw = r.json()
        rows = raw.get("data", []) if isinstance(raw, dict) else raw or []
        print(f"[Agendor] página {page}: {len(rows)} negócios")
        if not rows:
            break
        for deal in rows:
            status = deal.get("dealStatus") or deal.get("status") or {}
            statuses.add(str(status.get("name") if isinstance(status, dict) else status))
            if not is_won(deal):
                continue
            deal_id = str(deal.get("id") or deal.get("dealId") or id(deal))
            if deal_id in seen:
                continue
            dt = parse_dt(deal.get("wonAt") or deal.get("endTime") or deal.get("updatedAt") or deal.get("createdAt"))
            if dt and dt < since_dt:
                continue
            seen.add(deal_id)
            items.append(deal)
        if len(rows) < per_page:
            break
        page += 1
    print(f"[Agendor] status encontrados: {sorted(statuses)}")
    print(f"[Agendor] ganhos no período: {len(items)}")
    return items


def fetch_agendor_users():
    if not AGENDOR_TOKEN:
        return []
    r = requests.get(f"{AGENDOR_BASE}/users", headers=ag_headers(), timeout=25)
    r.raise_for_status()
    raw = r.json()
    return raw.get("data", []) if isinstance(raw, dict) else raw or []


def aggregate(leads, deals, ag_users):
    days = labels()
    leads_day, deals_day, value_day = defaultdict(int), defaultdict(int), defaultdict(float)
    seller_leads, seller_deals, seller_value = defaultdict(int), defaultdict(int), defaultdict(float)
    ag_map = {str(u.get("id")): u.get("name", "Sem usuário") for u in ag_users if isinstance(u, dict)}

    for lead in leads:
        dt = parse_dt(lead.get("end_date") or lead.get("endDate") or lead.get("last_activity_date") or lead.get("created_date"))
        if dt:
            leads_day[dt.strftime("%d/%m")] += 1
        name = lead.get("owner_name") or lead.get("salesman_name") or str(lead.get("owner_id") or "Sem usuário")
        seller_leads[name] += 1

    for deal in deals:
        dt = parse_dt(deal.get("wonAt") or deal.get("endTime") or deal.get("updatedAt") or deal.get("createdAt"))
        value = to_float(deal.get("value") or deal.get("dealValue") or deal.get("amount"))
        if dt:
            deals_day[dt.strftime("%d/%m")] += 1
            value_day[dt.strftime("%d/%m")] += value
        owner = deal.get("owner") or deal.get("user") or {}
        name = owner.get("name") or ag_map.get(str(owner.get("id", "")), "Sem usuário") if isinstance(owner, dict) else ag_map.get(str(owner), "Sem usuário")
        seller_deals[name] += 1
        seller_value[name] += value

    total_leads = len(leads)
    total_deals = len(deals)
    total_value = sum(to_float(d.get("value") or d.get("dealValue") or d.get("amount")) for d in deals)
    sellers = sorted([{
        "name": s,
        "leads": seller_leads[s],
        "deals": seller_deals[s],
        "value": seller_value[s],
        "conversion": round((seller_deals[s] / seller_leads[s]) * 100, 1) if seller_leads[s] else 0
    } for s in set(seller_leads) | set(seller_deals)], key=lambda x: (x["deals"], x["value"]), reverse=True)

    return {
        "kpis": {"total_leads": total_leads, "total_deals": total_deals, "total_value": total_value, "conv_rate": round(total_deals / total_leads * 100, 1) if total_leads else 0},
        "time_series": {"labels": days, "leads": [leads_day[d] for d in days], "deals": [deals_day[d] for d in days], "value": [value_day[d] for d in days]},
        "funnel": {"labels": ["Base estimada", "Leads ganhos", "Contratos"], "values": [max(total_leads * 3, 1), max(total_leads, 1), max(total_deals, 1)]},
        "ranking": sellers[:12],
        "last_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "demo": False,
    }


def demo_data():
    rng = random.Random(7)
    days = labels()
    l = [rng.randint(3, 18) for _ in days]
    d = [rng.randint(0, 7) for _ in days]
    v = [x * rng.randint(1600, 5200) for x in d]
    names = ["Lucas Farinazzo", "Bruno Almeida", "Welington Costa", "Camila Rocha"]
    return {"kpis": {"total_leads": sum(l), "total_deals": sum(d), "total_value": sum(v), "conv_rate": round(sum(d)/sum(l)*100,1)}, "time_series": {"labels": days, "leads": l, "deals": d, "value": v}, "funnel": {"labels": ["Base estimada", "Leads ganhos", "Contratos"], "values": [sum(l)*3, sum(l), sum(d)]}, "ranking": [{"name": n, "leads": rng.randint(20,70), "deals": rng.randint(4,16), "value": rng.randint(18000,130000), "conversion": rng.randint(8,30)} for n in names], "last_update": datetime.now().strftime("%d/%m/%Y %H:%M:%S") + " · demo", "demo": True}


def refresh():
    if not MEETIME_TOKEN or not AGENDOR_TOKEN:
        cache["data"] = demo_data()
        return
    try:
        leads = fetch_meetime_won_leads()
        deals = fetch_agendor_won_deals()
        ag_users = fetch_agendor_users()
        cache["data"] = aggregate(leads, deals, ag_users)
    except Exception as exc:
        print(f"[Dashboard] erro: {exc}")
        cache["data"] = demo_data()


def auto_refresh():
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh()


HTML = """
<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Sales Dashboard</title><link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap' rel='stylesheet'><script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js'></script><style>:root{--bg:#080d18;--card:#101827;--card2:#0d1422;--line:rgba(148,163,184,.16);--text:#e5edf8;--muted:#8b9bb3;--blue:#60a5fa;--green:#34d399;--amber:#fbbf24;--purple:#a78bfa;--cyan:#22d3ee}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,rgba(96,165,250,.18),transparent 28%),radial-gradient(circle at 90% 0,rgba(167,139,250,.14),transparent 32%),var(--bg);font-family:Inter,system-ui,sans-serif;color:var(--text)}.wrap{max-width:1480px;margin:0 auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}.brand{display:flex;gap:13px;align-items:center}.mark{width:44px;height:44px;border-radius:15px;background:linear-gradient(135deg,var(--blue),var(--purple));display:grid;place-items:center;font-weight:800}.brand h1{margin:0;font-size:19px}.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}.actions{display:flex;gap:10px;flex-wrap:wrap}.search,.pill,.btn{height:42px;border:1px solid var(--line);border-radius:14px;background:rgba(16,24,39,.78);color:var(--text);padding:0 14px;display:flex;align-items:center;gap:9px}.search input{all:unset;width:250px;font-size:13px}.btn{background:linear-gradient(135deg,var(--blue),#6366f1);border:0;font-weight:700;cursor:pointer}.filters{display:flex;gap:9px;margin-bottom:16px;flex-wrap:wrap}.filter{border:1px solid var(--line);background:rgba(16,24,39,.55);padding:9px 13px;border-radius:999px;color:var(--muted);font-size:12px}.filter.on{color:white;border-color:rgba(96,165,250,.5);background:rgba(96,165,250,.13)}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:16px}.card{background:linear-gradient(180deg,rgba(16,24,39,.92),rgba(13,20,34,.92));border:1px solid var(--line);border-radius:22px;box-shadow:0 18px 60px rgba(0,0,0,.25)}.kpi{padding:18px;overflow:hidden;position:relative}.kpi:after{content:'';position:absolute;right:-40px;top:-45px;width:130px;height:130px;background:var(--tone);filter:blur(50px);opacity:.22}.khead{display:flex;align-items:center;justify-content:space-between;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-size:12px;font-weight:800}.ico{width:34px;height:34px;border-radius:12px;background:rgba(255,255,255,.06);display:grid;place-items:center;color:var(--tone)}.kval{font-size:32px;font-weight:800;letter-spacing:-.04em;margin:18px 0 8px}.ksub{font-size:12px;color:var(--muted)}.grid{display:grid;grid-template-columns:1.12fr .88fr;gap:16px;margin-bottom:16px}.panel{padding:18px}.ph{display:flex;justify-content:space-between;margin-bottom:16px}.title{font-weight:800}.sub{color:var(--muted);font-size:12px;margin-top:4px}.chart{height:285px}.chart.sm{height:235px}.badge{display:inline-flex;align-items:center;gap:6px;color:var(--green);background:rgba(52,211,153,.1);border-radius:999px;padding:6px 10px;font-size:12px;font-weight:700}.dot{width:7px;height:7px;border-radius:50%;background:currentColor}table{width:100%;border-collapse:collapse}th{color:var(--muted);font-size:11px;text-align:left;text-transform:uppercase;letter-spacing:.06em;padding:10px;border-bottom:1px solid var(--line)}td{padding:13px 10px;border-bottom:1px solid rgba(148,163,184,.08);font-size:13px}.person{display:flex;gap:10px;align-items:center}.avatar{width:34px;height:34px;border-radius:12px;background:linear-gradient(135deg,rgba(96,165,250,.36),rgba(167,139,250,.36));display:grid;place-items:center;font-size:12px;font-weight:800}.name{font-weight:800}.muted{color:var(--muted);font-size:12px}.bar{height:7px;border-radius:999px;background:rgba(148,163,184,.13);overflow:hidden;margin-top:7px}.bar span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--blue),var(--cyan))}.spinner{position:fixed;inset:0;background:rgba(8,13,24,.72);display:none;place-items:center;z-index:9;backdrop-filter:blur(8px)}.spinner.show{display:grid}.loader{width:42px;height:42px;border-radius:50%;border:3px solid rgba(255,255,255,.16);border-top-color:var(--blue);animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:1000px){.kpis,.grid{grid-template-columns:1fr 1fr}.top{flex-direction:column;align-items:flex-start}}@media(max-width:720px){.wrap{padding:16px}.kpis,.grid{grid-template-columns:1fr}.search input{width:170px}}</style></head><body><div class='spinner' id='spinner'><div class='loader'></div></div><main class='wrap'><header class='top'><div class='brand'><div class='mark'>OD</div><div><h1>Sales Dashboard</h1><p>Meetime + Agendor · operação comercial</p></div></div><div class='actions'><div class='search'>⌕<input id='q' placeholder='Buscar vendedor' oninput='renderRanking(last.ranking)'></div><div class='pill' id='upd'>Carregando...</div><button class='btn' onclick='load(true)'>Atualizar</button></div></header><div class='filters'><div class='filter on'>Últimos 30 dias</div><div class='filter'>WON Meetime</div><div class='filter'>Ganhos Agendor</div><div class='filter'>Render ready</div></div><section class='kpis'><div class='card kpi' style='--tone:var(--blue)'><div class='khead'>Leads ganhos<div class='ico'>↗</div></div><div class='kval' id='leads'>—</div><div class='ksub'>Meetime no período</div></div><div class='card kpi' style='--tone:var(--green)'><div class='khead'>Contratos<div class='ico'>✓</div></div><div class='kval' id='deals'>—</div><div class='ksub'>Agendor ganhos</div></div><div class='card kpi' style='--tone:var(--amber)'><div class='khead'>Receita<div class='ico'>R$</div></div><div class='kval' id='value'>—</div><div class='ksub'>Valor total fechado</div></div><div class='card kpi' style='--tone:var(--purple)'><div class='khead'>Conversão<div class='ico'>%</div></div><div class='kval' id='conv'>—</div><div class='ksub'>Leads ganhos → contratos</div></div></section><section class='grid'><article class='card panel'><div class='ph'><div><div class='title'>Performance diária</div><div class='sub'>Leads e contratos por dia</div></div><span class='badge'><i class='dot'></i>online</span></div><div class='chart'><canvas id='perf'></canvas></div></article><article class='card panel'><div class='ph'><div><div class='title'>Funil</div><div class='sub'>Base, leads e contratos</div></div></div><div class='chart sm'><canvas id='funil'></canvas></div></article></section><section class='grid'><article class='card panel'><div class='ph'><div><div class='title'>Receita por dia</div><div class='sub'>Valor dos contratos ganhos</div></div></div><div class='chart sm'><canvas id='receita'></canvas></div></article><article class='card panel'><div class='ph'><div><div class='title'>Ranking de vendedores</div><div class='sub'>Resultado por responsável</div></div></div><table><thead><tr><th>Vendedor</th><th>Leads</th><th>Contratos</th><th>Conv.</th><th>Receita</th></tr></thead><tbody id='rank'></tbody></table></article></section></main><script>const fmt=n=>Number(n||0).toLocaleString('pt-BR'),brl=n=>'R$ '+Number(n||0).toLocaleString('pt-BR',{maximumFractionDigits:0}),pct=n=>Number(n||0).toLocaleString('pt-BR',{minimumFractionDigits:1,maximumFractionDigits:1})+'%';const C={blue:'#60a5fa',green:'#34d399',cyan:'#22d3ee',purple:'#a78bfa',grid:'rgba(148,163,184,.13)',text:'#8b9bb3'};Chart.defaults.color=C.text;Chart.defaults.borderColor=C.grid;Chart.defaults.font.family='Inter,system-ui,sans-serif';let charts={},last={ranking:[]};function ini(n){return String(n||'?').split(' ').filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase()}function kill(id){if(charts[id])charts[id].destroy()}function renderRanking(rows){rows=rows||[];const s=(q.value||'').toLowerCase();rows=rows.filter(r=>String(r.name).toLowerCase().includes(s));const max=Math.max(...rows.map(r=>r.value||0),1);rank.innerHTML=rows.map(r=>`<tr><td><div class='person'><div class='avatar'>${ini(r.name)}</div><div><div class='name'>${r.name}</div><div class='muted'>${brl(r.value)}</div></div></div></td><td>${fmt(r.leads)}</td><td><span class='badge'><i class='dot'></i>${fmt(r.deals)}</span></td><td>${pct(r.conversion)}<div class='bar'><span style='width:${Math.min(r.conversion,100)}%'></span></div></td><td>${brl(r.value)}<div class='bar'><span style='width:${Math.round((r.value||0)/max*100)}%'></span></div></td></tr>`).join('')||`<tr><td colspan='5' class='muted'>Nenhum vendedor.</td></tr>`}function renderCharts(d){const ts=d.time_series;kill('perf');charts.perf=new Chart(perf,{type:'line',data:{labels:ts.labels,datasets:[{label:'Leads',data:ts.leads,borderColor:C.blue,backgroundColor:'rgba(96,165,250,.12)',fill:true,tension:.42,pointRadius:0,borderWidth:2},{label:'Contratos',data:ts.deals,borderColor:C.green,backgroundColor:'rgba(52,211,153,.1)',fill:true,tension:.42,pointRadius:0,borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{usePointStyle:true,boxWidth:8}}},scales:{x:{grid:{display:false}},y:{beginAtZero:true}}}});kill('receita');charts.receita=new Chart(receita,{type:'bar',data:{labels:ts.labels,datasets:[{data:ts.value,backgroundColor:'rgba(34,211,238,.42)',borderColor:C.cyan,borderWidth:1,borderRadius:8}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false}},y:{beginAtZero:true,ticks:{callback:v=>v>=1000?'R$ '+(v/1000).toFixed(0)+'k':'R$ '+v}}}}});kill('funil');charts.funil=new Chart(funil,{type:'bar',data:{labels:d.funnel.labels,datasets:[{data:d.funnel.values,backgroundColor:['rgba(96,165,250,.42)','rgba(167,139,250,.42)','rgba(52,211,153,.42)'],borderRadius:10}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{beginAtZero:true},y:{grid:{display:false}}}}})}async function load(force=false){spinner.classList.add('show');try{if(force)await fetch('/api/refresh');const r=await fetch('/api/data');const d=await r.json();last=d;leads.textContent=fmt(d.kpis.total_leads);deals.textContent=fmt(d.kpis.total_deals);value.textContent=brl(d.kpis.total_value);conv.textContent=pct(d.kpis.conv_rate);upd.textContent=(d.demo?'Demo · ':'')+d.last_update;renderRanking(d.ranking);renderCharts(d)}catch(e){console.error(e)}finally{spinner.classList.remove('show')}}load();setInterval(()=>load(false),300000)</script></body></html>
"""


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


refresh()
threading.Thread(target=auto_refresh, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
