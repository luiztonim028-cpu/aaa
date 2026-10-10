import csv
import io
import json
import sqlite3
from datetime import datetime

import streamlit as st

DB_FILE = "historico_valuation.db"

st.set_page_config(
    page_title="Preço Justo & Preço Teto",
    page_icon="📊",
    layout="wide",
)

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

def save_valuation(asset_type, ticker, market_price, margin, ceiling, methods):
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
                json.dumps(methods, ensure_ascii=False),
            ),
        )
        conn.commit()

def load_history():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM valuations ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]

def brl(value):
    # Formatação correta para o padrão brasileiro (R$ 1.000,00)
    return f"R$ {value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")

def show_results(asset_type, ticker, market_price, margin, methods):
    if not methods:
        st.error(
            "Não foi possível calcular nenhum método. "
            "Confira os campos e informe dados positivos quando necessário."
        )
        return

    # O preço teto será o menor valor entre os métodos calculados (conservadorismo)
    ceiling = min(item["ceiling"] for item in methods)
    upside = (ceiling / market_price) - 1 if market_price > 0 else 0

    st.markdown(f"### Análise de {ticker.upper().strip()}")
    
    # Lógica de Cores: Verde se estiver abaixo do teto, Laranja/Aviso se estiver acima
    if upside > 0:
        st.success(f"Oportunidade! Preço Teto: {brl(ceiling)}")
    else:
        st.warning(f"Cuidado! Ativo acima do Preço Teto: {brl(ceiling)}")

    if market_price > 0:
        st.write(
            f"Preço de tela: **{brl(market_price)}** | "
            f"Upside/Desconto até o teto: **{upside:.1%}**"
        )

    st.caption(
        f"Margem de segurança aplicada: {margin:.1f}%. "
        "O sistema adota o menor resultado como a referência mais segura."
    )

    # Exibir tabela com os métodos
    st.dataframe(
        [
            {
                "Método": item["name"],
                "Valor Justo Base": brl(item["fair_value"]),
                "Preço-Teto (C/ Margem)": brl(item["ceiling"]),
                "Premissa": item["assumption"],
            }
            for item in methods
        ],
        use_container_width=True,
        hide_index=True,
    )

    save_valuation(asset_type, ticker, market_price, margin, ceiling, methods)
    st.info("💾 Análise salva no histórico.")

init_db()

st.title("📊 Calculadora de Preço Teto (Valuation)")
st.write(
    "Utilize as métricas mais consagradas do mercado (Graham, Bazin, FCD) "
    "para encontrar o preço justo de seus ativos."
)

tab_stock, tab_fii, tab_history = st.tabs(
    ["📈 Ações", "🏢 Fundos Imobiliários", "📂 Histórico"]
)

# ---------------------------------------------------------------------
# AÇÕES
# ---------------------------------------------------------------------
with tab_stock:
    st.subheader("Valuation de Ações")
    st.caption("Preencha os dados fundamentalistas da ação. Deixe em zero os métodos que não quiser calcular.")

    with st.form("stock_form"):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Dados de Mercado**")
            stock_ticker = st.text_input("Ticker (Ex: BBAS3)")
            stock_price = st.number_input("Preço atual (R$)", min_value=0.0, step=0.10)
            stock_margin = st.number_input("Margem de segurança global (%)", min_value=0.0, max_value=99.0, value=20.0, step=1.0)
            
            st.markdown("**Método Décio Bazin (Dividendos)**")
            dpa = st.number_input("DPA - Dividendos pagos em 12m (R$)", min_value=0.0, step=0.10)
            bazin_yield = st.number_input("Yield mínimo aceitável (%)", min_value=0.1, value=6.0, step=0.5)

        with col2:
            st.markdown("**Método de Graham (Valor Patrimonial)**")
            eps = st.number_input("LPA — Lucro por ação (R$)", min_value=0.0, step=0.10)
            bvps = st.number_input("VPA — Valor patrimonial p/ ação (R$)", min_value=0.0, step=0.10)
            target_pe = st.number_input("P/L-alvo (Múltiplo histórico)", min_value=0.0, value=10.0, step=0.5)

        with col3:
            st.markdown("**Método FCD (Fluxo de Caixa Descontado)**")
            fcf = st.number_input("FCL - Fluxo de caixa livre p/ ação (R$)", min_value=0.0, step=0.10)
            growth = st.number_input("Crescimento anual FCL (% ao ano)", min_value=-99.0, value=5.0, step=0.5)
            discount_rate = st.number_input("Taxa de desconto - Custo de capital (%)", min_value=0.1, value=12.0, step=0.5)
            terminal_growth = st.number_input("Crescimento terminal (%)", min_value=-99.0, value=3.0, step=0.5)
            years = st.number_input("Anos projetados", min_value=1, value=5)

        stock_submit = st.form_submit_button("Calcular Preço Teto", use_container_width=True)

    if stock_submit:
        if not stock_ticker.strip() or stock_price <= 0:
            st.error("Informe um Ticker e um Preço de Tela válido maior que zero.")
        else:
            methods = []
            margin_multiplier = (1 - stock_margin / 100)

            # 1. Método Décio Bazin (Novo)
            if dpa > 0 and bazin_yield > 0:
                fair_value = dpa / (bazin_yield / 100)
                methods.append({
                    "name": "Décio Bazin (Dividendos)",
                    "fair_value": fair_value,
                    "ceiling": fair_value * margin_multiplier,
                    "assumption": f"Exige Dividend Yield mínimo de {bazin_yield:.1f}%.",
                })

            # 2. Número de Graham
            if eps > 0 and bvps > 0:
                fair_value = (22.5 * eps * bvps) ** 0.5
                methods.append({
                    "name": "Fórmula de Graham",
                    "fair_value": fair_value,
                    "ceiling": fair_value * margin_multiplier,
                    "assumption": "Avaliação por ativos tangíveis e lucro (VPA e LPA).",
                })

            # 3. Múltiplos (LPA x P/L Alvo)
            if eps > 0 and target_pe > 0:
                fair_value = eps * target_pe
                methods.append({
                    "name": "Múltiplo Histórico (P/L)",
                    "fair_value": fair_value,
                    "ceiling": fair_value * margin_multiplier,
                    "assumption": f"Projeta a ação sendo negociada a {target_pe:.1f}x o Lucro.",
                })

            # 4. FCD (Fluxo de Caixa Descontado)
            if fcf > 0:
                r, g, gt = discount_rate / 100, growth / 100, terminal_growth / 100
                if r <= gt:
                    st.warning("No FCD, a taxa de desconto precisa ser maior que o crescimento terminal. FCD ignorado.")
                else:
                    present_value, projected_fcf = 0.0, fcf
                    for year in range(1, int(years) + 1):
                        projected_fcf *= 1 + g
                        present_value += projected_fcf / ((1 + r) ** year)
                    
                    terminal_value = (projected_fcf * (1 + gt)) / (r - gt)
                    discounted_terminal = terminal_value / ((1 + r) ** int(years))
                    fair_value = present_value + discounted_terminal

                    if fair_value > 0:
                        methods.append({
                            "name": "FCD (Fluxo de Caixa Descontado)",
                            "fair_value": fair_value,
                            "ceiling": fair_value * margin_multiplier,
                            "assumption": f"FCL cresce {growth}% a.a por {years} anos. Desconto de {discount_rate}%.",
                        })

            show_results("Ação", stock_ticker, stock_price, stock_margin, methods)

# ---------------------------------------------------------------------
# FUNDOS IMOBILIÁRIOS (FIIs)
# ---------------------------------------------------------------------
with tab_fii:
    st.subheader("Valuation de Fundos Imobiliários (FIIs e FIAGROs)")
    st.info(
        "💡 **Dica de Ouro:** Para fundos imobiliários, aplicar Margem de Segurança geralmente não faz sentido "
        "se você já definiu o seu Rendimento (Yield) alvo. Recomenda-se deixar a margem em 0%."
    )

    with st.form("fii_form"):
        col1, col2 = st.columns(2)

        with col1:
            fii_ticker = st.text_input("Ticker do FII (Ex: HGLG11)")
            fii_price = st.number_input("Preço atual da cota (R$)", min_value=0.0, step=0.10)
            monthly_distribution = st.number_input("Dividendo mensal médio (R$ por cota)", min_value=0.0, step=0.01)
            target_yield = st.number_input("Dividend Yield alvo exigido (% ao ano)", min_value=0.1, value=9.0, step=0.5)

        with col2:
            nav_per_share = st.number_input("VPA - Valor Patrimonial por cota (R$)", min_value=0.0, step=0.10)
            target_pvp = st.number_input("P/VP teto aceitável (Múltiplo)", min_value=0.0, value=1.0, step=0.05)
            fii_margin = st.number_input("Margem de segurança adicional (%)", min_value=0.0, max_value=99.0, value=0.0, step=1.0)

        fii_submit = st.form_submit_button("Calcular Preço Teto", use_container_width=True)

    if fii_submit:
        if not fii_ticker.strip() or fii_price <= 0:
            st.error("Informe o ticker e o preço atual da cota válido.")
        else:
            methods = []
            margin_multiplier = (1 - fii_margin / 100)

            # 1. Teto por Rendimento (Gordon FIIs)
            if monthly_distribution > 0 and target_yield > 0:
                annual_distribution = monthly_distribution * 12
                fair_value = annual_distribution / (target_yield / 100)
                methods.append({
                    "name": "Preço Teto por Rendimento (Yield)",
                    "fair_value": fair_value,
                    "ceiling": fair_value * margin_multiplier,
                    "assumption": f"Garante no mínimo {target_yield:.1f}% ao ano em dividendos.",
                })

            # 2. Teto por Patrimônio (P/VP)
            if nav_per_share > 0 and target_pvp > 0:
                fair_value = nav_per_share * target_pvp
                methods.append({
                    "name": "Preço Teto pelo P/VP Alvo",
                    "fair_value": fair_value,
                    "ceiling": fair_value * margin_multiplier,
                    "assumption": f"Evita pagar mais que {target_pvp:.2f}x o valor dos imóveis do fundo.",
                })

            show_results("FII", fii_ticker, fii_price, fii_margin, methods)

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
                "Margem Usada": f'{row["margin_safety"]:.1f}%',
                "Situação": "🟢 Comprar" if row["ceiling"] > row["market_price"] else "🔴 Aguardar"
            }
            for row in history
        ]

        st.dataframe(table_data, use_container_width=True, hide_index=True)

        # Funcionalidade de Download CSV
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=table_data[0].keys())
        writer.writeheader()
        writer.writerows(table_data)

        st.download_button(
            "📥 Baixar histórico em Planilha (CSV)",
            data=csv_buffer.getvalue().encode("utf-8-sig"),
            file_name="historico_precos_teto.csv",
            mime="text/csv",
        )