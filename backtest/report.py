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


def build_multi(meta, windows):
    """windows: {name: {days, bars, buy_and_hold_pct, runs:{label:{equity,orders}}, ...}}

    One page covering every window, because the interesting question is not
    what the strategy did over any single period but whether it holds up as
    the period changes. A month that flatters it and a year that does not is
    the finding; two separate pages would hide it."""
    start = meta["start_balance"]
    payload = {"meta": meta, "windows": {}}
    stats = {}

    for name, w in windows.items():
        bh = w["buy_and_hold_pct"]
        w_stats = {label: metrics(label, r["equity"], r["orders"], start, bh)
                   for label, r in w["runs"].items()}
        stats[name] = w_stats

        first_eq = next(iter(w["runs"].values()))["equity"]
        p0 = first_eq[0]["price"]
        payload["windows"][name] = {
            "days": w["days"],
            "bars": w["bars"],
            "conditional": w.get("conditional", False),
            "buy_and_hold_pct": bh,
            "first_price": w["first_price"],
            "last_price": w["last_price"],
            "low_price": w.get("low_price", w["last_price"]),
            "zone_sets": w.get("zone_sets", 0),
            "stats": w_stats,
            "equity": {label: _series(r["equity"], "equity")
                       for label, r in w["runs"].items()},
            "hold": [[p["time"], round(start * p["price"] / p0, 4)]
                     for p in _downsample(first_eq)],
            "drawdown": {label: _drawdown_series(r["equity"])
                         for label, r in w["runs"].items()},
        }

    html = _render(payload)
    (RESULTS / "report.html").write_text(html, encoding="utf-8")
    (RESULTS / "metrics.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats, RESULTS / "report.html"


def _render(payload):
    return TEMPLATE.replace("__DATA__", json.dumps(payload))


TEMPLATE = r"""<title>Zone Ladder Backtest</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --surface:#fbfbfa; --panel:#ffffff; --line:#e3e4e1; --line-soft:#eff0ed;
  --ink:#14161a; --ink-2:#565a61; --ink-3:#8a8f97;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --warn:#b4690e; --crit:#c0392f; --good:#1baf7a; --grid:#e8e9e6;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --surface:#17181a; --panel:#1e2023; --line:#2f3237; --line-soft:#25282c;
    --ink:#f2f3f5; --ink-2:#b4b8bf; --ink-3:#7d838c;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70;
    --warn:#d9a441; --crit:#e07a70; --good:#199e70; --grid:#2a2d31;
  }
}
:root[data-theme="dark"]{
  --surface:#17181a; --panel:#1e2023; --line:#2f3237; --line-soft:#25282c;
  --ink:#f2f3f5; --ink-2:#b4b8bf; --ink-3:#7d838c;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --warn:#d9a441; --crit:#e07a70; --good:#199e70; --grid:#2a2d31;
}
*{box-sizing:border-box}
body{background:var(--surface);color:var(--ink);margin:0;line-height:1.5;
  font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding-inline:20px;padding-block:40px 72px}
h1{font-size:clamp(26px,4vw,36px);font-weight:600;letter-spacing:-.02em;margin:0 0 6px;text-wrap:balance}
.sub{color:var(--ink-2);font-size:14px;margin:0 0 4px}
.mono{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.eyebrow{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--ink-3);font-weight:600;margin:0 0 10px}
section{margin-top:46px}
h2{font-size:19px;font-weight:600;margin:0 0 4px;letter-spacing:-.01em}
.note{color:var(--ink-2);font-size:13.5px;margin:0 0 18px;max-width:70ch}
.verdict{border-left:3px solid var(--crit);background:var(--panel);padding:16px 18px;border-radius:0 8px 8px 0;margin-top:26px}
.verdict p{margin:0;font-size:14.5px}
.verdict p+p{margin-top:9px;color:var(--ink-2)}
.zones{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 0}
.zone{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:7px 11px;font-size:12.5px}
.zone.new{border-color:var(--s2);color:var(--s2)}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px 14px 10px}
.card h3{font-size:14px;font-weight:600;margin:0 0 2px}
.card.cond{border-color:var(--warn)}
.flag{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--warn);
  font-weight:600;border:1px solid var(--warn);border-radius:4px;padding:1px 5px;margin-left:6px}
.card .meta{font-size:12px;color:var(--ink-3);margin:0 0 12px}
.row{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;margin:2px 4px 10px;color:var(--ink-2)}
.row b{font-weight:600}
svg{display:block;width:100%;height:auto;overflow:visible}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:13px;color:var(--ink-2);margin:0 0 16px}
.legend span{display:inline-flex;align-items:center;gap:7px}
.swatch{width:11px;height:11px;border-radius:2px;flex:none}
.pos{color:var(--good)} .neg{color:var(--crit)}
.tip{position:fixed;pointer-events:none;background:var(--panel);border:1px solid var(--line);
  border-radius:7px;padding:9px 11px;font-size:12.5px;box-shadow:0 5px 18px rgba(0,0,0,.14);z-index:9;opacity:0;transition:opacity .1s}
.tip .r{display:flex;justify-content:space-between;gap:16px}
.tip .r+.r{margin-top:3px}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:660px}
th,td{text-align:right;padding:9px 13px;border-bottom:1px solid var(--line-soft)}
th:first-child,td:first-child{text-align:left}
thead th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);font-weight:600}
tbody tr:last-child td{border-bottom:0}
tbody tr.sep td{border-top:2px solid var(--line)}
ol.lim{margin:0;padding-left:20px;font-size:14px;color:var(--ink-2);max-width:72ch}
ol.lim li{margin-bottom:11px}
ol.lim strong{color:var(--ink);font-weight:600}
.foot{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--ink-3)}
</style>

<div class="wrap">
  <p class="eyebrow">Binance USD&#8288;-&#8288;M Futures &middot; ETHUSDT perpetual</p>
  <h1>Zone Ladder Backtest</h1>
  <p class="sub mono" id="window"></p>
  <p class="sub">5&times; isolated, exposure fraction 1.00, rung spacing 0.50&#37;, 1-minute bars.
     Maker 2&nbsp;bps, taker 4&nbsp;bps, funding charged every 8&nbsp;hours at the rates actually realised.</p>
  <div class="zones" id="zones"></div>
  <div class="verdict" id="verdict"></div>

  <section>
    <p class="eyebrow">Headline</p>
    <h2>Four windows, same ladder</h2>
    <p class="note"><b>With&nbsp;1411</b> is the ladder as configured today.
      <b>Without&nbsp;1411</b> is the same ladder minus the newly added support &mdash; the control, which
      isolates what that one line did. <b>Walk-forward</b> re-derives its zones every 7&nbsp;days from the
      trailing 30&nbsp;days only, so no decision ever sees its own future. All start from 5,000&nbsp;USDT.
      Adding a support does not only add a zone: it moves where <span class="mono">past_adverse_end</span>
      decides the thesis is dead, so it changes the stop as well as the sizing curve.</p>
    <div class="grid3" id="cards"></div>
  </section>

  <section>
    <h2>Equity</h2>
    <p class="note">Wallet plus unrealised P&amp;L, marked every bar. Each panel is its own window, drawn
      to its own scale &mdash; compare shapes, not heights.</p>
    <div class="legend" id="leg"></div>
    <div class="grid3" id="eqcharts"></div>
  </section>

  <section>
    <h2>Drawdown</h2>
    <p class="note">Distance below the running peak &mdash; what an operator would have had to sit through.</p>
    <div class="grid3" id="ddcharts"></div>
  </section>

  <section>
    <h2>All figures</h2>
    <div class="tbl"><table id="table"></table></div>
  </section>

  <section>
    <h2>What this cannot tell you</h2>
    <ol class="lim">
      <li><strong>One symbol.</strong> Every number here is ETHUSDT. Nothing says the ladder transfers.</li>
      <li><strong>The simulated bot decides once a minute; the real one polls every second.</strong>
        Inside a bar only open, high, low and close are known, so the detect&ndash;settle&ndash;reconcile
        cycle is coarser here than in production.</li>
      <li><strong>The intrabar path is unknowable.</strong> A bar is treated as one directional sweep and
        only rungs on the dominant side can fill, so the book can never round-trip against itself
        within a minute. An earlier version allowed both sides and reported +318&#37; for a month &mdash;
        essentially all of it manufactured by that one assumption.</li>
      <li><strong>No order-book depth and no market impact.</strong> Fills happen at the rung price in full.</li>
      <li><strong>The configured zones were drawn with recent history visible.</strong> Over the longer
        windows that matters less, but it never becomes a forecast. Read walk-forward for that &mdash; and
        note what it did over a year.</li>
      <li><strong>The July window was chosen because the long call was right from there.</strong>
        That is selection bias, the same family as look-ahead: it measures execution GIVEN a correct
        regime call, not the strategy end to end. Read it as "how well does the machinery work once
        the direction is right", and note that nothing here picks the direction for you.</li>
      <li><strong>A stop that never triggers cannot be judged by the run where it never triggered.</strong>
        1411 looks good here because ETH bottomed at 1504. One path is not a distribution.</li>
      <li><strong>Survivorship of the configuration itself.</strong> These are the zones that happen to be
        configured today. Testing the ladder you already chose is not the same as testing the method
        that chose it.</li>
    </ol>
  </section>

  <p class="foot" id="foot"></p>
</div>
<div class="tip" id="tip"></div>

<script>
const D = __DATA__;
/* Three configurations plus a benchmark. Hold is drawn in neutral ink rather
   than a fourth categorical hue: it is a reference line, not a peer series,
   and the validated categorical set is three slots deep for all-pairs use. */
const COL = {"With 1411":"--s1","Without 1411":"--s2","Walk-forward":"--s3","Buy and hold":"--ink-3"};
const SHORT = {"With 1411":"+","Without 1411":"-","Walk-forward":"W","Buy and hold":"H"};
const ORDER = ["With 1411","Without 1411","Walk-forward"];
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const money = v => (v<0?"-":"")+"$"+Math.abs(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = v => (v>=0?"+":"")+v.toFixed(2)+"%";
const NAMES = Object.keys(D.windows);

document.getElementById("window").textContent =
  "windows ending " + new Date(D.meta.window_end).toISOString().slice(0,10) +
  "  |  " + NAMES.join("  |  ") + "  |  start 5,000 USDT";

document.getElementById("zones").innerHTML = D.meta.zones.map((z,i) =>
  '<div class="zone'+(z[0]===1411?' new':'')+'"><span class="mono">'+
  z[0].toFixed(2)+" &ndash; "+z[1].toFixed(2)+'</span>'+(z[0]===1411?" &middot; new":"")+'</div>').join("");

/* ---------- verdict ---------- */
const rows = NAMES.map(n => {
  const w = D.windows[n];
  return {n, w, on:w.stats["With 1411"], off:w.stats["Without 1411"],
          wf:w.stats["Walk-forward"], h:w.buy_and_hold_pct};
});
/* The verdict anchors on the longest ROLLING window, not the last entry -
   an operator-chosen window is a conditional result and must not headline. */
const yr = rows.find(r => r.n === "1 year") || rows[rows.length-1];
const jul = rows.find(r => r.w.conditional);
const gain = yr.on.return_pct - yr.off.return_pct;
document.getElementById("verdict").innerHTML =
  "<p><strong>Over a full year a ladder drawn without hindsight lost most of the account.</strong> " +
  "Walk-forward returned "+pct(yr.wf.return_pct)+" with a "+yr.wf.max_drawdown_pct.toFixed(0)+
  "% peak drawdown, while the configured ladder returned "+pct(yr.on.return_pct)+
  ". That gap is not strategy quality, it is information: the configured zones were drawn knowing "+
  "where the bottom fell.</p>" +
  (jul ? "<p><strong>Over "+jul.n+", chosen because the long call was right from there, all three "+
    "configurations beat holding</strong> &mdash; walk-forward "+pct(jul.wf.return_pct)+
    " against "+pct(jul.h)+" for hold, at a "+jul.wf.max_drawdown_pct.toFixed(0)+"% drawdown. "+
    "Read that as how the machinery performs once the direction is right, not as a forecast: "+
    "nothing here picks the direction.</p>" : "") +
  "<p>The 1411 support helped on this path &mdash; "+pct(yr.on.return_pct)+" against "+
  pct(yr.off.return_pct)+" over the year, a "+gain.toFixed(0)+" point difference &mdash; but only "+
  "because ETH bottomed at "+yr.w.low_price.toFixed(0)+", above 1411 and below the 1853 stop the old "+
  "ladder had. That is a stop that failed to trigger, not a demonstrated improvement.</p>";

/* ---------- per-window cards ---------- */
document.getElementById("cards").innerHTML = rows.map(r => {
  const line = (lab,v,cls) => '<div><b>'+lab+'</b> <span class="mono '+cls+'">'+v+'</span></div>';
  const cls = v => v>=0?"pos":"neg";
  return '<div class="card'+(r.w.conditional?' cond':'')+'"><h3>'+r.n+
    (r.w.conditional?' <span class="flag">operator-chosen start</span>':'')+
    '</h3><p class="meta mono">'+
    r.w.bars.toLocaleString()+" bars &middot; ETH "+r.w.first_price.toFixed(0)+" &rarr; "+r.w.last_price.toFixed(0)+
    " &middot; low "+r.w.low_price.toFixed(0)+'</p><div class="row">'+
    line("With 1411", pct(r.on.return_pct), cls(r.on.return_pct))+
    line("Without", pct(r.off.return_pct), cls(r.off.return_pct))+
    '</div><div class="row">'+
    line("Walk-forward", pct(r.wf.return_pct), cls(r.wf.return_pct))+
    line("Hold", pct(r.h), cls(r.h))+
    '</div><div class="row">'+
    line("worst DD", Math.min(r.on.max_drawdown_pct, r.off.max_drawdown_pct, r.wf.max_drawdown_pct).toFixed(0)+"%","neg")+
    '</div></div>';
}).join("");

/* ---------- charts ---------- */
function draw(svg, series, fmt, opts){
  const vb = svg.viewBox.baseVal, W = vb.width, H = vb.height;
  const m = {t:12, r:56, b:26, l:6};
  const all = series.flatMap(s => s.points);
  let lo = Math.min(...all.map(p=>p[1])), hi = Math.max(...all.map(p=>p[1]));
  if (opts && opts.zero){ lo = Math.min(lo,0); hi = Math.max(hi,0); }
  const pad = (hi-lo)*0.08 || 1; lo -= pad; hi += pad;
  if (opts && opts.floorZero) lo = 0;
  const t0 = all[0][0], t1 = all[all.length-1][0];
  const X = t => m.l + (t-t0)/(t1-t0)*(W-m.l-m.r);
  const Y = v => m.t + (hi-v)/(hi-lo)*(H-m.t-m.b);
  const day = t => new Date(t).toLocaleDateString("en-US",{month:"short",day:"numeric",timeZone:"UTC"});

  let out = "";
  for (let i=0;i<=3;i++){
    const v = lo+(hi-lo)*i/3, y = Y(v);
    out += '<line x1="'+m.l+'" y1="'+y+'" x2="'+(W-m.r)+'" y2="'+y+'" stroke="'+cssv("--grid")+'" stroke-width="1"/>';
    out += '<text x="'+(W-m.r+7)+'" y="'+(y+4)+'" fill="'+cssv("--ink-3")+'" font-size="10" font-family="IBM Plex Mono, monospace">'+fmt(v)+'</text>';
  }
  [0,1].forEach(i => {
    const t = i? t1 : t0, x = X(t);
    out += '<text x="'+x+'" y="'+(H-6)+'" fill="'+cssv("--ink-3")+'" font-size="10" text-anchor="'+(i?"end":"start")+'">'+day(t)+'</text>';
  });
  const placed = [];
  series.forEach(s => {
    const d = s.points.map((p,i)=>(i?"L":"M")+X(p[0]).toFixed(1)+" "+Y(p[1]).toFixed(1)).join(" ");
    out += '<path d="'+d+'" fill="none" stroke="'+cssv(s.token)+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    const last = s.points[s.points.length-1];
    out += '<circle cx="'+X(last[0]).toFixed(1)+'" cy="'+Y(last[1]).toFixed(1)+'" r="3.5" fill="'+cssv(s.token)+'" stroke="'+cssv("--panel")+'" stroke-width="2"/>';
    let ly = Y(last[1])+4;
    while (placed.some(v => Math.abs(v-ly) < 12)) ly += 12;
    placed.push(ly);
    out += '<text x="'+(X(last[0])+8)+'" y="'+ly+'" fill="'+cssv(s.token)+'" font-size="10" font-weight="600" font-family="IBM Plex Mono, monospace">'+s.short+'</text>';
  });
  svg.innerHTML = out;

  const tip = document.getElementById("tip");
  svg.addEventListener("pointermove", e => {
    const r = svg.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, ((e.clientX-r.left)/r.width*W - m.l)/(W-m.l-m.r)));
    const idx = Math.round(frac*(series[0].points.length-1));
    tip.innerHTML = '<div class="r"><b>'+day(t0+(t1-t0)*frac)+'</b></div>' + series.map(s => {
      const p = s.points[Math.min(idx, s.points.length-1)];
      return '<div class="r"><span style="color:'+cssv(s.token)+'">'+s.name+'</span><span class="mono">'+fmt(p[1])+'</span></div>';
    }).join("");
    tip.style.opacity = 1;
    tip.style.left = Math.min(window.innerWidth-190, e.clientX+14)+"px";
    tip.style.top = (e.clientY-12)+"px";
  });
  svg.addEventListener("pointerleave", () => tip.style.opacity = 0);
}

function panels(hostId, pick, fmt, opts){
  const host = document.getElementById(hostId);
  host.innerHTML = NAMES.map(n =>
    '<div class="card'+(D.windows[n].conditional?' cond':'')+'"><h3>'+n+
    (D.windows[n].conditional?' <span class="flag">chosen start</span>':'')+'</h3><p class="meta">'+
    (hostId==="eqcharts" ? "equity, USDT" : "drawdown from peak")+
    '</p><svg viewBox="0 0 420 210" role="img" aria-label="'+n+'"></svg></div>').join("");
  [...host.querySelectorAll("svg")].forEach((svg,i) => draw(svg, pick(D.windows[NAMES[i]]), fmt, opts));
}

function toSeries(map, extra){
  const out = ORDER.filter(k => map[k]).map(k => ({name:k, short:SHORT[k], token:COL[k], points:map[k]}));
  if (extra) out.push(extra);
  return out;
}

function render(){
  panels("eqcharts", w => toSeries(w.equity, {name:"Buy and hold", short:"H", token:COL["Buy and hold"], points:w.hold}),
         v => "$"+Math.round(v).toLocaleString());
  panels("ddcharts", w => toSeries(w.drawdown), v => v.toFixed(0)+"%", {zero:true});
  document.getElementById("leg").innerHTML =
    ORDER.concat(["Buy and hold"]).map(k =>
      '<span><i class="swatch" style="background:'+cssv(COL[k])+'"></i>'+k+'</span>').join("");
}
render();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

/* ---------- table ---------- */
const METRICS = [
  ["Final equity", s=>money(s.final_equity)],
  ["Return", s=>pct(s.return_pct)],
  ["Excess over hold", s=>pct(s.excess_pct)],
  ["Max drawdown", s=>s.max_drawdown_pct.toFixed(2)+"%"],
  ["Fills", s=>s.orders.toLocaleString()],
  ["Win rate", s=>s.win_rate_pct.toFixed(1)+"%"],
  ["Realised P&L", s=>money(s.realized)],
  ["Largest single trade", s=>money(s.biggest_trade)],
  ["Profit from that one trade", s=>s.concentration_pct.toFixed(0)+"%"],
  ["Peak notional", s=>money(s.max_notional)],
  ["Time inside a zone", s=>s.bars_in_zone_pct.toFixed(1)+"%"],
];
let html = "<thead><tr><th>Metric</th>" +
  NAMES.map(n => ORDER.map(k=>'<th>'+n+' &middot; '+SHORT[k]+'</th>').join("")).join("") + "</tr></thead><tbody>";
html += '<tr><td>Buy and hold</td>' + NAMES.map(n =>
  '<td class="mono" colspan="3" style="text-align:right">'+pct(D.windows[n].buy_and_hold_pct)+'</td>').join("") + "</tr>";
html += METRICS.map(([k,f],i) => '<tr'+(i===0?' class="sep"':'')+'><td>'+k+'</td>' +
  NAMES.map(n => ORDER.map(key =>
    '<td class="mono">'+f(D.windows[n].stats[key])+'</td>').join("")).join("") + "</tr>").join("");
document.getElementById("table").innerHTML = html + "</tbody>";

document.getElementById("foot").textContent =
  "Generated " + new Date(D.meta.generated_at).toISOString().slice(0,16).replace("T"," ") +
  " UTC from live Binance market data. Simulation drives futures/strategy.py and futures/ladder.py directly; only the exchange is simulated.";
</script>
"""
