"""Metrics and the HTML report.

Two jobs, kept apart: `metrics()` is pure arithmetic over a run's equity and
order series, and `render()` turns the numbers into a page. Nothing here
decides anything about trading.

The report is written to be read by someone deciding whether to risk money on
this, which means the limitations are part of the content, not an appendix. A
backtest that shows only its equity curve is an advertisement.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# Downsampled to roughly hourly. 43,200 points per series would make the page
# enormous and the line no more informative than 720 does.
_TARGET_POINTS = 720


def _downsample(rows, target=_TARGET_POINTS):
    if len(rows) <= target:
        return rows
    stride = len(rows) / target
    picked = [rows[int(i * stride)] for i in range(target)]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return picked


def max_drawdown(equity):
    """Deepest peak-to-trough fall in equity, as a negative fraction.

    On the equity curve, not on closed trades: an open position marked against
    it is money the account could actually have lost, and it is what an
    operator watching the screen would have had to sit through."""
    peak, worst = float("-inf"), 0.0
    for point in equity:
        peak = max(peak, point["equity"])
        if peak > 0:
            worst = min(worst, (point["equity"] - peak) / peak)
    return worst


def metrics(label, equity, orders, start_balance, buy_hold_pct):
    final = equity[-1]["equity"]
    closes = [o for o in orders if o.get("realized_pnl")]
    wins = [o for o in closes if o["realized_pnl"] > 0]
    realized = sum(o.get("realized_pnl", 0.0) for o in orders)
    biggest = max((o.get("realized_pnl", 0.0) for o in orders), default=0.0)

    return {
        "label": label,
        "final_equity": final,
        "return_pct": (final / start_balance - 1) * 100,
        "excess_pct": (final / start_balance - 1) * 100 - buy_hold_pct,
        "max_drawdown_pct": max_drawdown(equity) * 100,
        "orders": len(orders),
        "closing_trades": len(closes),
        "win_rate_pct": (100 * len(wins) / len(closes)) if closes else 0.0,
        "realized": realized,
        "biggest_trade": biggest,
        # How much of the profit rode on one fill. A high number means the
        # headline return is a single event, not a repeatable process.
        "concentration_pct": (100 * biggest / realized) if realized > 0 else 0.0,
        "max_notional": max(abs(p["notional"]) for p in equity),
        "bars_in_zone_pct": 100 * sum(1 for p in equity if p["zone"] is not None) / len(equity),
    }


def _series(equity, key):
    return [[p["time"], round(p[key], 4)] for p in _downsample(equity)]


def _drawdown_series(equity):
    """Drawdown computed on the FULL series, THEN downsampled.

    Order matters and getting it backwards is a real defect: running the peak
    over hourly samples skips the troughs between them, so the chart bottomed
    out at -21% while the table beside it reported -36.2% from the complete
    series. A chart that disagrees with its own figures is worse than no
    chart. Peak-tracking happens at full resolution; only the finished curve
    is thinned for drawing."""
    peak, full = float("-inf"), []
    for point in equity:
        peak = max(peak, point["equity"])
        full.append({"time": point["time"],
                     "dd": 100 * (point["equity"] - peak) / peak})
    return [[p["time"], round(p["dd"], 4)] for p in _downsample(full)]


def build(meta, runs):
    """runs: {label: {"equity": [...], "orders": [...]}}"""
    start = meta["start_balance"]
    bh = meta["buy_and_hold_pct"]

    stats = {label: metrics(label, r["equity"], r["orders"], start, bh)
             for label, r in runs.items()}

    first_eq = next(iter(runs.values()))["equity"]
    p0 = first_eq[0]["price"]
    hold = [[p["time"], round(start * p["price"] / p0, 4)] for p in _downsample(first_eq)]

    payload = {
        "meta": meta,
        "stats": stats,
        "equity": {label: _series(r["equity"], "equity") for label, r in runs.items()},
        "hold": hold,
        "drawdown": {label: _drawdown_series(r["equity"]) for label, r in runs.items()},
        "exposure": {label: _series(r["equity"], "notional") for label, r in runs.items()},
    }
    html = _render(payload)
    (RESULTS / "report.html").write_text(html, encoding="utf-8")
    (RESULTS / "metrics.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats, RESULTS / "report.html"


def _render(payload):
    data = json.dumps(payload)
    meta = payload["meta"]
    start_d = datetime.fromtimestamp(meta["window_start"] / 1000, timezone.utc).strftime("%d %b %Y")
    end_d = datetime.fromtimestamp(meta["window_end"] / 1000, timezone.utc).strftime("%d %b %Y")
    return TEMPLATE.replace("__DATA__", data).replace("__START__", start_d).replace("__END__", end_d)


TEMPLATE = r"""<title>Zone Ladder Backtest</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --surface:#fbfbfa; --panel:#ffffff; --line:#e3e4e1; --line-soft:#eff0ed;
  --ink:#14161a; --ink-2:#565a61; --ink-3:#8a8f97;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --warn:#b4690e; --crit:#c0392f; --good:#1baf7a;
  --grid:#e8e9e6;
}
:root:not([data-theme="light"]){}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --surface:#17181a; --panel:#1e2023; --line:#2f3237; --line-soft:#25282c;
    --ink:#f2f3f5; --ink-2:#b4b8bf; --ink-3:#7d838c;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70;
    --warn:#d9a441; --crit:#e07a70; --good:#199e70;
    --grid:#2a2d31;
  }
}
:root[data-theme="dark"]{
  --surface:#17181a; --panel:#1e2023; --line:#2f3237; --line-soft:#25282c;
  --ink:#f2f3f5; --ink-2:#b4b8bf; --ink-3:#7d838c;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --warn:#d9a441; --crit:#e07a70; --good:#199e70;
  --grid:#2a2d31;
}
*{box-sizing:border-box}
body{background:var(--surface);color:var(--ink);
  font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  line-height:1.5;margin:0}
.wrap{max-width:1080px;margin:0 auto;padding-inline:20px;padding-block:40px 72px}
h1{font-size:clamp(26px,4vw,36px);font-weight:600;letter-spacing:-.02em;margin:0 0 6px;text-wrap:balance}
.sub{color:var(--ink-2);font-size:14px;margin:0 0 4px}
.mono{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.eyebrow{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--ink-3);
  font-weight:600;margin:0 0 10px}
section{margin-top:44px}
h2{font-size:19px;font-weight:600;margin:0 0 4px;letter-spacing:-.01em}
.note{color:var(--ink-2);font-size:13.5px;margin:0 0 18px;max-width:68ch}
.verdict{border-left:3px solid var(--warn);background:var(--panel);padding:16px 18px;
  border-radius:0 8px 8px 0;margin:26px 0 0}
.verdict p{margin:0;font-size:14.5px;color:var(--ink)}
.verdict p + p{margin-top:9px;color:var(--ink-2)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-top:22px}
.tile{background:var(--panel);padding:15px 16px}
.tile .k{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);font-weight:600}
.tile .v{font-size:25px;font-weight:600;margin-top:5px;letter-spacing:-.02em}
.tile .m{font-size:12px;color:var(--ink-2);margin-top:2px}
.pos{color:var(--good)} .neg{color:var(--crit)}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 14px 8px}
.legend{display:flex;flex-wrap:wrap;gap:16px;padding:0 4px 12px;font-size:13px;color:var(--ink-2)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.swatch{width:11px;height:11px;border-radius:2px;flex:none}
svg{display:block;width:100%;height:auto;overflow:visible}
.tip{position:fixed;pointer-events:none;background:var(--panel);border:1px solid var(--line);
  border-radius:7px;padding:9px 11px;font-size:12.5px;box-shadow:0 5px 18px rgba(0,0,0,.14);
  z-index:9;opacity:0;transition:opacity .1s}
.tip .r{display:flex;justify-content:space-between;gap:16px}
.tip .r + .r{margin-top:3px}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:560px}
th,td{text-align:right;padding:10px 14px;border-bottom:1px solid var(--line-soft)}
th:first-child,td:first-child{text-align:left}
thead th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);font-weight:600}
tbody tr:last-child td{border-bottom:0}
ol.lim{margin:0;padding-left:20px;font-size:14px;color:var(--ink-2);max-width:72ch}
ol.lim li{margin-bottom:11px}
ol.lim strong{color:var(--ink);font-weight:600}
.foot{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--ink-3)}
@media (max-width:520px){ .tile .v{font-size:21px} }
</style>

<div class="wrap">
  <p class="eyebrow">Binance USD&#8288;-&#8288;M Futures &middot; ETHUSDT perpetual</p>
  <h1>Zone Ladder Backtest</h1>
  <p class="sub mono" id="window"></p>
  <p class="sub">5&times; isolated, exposure fraction 1.00, rung spacing 0.50&#37;, 1-minute bars.
     Maker 2&nbsp;bps, taker 4&nbsp;bps, funding charged every 8&nbsp;hours at the rates actually realised.</p>

  <div class="verdict" id="verdict"></div>

  <section>
    <p class="eyebrow">Headline</p>
    <h2>Both runs, against simply holding</h2>
    <p class="note">Run&nbsp;A uses the ladder in <span class="mono">futures/test/settings.json</span> &mdash;
      drawn by hand after this period was already visible, so it carries look-ahead.
      Run&nbsp;B re-derives its zones every 7&nbsp;days from the trailing 30&nbsp;days only, so no
      decision ever sees its own future.</p>
    <div class="tiles" id="tiles"></div>
  </section>

  <section>
    <h2>Equity</h2>
    <p class="note">Wallet balance plus unrealised P&amp;L, marked every bar. The hold line is
      5,000&nbsp;USDT of ETH bought at the first close and never touched.</p>
    <div class="chart">
      <div class="legend" id="leg-eq"></div>
      <svg id="c-eq" viewBox="0 0 900 340" role="img" aria-label="Equity over time"></svg>
    </div>
  </section>

  <section>
    <h2>Drawdown</h2>
    <p class="note">Distance below the running peak. This is what an operator would have had to
      sit through, not a closed-trade statistic.</p>
    <div class="chart">
      <div class="legend" id="leg-dd"></div>
      <svg id="c-dd" viewBox="0 0 900 260" role="img" aria-label="Drawdown from peak"></svg>
    </div>
  </section>

  <section>
    <h2>Exposure</h2>
    <p class="note">Position notional in USDT. The cap is wallet &times; 5 &times; 1.00 and moves with the
      wallet; orders that would breach available margin are rejected, as the exchange rejects them
      with <span class="mono">-2019</span>.</p>
    <div class="chart">
      <div class="legend" id="leg-ex"></div>
      <svg id="c-ex" viewBox="0 0 900 260" role="img" aria-label="Position notional over time"></svg>
    </div>
  </section>

  <section>
    <h2>All figures</h2>
    <p class="note">The same numbers as the charts, for reading precisely.</p>
    <div class="tbl"><table id="table"></table></div>
  </section>

  <section>
    <h2>What this cannot tell you</h2>
    <p class="note">Stated here rather than in a footnote, because each one can move the result
      more than the strategy does.</p>
    <ol class="lim">
      <li><strong>One month, one symbol, one regime.</strong> ETH ranged roughly 2,355&ndash;2,669
        and drifted up. A zone-scaling ladder lives or dies on regime; nothing here says how it
        behaves in a breakout, a crash, or a flat quarter.</li>
      <li><strong>The simulated bot decides once a minute; the real one polls every second.</strong>
        Inside a bar only open, high, low and close are known, so the ladder's detect&ndash;settle&ndash;reconcile
        cycle is coarser here than in production.</li>
      <li><strong>The intrabar path is unknowable.</strong> A bar is treated as one directional
        sweep, and only rungs on the dominant side can fill, so the book can never round-trip
        against itself within a minute. An earlier version allowed both sides and reported
        +318&#37; for the month &mdash; essentially all of it manufactured by that one assumption.</li>
      <li><strong>No order-book depth and no market impact.</strong> Fills happen at the rung price
        in full. At this size on ETHUSDT that is a fair approximation, but it is an approximation.</li>
      <li><strong>Run&nbsp;A's zones knew the answer.</strong> Its support at 2,371.26 sits just under
        the period's low of 2,355.12. Read run&nbsp;B for anything forward-looking.</li>
    </ol>
  </section>

  <p class="foot" id="foot"></p>
</div>
<div class="tip" id="tip"></div>

<script>
const D = __DATA__;
const LAB = {A:"A-configured", B:"B-walkforward"};
const COL = {"A-configured":"--s1","B-walkforward":"--s2","Buy and hold":"--s3"};
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const money = v => (v<0?"-":"") + "$" + Math.abs(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = v => (v>=0?"+":"") + v.toFixed(2) + "%";
const day = t => new Date(t).toLocaleDateString("en-US",{month:"short",day:"numeric",timeZone:"UTC"});

document.getElementById("window").textContent =
  "__START__ to __END__  ·  " + D.meta.bars.toLocaleString() + " one-minute bars  ·  " +
  "ETH " + D.meta.first_price.toFixed(2) + " → " + D.meta.last_price.toFixed(2);

/* ---------- verdict ---------- */
const a = D.stats["A-configured"], b = D.stats["B-walkforward"], bh = D.meta.buy_and_hold_pct;
document.getElementById("verdict").innerHTML =
  "<p><strong>Holding ETH returned " + pct(bh) + " over the same month.</strong> " +
  "Run&nbsp;A returned " + pct(a.return_pct) + " and run&nbsp;B " + pct(b.return_pct) +
  ", with peak drawdowns of " + a.max_drawdown_pct.toFixed(1) + "% and " +
  b.max_drawdown_pct.toFixed(1) + "% against a wallet that never exceeded " +
  money(Math.max(a.max_notional,b.max_notional)) + " of notional at 5× leverage.</p>" +
  "<p>" + (b.concentration_pct > 30
    ? "In run B a single fill accounts for " + b.concentration_pct.toFixed(0) +
      "% of all realised profit, so its headline is one event rather than a repeatable process. "
    : "") +
  "Judge the strategy on drawdown per unit of return, not on the return alone.</p>";

/* ---------- tiles ---------- */
const tiles = [
  ["Run A return", pct(a.return_pct), "configured zones · look-ahead", a.return_pct],
  ["Run B return", pct(b.return_pct), "walk-forward · honest", b.return_pct],
  ["Buy and hold", pct(bh), "same month, no trading", bh],
  ["Run A max drawdown", a.max_drawdown_pct.toFixed(1)+"%", "peak to trough", a.max_drawdown_pct],
  ["Run B max drawdown", b.max_drawdown_pct.toFixed(1)+"%", "peak to trough", b.max_drawdown_pct],
  ["Fills", (a.orders+b.orders).toLocaleString(), "both runs combined", 0],
];
document.getElementById("tiles").innerHTML = tiles.map(([k,v,m,sign]) =>
  '<div class="tile"><div class="k">'+k+'</div><div class="v mono '+
  (sign>0?"pos":sign<0?"neg":"")+'">'+v+'</div><div class="m">'+m+'</div></div>').join("");

/* ---------- charts ---------- */
function draw(svgId, legId, series, fmt, opts){
  const svg = document.getElementById(svgId);
  const vb = svg.viewBox.baseVal, W = vb.width, H = vb.height;
  const m = {t:14, r:64, b:30, l:8};
  const all = series.flatMap(s => s.points);
  let lo = Math.min(...all.map(p=>p[1])), hi = Math.max(...all.map(p=>p[1]));
  if (opts && opts.zero) { lo = Math.min(lo,0); hi = Math.max(hi,0); }
  const pad = (hi-lo)*0.08 || 1; lo -= pad; hi += pad;
  if (opts && opts.floorZero) lo = 0;
  const t0 = all[0][0], t1 = all[all.length-1][0];
  const X = t => m.l + (t-t0)/(t1-t0) * (W-m.l-m.r);
  const Y = v => m.t + (hi-v)/(hi-lo) * (H-m.t-m.b);

  let out = "";
  /* gridlines, each labelled with a value the chart actually reaches */
  for (let i=0;i<=4;i++){
    const v = lo + (hi-lo)*i/4, y = Y(v);
    out += '<line x1="'+m.l+'" y1="'+y+'" x2="'+(W-m.r)+'" y2="'+y+'" stroke="'+cssv("--grid")+'" stroke-width="1"/>';
    out += '<text x="'+(W-m.r+8)+'" y="'+(y+4)+'" fill="'+cssv("--ink-3")+'" font-size="11" font-family="IBM Plex Mono, monospace">'+fmt(v)+'</text>';
  }
  for (let i=0;i<=5;i++){
    const t = t0 + (t1-t0)*i/5, x = X(t);
    out += '<text x="'+x+'" y="'+(H-8)+'" fill="'+cssv("--ink-3")+'" font-size="11" text-anchor="'+(i===0?"start":i===5?"end":"middle")+'">'+day(t)+'</text>';
  }
  const placed = [];
  series.forEach(s => {
    const d = s.points.map((p,i)=>(i?"L":"M")+X(p[0]).toFixed(1)+" "+Y(p[1]).toFixed(1)).join(" ");
    out += '<path d="'+d+'" fill="none" stroke="'+cssv(s.token)+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    const last = s.points[s.points.length-1];
    out += '<circle cx="'+X(last[0]).toFixed(1)+'" cy="'+Y(last[1]).toFixed(1)+'" r="4" fill="'+cssv(s.token)+'" stroke="'+cssv("--panel")+'" stroke-width="2"/>';
    /* direct label at the endpoint: identity never rests on colour alone */
    let ly = Y(last[1])+4;
    while (placed.some(v => Math.abs(v-ly) < 13)) ly += 13;
    placed.push(ly);
    out += '<text x="'+(X(last[0])+9)+'" y="'+ly+'" fill="'+cssv(s.token)+'" font-size="11" font-weight="600" font-family="IBM Plex Mono, monospace">'+s.short+'</text>';
  });
  out += '<line id="'+svgId+'-cross" x1="0" y1="'+m.t+'" x2="0" y2="'+(H-m.b)+'" stroke="'+cssv("--ink-3")+'" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>';
  svg.innerHTML = out;

  document.getElementById(legId).innerHTML = series.map(s =>
    '<span><i class="swatch" style="background:'+cssv(s.token)+'"></i>'+s.name+'</span>').join("");

  const tip = document.getElementById("tip"), cross = document.getElementById(svgId+"-cross");
  svg.addEventListener("pointermove", e => {
    const r = svg.getBoundingClientRect();
    const sx = (e.clientX - r.left) / r.width * W;
    const frac = Math.min(1, Math.max(0, (sx - m.l) / (W-m.l-m.r)));
    const t = t0 + (t1-t0)*frac;
    const idx = Math.round(frac * (series[0].points.length-1));
    cross.setAttribute("x1", X(t)); cross.setAttribute("x2", X(t)); cross.setAttribute("opacity","1");
    tip.innerHTML = '<div class="r"><b>'+day(t)+'</b></div>' + series.map(s => {
      const p = s.points[Math.min(idx, s.points.length-1)];
      return '<div class="r"><span style="color:'+cssv(s.token)+'">'+s.name+'</span><span class="mono">'+fmt(p[1])+'</span></div>';
    }).join("");
    tip.style.opacity = 1;
    tip.style.left = Math.min(window.innerWidth-190, e.clientX+14)+"px";
    tip.style.top = (e.clientY-12)+"px";
  });
  svg.addEventListener("pointerleave", () => { tip.style.opacity = 0; cross.setAttribute("opacity","0"); });
}

function series(map, extra){
  const out = Object.entries(map).map(([k,v]) => ({
    name:k, short:k.charAt(0), token:COL[k], points:v}));
  if (extra) out.push(extra);
  return out;
}

function render(){
  draw("c-eq","leg-eq", series(D.equity, {name:"Buy and hold", short:"H", token:COL["Buy and hold"], points:D.hold}),
       v=>"$"+Math.round(v).toLocaleString());
  draw("c-dd","leg-dd", series(D.drawdown), v=>v.toFixed(0)+"%", {zero:true});
  draw("c-ex","leg-ex", series(D.exposure), v=>"$"+Math.round(v/1000)+"k", {zero:true, floorZero:true});
}
render();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

/* ---------- table ---------- */
const rows = [
  ["Final equity", s=>money(s.final_equity)],
  ["Return", s=>pct(s.return_pct)],
  ["Excess over hold", s=>pct(s.excess_pct)],
  ["Max drawdown", s=>s.max_drawdown_pct.toFixed(2)+"%"],
  ["Fills", s=>s.orders.toLocaleString()],
  ["Closing trades", s=>s.closing_trades.toLocaleString()],
  ["Win rate", s=>s.win_rate_pct.toFixed(1)+"%"],
  ["Realised P&L", s=>money(s.realized)],
  ["Largest single trade", s=>money(s.biggest_trade)],
  ["Profit from that one trade", s=>s.concentration_pct.toFixed(0)+"%"],
  ["Peak notional", s=>money(s.max_notional)],
  ["Time inside a zone", s=>s.bars_in_zone_pct.toFixed(1)+"%"],
];
document.getElementById("table").innerHTML =
  "<thead><tr><th>Metric</th><th>A &mdash; configured</th><th>B &mdash; walk-forward</th></tr></thead><tbody>" +
  rows.map(([k,f]) => "<tr><td>"+k+"</td><td class='mono'>"+f(a)+"</td><td class='mono'>"+f(b)+"</td></tr>").join("") +
  "</tbody></table>";

document.getElementById("foot").textContent =
  "Generated " + new Date(D.meta.generated_at).toISOString().slice(0,16).replace("T"," ") +
  " UTC from live Binance market data. Simulation drives futures/strategy.py and futures/ladder.py directly; " +
  "only the exchange is simulated.";
</script>
"""
