# CINAX DJ v3 — Producción Diaria (Cloud Run)
# Igual que antes + servidor HTTP para health check de Cloud Run

import numpy as np
import pandas as pd
import yfinance as yf
import pickle
import os
import time
import warnings
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pytz
warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════
# HEALTH CHECK SERVER — necesario para Cloud Run
# ══════════════════════════════════════════════════════════════

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass  # silenciar logs del servidor http

def iniciar_servidor_health():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[Health] Servidor HTTP en puerto {port}", flush=True)

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN  (variables de entorno en Cloud Run)
# ══════════════════════════════════════════════════════════════

RUTA_PKL        = os.environ.get("RUTA_PKL", "modelo.pkl")
PCTIL           = int(os.environ.get("PCTIL", "70"))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# ══════════════════════════════════════════════════════════════
# CONFIG FIJA — NO TOCAR
# ══════════════════════════════════════════════════════════════

ACTIVO       = "^DJI"
WINDOW_PCT   = 252
MERCADO_TZ   = pytz.timezone("America/New_York")
CHECK_MINS   = 60
DIAS_ENTRADA = {0, 1, 2}
NOMBRES_DIA  = {0:"Lunes", 1:"Martes", 2:"Miércoles", 3:"Jueves", 4:"Viernes"}

MACRO_TICKERS = [
    "DX-Y.NYB", "CL=F",   "HG=F",  "XLU",  "RSP",
    "^VVIX",    "SMH",    "HYG",   "GC=F",  "SPHB",
    "SPLV",     "TLT",    "IEF",   "LQD",   "^VIX",
    "^VIX3M",   "XLK",   "XLF",   "XLI",   "XLP",
    "XLV",      "^IRX",  "^TNX",  "^DJT",  "XLB",
    "XLE",      "XLY",   "XLRE",  "KRE",   "JNK",
    "SHY",      "BIL",   "IAU",   "USO",   "^GSPC",
    "QQQ",      "IWM",   "EEM",   "UUP",
]

COLS_FIN = [
    "bb_squeeze_pct",
    "sma50_vs_200_pct",
    "bb20_width_pct",
    "bb50_width_pct",
    "bb30_width_pct",
    "curve_2_10_pct",
    "yield_3m_pct",
    "yield_10y_pct",
    "hy_ig_spread_pct",
    "uup_pct",
    "h6_flow_pct",
    "vol_accel_pct",
    "vol_ratio_pct",
    "vol_60_pct",
    "rsi28_pct",
    "rates_x_vol",
    "bond_eq_corr_pct",
    "xlf_vs_xlu_pct",
    "vix_ts_pct",
    "mom_60_pct",
]

DATA_DIR       = os.environ.get("DATA_DIR", "/data")
LOG_FILE       = f"{DATA_DIR}/cinax_dj.log"
SEÑALES_CSV    = f"{DATA_DIR}/cinax_dj_señales.csv"
POSICIONES_CSV = f"{DATA_DIR}/cinax_dj_posiciones.csv"
INTRA_CSV      = f"{DATA_DIR}/cinax_dj_intra.csv"

os.makedirs(DATA_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════════
# LOG
# ══════════════════════════════════════════════════════════════

def log(msg, nivel="INFO"):
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sym = {"INFO":"·", "SEÑAL":"★", "WARN":"!", "ERR":"✗", "OK":"✓"}.get(nivel, "·")
    txt = f"[{ts}] {sym} {msg}"
    print(txt, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(txt + "\n")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════
# DISCORD
# ══════════════════════════════════════════════════════════════

def discord(mensaje):
    if not DISCORD_WEBHOOK:
        log("Discord no configurado — mensaje omitido.", "WARN")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": mensaje}, timeout=10)
        if r.status_code not in (200, 204):
            log(f"Discord status inesperado: {r.status_code}", "WARN")
    except Exception as e:
        log(f"Discord error: {e}", "WARN")


def discord_resumen_diario(fecha_barra, precio, prob, umbral, señal, cerradas_hoy=None):
    hoy    = fecha_barra.strftime("%Y-%m-%d")
    dia    = NOMBRES_DIA.get(fecha_barra.weekday(), "")
    dj_fmt = f"{precio:,.1f}"

    if señal:
        viernes = next_friday(fecha_barra).strftime("%Y-%m-%d")
        header  = f"🟢 **CINAX DJ v3 — SEÑAL ACTIVA** | {hoy} ({dia})"
        detalle = (f"```\n"
                   f"Dow Jones Close : {dj_fmt}\n"
                   f"Probabilidad    : {prob:.4f}  (umbral p{PCTIL}: {umbral:.4f})\n"
                   f"Entrada         : HOY al CLOSE\n"
                   f"Exit esperado   : {viernes} (viernes al CLOSE)\n"
                   f"```")
    else:
        header  = f"⚪ **CINAX DJ v3 — Sin señal** | {hoy} ({dia})"
        detalle = (f"```\n"
                   f"Dow Jones Close : {dj_fmt}\n"
                   f"Probabilidad    : {prob:.4f}  (umbral p{PCTIL}: {umbral:.4f})\n"
                   f"```")

    cierre_txt  = _bloque_cerradas(cerradas_hoy)
    resumen_txt = _bloque_acumulado()
    discord(f"{header}\n{detalle}{cierre_txt}{resumen_txt}")


def discord_seguimiento_posicion(fecha_barra, precio_actual, cerradas_hoy=None):
    if not os.path.exists(POSICIONES_CSV):
        return

    df_pos   = pd.read_csv(POSICIONES_CSV)
    abiertas = df_pos[df_pos["estado"] == "ABIERTA"]
    cierre_txt = _bloque_cerradas(cerradas_hoy)

    if abiertas.empty and not cierre_txt:
        return

    hoy    = fecha_barra.strftime("%Y-%m-%d")
    dia    = NOMBRES_DIA.get(fecha_barra.weekday(), "")
    header = f"📊 **CINAX DJ v3 — Seguimiento** | {hoy} ({dia})"

    pos_txt = ""
    if not abiertas.empty:
        lineas = []
        for _, p in abiertas.iterrows():
            entry_price = float(p["entry_price"])
            ret_actual  = precio_actual / entry_price - 1
            exit_esp    = p["exit_date_esperado"]
            emoji = "🟢" if ret_actual >= 0 else "🔴"
            lineas.append(
                f"{emoji}  entry {p['entry_date']} @ {entry_price:,.1f}"
                f"  →  ahora {precio_actual:,.1f}"
                f"  ret {ret_actual*100:+.2f}%"
                f"  | exit {exit_esp}"
            )
        pos_txt = "\n**Posiciones abiertas:**\n```\n" + "\n".join(lineas) + "\n```"

    resumen_txt = _bloque_acumulado()
    discord(f"{header}{pos_txt}{cierre_txt}{resumen_txt}")


def _bloque_cerradas(cerradas_hoy):
    if cerradas_hoy is None or len(cerradas_hoy) == 0:
        return ""
    lineas = []
    for _, p in cerradas_hoy.iterrows():
        ret   = float(p["retorno"])
        emoji = "✅" if ret > 0 else "❌"
        lineas.append(
            f"{emoji}  entry {p['entry_date']}  →  exit HOY   ret {ret*100:+.2f}%"
        )
    return "\n**Posiciones cerradas hoy:**\n```\n" + "\n".join(lineas) + "\n```"


def _bloque_acumulado():
    if not os.path.exists(POSICIONES_CSV):
        return ""
    df_all   = pd.read_csv(POSICIONES_CSV)
    cerradas = df_all[df_all["estado"] == "CERRADA"]
    abiertas = df_all[df_all["estado"] == "ABIERTA"]
    if len(cerradas) == 0:
        return ""
    rets = cerradas["retorno"].astype(float)
    wr   = (rets > 0).mean()
    pf   = rets[rets > 0].sum() / (abs(rets[rets < 0].sum()) + 1e-8)
    acum = (1 + rets).prod() - 1
    return (f"\n**Acumulado ({len(cerradas)} trades cerrados):**\n"
            f"```\n"
            f"Win Rate       : {wr:.1%}\n"
            f"Profit Factor  : {pf:.2f}\n"
            f"Retorno acum ∏ : {acum*100:+.1f}%\n"
            f"Abiertas ahora : {len(abiertas)}\n"
            f"```")

# ══════════════════════════════════════════════════════════════
# HORARIO
# ══════════════════════════════════════════════════════════════

def next_friday(date):
    days_ahead = 4 - date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return date + pd.Timedelta(days=days_ahead)

def mercado_cerrado_hoy():
    ahora = datetime.now(MERCADO_TZ)
    if ahora.weekday() >= 5:
        return False
    return ahora >= ahora.replace(hour=16, minute=5, second=0, microsecond=0)

def segundos_hasta_cierre():
    ahora = datetime.now(MERCADO_TZ)
    ci    = ahora.replace(hour=16, minute=5, second=0, microsecond=0)
    if ahora.weekday() < 5 and ahora < ci:
        return (ci - ahora).total_seconds()
    dias_extra = 1
    while True:
        prox = ahora + pd.Timedelta(days=dias_extra)
        if prox.weekday() < 5:
            return (
                prox.replace(hour=16, minute=5, second=0, microsecond=0) - ahora
            ).total_seconds()
        dias_extra += 1

# ══════════════════════════════════════════════════════════════
# DESCARGA DE DATOS
# ══════════════════════════════════════════════════════════════

def descargar_datos():
    log("Descargando datos históricos (desde 2000 para warmup)...")
    df_dj = yf.download(ACTIVO, start="2000-01-01", interval="1d",
                        auto_adjust=False, progress=False)
    if isinstance(df_dj.columns, pd.MultiIndex):
        df_dj.columns = df_dj.columns.droplevel(1)
    df_dj.columns = [c.lower() for c in df_dj.columns]

    log(f"Descargando {len(MACRO_TICKERS)} tickers macro...")
    df_macro = yf.download(MACRO_TICKERS, start="2000-01-01",
                           interval="1d", progress=False)
    if isinstance(df_macro.columns, pd.MultiIndex):
        df_macro = df_macro["Close"]

    df_macro = df_macro.rename(columns={
        "DX-Y.NYB":"dxy",  "CL=F":"oil",   "HG=F":"copper", "XLU":"xlu",
        "RSP":"rsp",       "^VVIX":"vvix", "SMH":"smh",     "HYG":"hyg",
        "GC=F":"gold",     "SPHB":"sphb",  "SPLV":"splv",   "TLT":"tlt",
        "IEF":"ief",       "LQD":"lqd",    "^VIX":"vix",    "^VIX3M":"vix3m",
        "XLK":"xlk",       "XLF":"xlf",    "XLI":"xli",     "XLP":"xlp",
        "XLV":"xlv",       "^IRX":"irx",   "^TNX":"tnx",    "^DJT":"djt",
        "XLB":"xlb",       "XLE":"xle",    "XLY":"xly",     "XLRE":"xlre",
        "KRE":"kre",       "JNK":"jnk",    "SHY":"shy",     "BIL":"bil",
        "IAU":"iau",       "USO":"uso",    "^GSPC":"spx",   "QQQ":"qqq",
        "IWM":"iwm",       "EEM":"eem",    "UUP":"uup",
    })

    df_raw = df_dj.join(df_macro, how="left").ffill()
    df_raw.fillna(df_raw.median(numeric_only=True), inplace=True)
    df_raw.dropna(subset=["close", "open", "high", "low"], inplace=True)
    log(f"✓ {len(df_raw)} barras cargadas")
    return df_raw

# ══════════════════════════════════════════════════════════════
# FEATURES
# ══════════════════════════════════════════════════════════════

def P(series, w=WINDOW_PCT):
    return series.rolling(w, min_periods=60).rank(pct=True)

def rsi_calc(s, n):
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def build_features(d):
    d  = d.copy()
    r1 = d["close"].pct_change(1)

    vols = {n: r1.rolling(n).std() for n in [5, 10, 20, 30, 60]}
    for n, v in vols.items():
        d[f"vol_{n}_pct"] = P(v)
    d["vol_ratio_pct"] = P(vols[10] / (vols[30] + 1e-8))
    d["vol_accel_pct"] = P(vols[5].diff(3))
    d["vol_60_pct"]    = P(vols[60])

    rsis = {n: rsi_calc(d["close"], n) for n in [7, 14, 21, 28]}
    for n, v in rsis.items():
        d[f"rsi{n}"]     = v
        d[f"rsi{n}_pct"] = P(v)
    d["rsi14_slope_pct"] = P(rsis[14].diff(3))

    for n in [20, 30, 50]:
        mid = d["close"].rolling(n).mean()
        std = d["close"].rolling(n).std()
        d[f"bb{n}_pos_pct"]   = P((d["close"] - (mid - 2*std)) / (4*std + 1e-8))
        d[f"bb{n}_width_pct"] = P(4 * std / (mid + 1e-8))
    d["bb_squeeze_pct"] = P(
        (4 * d["close"].rolling(20).std() / (d["close"].rolling(20).mean() + 1e-8)) /
        (4 * d["close"].rolling(50).std() / (d["close"].rolling(50).mean() + 1e-8) + 1e-8)
    )

    smas = {n: d["close"].rolling(n).mean() for n in [20, 50, 100, 200]}
    for n, v in smas.items():
        d[f"sma{n}"] = v
    d["sma20_vs_200_pct"] = P(smas[20] / smas[200] - 1)
    d["sma50_vs_200_pct"] = P(smas[50] / smas[200] - 1)
    d["dist_sma50_pct"]   = P((d["close"] - smas[50]) / smas[50])
    d["pos_52w"]          = P(d["close"])
    d["golden_cross"]     = (smas[50] > smas[200]).astype(float)

    for n in [5, 10, 20, 60]:
        d[f"mom_{n}_pct"] = P(d["close"].pct_change(n))
    d["mom_60_pct"] = P(d["close"].pct_change(60))

    d["vix_pct"]        = P(d["vix"])
    d["vvix_pct"]       = P(d["vvix"])
    d["vix_zscore_pct"] = P((d["vix"] - d["vix"].rolling(60).mean()) /
                             (d["vix"].rolling(60).std() + 1e-8))
    d["vix_ts_pct"]     = P(d["vix"] / (d["vix3m"] + 1e-8))
    d["vix_spread_pct"] = P(d["vix"] - d["vix3m"])
    d["vix_spike_pct"]  = P(d["vix"].pct_change(3))

    d["yield_3m_pct"]    = P(d["irx"] / 100)
    d["yield_10y_pct"]   = P(d["tnx"] / 100)
    d["curve_2_10_pct"]  = P(d["tnx"] / 100 - d["irx"] / 100)
    d["curve_slope_pct"] = P((d["tnx"] / 100 - d["irx"] / 100).diff(5))
    d["real_yield_pct"]  = P(d["tnx"] / 100 - d["vix"] / 100)
    d["tlt_mom_pct"]     = P(d["tlt"].pct_change(20))
    d["liq_tlt_pct"]     = P(d["tlt"].pct_change(3))
    d["bil_shy_pct"]     = P(d["bil"].pct_change(5) - d["shy"].pct_change(5))

    d["credit_stress_pct"] = P(d["hyg"].pct_change(5))
    d["jnk_pct"]           = P(d["jnk"].pct_change(5))
    d["hy_ig_spread_pct"]  = P(d["jnk"] / (d["lqd"] + 1e-8))
    d["ig_spread_pct"]     = P(d["lqd"].pct_change(5) - d["tlt"].pct_change(5))

    d["dxy_pct"]          = P(d["dxy"].pct_change(3))
    d["uup_pct"]          = P(d["uup"].pct_change(5))
    d["oil_pct"]          = P(d["oil"].pct_change(5))
    d["gold_pct"]         = P(d["gold"].pct_change(10))
    d["copper_pct"]       = P(d["copper"].pct_change(10))
    d["macro_growth_pct"] = P((d["copper"] / d["gold"]).pct_change(5))

    d["breadth_div_pct"] = P(d["close"].pct_change(5) - d["rsp"].pct_change(5))
    d["semi_lead_pct"]   = P(d["smh"].pct_change(5) - d["close"].pct_change(5))
    d["risk_flow_pct"]   = P((d["sphb"] / d["splv"]).pct_change(5))
    ratio = d["sphb"] / (d["splv"] + 1e-8)
    d["h6_flow_pct"]     = P(ratio.rolling(5).mean() /
                              (ratio.rolling(20).mean() + 1e-8) - 1)
    d["iwm_risk_pct"]    = P(d["iwm"].pct_change(5))
    d["eem_risk_pct"]    = P(d["eem"].pct_change(5))

    d["xlf_vs_xlu_pct"]  = P(d["xlf"].pct_change(5) - d["xlu"].pct_change(5))
    d["xlf_mom_pct"]     = P(d["xlf"].pct_change(10))
    d["kre_lead_pct"]    = P(d["kre"].pct_change(3) - d["close"].pct_change(3))
    d["xli_lead_pct"]    = P(d["xli"].pct_change(5) - d["close"].pct_change(5))
    d["xle_lead_pct"]    = P(d["xle"].pct_change(5) - d["close"].pct_change(5))
    d["xlb_lead_pct"]    = P(d["xlb"].pct_change(5) - d["close"].pct_change(5))
    d["xlre_stress_pct"] = P(d["xlre"].pct_change(5) - d["close"].pct_change(5))
    d["tech_staples_pct"]= P(d["xlk"].pct_change(5) - d["xlp"].pct_change(5))
    d["xly_xlp_pct"]     = P(d["xly"].pct_change(5) - d["xlp"].pct_change(5))

    d["djt_lead_pct"]    = P(d["djt"].pct_change(5) - d["close"].pct_change(5))
    d["djt_mom_pct"]     = P(d["djt"].pct_change(10))
    d["djt_confirm_pct"] = P(d["djt"].pct_change(5) * d["close"].pct_change(5))

    d["vs_spx_pct"]   = P(d["close"].pct_change(5) - d["spx"].pct_change(5))
    d["vs_qqq_pct"]   = P(d["close"].pct_change(5) - d["qqq"].pct_change(5))
    d["qqq_lead_pct"] = P(d["qqq"].pct_change(3) - d["close"].pct_change(3))

    d["bond_eq_corr_pct"]    = P(r1.rolling(10).corr(d["tlt"].pct_change(1)))
    d["bond_eq_corr_20_pct"] = P(r1.rolling(20).corr(d["tlt"].pct_change(1)))
    d["bond_eq_corr_60_pct"] = P(r1.rolling(60).corr(d["tlt"].pct_change(1)))
    d["crisis_signal"]       = (r1.rolling(10).corr(d["tlt"].pct_change(1)) > 0.2).astype(float)

    d["gap_pct"]       = P((d["open"] - d["close"].shift(1)) / d["close"].shift(1))
    d["range_pct"]     = P((d["high"] - d["low"]) / d["close"])
    d["close_pos_pct"] = P((d["close"] - d["low"]) / (d["high"] - d["low"] + 1e-8))

    d["vol_regime_pct"]   = P(vols[30])
    d["trend_regime_pct"] = P(smas[20] / smas[200] - 1)
    d["vix_regime_pct"]   = P(d["vix"])
    vol_bin    = (d["vol_regime_pct"] > 0.5).astype(int)
    trend_bin  = (d["trend_regime_pct"] > 0.5).astype(int)
    crisis_bin = d["crisis_signal"].astype(int)
    d["vol_regime"]    = vol_bin
    d["trend_regime"]  = trend_bin
    d["crisis_regime"] = crisis_bin
    d["regime_quad"]   = vol_bin * 2 + trend_bin
    d["regime_3d"]     = vol_bin * 4 + trend_bin * 2 + crisis_bin

    d["rates_x_vol"]     = d["curve_2_10_pct"] * d["vol_regime_pct"]
    d["bb_x_vol"]        = d["bb20_width_pct"] * d["vol_regime_pct"]
    d["rsi14_x_vol"]     = d["rsi14_pct"] * d["vol_regime_pct"]
    d["mom10_x_trend"]   = d["mom_10_pct"] * d["trend_regime_pct"]
    d["vix_ts_x_vol"]    = d["vix_ts_pct"] * d["vol_regime_pct"]
    d["credit_x_vol"]    = d["credit_stress_pct"] * d["vol_regime_pct"]
    d["credit_x_crisis"] = d["credit_stress_pct"] * crisis_bin
    d["hy_x_vol"]        = d["hy_ig_spread_pct"] * d["vol_regime_pct"]
    d["breadth_x_trend"] = d["breadth_div_pct"] * d["trend_regime_pct"]
    d["macro_x_trend"]   = d["macro_growth_pct"] * d["trend_regime_pct"]
    d["xlf_x_crisis"]    = d["xlf_mom_pct"] * (1 - crisis_bin)
    d["djt_x_trend"]     = d["djt_lead_pct"] * d["trend_regime_pct"]
    d["bond_x_crisis"]   = d["bond_eq_corr_pct"] * crisis_bin
    d["gold_x_crisis"]   = d["gold_pct"] * crisis_bin
    d["vix_x_crisis"]    = d["vix_pct"] * crisis_bin
    d["semi_x_trend"]    = d["semi_lead_pct"] * d["trend_regime_pct"]
    d["dj_spx_x_trend"]  = d["vs_spx_pct"] * d["trend_regime_pct"]

    return d.dropna()

# ══════════════════════════════════════════════════════════════
# PREDICCIÓN
# ══════════════════════════════════════════════════════════════

def predecir(modelo, df_feat):
    fila   = df_feat.iloc[-1]
    fecha  = df_feat.index[-1]
    precio = float(fila["close"])
    X      = fila[COLS_FIN].fillna(0.5).values.reshape(1, -1)
    prob   = float(modelo.predict_proba(X)[:, 1][0])
    return prob, fecha, precio


def calcular_umbral_rolling(modelo, df_feat):
    X_hist  = df_feat[COLS_FIN].fillna(0.5).values
    probs_h = modelo.predict_proba(X_hist)[:, 1]
    s       = pd.Series(probs_h, index=df_feat.index)
    umbral  = float(
        s.expanding(min_periods=252)
         .quantile(PCTIL / 100)
         .shift(1)
         .iloc[-1]
    )
    return umbral

# ══════════════════════════════════════════════════════════════
# REGISTROS CSV
# ══════════════════════════════════════════════════════════════

def guardar_señal(fecha_barra, precio, prob, umbral, señal):
    nuevo = not os.path.exists(SEÑALES_CSV)
    with open(SEÑALES_CSV, "a", encoding="utf-8") as f:
        if nuevo:
            f.write("timestamp,fecha_barra,dia_semana,precio,prob,umbral,señal\n")
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dia = NOMBRES_DIA.get(fecha_barra.weekday(), "")
        f.write(f"{ts},{fecha_barra.date()},{dia},"
                f"{precio:.2f},{prob:.4f},{umbral:.4f},{int(señal)}\n")


def abrir_posicion(fecha_barra, precio_entrada, prob, umbral):
    exit_date = next_friday(fecha_barra)
    nuevo = not os.path.exists(POSICIONES_CSV)
    with open(POSICIONES_CSV, "a", encoding="utf-8") as f:
        if nuevo:
            f.write("entry_date,dia_semana,entry_price,exit_date_esperado,"
                    "exit_price,retorno,prob,umbral,estado\n")
        dia = NOMBRES_DIA.get(fecha_barra.weekday(), "")
        f.write(f"{fecha_barra.date()},{dia},{precio_entrada:.2f},"
                f"{exit_date.date()},,,{prob:.4f},{umbral:.4f},ABIERTA\n")
    log(f"Posición abierta | entry={fecha_barra.date()} | precio={precio_entrada:,.1f} | "
        f"exit={exit_date.date()} | prob={prob:.4f}", "SEÑAL")
    return exit_date


def registrar_barra_intra(df_raw, fecha_barra):
    if not os.path.exists(POSICIONES_CSV):
        return
    df_pos   = pd.read_csv(POSICIONES_CSV, parse_dates=["entry_date", "exit_date_esperado"])
    abiertas = df_pos[df_pos["estado"] == "ABIERTA"]
    if abiertas.empty:
        return

    hoy = fecha_barra.date()
    if hoy not in df_raw.index.date:
        return

    barra  = df_raw[df_raw.index.date == hoy].iloc[-1]
    open_  = float(barra["open"])
    high_  = float(barra["high"])
    low_   = float(barra["low"])
    close_ = float(barra["close"])

    df_intra = (pd.read_csv(INTRA_CSV) if os.path.exists(INTRA_CSV)
                else pd.DataFrame(columns=["entry_date", "fecha_barra", "dia_semana",
                                            "open", "high", "low", "close",
                                            "ret_vs_entry", "ret_diario"]))

    nuevas = []
    for _, pos in abiertas.iterrows():
        entry_date = pos["entry_date"].date()
        exit_date  = pos["exit_date_esperado"].date()
        if hoy <= entry_date or hoy > exit_date:
            continue
        ya_existe = (
            (df_intra["entry_date"].astype(str) == str(entry_date)) &
            (df_intra["fecha_barra"].astype(str) == str(hoy))
        ).any() if len(df_intra) > 0 else False
        if ya_existe:
            continue

        ret_vs_entry = close_ / float(pos["entry_price"]) - 1
        dia_str      = NOMBRES_DIA.get(fecha_barra.weekday(), "")
        nuevas.append({
            "entry_date":   str(entry_date),
            "fecha_barra":  str(hoy),
            "dia_semana":   dia_str,
            "open":  round(open_, 2),  "high": round(high_, 2),
            "low":   round(low_, 2),   "close": round(close_, 2),
            "ret_vs_entry": round(ret_vs_entry, 6),
            "ret_diario":   round(close_ / open_ - 1, 6),
        })
        log(f"Intra | pos={entry_date} barra={hoy} C={close_:,.1f} ret={ret_vs_entry:+.2%}")

    if nuevas:
        df_new   = pd.DataFrame(nuevas)
        df_intra = pd.concat([df_intra, df_new], ignore_index=True)
        df_intra.to_csv(INTRA_CSV, index=False)


def cerrar_posiciones_vencidas(df_feat, df_raw):
    if not os.path.exists(POSICIONES_CSV):
        return pd.DataFrame()
    df_pos   = pd.read_csv(POSICIONES_CSV, parse_dates=["entry_date", "exit_date_esperado"])
    abiertas = df_pos[df_pos["estado"] == "ABIERTA"]
    if abiertas.empty:
        return pd.DataFrame()

    hoy          = df_feat.index[-1].date()
    cerradas_hoy = []

    for idx_pos, pos in abiertas.iterrows():
        exit_esp = pos["exit_date_esperado"].date()
        if hoy >= exit_esp:
            fechas_post = df_feat.index.date[df_feat.index.date >= exit_esp]
            if len(fechas_post) == 0:
                continue
            fecha_real    = fechas_post[0]
            precio_cierre = float(df_feat[df_feat.index.date == fecha_real]["close"].iloc[-1])
            retorno       = precio_cierre / float(pos["entry_price"]) - 1

            df_pos.at[idx_pos, "exit_price"] = round(precio_cierre, 2)
            df_pos.at[idx_pos, "retorno"]    = round(retorno, 6)
            df_pos.at[idx_pos, "estado"]     = "CERRADA"
            cerradas_hoy.append(df_pos.loc[idx_pos].to_dict())

            emoji = "GANADORA ✓" if retorno > 0 else "PERDEDORA ✗"
            log(f"Cerrada | entry={pos['entry_date'].date()} exit={fecha_real} "
                f"ret={retorno:+.2%} {emoji}", "OK")

    df_pos.to_csv(POSICIONES_CSV, index=False)
    return pd.DataFrame(cerradas_hoy) if cerradas_hoy else pd.DataFrame()


def resumen_log():
    if not os.path.exists(POSICIONES_CSV):
        return
    df_pos   = pd.read_csv(POSICIONES_CSV)
    cerradas = df_pos[df_pos["estado"] == "CERRADA"]
    abiertas = df_pos[df_pos["estado"] == "ABIERTA"]
    if cerradas.empty:
        log(f"Sin posiciones cerradas aún | Abiertas: {len(abiertas)}")
        return
    rets = cerradas["retorno"].astype(float)
    wr   = (rets > 0).mean()
    pf   = rets[rets > 0].sum() / (abs(rets[rets < 0].sum()) + 1e-8)
    acum = (1 + rets).prod() - 1
    log(f"RESUMEN | Cerradas: {len(cerradas)} | Abiertas: {len(abiertas)} | "
        f"WR: {wr:.1%} | PF: {pf:.2f} | Acum∏: {acum*100:+.1f}%")

# ══════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main():
    # ← ÚNICA LÍNEA NUEVA: arrancar el servidor HTTP antes de todo
    iniciar_servidor_health()

    log("=" * 60)
    log("CINAX DJ v3 — Producción Diaria (Cloud Run)")
    log(f"Activo  : Dow Jones (^DJI)")
    log(f"Config  : percentil={PCTIL} | entrada=Lun/Mar/Mié | exit=viernes CLOSE")
    log(f"Modelo  : {RUTA_PKL}")
    log(f"Datos   : {DATA_DIR}")
    log(f"Discord : {'configurado ✓' if DISCORD_WEBHOOK else 'NO configurado ✗'}")
    log("=" * 60)

    if not os.path.exists(RUTA_PKL):
        log(f"ERROR: No se encontró el modelo en: {RUTA_PKL}", "ERR")
        discord(f"❌ **CINAX DJ v3** — Error al arrancar: modelo no encontrado en `{RUTA_PKL}`")
        return

    with open(RUTA_PKL, "rb") as f:
        modelo = pickle.load(f)
    log(f"Modelo cargado ✓", "OK")
    discord(f"🚀 **CINAX DJ v3** arrancado correctamente en Cloud Run | p{PCTIL} | ^DJI")

    ultima_fecha_evaluada = None

    while True:
        try:
            ahora_et   = datetime.now(MERCADO_TZ)
            dia_semana = ahora_et.weekday()

            if dia_semana >= 5:
                secs = segundos_hasta_cierre()
                log(f"Fin de semana. Próxima evaluación en {secs/3600:.1f}h", "WARN")
                time.sleep(secs + 60)
                continue

            if not mercado_cerrado_hoy():
                secs = segundos_hasta_cierre()
                log(f"Esperando cierre del mercado en {secs/60:.0f} min...")
                time.sleep(min(secs, CHECK_MINS * 60))
                continue

            log("Descargando datos y calculando features...")
            df_raw  = descargar_datos()
            df_feat = build_features(df_raw)

            if df_feat.empty:
                log("DataFrame vacío — reintentando en 5 min.", "WARN")
                time.sleep(300)
                continue

            prob, fecha_barra, precio = predecir(modelo, df_feat)
            umbral = calcular_umbral_rolling(modelo, df_feat)

            registrar_barra_intra(df_raw, fecha_barra)

            if fecha_barra == ultima_fecha_evaluada:
                secs = segundos_hasta_cierre()
                log(f"Barra {fecha_barra.date()} ya evaluada. Próxima en {secs/3600:.1f}h")
                time.sleep(CHECK_MINS * 60)
                continue

            cerradas_hoy = cerrar_posiciones_vencidas(df_feat, df_raw)

            if dia_semana in DIAS_ENTRADA:
                señal = prob >= umbral
                info  = (f"{fecha_barra.date()} ({NOMBRES_DIA.get(dia_semana, '')}) | "
                         f"^DJI: {precio:,.1f} | Prob: {prob:.4f} | Umbral: {umbral:.4f}")

                if señal:
                    log(f"★ SEÑAL LARGA ★ — {info}", "SEÑAL")
                    log(f"  → Exit: {next_friday(fecha_barra).date()} (viernes al CLOSE)", "SEÑAL")
                    abrir_posicion(fecha_barra, precio, prob, umbral)
                else:
                    log(f"Sin señal — {info}")

                guardar_señal(fecha_barra, precio, prob, umbral, señal)
                resumen_log()
                discord_resumen_diario(
                    fecha_barra, precio, prob, umbral, señal,
                    cerradas_hoy if len(cerradas_hoy) > 0 else None
                )

            elif dia_semana == 3:
                resumen_log()
                discord_seguimiento_posicion(
                    fecha_barra, precio,
                    cerradas_hoy if len(cerradas_hoy) > 0 else None
                )

            elif dia_semana == 4:
                resumen_log()
                if len(cerradas_hoy) > 0:
                    discord_seguimiento_posicion(fecha_barra, precio, cerradas_hoy)

            ultima_fecha_evaluada = fecha_barra
            secs = segundos_hasta_cierre()
            log(f"Ciclo completo. Próxima evaluación en {secs/3600:.1f}h")
            time.sleep(secs + 120)

        except KeyboardInterrupt:
            log("Detenido por usuario.", "WARN")
            resumen_log()
            break
        except Exception as e:
            import traceback
            log(f"Error: {e}", "ERR")
            log(traceback.format_exc(), "ERR")
            time.sleep(60)


if __name__ == "__main__":
    main()
