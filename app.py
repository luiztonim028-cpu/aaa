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

# Critérios para consolidar os métodos em um único preço-teto
CRITERIA = {
    "Mediana dos métodos (equilibrado)": "median",
    "Menor valor (mais conservador)": "min",
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
# UTILIDADES
# ---------------------------------------------------------------------
def brl(value):
    # Formato brasileiro: R$ 1.000,00
    return f"R$ {value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


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


# ---------------------------------------------------------------------
# EXIBIÇÃO DOS RESULTADOS
# ---------------------------------------------------------------------
def show_results(asset_type, ticker, market_price, margin, methods,
                 criterion_label, notes=None, indicators=None):
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

    st.caption(f"Consolidação: {criterion_label}.")

    st.dataframe(
        [
            {
                "Método": item["name"],
                "Valor justo / base": brl(item["fair_value"]),
                "Margem aplicada": pct(item["margin_applied"]),
                "Preço-teto": brl(item["ceiling"]),
                "Premissa": item["assumption"],
            }
            for item in methods
        ],
        use_container_width=True,
        hide_index=True,
    )

    save_valuation(asset_type, ticker, market_price, margin, ceiling, methods, criterion_label)
    st.info("💾 Análise salva no histórico.")


# ---------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------
init_db()

st.title("📊 Calculadora de Preço Teto (Valuation)")
st.write(
    "Métodos clássicos e consagrados (Bazin, Graham, Fluxo Descontado e P/VP) "
    "para descobrir até que preço vale a pena começar a comprar uma ação ou FII."
)

tab_stock, tab_fii, tab_history = st.tabs(
    ["📈 Ações", "🏢 Fundos Imobiliários", "📂 Histórico"]
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
                "Margem de segurança (%)", min_value=0.0, max_value=99.0, value=20.0, step=1.0,
                help="Aplicada ao Fluxo Descontado, que é uma estimativa sujeita a erro de premissa.",
            )
            apply_all = st.checkbox(
                "Aplicar a margem também a Bazin e Graham",
                value=False,
                help="Bazin e Graham já resultam em preços-teto conservadores por construção. "
                     "Marque apenas se quiser ser ainda mais rigoroso.",
            )
            stock_criterion = st.selectbox("Como consolidar os métodos", list(CRITERIA.keys()))

        with col2:
            st.markdown("**Décio Bazin (Dividendos)**")
            dpa = st.number_input(
                "DPA médio anual (R$)", min_value=0.0, step=0.10,
                help="Ideal: média dos dividendos por ação dos últimos 5 anos.",
            )
            bazin_yield = st.number_input(
                "Yield mínimo exigido (%)", min_value=0.1, value=6.0, step=0.5,
                help="Bazin usa 6%. Em juros altos, considere exigir mais.",
            )

            st.markdown("**Número de Graham**")
            eps = st.number_input(
                "LPA — Lucro por ação (R$)", min_value=0.0, step=0.10,
                help="Ideal: média dos últimos 3 a 5 anos (suaviza ciclos).",
            )
            bvps = st.number_input("VPA — Valor patrimonial por ação (R$)", min_value=0.0, step=0.10)
            max_pl = st.number_input("P/L máximo", min_value=0.1, value=15.0, step=0.5)
            max_pvp = st.number_input("P/VP máximo", min_value=0.1, value=1.5, step=0.1)

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
            if base_flow > 0:
                r, g, gt = discount_rate / 100, growth / 100, terminal_growth / 100
                if r <= gt:
                    notes.append(
                        "Fluxo descontado ignorado: a taxa de desconto precisa ser maior "
                        "que o crescimento na perpetuidade."
                    )
                else:
                    fair_value, terminal_share = discounted_cash_flow(
                        base_flow, g, r, gt, int(years)
                    )
                    label = "DDM (Dividendos descontados)" if flow_type.startswith("Dividendos") else "FCD (Fluxo de Caixa Descontado)"
                    methods.append(make_method(
                        label,
                        fair_value,
                        f"Fluxo cresce {growth:.1f}% a.a. por {int(years)} anos, "
                        f"depois {terminal_growth:.1f}% na perpetuidade; desconto de {discount_rate:.1f}%.",
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

            indicators = [("P/L atual", f"{stock_price / eps:.1f}".replace(".", ",") if eps > 0 else "—"),
                          ("P/VP atual", f"{stock_price / bvps:.2f}".replace(".", ",") if bvps > 0 else "—"),
                          ("Dividend Yield", pct(dpa / stock_price * 100) if dpa > 0 else "—")]

            show_results("Ação", stock_ticker, stock_price, stock_margin, methods,
                         stock_criterion, notes, indicators)

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
                ("Dividend Yield atual",
                 pct(monthly_distribution * 12 / fii_price * 100) if monthly_distribution > 0 else "—"),
                ("P/VP atual",
                 f"{fii_price / nav_per_share:.2f}".replace(".", ",") if nav_per_share > 0 else "—"),
            ]

            show_results("FII", fii_ticker, fii_price, fii_margin, methods,
                         fii_criterion, indicators=indicators)

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
