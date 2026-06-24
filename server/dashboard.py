"""Self-contained operator console for the Jarviz backend.

Single HTML page (no build, no npm, no static-file mount) served from
`GET /dashboard`. Polls six JSON endpoints every two seconds and renders:

    I.   Device status hero       (/sessions/live)
    II.  Editorial transcript     (/transcripts/recent)
    III. 48-hour reminders chart  (/reminders/stats)
    IV.  Network panel            (/network)
    V.   Telemetry strip          (/metrics)
    VI.  Live log tail            (/logs/recent)

Aesthetic: audiophile mission-control. Warm near-black panel, VU-meter
amber and CRT phosphor accents, Fraunces serif with italic for spoken
text, system mono for technical data. The transcript reads like an
interview transcript rather than a chat app on purpose.

Mobile-friendly. Degrades gracefully when every endpoint is empty.
"""

from __future__ import annotations

# Inline page. CSS first, then JS, then markup. Kept as a single Python
# string so deployment is just `uvicorn server.main:app` — no extra
# StaticFiles mount, no separate frontend build pipeline.
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Jarviz · Operator Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,400;0,9..144,500;1,9..144,400&display=swap" rel="stylesheet">
<style>
  /* ---------- Tokens ---------- */
  :root {
    /* Surfaces — warm near-blacks, not pure #000 */
    --bg:           #0d0b08;
    --bg-surface:   #15110c;
    --bg-elev:      #1d1814;
    --bg-inset:     #0a0807;

    /* Hairlines */
    --rule:         rgba(244, 237, 224, 0.07);
    --rule-strong:  rgba(244, 237, 224, 0.14);
    --rule-bright:  rgba(244, 237, 224, 0.28);

    /* Text */
    --fg:           #f4ede0;
    --fg-dim:       #9a9087;
    --fg-faint:     #5c554e;

    /* Two accents — one warm, one cool. Used sparingly. */
    --warm:         #f0b659;
    --warm-soft:    rgba(240, 182, 89, 0.22);
    --warm-deep:    #c98d2e;
    --cool:         #7ddec0;
    --cool-soft:    rgba(125, 222, 192, 0.22);
    --info:         #6db9e8;

    /* Device-state palette */
    --st-idle:        #6e645a;
    --st-listening:   #e8634a;
    --st-processing:  #f0b659;
    --st-speaking:    #7ddec0;

    /* Type stacks */
    --font-display: 'Fraunces', 'Iowan Old Style', 'Source Serif Pro', Georgia, serif;
    --font-body:    -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;
    --font-mono:    ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: var(--font-body);
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    /* Subtle warm halo from the top */
    background-image:
      radial-gradient(ellipse at 50% -10%, rgba(240, 182, 89, 0.06) 0%, transparent 55%),
      radial-gradient(ellipse at 100% 100%, rgba(125, 222, 192, 0.03) 0%, transparent 50%);
  }

  ::selection { background: var(--warm-soft); color: var(--fg); }

  /* ---------- Header ---------- */
  .masthead {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 26px 40px;
    border-bottom: 1px solid var(--rule);
    position: sticky;
    top: 0;
    background: linear-gradient(to bottom, var(--bg) 80%, rgba(13,11,8,0.6) 100%);
    backdrop-filter: blur(8px);
    z-index: 10;
  }
  .brand {
    display: flex;
    align-items: baseline;
    gap: 14px;
  }
  .brand-mark {
    width: 9px; height: 9px;
    background: var(--warm);
    border-radius: 1px;
    box-shadow: 0 0 14px var(--warm-soft), 0 0 4px var(--warm);
    align-self: center;
    animation: tally 3.4s ease-in-out infinite;
  }
  @keyframes tally {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.55; }
  }
  .brand-name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 26px;
    letter-spacing: -0.02em;
    font-variation-settings: 'opsz' 144;
  }
  .brand-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.28em;
    color: var(--fg-faint);
    margin-left: 6px;
  }

  .meta {
    display: flex;
    align-items: center;
    gap: 20px;
  }
  .conn {
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.06em;
    color: var(--fg-dim);
  }
  .conn .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--fg-faint);
    transition: background-color 0.3s, box-shadow 0.3s;
  }
  .conn[data-state="ok"] .dot {
    background: var(--cool);
    box-shadow: 0 0 8px var(--cool-soft);
  }
  .conn[data-state="err"] .dot {
    background: var(--st-listening);
    box-shadow: 0 0 8px rgba(232,99,74,0.5);
    animation: blink 1.2s ease-in-out infinite;
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  .pause-btn {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: var(--fg-dim);
    background: transparent;
    border: 1px solid var(--rule-strong);
    padding: 7px 14px;
    border-radius: 2px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .pause-btn:hover { border-color: var(--rule-bright); color: var(--fg); }
  .pause-btn.active {
    color: var(--warm);
    border-color: var(--warm-soft);
    background: rgba(240, 182, 89, 0.05);
  }

  /* ---------- Page grid ---------- */
  main {
    padding: 36px 40px 80px;
    display: grid;
    gap: 48px;
    max-width: 1500px;
    margin: 0 auto;
  }

  @media (max-width: 720px) {
    .masthead { padding: 20px; }
    main { padding: 24px 20px 60px; gap: 36px; }
    .brand-sub { display: none; }
  }

  /* ---------- Section heading ---------- */
  .sec-head {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 22px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--rule);
  }
  .sec-num {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.22em;
    color: var(--warm);
  }
  .sec-title {
    font-family: var(--font-display);
    font-weight: 400;
    font-size: 22px;
    font-variation-settings: 'opsz' 60;
    letter-spacing: -0.01em;
    margin: 0;
  }
  .sec-aside {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.12em;
    color: var(--fg-faint);
    text-transform: uppercase;
  }

  /* ---------- Hero: device cards ---------- */
  .hero-grid {
    display: grid;
    gap: 18px;
    grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  }

  .device-card {
    position: relative;
    padding: 26px 28px 22px;
    background: var(--bg-surface);
    border: 1px solid var(--rule);
    border-radius: 3px;
    overflow: hidden;
    transition: border-color 0.4s;
  }
  .device-card::before {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(circle at 100% 0%, var(--state-glow, transparent) 0%, transparent 55%);
    opacity: 0.55;
    pointer-events: none;
    transition: opacity 0.4s;
  }
  .device-card[data-state="idle"]       { --state-color: var(--st-idle);       --state-glow: transparent; }
  .device-card[data-state="listening"]  { --state-color: var(--st-listening);  --state-glow: rgba(232,99,74,0.18); border-color: rgba(232,99,74,0.22); }
  .device-card[data-state="processing"] { --state-color: var(--st-processing); --state-glow: var(--warm-soft);      border-color: rgba(240,182,89,0.22); }
  .device-card[data-state="speaking"]   { --state-color: var(--st-speaking);   --state-glow: var(--cool-soft);      border-color: rgba(125,222,192,0.22); }

  .device-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: var(--fg-faint);
    margin-bottom: 14px;
  }
  .device-id { color: var(--fg-dim); }

  .pilot {
    display: flex;
    align-items: center;
    gap: 18px;
    margin-bottom: 22px;
  }
  .pilot-light {
    flex: 0 0 auto;
    width: 14px; height: 14px;
    border-radius: 50%;
    background: var(--state-color);
    box-shadow:
      inset 0 0 0 1px rgba(0,0,0,0.4),
      0 0 24px var(--state-glow),
      0 0 8px var(--state-glow);
    position: relative;
  }
  .device-card[data-state="processing"] .pilot-light {
    animation: pulse-warm 1.4s ease-in-out infinite;
  }
  .device-card[data-state="speaking"] .pilot-light {
    animation: pulse-cool 2s ease-in-out infinite;
  }
  @keyframes pulse-warm {
    0%, 100% { transform: scale(0.85); box-shadow: 0 0 12px var(--state-glow); }
    50%      { transform: scale(1.12); box-shadow: 0 0 28px var(--state-glow), 0 0 10px var(--state-color); }
  }
  @keyframes pulse-cool {
    0%, 100% { opacity: 0.85; }
    50%      { opacity: 1; box-shadow: 0 0 30px var(--state-glow), 0 0 12px var(--state-color); }
  }

  .state-text {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 400;
    font-size: 60px;
    line-height: 0.95;
    letter-spacing: -0.025em;
    color: var(--state-color);
    font-variation-settings: 'opsz' 144;
    text-transform: lowercase;
  }

  .device-meta {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin: 0;
    padding-top: 18px;
    border-top: 1px solid var(--rule);
  }
  .device-meta > div { margin: 0; }
  .device-meta dt {
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--fg-faint);
    margin-bottom: 4px;
  }
  .device-meta dd {
    margin: 0;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--fg);
    font-variant-numeric: tabular-nums;
  }

  /* ---------- Empty states ---------- */
  .empty {
    padding: 56px 40px;
    text-align: center;
    background: var(--bg-surface);
    border: 1px dashed var(--rule-strong);
    border-radius: 3px;
  }
  .empty-title {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 400;
    font-size: 22px;
    color: var(--fg-dim);
    margin: 0 0 10px;
    font-variation-settings: 'opsz' 60;
  }
  .empty-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--fg-faint);
  }

  /* ---------- Main two-column area ---------- */
  .main-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.55fr) minmax(280px, 1fr);
    gap: 48px;
  }
  @media (max-width: 960px) {
    .main-grid { grid-template-columns: 1fr; gap: 36px; }
  }

  .side {
    display: flex;
    flex-direction: column;
    gap: 48px;
  }

  /* ---------- Transcript ---------- */
  .transcript {
    background: var(--bg-surface);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 28px 32px;
    height: 580px;
    overflow-y: auto;
    scroll-behavior: smooth;
  }
  .transcript-empty {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    height: 100%;
    text-align: center;
    color: var(--fg-faint);
  }
  .transcript-empty .empty-title { color: var(--fg-faint); }

  .turn {
    padding: 22px 0;
    border-bottom: 1px solid var(--rule);
  }
  .turn:last-child { border-bottom: none; }

  .said {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 400;
    font-size: 19px;
    line-height: 1.45;
    color: var(--fg-dim);
    margin: 0 0 8px;
    font-variation-settings: 'opsz' 60;
  }
  .said::before {
    content: '\\201C';
    color: var(--fg-faint);
    margin-right: 2px;
  }
  .said::after {
    content: '\\201D';
    color: var(--fg-faint);
    margin-left: 2px;
  }
  .said-attr {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--fg-faint);
    margin-bottom: 14px;
  }
  .said-attr::before { content: '\\2014\\00a0'; }

  .reply {
    font-family: var(--font-body);
    font-size: 15px;
    line-height: 1.55;
    color: var(--fg);
    margin: 0 0 8px;
    padding-left: 14px;
    border-left: 2px solid var(--cool);
  }
  .reply-attr {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--fg-faint);
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
    padding-left: 16px;
  }
  .reply-attr::before { content: '\\2014\\00a0Jarviz'; color: var(--cool); margin-right: 6px; }
  .reply-attr .lat {
    color: var(--fg-dim);
  }
  .reply-attr .tool {
    background: rgba(240, 182, 89, 0.08);
    color: var(--warm);
    padding: 2px 8px;
    border-radius: 2px;
    text-transform: none;
    letter-spacing: 0.04em;
    font-size: 10px;
  }
  .turn.cancelled .reply {
    border-left-color: var(--st-listening);
    color: var(--fg-dim);
  }
  .turn.cancelled .reply-attr::after {
    content: 'cancelled';
    color: var(--st-listening);
  }
  .turn.overloaded .reply {
    border-left-color: var(--warm);
  }
  .turn.overloaded .reply-attr::after {
    content: 'overloaded';
    color: var(--warm);
  }

  /* ---------- Network panel ---------- */
  .panel-box {
    background: var(--bg-surface);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 26px 28px;
  }
  .net-stats {
    display: grid;
    gap: 18px;
    margin: 0;
  }
  .net-stats > div { margin: 0; }
  .net-stats dt {
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--fg-faint);
    margin-bottom: 6px;
  }
  .net-stats dd {
    margin: 0;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--fg);
    word-break: break-all;
  }
  .net-stats dd.url { color: var(--cool); }

  .peers-hd {
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--fg-faint);
    margin: 22px 0 12px;
    padding-top: 22px;
    border-top: 1px solid var(--rule);
  }
  .peers {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .peers li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 6px 0;
  }
  .peers .pip {
    color: var(--fg-dim);
    font-variant-numeric: tabular-nums;
  }
  .peer-empty {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--fg-faint);
  }
  .state-badge {
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    padding: 3px 9px;
    border-radius: 999px;
    background: rgba(255,255,255,0.04);
    color: var(--fg-dim);
    border: 1px solid var(--rule-strong);
  }
  .state-badge[data-state="idle"]       { color: var(--st-idle); }
  .state-badge[data-state="listening"]  { color: var(--st-listening);  border-color: rgba(232,99,74,0.4); background: rgba(232,99,74,0.08); }
  .state-badge[data-state="processing"] { color: var(--st-processing); border-color: rgba(240,182,89,0.4); background: rgba(240,182,89,0.08); }
  .state-badge[data-state="speaking"]   { color: var(--st-speaking);   border-color: rgba(125,222,192,0.4); background: rgba(125,222,192,0.08); }

  /* ---------- Telemetry strip ---------- */
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 18px;
  }
  @media (max-width: 600px) {
    .metrics-grid { grid-template-columns: repeat(2, 1fr); }
  }
  .metric {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .metric-label {
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--fg-faint);
  }
  .metric-value {
    font-family: var(--font-display);
    font-weight: 400;
    font-size: 32px;
    line-height: 1;
    color: var(--fg);
    font-variation-settings: 'opsz' 60;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .metric-value .unit {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--fg-faint);
    margin-left: 4px;
    letter-spacing: 0.05em;
  }

  .sparkline-wrap {
    margin-top: 18px;
    padding-top: 18px;
    border-top: 1px solid var(--rule);
  }
  .sparkline-label {
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--fg-faint);
    margin-bottom: 8px;
  }
  .sparkline {
    width: 100%;
    height: 40px;
    display: block;
  }

  /* ---------- Reminders chart ---------- */
  .chart-counters {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 24px;
    padding: 28px 32px;
    background: var(--bg-surface);
    border: 1px solid var(--rule);
    border-radius: 3px;
    margin-bottom: 18px;
  }
  @media (max-width: 600px) {
    .chart-counters { grid-template-columns: 1fr; gap: 18px; padding: 24px; }
  }
  .ctr {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .ctr-label {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--fg-faint);
  }
  .ctr-value {
    font-family: var(--font-display);
    font-weight: 400;
    font-size: 64px;
    line-height: 0.95;
    font-variation-settings: 'opsz' 144;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.025em;
  }
  .ctr.created .ctr-value { color: var(--st-speaking); }
  .ctr.fired   .ctr-value { color: var(--info); }
  .ctr.deleted .ctr-value { color: var(--st-listening); }

  .chart-frame {
    background: var(--bg-surface);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 22px 26px;
  }
  .chart-svg {
    width: 100%;
    height: 160px;
    display: block;
  }
  .chart-axis {
    display: flex;
    justify-content: space-between;
    font-family: var(--font-mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: var(--fg-faint);
    margin-top: 8px;
    padding: 0 4px;
  }
  .chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    margin-top: 18px;
    padding-top: 18px;
    border-top: 1px solid var(--rule);
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: var(--fg-dim);
  }
  .chart-legend .sw {
    display: inline-block;
    width: 10px; height: 10px;
    margin-right: 8px;
    vertical-align: middle;
    border-radius: 1px;
  }
  .chart-legend .sw.created { background: var(--st-speaking); }
  .chart-legend .sw.fired   { background: var(--info); }
  .chart-legend .sw.deleted { background: var(--st-listening); }
  .chart-legend .sw.listed  { background: var(--st-idle); }

  /* ---------- Reminders: scheduled list + activity blocks ---------- */
  .rem-block { margin-top: 22px; }
  .rem-block:first-of-type { margin-top: 18px; }
  .rem-block-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .rem-block-head > span:first-child {
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--fg-dim);
  }
  .rem-count {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--fg-faint);
  }

  .rem-list { display: flex; flex-direction: column; gap: 8px; }

  .rem-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 18px;
    padding: 15px 20px;
    background: var(--bg-surface);
    border: 1px solid var(--rule);
    border-left-width: 2px;
    border-radius: 3px;
    transition: border-color 0.3s, background 0.3s;
  }
  /* Urgency drives the left border + dot colour. */
  .rem-row[data-urgency="imminent"] { border-left-color: var(--st-listening); }
  .rem-row[data-urgency="soon"]     { border-left-color: var(--st-processing); }
  .rem-row[data-urgency="later"]    { border-left-color: var(--st-speaking); }
  .rem-row[data-urgency="overdue"]  { border-left-color: var(--fg-faint); }
  .rem-row[data-urgency="none"]     { border-left-color: var(--rule-strong); }

  .rem-dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    background: var(--st-idle);
  }
  .rem-row[data-urgency="imminent"] .rem-dot {
    background: var(--st-listening);
    box-shadow: 0 0 10px rgba(232,99,74,0.55);
    animation: pulse-cool 1.1s ease-in-out infinite;
  }
  .rem-row[data-urgency="soon"]    .rem-dot { background: var(--st-processing); box-shadow: 0 0 8px var(--warm-soft); }
  .rem-row[data-urgency="later"]   .rem-dot { background: var(--st-speaking); }
  .rem-row[data-urgency="overdue"] .rem-dot { background: var(--fg-faint); }

  .rem-body { min-width: 0; }
  .rem-text {
    font-family: var(--font-display);
    font-weight: 400;
    font-size: 18px;
    line-height: 1.25;
    color: var(--fg);
    font-variation-settings: 'opsz' 40;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rem-row[data-urgency="overdue"] .rem-text { color: var(--fg-dim); }
  .rem-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.06em;
    color: var(--fg-faint);
    margin-top: 3px;
  }
  .rem-when { text-align: right; white-space: nowrap; }
  .rem-countdown {
    font-family: var(--font-mono);
    font-size: 15px;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }
  .rem-row[data-urgency="imminent"] .rem-countdown { color: var(--st-listening); }
  .rem-row[data-urgency="soon"]     .rem-countdown { color: var(--st-processing); }
  .rem-row[data-urgency="later"]    .rem-countdown { color: var(--st-speaking); }
  .rem-row[data-urgency="overdue"]  .rem-countdown { color: var(--fg-faint); }
  .rem-abs {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.06em;
    color: var(--fg-faint);
    margin-top: 3px;
  }

  .rem-empty {
    padding: 34px 20px;
    text-align: center;
    background: var(--bg-surface);
    border: 1px dashed var(--rule-strong);
    border-radius: 3px;
  }
  .rem-empty .empty-title { font-size: 18px; margin-bottom: 8px; }

  /* ---------- Live log ---------- */
  .log {
    background: var(--bg-inset);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 16px 22px;
    margin: 0;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.65;
    height: 320px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log .ln { display: block; color: var(--fg-dim); }
  .log .ln.INFO    { color: var(--fg-dim); }
  .log .ln.DEBUG   { color: var(--fg-faint); }
  .log .ln.WARNING,
  .log .ln.WARN    { color: var(--warm); }
  .log .ln.ERROR   { color: var(--st-listening); }
  .log .ln.CRITICAL{ color: var(--st-listening); font-weight: 600; }
  .log-stats {
    margin-top: 10px;
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-faint);
  }

  /* ---------- Scrollbars ---------- */
  .transcript::-webkit-scrollbar,
  .log::-webkit-scrollbar { width: 6px; height: 6px; }
  .transcript::-webkit-scrollbar-track,
  .log::-webkit-scrollbar-track { background: transparent; }
  .transcript::-webkit-scrollbar-thumb,
  .log::-webkit-scrollbar-thumb {
    background: var(--rule-strong);
    border-radius: 3px;
  }
  .transcript::-webkit-scrollbar-thumb:hover,
  .log::-webkit-scrollbar-thumb:hover { background: var(--rule-bright); }
  .transcript, .log { scrollbar-width: thin; scrollbar-color: var(--rule-strong) transparent; }

  /* Pause state: dim the transcript so it's obvious it's frozen */
  body.paused .transcript { opacity: 0.5; }
  body.paused .conn .dot { background: var(--warm); box-shadow: 0 0 8px var(--warm-soft); }
</style>
</head>
<body>

<header class="masthead">
  <div class="brand">
    <span class="brand-mark"></span>
    <span class="brand-name">Jarviz</span>
    <span class="brand-sub">Operator Console</span>
  </div>
  <div class="meta">
    <button class="pause-btn" id="pauseBtn" type="button">Pause</button>
    <div class="conn" id="conn" data-state="">
      <span class="dot"></span>
      <span id="connText">connecting</span>
    </div>
  </div>
</header>

<main>

  <!-- I. DEVICE STATUS -->
  <section>
    <div class="sec-head">
      <span class="sec-num">I</span>
      <h2 class="sec-title">Device Status</h2>
      <span class="sec-aside" id="heroAside"></span>
    </div>
    <div id="heroGrid"></div>
  </section>

  <!-- II + IV/V: Transcript on left, Network + Telemetry on right -->
  <div class="main-grid">

    <section>
      <div class="sec-head">
        <span class="sec-num">II</span>
        <h2 class="sec-title">Transcript</h2>
        <span class="sec-aside" id="trAside"></span>
      </div>
      <div class="transcript" id="transcript"></div>
    </section>

    <div class="side">
      <section>
        <div class="sec-head">
          <span class="sec-num">IV</span>
          <h2 class="sec-title">Network</h2>
        </div>
        <div class="panel-box">
          <dl class="net-stats">
            <div><dt>Host</dt><dd id="netHost">—</dd></div>
            <div><dt>WS Endpoint</dt><dd class="url" id="netWs">—</dd></div>
            <div><dt>LAN Address</dt><dd id="netLan">—</dd></div>
            <div><dt>Listen Bind</dt><dd id="netBind">—</dd></div>
          </dl>
          <div class="peers-hd">Connected Peers</div>
          <ul class="peers" id="peers"></ul>
        </div>
      </section>

      <section>
        <div class="sec-head">
          <span class="sec-num">V</span>
          <h2 class="sec-title">Telemetry</h2>
        </div>
        <div class="panel-box">
          <div class="metrics-grid">
            <div class="metric"><div class="metric-label">Active Sessions</div><div class="metric-value" id="mActive">—</div></div>
            <div class="metric"><div class="metric-label">Turns in Flight</div><div class="metric-value" id="mInflight">—</div></div>
            <div class="metric"><div class="metric-label">Latency p50</div><div class="metric-value" id="mP50">—<span class="unit">ms</span></div></div>
            <div class="metric"><div class="metric-label">Latency p95</div><div class="metric-value" id="mP95">—<span class="unit">ms</span></div></div>
          </div>
          <div class="sparkline-wrap">
            <div class="sparkline-label">Recent Turn Times (last 50)</div>
            <svg class="sparkline" id="sparkline" viewBox="0 0 400 40" preserveAspectRatio="none"></svg>
          </div>
        </div>
      </section>
    </div>
  </div>

  <!-- III. REMINDERS -->
  <section>
    <div class="sec-head">
      <span class="sec-num">III</span>
      <h2 class="sec-title">Reminders</h2>
      <span class="sec-aside" id="remAside"></span>
    </div>

    <div class="chart-counters">
      <div class="ctr created"><span class="ctr-label">Created</span><span class="ctr-value" id="ctCreated">0</span></div>
      <div class="ctr fired"><span class="ctr-label">Fired</span><span class="ctr-value" id="ctFired">0</span></div>
      <div class="ctr deleted"><span class="ctr-label">Deleted</span><span class="ctr-value" id="ctDeleted">0</span></div>
    </div>

    <!-- Scheduled list — the actual pending reminders -->
    <div class="rem-block">
      <div class="rem-block-head">
        <span>Scheduled</span>
        <span class="rem-count" id="remCount"></span>
      </div>
      <div class="rem-list" id="remList"></div>
    </div>

    <!-- Activity over the last 48 hours -->
    <div class="rem-block">
      <div class="rem-block-head">
        <span>Activity</span>
        <span class="rem-count">last 48 hours</span>
      </div>
      <div class="chart-frame">
        <svg class="chart-svg" id="chart" viewBox="0 0 1000 120" preserveAspectRatio="none"></svg>
        <div class="chart-axis">
          <span>−48h</span>
          <span>−36h</span>
          <span>−24h</span>
          <span>−12h</span>
          <span>now</span>
        </div>
        <div class="chart-legend">
          <span><span class="sw created"></span>Created</span>
          <span><span class="sw fired"></span>Fired</span>
          <span><span class="sw deleted"></span>Deleted</span>
          <span><span class="sw listed"></span>Listed</span>
        </div>
      </div>
    </div>
  </section>

  <!-- VI. LIVE LOG -->
  <section>
    <div class="sec-head">
      <span class="sec-num">VI</span>
      <h2 class="sec-title">Console</h2>
      <span class="sec-aside" id="logAside"></span>
    </div>
    <pre class="log" id="log"></pre>
    <div class="log-stats" id="logStats"></div>
  </section>

</main>

<script>
/*
 * Jarviz Operator Console — data flow
 * ===================================
 * Polling cadence: every POLL_MS (2000) ms.
 *
 * Endpoint  ->  Panel
 * ----------------------------------------------------------------------
 * /sessions/live          ->  Section I  (Device Status hero)
 * /transcripts/recent     ->  Section II (Transcript)
 * /reminders/stats        ->  Section III (Reminders chart + counters)
 * /network                ->  Section IV (Network panel + peer list)
 * /metrics                ->  Section V  (Telemetry strip + sparkline)
 * /logs/recent?n=200      ->  Section VI (Live log tail)
 *
 * All six requests fire in parallel via Promise.all. Any single
 * non-200 marks the dashboard "disconnected"; the next tick recovers.
 * All user-supplied strings (transcripts, log lines, log messages) are
 * HTML-escaped before insertion via the esc() helper.
 */
(() => {
  'use strict';

  const POLL_MS = 2000;
  const $ = id => document.getElementById(id);

  // ---------- helpers ----------
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  const fmtInt = n => (n == null ? '—' : Number(n).toLocaleString());
  const fmtMs  = n => (n == null ? '—' : Math.round(Number(n)).toLocaleString());

  function fmtDuration(seconds) {
    if (seconds == null) return '—';
    const s = Math.max(0, Math.floor(Number(seconds)));
    if (s < 60)   return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h + 'h ' + m + 'm';
  }

  function fmtClockTime(unixSec) {
    const d = new Date(Number(unixSec) * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }

  function shortDevice(id) {
    if (!id) return '—';
    if (id.length <= 12) return id;
    return id.slice(0, 8) + '…' + id.slice(-4);
  }

  // ---------- state ----------
  let paused = false;
  let lastOkAt = 0;

  $('pauseBtn').addEventListener('click', () => {
    paused = !paused;
    $('pauseBtn').classList.toggle('active', paused);
    $('pauseBtn').textContent = paused ? 'Paused' : 'Pause';
    document.body.classList.toggle('paused', paused);
  });

  function setConn(ok, msg) {
    const el = $('conn');
    const txt = $('connText');
    if (ok) {
      lastOkAt = Date.now();
      el.dataset.state = 'ok';
      const ago = Math.max(0, Math.floor((Date.now() - lastOkAt) / 1000));
      txt.textContent = ago === 0 ? 'live' : ('live · ' + ago + 's');
    } else {
      el.dataset.state = 'err';
      txt.textContent = msg || 'disconnected';
    }
  }

  // ---------- renderers ----------
  function renderHero(sessions) {
    const grid = $('heroGrid');
    $('heroAside').textContent = sessions.length + (sessions.length === 1 ? ' device' : ' devices');
    if (!sessions || sessions.length === 0) {
      grid.innerHTML = '<div class="empty">'
        + '<div class="empty-title">Awaiting a device</div>'
        + '<div class="empty-sub">No Jarviz hardware connected</div>'
        + '</div>';
      return;
    }
    const out = sessions.map(s => {
      const st = s.state || 'idle';
      return '<article class="device-card" data-state="' + esc(st) + '">'
        + '<div class="device-head">'
        +   '<span class="device-id">' + esc(s.device_id || '—') + '</span>'
        +   '<span>' + esc(s.peer_ip || '—') + '</span>'
        + '</div>'
        + '<div class="pilot">'
        +   '<span class="pilot-light"></span>'
        +   '<span class="state-text">' + esc(st) + '</span>'
        + '</div>'
        + '<dl class="device-meta">'
        +   '<div><dt>Held for</dt><dd>' + esc(fmtDuration(s.state_age_s)) + '</dd></div>'
        +   '<div><dt>Session</dt><dd>' + esc(fmtDuration(s.session_age_s)) + '</dd></div>'
        +   '<div><dt>Client</dt><dd>' + esc(shortDevice(s.client_id || '')) + '</dd></div>'
        + '</dl>'
        + '</article>';
    }).join('');
    grid.innerHTML = out;
  }

  function renderTranscript(turns) {
    const el = $('transcript');
    $('trAside').textContent = (turns ? turns.length : 0) + ' turn' + (turns && turns.length === 1 ? '' : 's');
    if (!turns || turns.length === 0) {
      el.innerHTML = '<div class="transcript-empty">'
        + '<div class="empty-title">No exchanges yet</div>'
        + '<div class="empty-sub">Awaiting first conversation</div>'
        + '</div>';
      return;
    }
    // Stick-to-bottom unless user has scrolled away
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    const slice = turns.slice(-30);
    el.innerHTML = slice.map(t => {
      const cls = 'turn'
        + (t.cancelled ? ' cancelled' : '')
        + (t.overloaded ? ' overloaded' : '');
      const userBlock = t.user_text
        ? '<p class="said">' + esc(t.user_text) + '</p>'
          + '<div class="said-attr">' + esc(fmtClockTime(t.ts)) + ' · ' + esc(shortDevice(t.device_id)) + '</div>'
        : '';
      const lat = t.latency_ms || {};
      const toolPills = (t.tool_calls || []).map(n =>
        '<span class="tool">' + esc(n.replace(/^jarviz\\./, '')) + '</span>'
      ).join('');
      const replyBlock = t.assistant_text
        ? '<p class="reply">' + esc(t.assistant_text) + '</p>'
          + '<div class="reply-attr">'
          +   '<span class="lat">' + esc(fmtMs(lat.total)) + 'ms</span>'
          +   (toolPills || '')
          + '</div>'
        : '';
      return '<article class="' + cls + '">' + userBlock + replyBlock + '</article>';
    }).join('');
    if (stick) el.scrollTop = el.scrollHeight;
  }

  function remUrgency(s) {
    if (s == null) return 'none';
    if (s < 0) return 'overdue';
    if (s < 60) return 'imminent';
    if (s < 3600) return 'soon';
    return 'later';
  }

  function fmtCountdown(s) {
    if (s == null) return 'no time set';
    if (s < 0) return 'overdue';
    if (s < 60) return 'in ' + Math.max(1, Math.round(s)) + 's';
    if (s < 3600) return 'in ' + Math.floor(s / 60) + 'm';
    if (s < 86400) {
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
      return 'in ' + h + 'h' + (m ? ' ' + m + 'm' : '');
    }
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
    return 'in ' + d + 'd' + (h ? ' ' + h + 'h' : '');
  }

  function fmtRemAbs(r) {
    // Prefer the device's own local_time string; fall back to deriving a
    // clock time from the unix timestamp.
    if (r.local_time) {
      const t = String(r.local_time).split('T')[1];
      if (t) return t.slice(0, 5);   // HH:MM
      return r.local_time;
    }
    if (r.unix_timestamp) return fmtClockTime(r.unix_timestamp);
    return '';
  }

  function renderReminderList(pending) {
    const list = $('remList');
    const count = pending ? pending.length : 0;
    $('remCount').textContent = count === 0 ? '' : count + (count === 1 ? ' pending' : ' pending');
    if (count === 0) {
      list.innerHTML = '<div class="rem-empty">'
        + '<div class="empty-title">Nothing scheduled</div>'
        + '<div class="empty-sub">Say &ldquo;remind me to&hellip;&rdquo; to the device</div>'
        + '</div>';
      return;
    }
    list.innerHTML = pending.map(r => {
      const u = remUrgency(r.fires_in_s);
      const dev = r.device_id ? shortDevice(r.device_id) : 'device';
      const abs = fmtRemAbs(r);
      const subBits = [dev];
      if (abs) subBits.push(abs);
      return '<div class="rem-row" data-urgency="' + esc(u) + '">'
        + '<span class="rem-dot"></span>'
        + '<div class="rem-body">'
        +   '<div class="rem-text">' + esc(r.text || '(untitled)') + '</div>'
        +   '<div class="rem-sub">#' + esc(r.id) + ' &middot; ' + esc(subBits.join(' \\u00b7 ')) + '</div>'
        + '</div>'
        + '<div class="rem-when">'
        +   '<div class="rem-countdown">' + esc(fmtCountdown(r.fires_in_s)) + '</div>'
        +   (abs ? '<div class="rem-abs">' + esc(abs) + '</div>' : '')
        + '</div>'
        + '</div>';
    }).join('');
  }

  function renderReminders(data) {
    const counters = (data && data.counters) || {};
    $('ctCreated').textContent = fmtInt(counters.created_total || 0);
    $('ctFired').textContent   = fmtInt(counters.fired_total   || 0);
    $('ctDeleted').textContent = fmtInt(counters.deleted_total || 0);

    renderReminderList((data && data.pending) || []);
    const pc = data && data.pending ? data.pending.length : 0;
    $('remAside').textContent = pc === 0 ? 'none scheduled'
      : pc + (pc === 1 ? ' scheduled' : ' scheduled');

    const hourly = (data && data.hourly) || [];
    const svg = $('chart');
    const W = 1000, H = 120, padX = 4, padY = 8;
    const n = hourly.length || 1;
    const colW = (W - padX * 2) / n;
    const innerH = H - padY * 2;

    let maxStack = 1;
    for (const b of hourly) {
      const s = (b.created || 0) + (b.fired || 0) + (b.deleted || 0) + (b.listed || 0);
      if (s > maxStack) maxStack = s;
    }

    const series = [
      { key: 'listed',  color: '#6e645a' },
      { key: 'deleted', color: '#e8634a' },
      { key: 'fired',   color: '#6db9e8' },
      { key: 'created', color: '#7ddec0' },
    ];

    let body = '';
    // Faint baseline
    body += '<line x1="' + padX + '" y1="' + (H - padY) + '" x2="' + (W - padX) + '" y2="' + (H - padY)
         + '" stroke="rgba(244,237,224,0.08)" stroke-width="1"/>';

    if (maxStack === 1 && hourly.every(b => (b.created||0)+(b.fired||0)+(b.deleted||0)+(b.listed||0) === 0)) {
      body += '<text x="' + (W/2) + '" y="' + (H/2) + '" text-anchor="middle" '
           + 'font-family="ui-monospace,monospace" font-size="11" fill="rgba(244,237,224,0.32)" '
           + 'letter-spacing="3" textLength="220">NO REMINDER ACTIVITY</text>';
      svg.innerHTML = body;
      return;
    }

    hourly.forEach((b, i) => {
      const x = padX + i * colW;
      let yBottom = H - padY;
      const barW = Math.max(0.8, colW - 1.2);
      for (const s of series) {
        const v = b[s.key] || 0;
        if (v <= 0) continue;
        const h = (v / maxStack) * innerH;
        const y = yBottom - h;
        body += '<rect x="' + (x + 0.6) + '" y="' + y.toFixed(2) + '" '
             + 'width="' + barW.toFixed(2) + '" height="' + h.toFixed(2) + '" '
             + 'fill="' + s.color + '"/>';
        yBottom = y;
      }
    });
    svg.innerHTML = body;
  }

  function renderNetwork(data) {
    if (!data) data = {};
    $('netHost').textContent = data.hostname || '—';
    $('netWs').textContent   = data.ws_public_url || '—';
    const lan = Array.isArray(data.lan_addresses) ? data.lan_addresses : [];
    $('netLan').innerHTML = lan.length
      ? lan.map(esc).join('<br>')
      : '<span style="color:var(--fg-faint)">—</span>';
    $('netBind').textContent = (data.listen_host || '0.0.0.0') + ':' + (data.listen_port || '?');

    const peers = data.connected_peers || [];
    const ul = $('peers');
    if (peers.length === 0) {
      ul.innerHTML = '<li class="peer-empty">no peers</li>';
      return;
    }
    ul.innerHTML = peers.map(p =>
      '<li>'
      + '<span class="pip">' + esc(p.peer_ip || '—') + '</span>'
      + '<span class="state-badge" data-state="' + esc(p.state || 'idle') + '">' + esc(p.state || 'idle') + '</span>'
      + '</li>'
    ).join('');
  }

  function renderMetrics(m) {
    if (!m) return;
    const sess = m.sessions || {};
    const turns = m.turns || {};
    const lat = m.latency_ms || {};
    $('mActive').textContent   = fmtInt(sess.active);
    $('mInflight').textContent = fmtInt(turns.in_flight);
    $('mP50').innerHTML = fmtMs(lat.total_p50) + '<span class="unit">ms</span>';
    $('mP95').innerHTML = fmtMs(lat.total_p95) + '<span class="unit">ms</span>';
    renderSparkline(lat.recent_totals || []);
  }

  function renderSparkline(samples) {
    const svg = $('sparkline');
    if (!samples || samples.length === 0) {
      svg.innerHTML = '<text x="50%" y="55%" text-anchor="middle" '
        + 'font-family="ui-monospace,monospace" font-size="9" fill="rgba(244,237,224,0.28)" '
        + 'letter-spacing="3">NO SAMPLES YET</text>';
      return;
    }
    const W = 400, H = 40, pad = 3;
    const max = Math.max.apply(null, samples.concat([1]));
    const step = (W - pad * 2) / Math.max(samples.length - 1, 1);
    let path = '';
    samples.forEach((v, i) => {
      const x = pad + i * step;
      const y = H - pad - (v / max) * (H - pad * 2);
      path += (i === 0 ? 'M' : 'L') + x.toFixed(2) + ',' + y.toFixed(2);
    });
    const lastX = pad + (samples.length - 1) * step;
    const area = path + 'L' + lastX.toFixed(2) + ',' + (H - pad) + 'L' + pad + ',' + (H - pad) + 'Z';
    svg.innerHTML =
      '<path d="' + area + '" fill="#f0b659" fill-opacity="0.14"/>' +
      '<path d="' + path + '" fill="none" stroke="#f0b659" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>';
  }

  function renderLog(records) {
    const log = $('log');
    if (!records || records.length === 0) {
      log.innerHTML = '<span style="color:var(--fg-faint);font-family:ui-monospace,monospace;font-size:10px;letter-spacing:0.15em">NO LOG RECORDS YET</span>';
      $('logStats').textContent = '';
      return;
    }
    const stick = log.scrollTop + log.clientHeight + 30 >= log.scrollHeight;
    let warn = 0, err = 0;
    const html = records.map(r => {
      const lvl = (r.level || 'INFO').toUpperCase();
      if (lvl === 'WARNING' || lvl === 'WARN') warn++;
      else if (lvl === 'ERROR' || lvl === 'CRITICAL') err++;
      return '<span class="ln ' + esc(lvl) + '">' + esc(r.line || '') + '</span>';
    }).join('\\n');
    log.innerHTML = html;
    if (stick) log.scrollTop = log.scrollHeight;
    $('logAside').textContent = records.length + ' lines';
    $('logStats').textContent = records.length + ' records · '
      + warn + ' warning' + (warn === 1 ? '' : 's') + ' · '
      + err + ' error' + (err === 1 ? '' : 's');
  }

  // ---------- polling ----------
  async function fetchJson(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(url + ' -> HTTP ' + r.status);
    return r.json();
  }

  async function tick() {
    if (paused) return;
    try {
      const [metrics, sessions, transcripts, reminders, network, logs] = await Promise.all([
        fetchJson('/metrics'),
        fetchJson('/sessions/live'),
        fetchJson('/transcripts/recent?n=30'),
        fetchJson('/reminders/stats'),
        fetchJson('/network'),
        fetchJson('/logs/recent?n=200'),
      ]);
      renderHero(sessions.sessions || []);
      renderTranscript(transcripts.transcripts || []);
      renderReminders(reminders);
      renderNetwork(network);
      renderMetrics(metrics);
      renderLog((logs && logs.records) || []);
      setConn(true);
    } catch (e) {
      setConn(false, (e && e.message) ? e.message : 'connection failed');
    }
  }

  // Initial paint so panels never start blank with broken layout
  renderHero([]);
  renderTranscript([]);
  renderReminders({});
  renderNetwork({});
  renderLog([]);

  tick();
  setInterval(tick, POLL_MS);

  // Keep the "live · Ns ago" counter ticking smoothly between polls
  setInterval(() => {
    if ($('conn').dataset.state === 'ok' && !paused) {
      const ago = Math.max(0, Math.floor((Date.now() - lastOkAt) / 1000));
      $('connText').textContent = ago === 0 ? 'live' : ('live · ' + ago + 's');
    }
  }, 1000);
})();
</script>
</body>
</html>
"""
