"""
dashboard.py - Local web frontend UI for the AstroTick Kalshi BTC trading bot.

Reads dashboard_state.json (written by bot.py each cycle) and trades.csv
(written by risk_manager.py) and renders a single-page UI with live updates.

Routes:
    GET  /                  HTML single-page app (auto-refreshes via JS)
    GET  /api/state         JSON snapshot of the latest bot cycle state
    GET  /api/trades        JSON of recent trade history (most recent first)
    GET  /api/health        liveness + freshness of state file

Usage:
    python dashboard.py
    # Then open http://127.0.0.1:8000

Notes:
    - The dashboard never writes to bot files; it only reads.
    - Errors reading state/trade files never crash the page; they surface
      as warnings in the UI and structured JSON errors on the API.
"""
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from flask import Flask, jsonify, render_template_string

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "dashboard_state.json"
TRADE_LOG_FILE = Path(os.getenv("TRADE_LOG_FILE", str(ROOT / "trades.csv")))

# How many trade log rows to expose via /api/trades (newest first).
TRADE_HISTORY_LIMIT = int(os.getenv("DASHBOARD_TRADE_LIMIT", "50"))

# Browser polling interval (ms) - how often the UI fetches /api/state.
REFRESH_MS = int(os.getenv("DASHBOARD_REFRESH_MS", "3000"))

# Bind host/port - kept on loopback by default for safety.
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

log = logging.getLogger(__name__)
app = Flask(__name__)


# ─── Data loaders ────────────────────────────────────────────────────────────


def _load_state() -> tuple[dict | None, str | None, float | None]:
    """Return (state_dict, error_message, file_mtime). All may be None."""
    try:
        text = STATE_FILE.read_text(encoding="utf-8")
        mtime = STATE_FILE.stat().st_mtime
        return json.loads(text), None, mtime
    except FileNotFoundError:
        return None, None, None
    except Exception as exc:
        log.warning("Error reading %s: %s", STATE_FILE.name, exc)
        return None, f"Error reading dashboard_state.json: {exc}", None


def _load_trades(limit: int = TRADE_HISTORY_LIMIT) -> tuple[list[dict], str | None]:
    """Return (rows, error). Rows are newest-first, parsed from trades.csv."""
    if not TRADE_LOG_FILE.exists():
        return [], None
    try:
        with TRADE_LOG_FILE.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as exc:
        log.warning("Error reading %s: %s", TRADE_LOG_FILE.name, exc)
        return [], f"Error reading {TRADE_LOG_FILE.name}: {exc}"

    for r in rows:
        for key in ("size", "entry_price", "exit_price", "pnl"):
            if key in r and r[key] not in (None, ""):
                try:
                    r[key] = float(r[key]) if "." in r[key] else int(r[key])
                except (ValueError, TypeError):
                    pass

    rows.reverse()
    return rows[:limit], None


def _trade_summary(rows: list[dict]) -> dict:
    """Aggregate stats over the supplied rows for the summary cards."""
    total = len(rows)
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": None, "total_pnl": 0}
    wins = sum(1 for r in rows if isinstance(r.get("pnl"), (int, float)) and r["pnl"] > 0)
    losses = sum(1 for r in rows if isinstance(r.get("pnl"), (int, float)) and r["pnl"] < 0)
    pnl = sum(r["pnl"] for r in rows if isinstance(r.get("pnl"), (int, float)))
    decisive = wins + losses
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / decisive) if decisive else None,
        "total_pnl": pnl,
    }


# ─── HTML / Frontend ─────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AstroTick - Kalshi BTC Bot</title>
  <style>
    :root {
      --bg: #0b0d17;
      --panel: #131829;
      --panel-2: #1a2038;
      --border: #232a44;
      --muted: #6b7592;
      --text: #e6e9f4;
      --accent: #00d4aa;
      --accent-dim: #008f72;
      --warn: #ffb454;
      --neg: #ff6b6b;
      --pos: #00d4aa;
      --link: #6ea8ff;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   "Helvetica Neue", Arial, sans-serif;
      background: radial-gradient(1200px 600px at 20% -10%, #1b2240 0%, var(--bg) 60%);
      color: var(--text);
      min-height: 100vh;
    }
    header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 20px 32px; border-bottom: 1px solid var(--border);
      backdrop-filter: blur(8px);
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .logo {
      width: 36px; height: 36px; border-radius: 8px;
      background: linear-gradient(135deg, var(--accent), #6ea8ff);
      display: grid; place-items: center; color: #0b0d17; font-weight: 800;
    }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0.3px; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .status { display: flex; align-items: center; gap: 8px; font-size: 13px; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
    .dot.live { background: var(--accent); box-shadow: 0 0 8px var(--accent); animation: pulse 2s infinite; }
    .dot.stale { background: var(--warn); }
    .dot.down { background: var(--neg); }
    @keyframes pulse {
      0% { opacity: 1; } 50% { opacity: 0.45; } 100% { opacity: 1; }
    }

    main {
      max-width: 1200px; margin: 0 auto; padding: 24px 32px 48px;
      display: grid; gap: 20px;
    }
    .grid { display: grid; gap: 20px; }
    .grid-3 { grid-template-columns: repeat(3, 1fr); }
    .grid-2 { grid-template-columns: 2fr 1fr; }
    @media (max-width: 900px) {
      .grid-3, .grid-2 { grid-template-columns: 1fr; }
      header { padding: 14px 18px; }
      main { padding: 16px 18px 32px; }
    }

    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
    }
    .card h2 {
      margin: 0 0 12px; font-size: 12px; letter-spacing: 1.2px;
      color: var(--muted); text-transform: uppercase;
    }
    .kpi { font-size: 28px; font-weight: 700; letter-spacing: -0.5px; }
    .kpi.small { font-size: 20px; }
    .kpi-sub { color: var(--muted); font-size: 12px; margin-top: 4px; }

    .row { display: flex; align-items: center; justify-content: space-between;
           padding: 8px 0; border-bottom: 1px solid var(--border); }
    .row:last-child { border-bottom: 0; }
    .row .label { color: var(--muted); font-size: 13px; }
    .row .value { font-variant-numeric: tabular-nums; }

    .pos { color: var(--pos); }
    .neg { color: var(--neg); }
    .warn { color: var(--warn); }
    .muted { color: var(--muted); }
    .pill {
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      font-size: 11px; letter-spacing: 0.4px; text-transform: uppercase;
      border: 1px solid var(--border);
    }
    .pill.yes { color: var(--pos); border-color: rgba(0,212,170,0.4); }
    .pill.no  { color: var(--neg); border-color: rgba(255,107,107,0.4); }

    .meter {
      position: relative; height: 8px; border-radius: 999px;
      background: var(--panel-2); overflow: hidden; margin-top: 6px;
    }
    .meter > span {
      position: absolute; top: 0; bottom: 0; left: 50%;
      background: linear-gradient(90deg, var(--accent), #6ea8ff);
      border-radius: 999px;
    }
    .meter.signed > span.neg-fill {
      background: linear-gradient(90deg, var(--neg), #ffb454);
    }

    table.trades {
      width: 100%; border-collapse: collapse; font-size: 13px;
      font-variant-numeric: tabular-nums;
    }
    table.trades th, table.trades td {
      text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border);
    }
    table.trades th { color: var(--muted); font-weight: 500; font-size: 11px;
                      letter-spacing: 0.6px; text-transform: uppercase; }
    table.trades tbody tr:hover { background: var(--panel-2); }
    .empty { color: var(--muted); padding: 12px 0; font-size: 13px; }

    footer {
      margin-top: 16px; color: var(--muted); font-size: 12px; text-align: center;
    }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="logo">A</div>
      <div>
        <h1>AstroTick &middot; Kalshi BTC Bot</h1>
        <div class="sub">15-minute prediction markets &middot; live trading dashboard</div>
      </div>
    </div>
    <div class="status">
      <span class="dot" id="status-dot"></span>
      <span id="status-text">connecting&hellip;</span>
      <span class="muted" id="last-updated">&mdash;</span>
    </div>
  </header>

  <main>
    <section class="grid grid-3">
      <div class="card">
        <h2>Active Market</h2>
        <div class="kpi small" id="market-ticker">&mdash;</div>
        <div class="kpi-sub" id="market-mid">&mdash;</div>
      </div>
      <div class="card">
        <h2>Realized PnL Today</h2>
        <div class="kpi" id="pnl-today">&mdash;</div>
        <div class="kpi-sub" id="pnl-summary">&mdash;</div>
      </div>
      <div class="card">
        <h2>Open Position</h2>
        <div class="kpi" id="position-size">&mdash;</div>
        <div class="kpi-sub" id="position-meta">&mdash;</div>
      </div>
    </section>

    <section class="grid grid-2">
      <div class="card">
        <h2>Order Book Quotes</h2>
        <div class="row"><span class="label">YES bid / ask</span>
          <span class="value" id="yes-quote">&mdash;</span></div>
        <div class="row"><span class="label">NO bid / ask</span>
          <span class="value" id="no-quote">&mdash;</span></div>
        <div class="row"><span class="label">Mid price</span>
          <span class="value" id="mid-price">&mdash;</span></div>
        <div class="row"><span class="label">Spread</span>
          <span class="value" id="spread">&mdash;</span></div>
        <div class="row"><span class="label">Best bid sizes (Y / N)</span>
          <span class="value" id="bid-sizes">&mdash;</span></div>
      </div>

      <div class="card">
        <h2>Strategy Signal</h2>
        <div class="row"><span class="label">Composite</span>
          <span class="value" id="sig-composite">&mdash;</span></div>
        <div class="meter signed"><span id="sig-bar"></span></div>
        <div class="row" style="margin-top:8px"><span class="label">Momentum</span>
          <span class="value" id="sig-momentum">&mdash;</span></div>
        <div class="row"><span class="label">Order book skew</span>
          <span class="value" id="sig-skew">&mdash;</span></div>
        <div class="row"><span class="label">Confidence</span>
          <span class="value" id="sig-confidence">&mdash;</span></div>
      </div>
    </section>

    <section class="card">
      <h2>Recent Trades</h2>
      <div id="trades-summary" class="kpi-sub" style="margin-bottom:10px">&mdash;</div>
      <div id="trades-wrap">
        <div class="empty">No trades logged yet.</div>
      </div>
    </section>

    <footer>
      Auto-refreshes every <span id="refresh-secs">{{ refresh_secs }}</span>s &middot;
      data from <code>dashboard_state.json</code> and <code>{{ trade_log_name }}</code>.
    </footer>
  </main>

<script>
const REFRESH_MS = {{ refresh_ms }};
const fmtCents = v => (v == null ? '—' : (v.toFixed ? v.toFixed(0) : v) + '¢');
const fmt4 = v => (v == null ? '—' : Number(v).toFixed(4));
const setText = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
const setClass = (id, cls) => { const el = document.getElementById(id); if (el) el.className = cls; };

function pnlClass(v) {
  if (v == null || v === 0) return 'value';
  return 'value ' + (v > 0 ? 'pos' : 'neg');
}

function relTime(epoch) {
  if (!epoch) return '—';
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (sec < 5)  return 'just now';
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  return Math.floor(sec / 3600) + 'h ago';
}

function renderState(data) {
  const s   = data.state || {};
  const sum = data.trade_summary || {};
  const age = data.age_seconds;
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');

  if (data.error) {
    dot.className = 'dot down';
    txt.textContent = 'error reading state';
  } else if (!data.state) {
    dot.className = 'dot stale';
    txt.textContent = 'waiting for bot data';
  } else if (age != null && age > 30) {
    dot.className = 'dot stale';
    txt.textContent = 'stale (bot may be paused)';
  } else {
    dot.className = 'dot live';
    txt.textContent = 'live';
  }
  setText('last-updated', data.mtime ? relTime(data.mtime) : '');

  setText('market-ticker', s.active_market_ticker || '—');
  setText('market-mid', s.mid_price != null ? 'mid ' + fmtCents(s.mid_price) : '—');

  const pnl = s.realized_pnl_cents;
  setText('pnl-today', pnl == null ? '—' : (pnl > 0 ? '+' : '') + fmtCents(pnl));
  setClass('pnl-today', 'kpi ' + (pnl == null || pnl === 0 ? '' : (pnl > 0 ? 'pos' : 'neg')));
  if (sum.total != null) {
    const wr = sum.win_rate == null ? '—' : (sum.win_rate * 100).toFixed(0) + '%';
    setText('pnl-summary', sum.total + ' trades · ' + sum.wins + 'W/' + sum.losses + 'L · win rate ' + wr);
  }

  setText('position-size', s.position_size != null ? s.position_size : '—');
  setText('position-meta', s.position_size ? 'contracts open' : 'flat');

  setText('yes-quote', (s.yes_bid != null ? fmtCents(s.yes_bid) : '—') + ' / ' +
                       (s.yes_ask != null ? fmtCents(s.yes_ask) : '—'));
  setText('no-quote',  (s.no_bid != null ? fmtCents(s.no_bid) : '—') + ' / ' +
                       (s.no_ask != null ? fmtCents(s.no_ask) : '—'));
  setText('mid-price', fmtCents(s.mid_price));
  setText('spread', fmtCents(s.spread));
  setText('bid-sizes', (s.yes_bid_size ?? '—') + ' / ' + (s.no_bid_size ?? '—'));

  setText('sig-composite', fmt4(s.signal_composite));
  setText('sig-momentum',  fmt4(s.signal_momentum));
  setText('sig-skew',      fmt4(s.signal_skew));
  setText('sig-confidence', fmt4(s.signal_confidence));

  // Signed meter centered at 0; clamp [-1, 1].
  const bar = document.getElementById('sig-bar');
  const v = Math.max(-1, Math.min(1, Number(s.signal_composite) || 0));
  const widthPct = Math.abs(v) * 50;
  bar.style.width  = widthPct + '%';
  bar.style.left   = (v >= 0 ? 50 : 50 - widthPct) + '%';
  bar.className    = v < 0 ? 'neg-fill' : '';
}

function renderTrades(data) {
  const rows = data.rows || [];
  const sum = data.summary || {};
  const wrap = document.getElementById('trades-wrap');
  const summaryEl = document.getElementById('trades-summary');

  if (sum.total) {
    const wr = sum.win_rate == null ? '—' : (sum.win_rate * 100).toFixed(0) + '%';
    const pnl = sum.total_pnl;
    const pnlStr = pnl > 0 ? ('+' + pnl + '¢') : (pnl + '¢');
    summaryEl.innerHTML =
      sum.total + ' total · <span class="pos">' + sum.wins + 'W</span> / ' +
      '<span class="neg">' + sum.losses + 'L</span> · win rate ' + wr +
      ' · cumulative PnL <span class="' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'muted') +
      '">' + pnlStr + '</span>';
  } else {
    summaryEl.textContent = 'No trades logged yet.';
  }

  if (!rows.length) {
    wrap.innerHTML = '<div class="empty">No trades logged yet.</div>';
    return;
  }
  let html = '<table class="trades"><thead><tr>' +
    '<th>Time</th><th>Market</th><th>Side</th><th>Size</th>' +
    '<th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th>' +
    '</tr></thead><tbody>';
  for (const r of rows) {
    const sideCls = (r.side || '').toLowerCase() === 'yes' ? 'yes'
                  : (r.side || '').toLowerCase() === 'no'  ? 'no' : '';
    const pnlNum = Number(r.pnl);
    const pnlCls = isNaN(pnlNum) ? 'muted' : (pnlNum > 0 ? 'pos' : pnlNum < 0 ? 'neg' : 'muted');
    const ts = r.timestamp ? String(r.timestamp).replace('T', ' ').slice(0, 19) : '—';
    html += '<tr>' +
      '<td class="muted">' + ts + '</td>' +
      '<td>' + (r.market || '—') + '</td>' +
      '<td><span class="pill ' + sideCls + '">' + (r.side || '—') + '</span></td>' +
      '<td>' + (r.size ?? '—') + '</td>' +
      '<td>' + (r.entry_price != null ? fmtCents(r.entry_price) : '—') + '</td>' +
      '<td>' + (r.exit_price  != null ? fmtCents(r.exit_price)  : '—') + '</td>' +
      '<td class="' + pnlCls + '">' + (r.pnl != null ? (pnlNum > 0 ? '+' : '') + r.pnl + '¢' : '—') + '</td>' +
      '<td class="muted">' + (r.exit_reason || '—') + '</td>' +
    '</tr>';
  }
  html += '</tbody></table>';
  wrap.innerHTML = html;
}

async function tick() {
  try {
    const [stateRes, tradesRes] = await Promise.all([
      fetch('/api/state', { cache: 'no-store' }),
      fetch('/api/trades', { cache: 'no-store' }),
    ]);
    renderState(await stateRes.json());
    renderTrades(await tradesRes.json());
  } catch (e) {
    setClass('status-dot', 'dot down');
    setText('status-text', 'connection error');
  }
}

tick();
setInterval(tick, REFRESH_MS);
</script>
</body>
</html>
"""


# ─── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template_string(
        _HTML,
        refresh_ms=REFRESH_MS,
        refresh_secs=max(1, REFRESH_MS // 1000),
        trade_log_name=TRADE_LOG_FILE.name,
    )


@app.route("/api/state")
def api_state():
    state, error, mtime = _load_state()
    rows, _ = _load_trades(limit=TRADE_HISTORY_LIMIT)
    payload = {
        "state": state,
        "error": error,
        "mtime": mtime,
        "age_seconds": (time.time() - mtime) if mtime else None,
        "trade_summary": _trade_summary(rows),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(payload)


@app.route("/api/trades")
def api_trades():
    rows, error = _load_trades(limit=TRADE_HISTORY_LIMIT)
    return jsonify({
        "rows": rows,
        "summary": _trade_summary(rows),
        "limit": TRADE_HISTORY_LIMIT,
        "error": error,
    })


@app.route("/api/health")
def api_health():
    _, error, mtime = _load_state()
    age = (time.time() - mtime) if mtime else None
    status = "ok" if (mtime and (age is None or age < 60) and not error) else "stale"
    return jsonify({
        "status": status,
        "state_file_present": STATE_FILE.exists(),
        "state_age_seconds": age,
        "error": error,
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    log.info("AstroTick dashboard starting on http://%s:%d", HOST, PORT)
    log.info("Reading state from: %s", STATE_FILE)
    log.info("Reading trades from: %s", TRADE_LOG_FILE)
    app.run(host=HOST, port=PORT, debug=False)
