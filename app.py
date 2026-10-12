import csv
import io
import json
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


def discounted_cash_flow(base_flow, growth, rate, terminal_growth, years):
    """Valor presente de fluxos crescentes + valor terminal (Gordon).
    Retorna (valor_justo, fração do valor que vem da perpetuidade)."""
    present_value, flow = 0.0, base_flow
    for year in range(1, years + 1):
        flow *= 1 + growth
        present_value += flow / ((1 + rate) ** year)

    terminal_value = flow * (1 + terminal_growth) / (rate - terminal_growth)
    discounted_terminal = terminal_value / ((1 + rate) ** years)
    fair_value = present_value + discounted_terminal
    return fair_value, discounted_terminal / fair_value


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
        "Deixe em zero os dados dos métodos que não se aplicam à empresa. "
        "Use sempre dados de longo prazo (média de 5 anos para dividendos e lucros) "
        "para evitar distorções de um ano atípico."
    )

    with st.form("stock_form"):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Dados de mercado**")
            stock_ticker = st.text_input("Ticker (ex: BBAS3)")
            stock_price = st.number_input("Preço atual (R$)", min_value=0.0, step=0.10)
            stock_margin = st.number_input(
                "Margem de segurança (%)", min_value=0.0, max_value=99.0, value=15.0, step=1.0,
                help="Aplicada ao Fluxo Descontado e ao P/VP justo, que são estimativas "
                     "sujeitas a erro de premissa.",
            )
            apply_all = st.checkbox(
                "Aplicar a margem também a Bazin e Graham",
                value=False,
                help="Bazin e Graham já resultam em preços-teto conservadores por construção. "
                     "Marque apenas se quiser ser ainda mais rigoroso.",
            )
            stock_criterion = st.selectbox("Como consolidar os métodos", list(CRITERIA.keys()))

            st.markdown("**Referência histórica da empresa (opcional)**")
            st.caption("Do histórico de indicadores do site, sem anos atípicos. Deixe 0 para ignorar.")
            h_pl_avg = st.number_input("P/L médio histórico", min_value=0.0, step=0.5)
            h_pl_max = st.number_input("P/L máximo histórico", min_value=0.0, step=0.5)
            h_pvp_avg = st.number_input("P/VP médio histórico", min_value=0.0, step=0.05)
            h_pvp_max = st.number_input("P/VP máximo histórico", min_value=0.0, step=0.05)
            h_dy_avg = st.number_input("Yield médio histórico (%)", min_value=0.0, step=0.5)

        with col2:
            st.markdown("**Décio Bazin (Dividendos)**")
            dpa = st.number_input(
                "DPA médio anual (R$)", min_value=0.0, step=0.10,
                help="Ideal: média dos dividendos por ação dos últimos 5 anos, em reais.",
            )
            bazin_yield = st.number_input(
                "Yield mínimo exigido (%)", min_value=0.1, value=6.0, step=0.5,
                help="Bazin usa 6%. Para ser coerente com o fluxo descontado, o yield mínimo "
                     "deve ser parecido com o yield que o fluxo descontado equivale (aparece na "
                     "coluna Premissa do resultado).",
            )

            st.markdown("**Número de Graham**")
            eps = st.number_input(
                "LPA — Lucro por ação (R$)", min_value=0.0, step=0.10,
                help="Ideal: média dos últimos 3 a 5 anos (suaviza ciclos).",
            )
            bvps = st.number_input("VPA — Valor patrimonial por ação (R$)", min_value=0.0, step=0.10)
            max_pl = st.number_input("P/L máximo", min_value=0.1, value=15.0, step=0.5)
            max_pvp = st.number_input("P/VP máximo", min_value=0.1, value=1.5, step=0.1)

            st.markdown("**P/VP justo pelo ROE (ideal para bancos)**")
            roe = st.number_input(
                "ROE sustentável (%)", min_value=0.0, step=0.5,
                help="Média de 3 a 5 anos. Usa a taxa de desconto e o crescimento na perpetuidade "
                     "do bloco ao lado. Deixe 0 para ignorar.",
            )

        with col3:
            st.markdown("**Fluxo de Caixa Descontado**")
            flow_type = st.selectbox(
                "Fluxo a descontar",
                [
                    "FCL por ação (empresas comuns)",
                    "Dividendos por ação (bancos, seguradoras, elétricas)",
                ],
                help="Para bancos e seguradoras, o fluxo de caixa livre não é confiável; use dividendos.",
            )
            base_flow = st.number_input("Fluxo atual por ação (R$/ano)", min_value=0.0, step=0.10)
            growth = st.number_input("Crescimento anual do fluxo (%)", min_value=-50.0, value=5.0, step=0.5)
            discount_rate = st.number_input(
                "Taxa de desconto / retorno exigido (%)", min_value=0.1, value=12.0, step=0.5,
                help="Referência: taxa livre de risco (ex.: NTN-B) + prêmio de risco da empresa.",
            )
            terminal_growth = st.number_input(
                "Crescimento na perpetuidade (%)", min_value=-10.0, value=3.0, step=0.5,
                help="Deve ser conservador: no máximo próximo à inflação de longo prazo.",
            )
            years = st.number_input("Anos de projeção", min_value=1, max_value=30, value=5, step=1)

        stock_submit = st.form_submit_button("Calcular Preço Teto", use_container_width=True)

    if stock_submit:
        if not stock_ticker.strip() or stock_price <= 0:
            st.error("Informe um ticker e um preço atual maior que zero.")
        else:
            methods, notes = [], []
            r, g, gt = discount_rate / 100, growth / 100, terminal_growth / 100
            rate_ok = r > gt

            # 1. Bazin: preço máximo para receber o yield mínimo exigido
            if dpa > 0:
                methods.append(make_method(
                    "Décio Bazin (Dividendos)",
                    dpa / (bazin_yield / 100),
                    f"Paga no máximo o preço que rende {bazin_yield:.1f}% em dividendos.",
                    stock_margin, apply_all,
                ))

            # 2. Número de Graham: limita P/L e P/VP ao mesmo tempo
            if eps > 0 and bvps > 0:
                methods.append(make_method(
                    "Número de Graham",
                    (max_pl * max_pvp * eps * bvps) ** 0.5,
                    f"Respeita P/L ≤ {max_pl:.1f} e P/VP ≤ {max_pvp:.1f} "
                    f"(constante {max_pl * max_pvp:.1f}).",
                    stock_margin, apply_all,
                ))
            elif eps > 0 or bvps > 0:
                notes.append("Graham ignorado: informe LPA e VPA, ambos positivos.")

            # 3. Fluxo descontado (FCL ou dividendos)
            if (base_flow > 0 or roe > 0) and not rate_ok:
                notes.append(
                    "Fluxo descontado e P/VP justo ignorados: a taxa de desconto precisa ser "
                    "maior que o crescimento na perpetuidade."
                )
            if base_flow > 0 and rate_ok:
                fair_value, terminal_share = discounted_cash_flow(base_flow, g, r, gt, int(years))
                is_ddm = flow_type.startswith("Dividendos")
                label = "DDM (Dividendos descontados)" if is_ddm else "FCD (Fluxo de Caixa Descontado)"
                methods.append(make_method(
                    label,
                    fair_value,
                    f"Fluxo cresce {growth:.1f}% a.a. por {int(years)} anos, depois "
                    f"{terminal_growth:.1f}% na perpetuidade; desconto de {discount_rate:.1f}%. "
                    f"Equivale a exigir yield de {pct(base_flow / fair_value * 100)} "
                    f"sobre o fluxo atual (antes da margem).",
                    stock_margin, True,
                ))
                if terminal_share > 0.75:
                    notes.append(
                        f"{terminal_share:.0%} do valor do fluxo descontado vem da perpetuidade. "
                        "O resultado é muito sensível às premissas; considere aumentar os anos "
                        "de projeção ou revisar as taxas."
                    )
                if terminal_growth > 5:
                    notes.append("Crescimento na perpetuidade acima de 5% é agressivo.")

            # 4. P/VP justo pelo ROE (principal método para bancos)
            if roe > 0 and rate_ok:
                if bvps <= 0:
                    notes.append("P/VP justo ignorado: informe o VPA.")
                else:
                    fair_pvp = justified_pvp(roe / 100, r, gt)
                    if fair_pvp is None:
                        notes.append(
                            "P/VP justo ignorado: o ROE precisa ser maior que o crescimento "
                            "na perpetuidade."
                        )
                    else:
                        methods.append(make_method(
                            "P/VP justo pelo ROE",
                            bvps * fair_pvp,
                            f"P/VP justo = (ROE {roe:.1f}% − {terminal_growth:.1f}%) ÷ "
                            f"({discount_rate:.1f}% − {terminal_growth:.1f}%) = {num(fair_pvp)}x.",
                            stock_margin, True,
                        ))

            indicators = [
                ("P/L atual", num(stock_price / eps, 1) if eps > 0 else "—"),
                ("P/VP atual", num(stock_price / bvps) if bvps > 0 else "—"),
                ("Yield s/ DPA informado", pct(dpa / stock_price * 100) if dpa > 0 else "—"),
            ]
            sanity = {
                "eps": eps, "bvps": bvps, "dividend": dpa,
                "hist": {"pl_avg": h_pl_avg, "pl_max": h_pl_max, "pvp_avg": h_pvp_avg,
                         "pvp_max": h_pvp_max, "dy_avg": h_dy_avg},
            }

            show_results("Ação", stock_ticker, stock_price, stock_margin, methods,
                         stock_criterion, notes, indicators, sanity)

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
