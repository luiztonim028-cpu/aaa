import csv
import io
import json
import math
import sqlite3
import statistics
from datetime import datetime

import streamlit as st

DB_FILE = "historico_valuation.db"

st.set_page_config(
    page_title="Preço Justo & Preço Teto",
    page_icon="📊",
    layout="wide",
)

# Critérios para consolidar os métodos. O primeiro é o padrão (conservador).
CRITERIA = {
    "Menor valor (mais conservador)": "min",
    "Mediana dos métodos": "median",
    "Média dos métodos": "mean",
}


# ---------------------------------------------------------------------
# BANCO DE DADOS
# ---------------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS valuations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                market_price REAL NOT NULL,
                margin_safety REAL NOT NULL,
                ceiling REAL NOT NULL,
                methods_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker TEXT PRIMARY KEY,
                asset_type TEXT NOT NULL,
                ceiling REAL NOT NULL,
                last_price REAL NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()


def save_valuation(asset_type, ticker, market_price, margin, ceiling, methods, criterion):
    payload = {"criterion": criterion, "methods": methods}
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            INSERT INTO valuations
            (created_at, asset_type, ticker, market_price, margin_safety, ceiling, methods_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                asset_type,
                ticker.upper().strip(),
                market_price,
                margin,
                ceiling,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()


def load_history():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM valuations ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def clear_history():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM valuations")
        conn.commit()


# ---------------------------------------------------------------------
# LISTA DE OBSERVAÇÃO
# ---------------------------------------------------------------------
def latest_analyses():
    """Última análise salva de cada ticker."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ticker, asset_type, market_price, ceiling FROM valuations
            WHERE id IN (SELECT MAX(id) FROM valuations GROUP BY ticker)
            """
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_watch(ticker, asset_type, ceiling, last_price, note=""):
    """Insere ou atualiza um ativo. Observação vazia mantém a anterior."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (ticker, asset_type, ceiling, last_price, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                asset_type = excluded.asset_type,
                ceiling = excluded.ceiling,
                last_price = excluded.last_price,
                note = CASE WHEN excluded.note = '' THEN watchlist.note ELSE excluded.note END,
                updated_at = excluded.updated_at
            """,
            (
                ticker.upper().strip(),
                asset_type,
                ceiling,
                last_price,
                note.strip(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def get_watch(ticker):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM watchlist WHERE ticker = ?", (ticker.upper().strip(),)
        ).fetchone()
    return dict(row) if row else None


def load_watchlist():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM watchlist").fetchall()
    return [dict(row) for row in rows]


def delete_watch(ticker):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
        conn.commit()


def import_watch_from_history():
    analyses = latest_analyses()
    for item in analyses:
        upsert_watch(item["ticker"], item["asset_type"], item["ceiling"], item["market_price"])
    return len(analyses)


def watch_status(price, ceiling):
    if price <= 0:
        return "⚪ Sem preço atual"
    if price <= ceiling:
        return "🟢 Dentro do teto"
    if price <= ceiling * 1.05:
        return "🟡 Até 5% acima do teto"
    return "🔴 Acima do teto"


def watch_distance(row):
    """Folga até o teto: positivo = preço abaixo do teto. None se não há preço."""
    if row["last_price"] <= 0:
        return None
    return row["ceiling"] / row["last_price"] - 1


def days_since(timestamp):
    try:
        return (datetime.now() - datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")).days
    except ValueError:
        return 0


# ---------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------
def brl(value):
    # Formato brasileiro: R$ 1.000,00
    return f"R$ {value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def num(value, decimals=2):
    return f"{value:.{decimals}f}".replace(".", ",")


def pct(value):
    return f"{value:.1f}%".replace(".", ",")


def make_method(name, fair_value, assumption, margin_pct, apply_margin):
    """Monta o resultado de um método. A margem só é aplicada quando faz sentido."""
    multiplier = (1 - margin_pct / 100) if apply_margin else 1.0
    return {
        "name": name,
        "fair_value": fair_value,
        "margin_applied": margin_pct if apply_margin else 0.0,
        "ceiling": fair_value * multiplier,
        "assumption": assumption,
    }


def consolidate(methods, criterion_key):
    values = [m["ceiling"] for m in methods]
    if criterion_key == "min":
        return min(values)
    if criterion_key == "mean":
        return statistics.mean(values)
    return statistics.median(values)


RISK_FREE_DEFAULT = 13.75  # Selic em 16/09/2026. Atualize quando mudar (ou use a NTN-B longa).

BAZIN_YIELDS = [6.0, 7.0, 8.0, 9.0, 10.0]

# Pontos de partida por setor (heurísticas editáveis, não regras).
# yield: yield mínimo do Bazin | g1: crescimento inicial do lucro (% a.a.)
# premium: prêmio de risco (p.p.) somado à taxa livre de risco | margin: margem de segurança (%)
SECTOR_PRESETS = {
    "Bancos": {
        "yield": 8.0, "g1": 6.0, "premium": 1.0, "margin": 15.0,
        "note": "Lucro sensível a crédito e inadimplência: use o lucro líquido ajustado e confira ROE e P/VP. "
                "Parte do lucro fica retida para sustentar o crescimento da carteira.",
    },
    "Seguros": {
        "yield": 7.5, "g1": 5.0, "premium": 0.5, "margin": 15.0,
        "note": "Lucro mais estável e payout alto. O resultado financeiro depende da Selic.",
    },
    "Energia elétrica": {
        "yield": 7.0, "g1": 5.0, "premium": 0.5, "margin": 15.0,
        "note": "Receita regulada e indexada à inflação. Observe alavancagem e revisões tarifárias.",
    },
    "Saneamento": {
        "yield": 7.0, "g1": 5.0, "premium": 0.5, "margin": 15.0,
        "note": "Receita regulada, investimento alto (que reduz o fluxo ao acionista) e risco político.",
    },
    "Petróleo e commodities": {
        "yield": 9.0, "g1": 3.0, "premium": 2.5, "margin": 25.0,
        "note": "Lucro oscila com preço e câmbio: compare o lucro projetivo com a média do histórico "
                "(lucro normalizado) e use margem maior.",
    },
    "Construção e imobiliário": {
        "yield": 9.0, "g1": 5.0, "premium": 2.5, "margin": 25.0,
        "note": "Cíclico e sensível a juros. Observe caixa, alavancagem e distratos.",
    },
    "Varejo e consumo": {
        "yield": 9.0, "g1": 5.0, "premium": 2.5, "margin": 25.0,
        "note": "Margens pressionadas por juros altos e pelo endividamento do consumidor.",
    },
    "Tecnologia e indústria": {
        "yield": 8.0, "g1": 7.0, "premium": 2.0, "margin": 20.0,
        "note": "Crescimento maior e payout menor: o yield do Bazin pesa menos, dê mais peso ao fluxo descontado.",
    },
    "Infraestrutura e transporte": {
        "yield": 8.0, "g1": 5.0, "premium": 1.5, "margin": 20.0,
        "note": "Concessões têm fluxo previsível, mas costumam ser alavancadas.",
    },
    "Outro / personalizado": {
        "yield": 8.0, "g1": 5.0, "premium": 1.5, "margin": 20.0,
        "note": "Ajuste todas as premissas manualmente.",
    },
}

FLOW_MODES = {
    "FCFE: lucro × (1 − g ÷ ROE) (Damodaran)": "fcfe",
    "Dividendos: lucro × payout": "div",
    "Lucro integral (referência otimista, como no print)": "earnings",
}

FLOW_TITLES = {
    "fcfe": "FCD Damodaran (FCFE)",
    "div": "DDM projetado (lucro × payout)",
    "earnings": "FCD sobre lucro integral (referência)",
}


def thousands(value, decimals=1):
    return f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def parse_number(token):
    token = token.strip().replace(" ", "")
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    return float(token)


def parse_series(text, max_len=15):
    """'1177,6; 1151,5; 1503,4' -> [1177.6, 1151.5, 1503.4]. Levanta ValueError se inválido."""
    parts = [p for p in (x.strip() for x in text.split(";")) if p]
    if len(parts) > max_len:
        raise ValueError(f"Informe no máximo {max_len} valores.")
    values = []
    for part in parts:
        try:
            value = parse_number(part)
        except ValueError:
            raise ValueError(
                f"valor inválido '{part}'. Use vírgula como decimal e ';' entre os anos."
            )
        if not math.isfinite(value):
            raise ValueError(f"valor inválido '{part}'.")
        values.append(value)
    return values


def history_table(values, window, last_year):
    """Últimos `window` anos do histórico, com crescimento ano a ano, CAGR e média da janela."""
    n = len(values)
    start = max(0, n - window)
    rows = []
    for i in range(start, n):
        growth = values[i] / values[i - 1] - 1 if i > 0 and values[i - 1] > 0 else None
        rows.append({"year": last_year - (n - 1 - i), "earnings": values[i], "growth": growth})
    window_values = values[start:]
    cagr = None
    if len(window_values) >= 2 and window_values[0] > 0 and window_values[-1] > 0:
        cagr = (window_values[-1] / window_values[0]) ** (1 / (len(window_values) - 1)) - 1
    average = statistics.mean(window_values) if window_values else None
    return rows, cagr, average


def project_dcf(base_earnings, g1, decay, terminal_growth, years, rate, flow_mode,
                payout=0.0, roe=0.0, terminal_roe=None, fraction=1.0):
    """Projeta os lucros (R$ milhões) e desconta o fluxo ao acionista (todas as taxas em fração).

    O ano 1 é o ano corrente, com `base_earnings` (lucro projetivo). A partir do ano 2 o
    crescimento começa em `g1` e converge para `terminal_growth`, `decay` por ano.

    Fluxos:
      fcfe     -> lucro × (1 − g_próximo ÷ ROE): o que sobra depois de reinvestir para crescer
      div      -> lucro × payout
      earnings -> lucro integral (supõe que crescer não custa nada; só como referência)
    """
    notes = []
    growths, earnings, g = [None], [base_earnings], g1
    for _ in range(2, years + 1):
        growths.append(g)
        earnings.append(earnings[-1] * (1 + g))
        if g > terminal_growth:
            g = max(g - decay, terminal_growth)
        else:
            g = min(g + decay, terminal_growth)

    terminal_roe = terminal_roe if terminal_roe else rate

    def ratio_for(next_growth, terminal=False):
        if flow_mode == "fcfe":
            return 1 - next_growth / (terminal_roe if terminal else roe)
        if flow_mode == "div":
            return payout
        return 1.0

    rows, pv_total, clipped = [], 0.0, False
    for k, earn in enumerate(earnings):
        next_g = growths[k + 1] if k + 1 < years else terminal_growth
        ratio = ratio_for(next_g)
        if ratio < 0:
            clipped, ratio = True, 0.0
        flow = earn * ratio * (fraction if k == 0 else 1.0)
        pv = flow / (1 + rate) ** (fraction + k)
        pv_total += pv
        rows.append({"k": k, "earnings": earn, "growth": growths[k],
                     "ratio": ratio, "flow": flow, "pv": pv})

    terminal_ratio = ratio_for(terminal_growth, terminal=True)
    if terminal_ratio < 0:
        clipped, terminal_ratio = True, 0.0
    terminal_flow = earnings[-1] * (1 + terminal_growth) * terminal_ratio
    terminal_value = terminal_flow / (rate - terminal_growth)
    pv_terminal = terminal_value / (1 + rate) ** (fraction + years - 1)
    value = pv_total + pv_terminal

    if clipped:
        notes.append(
            "Em algum ano o crescimento exige reinvestir mais do que o ROE gera (fluxo negativo). "
            "O fluxo foi limitado a zero; revise o crescimento ou o ROE."
        )
    return {
        "value": value, "pv_terminal": pv_terminal,
        "terminal_share": pv_terminal / value if value > 0 else 0.0,
        "terminal_ratio": terminal_ratio, "rows": rows, "notes": notes,
    }


def analyze_stock(p):
    """Calcula todos os métodos de ações a partir de um dicionário de entradas."""
    methods, notes = [], []
    r, gt = p["rate"] / 100, p["terminal_growth"] / 100
    rate_ok = r > gt
    margin, apply_all = p["margin"], p["apply_all"]
    shares, payout, roe = p["shares_mi"], p["payout"], p["roe"]
    y = p["bazin_yield"]

    # Lucro projetivo do ano: informado, ou 1º semestre × (2 + variação do 2º semestre)
    if p["profit_override"] > 0:
        profit = p["profit_override"]
    elif p["profit_1s"] > 0:
        profit = p["profit_1s"] * (2 + p["adj_2s"] / 100)
    else:
        profit = 0.0

    lpa_proj = profit / shares if profit > 0 and shares > 0 else 0.0
    dpa_proj = lpa_proj * payout / 100
    dpa_values = [d for d in p["dpa"] if d > 0]
    dpa_avg = statistics.mean(dpa_values) if dpa_values else 0.0

    # 1. Bazin com média de proventos
    if dpa_avg > 0:
        methods.append(make_method(
            "Bazin: DPA médio de 3 anos",
            dpa_avg / (y / 100),
            f"Média de {len(dpa_values)} ano(s) de proventos (R$ {num(dpa_avg)}) ÷ yield mínimo de {y:.1f}%.",
            margin, apply_all,
        ))

    # 2. Bazin projetivo
    if dpa_proj > 0:
        methods.append(make_method(
            "Bazin projetivo",
            dpa_proj / (y / 100),
            f"DPA projetado de R$ {num(dpa_proj)} (lucro projetivo × payout ÷ ações) ÷ yield mínimo de {y:.1f}%.",
            margin, apply_all,
        ))
    elif profit > 0:
        notes.append("Bazin projetivo ignorado: informe o payout e o nº de ações.")

    # 3. Número de Graham (LPA informado ou LPA projetivo)
    eps = p["eps_graham"] if p["eps_graham"] > 0 else lpa_proj
    bvps = p["bvps"]
    if eps > 0 and bvps > 0:
        methods.append(make_method(
            "Número de Graham",
            (p["max_pl"] * p["max_pvp"] * eps * bvps) ** 0.5,
            f"Respeita P/L ≤ {p['max_pl']:.1f} e P/VP ≤ {p['max_pvp']:.1f} "
            f"(constante {p['max_pl'] * p['max_pvp']:.1f}), com LPA de R$ {num(eps)}.",
            margin, apply_all,
        ))
    elif p["eps_graham"] > 0 or bvps > 0:
        notes.append("Graham ignorado: informe LPA e VPA, ambos positivos.")

    # 4. P/VP justo pelo ROE
    if roe > 0 and bvps > 0 and rate_ok:
        fair_pvp = justified_pvp(roe / 100, r, gt)
        if fair_pvp is None:
            notes.append("P/VP justo ignorado: o ROE precisa ser maior que o crescimento na perpetuidade.")
        else:
            methods.append(make_method(
                "P/VP justo pelo ROE",
                bvps * fair_pvp,
                f"P/VP justo = (ROE {roe:.1f}% − {gt * 100:.1f}%) ÷ ({r * 100:.1f}% − {gt * 100:.1f}%) "
                f"= {num(fair_pvp)}x.",
                margin, True,
            ))
    elif roe > 0 and bvps > 0 and not rate_ok:
        notes.append("P/VP justo ignorado: a taxa de desconto precisa ser maior que o crescimento na perpetuidade.")

    # 5. Fluxo de caixa descontado (Damodaran)
    hist_values, hist_rows, cagr, hist_avg = [], [], None, None
    if p["hist_text"].strip():
        try:
            hist_values = parse_series(p["hist_text"])
        except ValueError as err:
            notes.append(f"Histórico de lucros ignorado: {err}")
    if hist_values:
        hist_rows, cagr, hist_avg = history_table(hist_values, p["hist_window"], p["current_year"] - 1)
        if len(hist_values) < p["hist_window"]:
            notes.append(
                f"Você informou {len(hist_values)} ano(s) de lucro; a janela de "
                f"{p['hist_window']} anos usa só esses."
            )

    report, flow_key = None, p["flow_key"]
    if profit <= 0:
        if p["hist_text"].strip():
            notes.append("Fluxo descontado ignorado: informe o lucro do 1º semestre ou o lucro projetado do ano.")
    elif shares <= 0:
        notes.append("Fluxo descontado ignorado: informe o nº de ações (em milhões).")
    elif not rate_ok:
        notes.append("Fluxo descontado ignorado: a taxa de desconto precisa ser maior que o crescimento na perpetuidade.")
    elif flow_key == "fcfe" and roe <= 0:
        notes.append("FCFE precisa do ROE: informe o ROE ou escolha o fluxo de dividendos.")
    elif flow_key == "div" and payout <= 0:
        notes.append("Fluxo de dividendos precisa do payout: informe o payout.")
    else:
        g1, decay, years = p["g1"] / 100, p["decay"] / 100, int(p["years"])
        fraction = p["fraction"] / 100
        terminal_roe = p["terminal_roe"] / 100 if p["terminal_roe"] > 0 else None

        def run(g1_value, rate_value):
            return project_dcf(profit, g1_value, decay, gt, years, rate_value, flow_key,
                               payout / 100, roe / 100,
                               terminal_roe if terminal_roe else rate_value, fraction)

        result = run(g1, r)
        fair_ps = result["value"] / shares
        methods.append(make_method(
            FLOW_TITLES[flow_key],
            fair_ps,
            f"Taxa de desconto {r * 100:.1f}%; crescimento inicial {g1 * 100:.1f}% caindo "
            f"{decay * 100:.1f} p.p./ano até {gt * 100:.1f}%; {years} anos. "
            f"Equivale a {num(result['value'] / profit)}x o lucro projetivo (antes da margem).",
            margin, True,
        ))
        notes.extend(result["notes"])

        if flow_key == "earnings":
            notes.append(
                "Fluxo sobre o lucro integral supõe que 100% do lucro chega ao acionista todo ano e que "
                "crescer não custa nada. Tende a superestimar o valor; use como referência, não como teto."
            )
        if result["terminal_share"] > 0.75:
            notes.append(
                f"{result['terminal_share']:.0%} do valor vem da perpetuidade. O resultado é muito sensível "
                "às premissas; considere mais anos de projeção ou revisar as taxas."
            )
        if p["terminal_growth"] > 5:
            notes.append("Crescimento na perpetuidade acima de 5% é agressivo.")
        if cagr is not None and g1 > cagr + 0.02:
            notes.append(
                f"O crescimento inicial ({g1 * 100:.1f}%) está acima do CAGR histórico da janela "
                f"({cagr * 100:.1f}%). Confirme se há motivo para acelerar."
            )
        g_sust = roe / 100 * (1 - payout / 100) if roe > 0 and payout > 0 else None
        if g_sust is not None and g1 > g_sust + 0.005:
            notes.append(
                f"O crescimento inicial ({g1 * 100:.1f}%) está acima do crescimento sustentável "
                f"ROE × (1 − payout) = {g_sust * 100:.1f}%. Para crescer isso, a empresa precisaria "
                "elevar o ROE ou reter mais lucro."
            )

        scenarios = []
        for name, dg, dr in (("Pessimista", -2.0, 1.0), ("Base", 0.0, 0.0), ("Otimista", 2.0, -1.0)):
            r_s, g_s = r + dr / 100, g1 + dg / 100
            if r_s <= gt:
                continue
            ps = run(g_s, r_s)["value"] / shares
            scenarios.append({"name": name, "g1": g_s, "rate": r_s, "fair": ps,
                              "ceiling": ps * (1 - margin / 100)})

        report = {
            "title": FLOW_TITLES[flow_key], "result": result, "shares": shares, "profit": profit,
            "fair_ps": fair_ps, "history": hist_rows, "cagr": cagr, "hist_avg": hist_avg,
            "last_closed": hist_values[-1] if hist_values else None, "window": p["hist_window"],
            "scenarios": scenarios, "base_year": p["current_year"], "g_sust": g_sust,
            "premises": {"rate": r, "g1": g1, "decay": decay, "gt": gt, "years": years,
                         "payout": payout / 100, "roe": roe / 100},
        }

    return {
        "methods": methods, "notes": notes, "report": report, "profit": profit,
        "lpa_proj": lpa_proj, "dpa_proj": dpa_proj, "dpa_avg": dpa_avg, "eps": eps,
    }


def justified_pvp(roe, rate, growth):
    """P/VP justo = (ROE - g) / (r - g). Retorna None se não for aplicável."""
    if rate <= growth or roe <= growth:
        return None
    return (roe - growth) / (rate - growth)


def multiple_flag(value, avg, maximum):
    """🔴 acima do máximo histórico, 🟡 entre média e máximo, 🟢 até a média."""
    if maximum > 0 and value > maximum:
        return "🔴"
    if avg > 0 and value > avg:
        return "🟡"
    if avg > 0 or maximum > 0:
        return "🟢"
    return ""


def sanity_check(ceiling, eps, bvps, annual_dividend, hist):
    """Múltiplos que o preço-teto pressupõe, comparados à história da própria empresa."""
    result = {}
    if eps > 0:
        pl = ceiling / eps
        result["P/L no teto"] = f"{num(pl, 1)} {multiple_flag(pl, hist['pl_avg'], hist['pl_max'])}".strip()
    if bvps > 0:
        pvp = ceiling / bvps
        result["P/VP no teto"] = f"{num(pvp)} {multiple_flag(pvp, hist['pvp_avg'], hist['pvp_max'])}".strip()
    if annual_dividend > 0:
        dy = annual_dividend / ceiling * 100
        flag = ""
        if hist["dy_avg"] > 0:
            flag = "🟡" if dy < hist["dy_avg"] else "🟢"
        result["Yield no teto"] = f"{num(dy, 1)}% {flag}".strip()
    return result


# ---------------------------------------------------------------------
# EXIBIÇÃO DOS RESULTADOS
# ---------------------------------------------------------------------
def show_results(asset_type, ticker, market_price, margin, methods,
                 criterion_label, notes=None, indicators=None, sanity=None):
    for note in notes or []:
        st.warning(note)

    if not methods:
        st.error(
            "Não foi possível calcular nenhum método. "
            "Preencha os campos de pelo menos um método com valores positivos."
        )
        return

    criterion_key = CRITERIA[criterion_label]
    ceiling = consolidate(methods, criterion_key)
    upside = ceiling / market_price - 1
    values = [m["ceiling"] for m in methods]

    st.markdown(f"### Análise de {ticker.upper().strip()}")

    if market_price <= ceiling:
        st.success(
            f"Zona de compra: o preço atual está abaixo do preço-teto de {brl(ceiling)}."
        )
    else:
        queda = 1 - ceiling / market_price
        st.warning(
            f"Aguardar: o ativo está acima do preço-teto de {brl(ceiling)}. "
            f"Seria preciso cair {pct(queda * 100)} para entrar na zona de compra."
        )

    m1, m2, m3 = st.columns(3)
    m1.metric("Preço atual", brl(market_price))
    m2.metric("Preço-teto (começar a comprar até)", brl(ceiling))
    m3.metric("Distância até o teto", pct(upside * 100))

    if indicators:
        cols = st.columns(len(indicators))
        for col, (label, value) in zip(cols, indicators):
            col.metric(label, value)

    st.caption(
        f"Consolidação: {criterion_label}. Faixa entre os métodos: "
        f"{brl(min(values))} (menor) · {brl(statistics.median(values))} (mediana) · "
        f"{brl(max(values))} (maior)."
    )

    if len(values) > 1 and max(values) / min(values) > 1.4:
        st.warning(
            f"Os métodos divergem bastante (o maior é {pct((max(values) / min(values) - 1) * 100)} "
            "acima do menor). Isso quase sempre indica premissas incoerentes entre os métodos "
            "(yield mínimo, taxa de desconto, múltiplos máximos). Revise antes de confiar no resultado."
        )

    rows = []
    for item in methods:
        row = {
            "Método": item["name"],
            "Valor justo / base": brl(item["fair_value"]),
            "Margem aplicada": pct(item["margin_applied"]),
            "Preço-teto": brl(item["ceiling"]),
        }
        if sanity:
            row.update(sanity_check(item["ceiling"], sanity["eps"], sanity["bvps"],
                                    sanity["dividend"], sanity["hist"]))
        row["Premissa"] = item["assumption"]
        rows.append(row)

    st.dataframe(rows, use_container_width=True, hide_index=True)

    if sanity and any(v > 0 for v in sanity["hist"].values()):
        st.caption(
            "Teste de sanidade: mostra o múltiplo que cada preço-teto pressupõe. "
            "🟢 até a média histórica da empresa · 🟡 entre a média e o máximo · "
            "🔴 acima do máximo histórico (pagaria mais do que o mercado já pagou). "
            "No yield, 🟡 significa abaixo da média histórica (preço acima do normal)."
        )

    save_valuation(asset_type, ticker, market_price, margin, ceiling, methods, criterion_label)
    st.info("💾 Análise salva no histórico.")


def render_dcf_report(rep, market_price):
    """Mostra a 'realidade projetada': histórico, projeção ano a ano, valor e cenários."""
    res, shares, pr = rep["result"], rep["shares"], rep["premises"]
    fair_ps = rep["fair_ps"]

    st.markdown("### Realidade projetada: fluxo de caixa descontado")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Market cap atual (R$ mi)", thousands(market_price * shares))
    c2.metric("Valor do patrimônio no FCD (R$ mi)", thousands(res["value"]))
    c3.metric("Preço justo por ação", brl(fair_ps))
    c4.metric("Upside / Downside", pct((fair_ps / market_price - 1) * 100))

    premises = (
        f"{rep['title']} · taxa de desconto {pct(pr['rate'] * 100)} · crescimento inicial "
        f"{pct(pr['g1'] * 100)} caindo {num(pr['decay'] * 100, 1)} p.p./ano até {pct(pr['gt'] * 100)} · "
        f"{pr['years']} anos"
    )
    if pr["roe"] > 0:
        premises += f" · ROE {pct(pr['roe'] * 100)}"
    if pr["payout"] > 0:
        premises += f" · payout {pct(pr['payout'] * 100)}"
    if rep["g_sust"] is not None:
        premises += f" · crescimento sustentável ROE × (1 − payout) = {pct(rep['g_sust'] * 100)}"
    st.caption(premises)

    if rep["last_closed"]:
        change = rep["profit"] / rep["last_closed"] - 1
        st.caption(
            f"Lucro projetivo do ano vs. último ano fechado: {pct(change * 100)}."
            + (f" CAGR do histórico ({rep['window']} anos): {pct(rep['cagr'] * 100)}." if rep["cagr"] is not None else "")
            + (f" Média do lucro na janela (lucro normalizado): R$ {thousands(rep['hist_avg'])} mi."
               if rep["hist_avg"] is not None else "")
        )

    rows = []
    for h in rep["history"]:
        rows.append({
            "Ano": f"{h['year']} (realizado)",
            "Lucro líquido (R$ mi)": thousands(h["earnings"]),
            "Crescimento": pct(h["growth"] * 100) if h["growth"] is not None else "—",
            "Fluxo ao acionista (R$ mi)": "—", "% do lucro": "—", "Valor presente (R$ mi)": "—",
        })
    for row in res["rows"]:
        year = rep["base_year"] + row["k"]
        rows.append({
            "Ano": f"{year} (projetado)",
            "Lucro líquido (R$ mi)": thousands(row["earnings"]),
            "Crescimento": pct(row["growth"] * 100) if row["growth"] is not None else "projetivo",
            "Fluxo ao acionista (R$ mi)": thousands(row["flow"]),
            "% do lucro": pct(row["ratio"] * 100),
            "Valor presente (R$ mi)": thousands(row["pv"]),
        })
    rows.append({
        "Ano": "Perpetuidade", "Lucro líquido (R$ mi)": "—",
        "Crescimento": pct(pr["gt"] * 100), "Fluxo ao acionista (R$ mi)": "—",
        "% do lucro": pct(res["terminal_ratio"] * 100),
        "Valor presente (R$ mi)": thousands(res["pv_terminal"]),
    })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    if rep["scenarios"]:
        st.markdown("**Cenários** (crescimento inicial ±2 p.p. e taxa de desconto ∓1 p.p.)")
        st.dataframe(
            [
                {
                    "Cenário": s["name"],
                    "Crescimento inicial": pct(s["g1"] * 100),
                    "Taxa de desconto": pct(s["rate"] * 100),
                    "Preço justo": brl(s["fair"]),
                    "Preço-teto (com margem)": brl(s["ceiling"]),
                    "Distância até o teto": pct((s["ceiling"] / market_price - 1) * 100),
                }
                for s in rep["scenarios"]
            ],
            use_container_width=True, hide_index=True,
        )


def render_bazin_tables(dpa_avg, dpa_proj, chosen_yield, price):
    """Preço-teto do Bazin para yields de 6% a 10% (e o escolhido), histórico e projetivo."""
    if dpa_avg <= 0 and dpa_proj <= 0:
        return
    st.markdown("### Bazin: preço-teto por yield mínimo")
    chosen = round(chosen_yield, 2)
    rows = []
    for y in sorted(set(BAZIN_YIELDS + [chosen])):
        row = {"Yield mínimo": pct(y) + (" ← escolhido" if abs(y - chosen) < 1e-9 else "")}
        for label, dpa in (("3 anos", dpa_avg), ("projetivo", dpa_proj)):
            if dpa > 0:
                ceiling = dpa / (y / 100)
                row[f"Teto: Bazin {label}"] = brl(ceiling)
                row[f"Distância ({label})"] = f"{'🟢' if price <= ceiling else '🔴'} {pct((ceiling / price - 1) * 100)}"
        rows.append(row)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(
        "Quanto maior o yield exigido, menor o preço-teto. 🟢 = preço atual dentro do teto; "
        "distância = teto ÷ preço atual − 1."
    )


def render_tracking_row(ticker, price, dy_pct, lpa, dpa, payout, shares_mi, profit, ceiling_proj):
    """Linha no formato da planilha de acompanhamento."""
    st.markdown("### Planilha de acompanhamento")
    st.dataframe(
        [{
            "Ação": ticker.upper().strip(),
            "Cotação atual": brl(price),
            "DY (12M)": pct(dy_pct) if dy_pct > 0 else "—",
            "LPA": brl(lpa),
            "DPA": brl(dpa),
            "Payout": pct(payout),
            "Nº de papéis (mi)": thousands(shares_mi),
            "Lucro projetivo (R$ mi)": thousands(profit),
            "Preço-teto projetivo": brl(ceiling_proj),
        }],
        use_container_width=True, hide_index=True,
    )
    st.caption("DPA = LPA projetivo × payout. Preço-teto projetivo = DPA ÷ yield mínimo escolhido.")


# ---------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------
init_db()

st.title("📊 Calculadora de Preço Teto (Valuation)")
st.write(
    "Métodos clássicos (Bazin, Graham, Fluxo Descontado, P/VP justo e P/VP alvo) "
    "para descobrir até que preço vale a pena começar a comprar uma ação ou FII, "
    "com um teste de sanidade contra os múltiplos históricos da própria empresa."
)

tab_stock, tab_fii, tab_watch, tab_history = st.tabs(
    ["📈 Ações", "🏢 Fundos Imobiliários", "👀 Lista de observação", "📂 Histórico"]
)

# ---------------------------------------------------------------------
# AÇÕES
# ---------------------------------------------------------------------
with tab_stock:
    st.subheader("Valuation de Ações")
    st.caption(
        "Deixe em zero os dados dos métodos que não quiser calcular. Valores totais da empresa "
        "(lucro, nº de ações) em milhões de reais / milhões de ações. Os resultados ficam abaixo do formulário."
    )

    sector = st.selectbox(
        "Setor (preenche sugestões iniciais; tudo pode ser editado)", list(SECTOR_PRESETS.keys())
    )
    preset = SECTOR_PRESETS[sector]
    default_rate = round(RISK_FREE_DEFAULT + preset["premium"], 2)
    st.info(
        f"**{sector}:** {preset['note']}  \n"
        f"Sugestões: yield mínimo {pct(preset['yield'])}, crescimento inicial {pct(preset['g1'])} ao ano, "
        f"taxa de desconto {pct(default_rate)} (taxa livre de risco {pct(RISK_FREE_DEFAULT)} + prêmio de "
        f"{num(preset['premium'], 1)} p.p.) e margem de segurança de {pct(preset['margin'])}."
    )
    key = sector.replace(" ", "_")  # as sugestões recarregam quando o setor muda

    with st.form("stock_form"):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Dados de mercado**")
            stock_ticker = st.text_input("Ticker (ex: BBAS3)")
            stock_price = st.number_input("Preço atual (R$)", min_value=0.0, step=0.10)
            shares_mi = st.number_input(
                "Nº de ações (milhões)", min_value=0.0, step=1.0,
                help="Total de ações em circulação (sem tesouraria). Use o mesmo número em todos os métodos.",
            )
            dy_site = st.number_input(
                "Dividend Yield 12M do site (%) — opcional", min_value=0.0, step=0.1,
                help="Só para a planilha de acompanhamento. Com 0, o app usa o DPA do último ano ÷ preço.",
            )
            payout = st.number_input(
                "Payout (%)", min_value=0.0, max_value=100.0, step=1.0,
                help="Parcela do lucro distribuída. DPA = LPA × payout.",
            )
            roe = st.number_input(
                "ROE (%)", min_value=0.0, step=0.5,
                help="Sustentável (média de 3 a 5 anos). Define o crescimento sustentável, "
                     "ROE × (1 − payout), e o reinvestimento do método FCFE.",
            )

            st.markdown("**Lucro projetivo do ano (R$ milhões)**")
            st.caption(
                "Pegue o lucro líquido ajustado do 1º semestre e projete o 2º com base no "
                "comportamento dos anos anteriores. Lucro do ano = 1º sem. × (2 + variação)."
            )
            profit_1s = st.number_input("Lucro líquido ajustado do 1º semestre (R$ mi)", min_value=0.0, step=10.0)
            adj_2s = st.number_input(
                "Variação esperada do 2º semestre vs. 1º (%)", min_value=-100.0, value=0.0, step=1.0,
            )
            profit_override = st.number_input(
                "Ou informe o lucro projetado do ano (R$ mi)", min_value=0.0, step=10.0,
                help="Se maior que zero, substitui o cálculo pelo semestre.",
            )

            st.markdown("**Margem e consolidação**")
            stock_margin = st.number_input(
                "Margem de segurança (%)", min_value=0.0, max_value=99.0,
                value=preset["margin"], step=1.0, key=f"margin_{key}",
                help="Aplicada ao fluxo descontado e ao P/VP justo, que são estimativas.",
            )
            apply_all = st.checkbox(
                "Aplicar a margem também a Bazin e Graham", value=False,
                help="Bazin e Graham já resultam em tetos conservadores por construção.",
            )
            stock_criterion = st.selectbox("Como consolidar os métodos", list(CRITERIA.keys()))

        with col2:
            st.markdown("**Décio Bazin (3 anos de proventos)**")
            dpa_1 = st.number_input("DPA do último ano (R$/ação)", min_value=0.0, step=0.10)
            dpa_2 = st.number_input("DPA do penúltimo ano (R$/ação)", min_value=0.0, step=0.10)
            dpa_3 = st.number_input("DPA do antepenúltimo ano (R$/ação)", min_value=0.0, step=0.10)
            bazin_yield = st.number_input(
                "Yield mínimo exigido (%)", min_value=1.0, value=preset["yield"], step=0.5,
                key=f"yield_{key}",
                help="O mínimo clássico é 6%. A tabela dos resultados mostra o teto de 6% a 10%. "
                     "Para ser coerente com o fluxo descontado, o yield deve ser parecido com a "
                     "taxa de desconto menos o crescimento de longo prazo.",
            )

            with st.expander("Outros métodos: Graham e P/VP justo"):
                eps_graham = st.number_input(
                    "LPA (R$/ação) — 0 usa o LPA projetivo", min_value=0.0, step=0.10,
                    help="Ideal: média de 3 a 5 anos.",
                )
                bvps = st.number_input("VPA — Valor patrimonial por ação (R$)", min_value=0.0, step=0.10)
                max_pl = st.number_input("P/L máximo (Graham)", min_value=0.1, value=15.0, step=0.5)
                max_pvp = st.number_input("P/VP máximo (Graham)", min_value=0.1, value=1.5, step=0.1)
                st.caption("O P/VP justo pelo ROE usa o ROE, o VPA, a taxa de desconto e a perpetuidade.")

            with st.expander("Teste de sanidade: múltiplos históricos (opcional)"):
                h_pl_avg = st.number_input("P/L médio histórico", min_value=0.0, step=0.5)
                h_pl_max = st.number_input("P/L máximo histórico", min_value=0.0, step=0.5)
                h_pvp_avg = st.number_input("P/VP médio histórico", min_value=0.0, step=0.05)
                h_pvp_max = st.number_input("P/VP máximo histórico", min_value=0.0, step=0.05)
                h_dy_avg = st.number_input("Yield médio histórico (%)", min_value=0.0, step=0.5)

        with col3:
            st.markdown("**Fluxo de Caixa Descontado (Damodaran)**")
            hist_text = st.text_input(
                "Lucro líquido dos últimos anos (R$ mi)",
                placeholder="Ex.: 1177,6; 1151,5; 1503,4; 1545,3; 2079,6",
                help="Do mais antigo ao mais recente, separados por ';'. Vírgula como decimal e sem ponto de "
                     "milhar. Serve de referência (crescimento e CAGR) e não entra no valor: o passado já "
                     "está no balanço e no preço.",
            )
            hist_window = st.selectbox("Janela do histórico (anos)", [3, 5, 7], index=1)
            flow_label = st.selectbox("Fluxo a descontar", list(FLOW_MODES.keys()))
            g1_pct = st.number_input(
                "Crescimento inicial a partir do 2º ano (%)", min_value=-50.0,
                value=preset["g1"], step=0.5, key=f"g1_{key}",
            )
            decay_pp = st.number_input(
                "Redução do crescimento por ano (p.p.)", min_value=0.0, value=0.5, step=0.1,
                help="O crescimento converge para o da perpetuidade, caindo esse valor a cada ano.",
            )
            years = st.select_slider("Anos de projeção (incluindo o ano corrente)", options=[3, 5, 7, 10], value=5)
            discount_rate = st.number_input(
                "Taxa de desconto (%)", min_value=0.1, value=default_rate, step=0.25, key=f"rate_{key}",
                help="Taxa livre de risco (Selic ou NTN-B) + prêmio de risco do setor.",
            )
            terminal_growth = st.number_input(
                "Crescimento na perpetuidade (%)", min_value=-10.0, value=3.0, step=0.5,
                help="No máximo próximo à inflação de longo prazo.",
            )
            terminal_roe = st.number_input(
                "ROE na perpetuidade (%) — 0 = igual à taxa de desconto", min_value=0.0, step=0.5,
                help="Usado só no FCFE. Igual à taxa de desconto significa que crescer não cria valor "
                     "depois do período projetado (hipótese conservadora de Damodaran).",
            )
            fraction_pct = st.number_input(
                "Fração do ano corrente que ainda gera fluxo (%)", min_value=1.0, max_value=100.0,
                value=100.0, step=5.0,
                help="Se parte dos dividendos do ano já foi paga, reduza. 100% = ano inteiro.",
            )

        stock_submit = st.form_submit_button("Calcular Preço Teto", use_container_width=True)

    if stock_submit:
        if not stock_ticker.strip() or stock_price <= 0:
            st.error("Informe um ticker e um preço atual maior que zero.")
        else:
            outcome = analyze_stock({
                "price": stock_price, "shares_mi": shares_mi, "payout": payout, "roe": roe,
                "profit_1s": profit_1s, "adj_2s": adj_2s, "profit_override": profit_override,
                "dpa": [dpa_1, dpa_2, dpa_3], "bazin_yield": bazin_yield,
                "eps_graham": eps_graham, "bvps": bvps, "max_pl": max_pl, "max_pvp": max_pvp,
                "hist_text": hist_text, "hist_window": hist_window, "flow_key": FLOW_MODES[flow_label],
                "g1": g1_pct, "decay": decay_pp, "years": years, "rate": discount_rate,
                "terminal_growth": terminal_growth, "terminal_roe": terminal_roe,
                "fraction": fraction_pct, "margin": stock_margin, "apply_all": apply_all,
                "current_year": datetime.now().year,
            })

            eps_used, dpa_ref = outcome["eps"], outcome["dpa_proj"] or outcome["dpa_avg"]
            dy_shown = dy_site if dy_site > 0 else (dpa_1 / stock_price * 100 if dpa_1 > 0 else 0.0)
            indicators = [
                ("P/L projetivo", num(stock_price / outcome["lpa_proj"], 1) if outcome["lpa_proj"] > 0 else "—"),
                ("P/VP atual", num(stock_price / bvps) if bvps > 0 else "—"),
                ("Dividend Yield 12M", pct(dy_shown) if dy_shown > 0 else "—"),
            ]
            sanity = {
                "eps": eps_used, "bvps": bvps, "dividend": dpa_ref,
                "hist": {"pl_avg": h_pl_avg, "pl_max": h_pl_max, "pvp_avg": h_pvp_avg,
                         "pvp_max": h_pvp_max, "dy_avg": h_dy_avg},
            }

            show_results("Ação", stock_ticker, stock_price, stock_margin, outcome["methods"],
                         stock_criterion, outcome["notes"], indicators, sanity)

            if outcome["report"]:
                render_dcf_report(outcome["report"], stock_price)
            render_bazin_tables(outcome["dpa_avg"], outcome["dpa_proj"], bazin_yield, stock_price)
            if outcome["dpa_proj"] > 0:
                render_tracking_row(
                    stock_ticker, stock_price, dy_shown, outcome["lpa_proj"], outcome["dpa_proj"],
                    payout, shares_mi, outcome["profit"], outcome["dpa_proj"] / (bazin_yield / 100),
                )

# ---------------------------------------------------------------------
# FUNDOS IMOBILIÁRIOS
# ---------------------------------------------------------------------
with tab_fii:
    st.subheader("Valuation de Fundos Imobiliários (FIIs e FIAGROs)")
    st.info(
        "💡 Em FIIs, o preço-teto por yield e por P/VP já são limites de compra por construção. "
        "A margem de segurança adicional é opcional e o padrão é 0%."
    )

    with st.form("fii_form"):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Método do rendimento (Yield exigido)**")
            fii_ticker = st.text_input("Ticker do FII (ex: HGLG11)")
            fii_price = st.number_input("Preço atual da cota (R$)", min_value=0.0, step=0.10)
            monthly_distribution = st.number_input(
                "Rendimento mensal médio (R$ por cota)", min_value=0.0, step=0.01,
                help="Média dos últimos 12 meses, sem rendimentos extraordinários (ganho de capital, "
                     "venda de imóveis).",
            )
            target_yield = st.number_input(
                "Dividend Yield exigido (% ao ano)", min_value=0.1, value=9.0, step=0.5,
                help="Sugestão: rendimento de um título longo (ex.: NTN-B) + prêmio de risco pelo FII.",
            )

            st.markdown("**Referência histórica do fundo (opcional)**")
            fh_pvp_avg = st.number_input("P/VP médio histórico", min_value=0.0, step=0.05, key="fh_pvp_avg")
            fh_pvp_max = st.number_input("P/VP máximo histórico", min_value=0.0, step=0.05, key="fh_pvp_max")
            fh_dy_avg = st.number_input("Yield médio histórico (%)", min_value=0.0, step=0.5, key="fh_dy_avg")

        with col2:
            st.markdown("**Método patrimonial (P/VP)**")
            nav_per_share = st.number_input("VPA — Valor patrimonial por cota (R$)", min_value=0.0, step=0.10)
            target_pvp = st.number_input(
                "P/VP máximo aceitável", min_value=0.0, value=1.0, step=0.05,
                help="Tijolo de boa qualidade costuma ser avaliado até ~1,0. "
                     "Fundos de papel podem ter pequeno prêmio; ativos fracos ou com vacância exigem desconto.",
            )
            fii_margin = st.number_input(
                "Margem de segurança adicional (%)", min_value=0.0, max_value=99.0, value=0.0, step=1.0,
            )
            fii_criterion = st.selectbox("Como consolidar os métodos", list(CRITERIA.keys()))

        fii_submit = st.form_submit_button("Calcular Preço Teto", use_container_width=True)

    if fii_submit:
        if not fii_ticker.strip() or fii_price <= 0:
            st.error("Informe o ticker e o preço atual da cota (maior que zero).")
        else:
            methods = []

            if monthly_distribution > 0:
                methods.append(make_method(
                    "Preço-teto por Yield exigido",
                    monthly_distribution * 12 / (target_yield / 100),
                    f"Garante pelo menos {target_yield:.1f}% ao ano em rendimentos.",
                    fii_margin, True,
                ))

            if nav_per_share > 0 and target_pvp > 0:
                methods.append(make_method(
                    "Preço-teto pelo P/VP máximo",
                    nav_per_share * target_pvp,
                    f"Não paga mais que {target_pvp:.2f}x o patrimônio por cota.",
                    fii_margin, True,
                ))

            indicators = [
                ("Yield atual (rendimento informado)",
                 pct(monthly_distribution * 12 / fii_price * 100) if monthly_distribution > 0 else "—"),
                ("P/VP atual", num(fii_price / nav_per_share) if nav_per_share > 0 else "—"),
            ]
            sanity = {
                "eps": 0.0, "bvps": nav_per_share, "dividend": monthly_distribution * 12,
                "hist": {"pl_avg": 0.0, "pl_max": 0.0, "pvp_avg": fh_pvp_avg,
                         "pvp_max": fh_pvp_max, "dy_avg": fh_dy_avg},
            }

            show_results("FII", fii_ticker, fii_price, fii_margin, methods,
                         fii_criterion, indicators=indicators, sanity=sanity)

# ---------------------------------------------------------------------
# LISTA DE OBSERVAÇÃO
# ---------------------------------------------------------------------
with tab_watch:
    st.subheader("Lista de observação")
    st.caption(
        "Guarde o preço-teto de cada ativo que você acompanha e atualize o preço atual quando "
        "quiser conferir. Cadastre também um alerta de preço na corretora no valor do preço-teto. "
        "Ativos com mais de 90 dias sem atualização aparecem marcados para revisão, "
        "porque os dados mudam a cada divulgação de resultado."
    )

    with st.form("watch_form", clear_on_submit=True):
        w1, w2, w3, w4 = st.columns(4)
        w_ticker = w1.text_input("Ticker")
        w_type = w2.selectbox("Tipo", ["Ação", "FII"])
        w_price = w3.number_input("Preço atual (R$)", min_value=0.0, step=0.10)
        w_ceiling = w4.number_input(
            "Preço-teto (R$)", min_value=0.0, step=0.10,
            help="Deixe 0 para usar o preço-teto da última análise salva desse ticker.",
        )
        w_note = st.text_input(
            "Observação", placeholder="Ex.: esperar o resultado do 3T26; checar inadimplência"
        )
        w_submit = st.form_submit_button("Salvar na lista", use_container_width=True)

    if w_submit:
        ticker = w_ticker.upper().strip()
        if not ticker:
            st.error("Informe o ticker.")
        else:
            existing = get_watch(ticker)
            ceiling = w_ceiling
            if ceiling <= 0:
                saved = {a["ticker"]: a for a in latest_analyses()}.get(ticker)
                ceiling = saved["ceiling"] if saved else (existing["ceiling"] if existing else 0.0)
            price = w_price if w_price > 0 else (existing["last_price"] if existing else 0.0)

            if ceiling <= 0:
                st.error(
                    "Sem preço-teto: informe um valor ou calcule o ativo nas abas Ações/FIIs antes."
                )
            else:
                upsert_watch(ticker, w_type, ceiling, price, w_note)
                st.success(f"{ticker} salvo na lista.")

    if st.button("Importar tickers do histórico de análises"):
        count = import_watch_from_history()
        st.success(f"{count} ativo(s) importado(s) ou atualizado(s) a partir do histórico.")

    watch_rows = load_watchlist()

    if not watch_rows:
        st.info("A lista está vazia. Adicione um ativo acima ou importe do histórico.")
    else:
        # Quem tem mais folga até o teto aparece primeiro; sem preço vai para o fim
        watch_rows.sort(
            key=lambda r: (watch_distance(r) is None, -(watch_distance(r) or 0.0))
        )

        in_zone = sum(1 for r in watch_rows if 0 < r["last_price"] <= r["ceiling"])
        k1, k2 = st.columns(2)
        k1.metric("Ativos na lista", len(watch_rows))
        k2.metric("Dentro do preço-teto", in_zone)

        st.dataframe(
            [
                {
                    "Ticker": r["ticker"],
                    "Tipo": r["asset_type"],
                    "Preço atual": brl(r["last_price"]) if r["last_price"] > 0 else "—",
                    "Preço-teto": brl(r["ceiling"]),
                    "Distância até o teto": (
                        pct(watch_distance(r) * 100) if watch_distance(r) is not None else "—"
                    ),
                    "Situação": watch_status(r["last_price"], r["ceiling"]),
                    "Revisar?": "⚠️ dados antigos" if days_since(r["updated_at"]) > 90 else "",
                    "Observação": r["note"],
                    "Atualizado em": r["updated_at"],
                }
                for r in watch_rows
            ],
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("Remover ativo da lista"):
            to_remove = st.selectbox("Ticker", [r["ticker"] for r in watch_rows])
            if st.button("Remover da lista"):
                delete_watch(to_remove)
                st.rerun()

# ---------------------------------------------------------------------
# HISTÓRICO
# ---------------------------------------------------------------------
with tab_history:
    st.subheader("Histórico de Análises")
    history = load_history()

    if not history:
        st.info("Ainda não há análises salvas. Calcule uma ação ou FII para começar.")
    else:
        table_data = [
            {
                "Data": row["created_at"],
                "Tipo": row["asset_type"],
                "Ticker": row["ticker"],
                "Preço de Tela": brl(row["market_price"]),
                "Preço-Teto": brl(row["ceiling"]),
                "Distância até o teto": pct((row["ceiling"] / row["market_price"] - 1) * 100),
                "Margem Usada": pct(row["margin_safety"]),
                "Situação": "🟢 Abaixo do teto" if row["ceiling"] >= row["market_price"] else "🔴 Acima do teto",
            }
            for row in history
        ]

        st.dataframe(table_data, use_container_width=True, hide_index=True)

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=table_data[0].keys())
        writer.writeheader()
        writer.writerows(table_data)

        st.download_button(
            "📥 Baixar histórico em planilha (CSV)",
            data=csv_buffer.getvalue().encode("utf-8-sig"),
            file_name="historico_precos_teto.csv",
            mime="text/csv",
        )

        with st.expander("Gerenciar histórico"):
            confirm = st.checkbox("Confirmo que quero apagar todo o histórico")
            if st.button("Apagar histórico", disabled=not confirm):
                clear_history()
                st.rerun()
