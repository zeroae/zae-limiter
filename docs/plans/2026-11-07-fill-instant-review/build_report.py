"""Builds report.html from results.json (self-contained, no network)."""

import json

R = json.load(open("results.json"))

# --- extra illustrative series -------------------------------------------------
ceiling, r = 1000.0, 1000 / 60
adj = {"strict": [], "proposed": []}
z_two, z_prop, b_strict, t_prev = None, None, None, None
ts = [i / 4 for i in range(0, 4 * 135 + 1)]
# events: t=0 admit 100, t=120 adjust +900, t=120.5 request 1000
z_two = z_prop = 0.0 + 100 / r
for t in ts:
    if abs(t - 120.0) < 1e-9:
        z_two = max(z_two, t) + 900 / r
        z_prop = z_prop + 900 / r
    if abs(t - 120.5) < 1e-9:
        if z_two <= t + (ceiling - 1000) / r:
            z_two = max(z_two, t) + 1000 / r
        if z_prop <= t + (ceiling - 1000) / r:
            z_prop = max(z_prop, t) + 1000 / r
    adj["strict"].append([t, ceiling - r * max(0.0, z_two - t)])
    adj["proposed"].append([t, ceiling - r * max(0.0, z_prop - t)])
R["adjust_demo"] = adj

sched = {"integrated": [], "today": []}
for i in range(0, 621):
    t = -60 + i * 0.1
    lo_ceiling, lo_r, hi_ceiling, hi_r = 100.0, 100 / 60, 1000.0, 1000 / 60
    if t < 0:
        integ = min(lo_ceiling, lo_r * (t + 60))
        today = integ
    else:
        integ = min(hi_ceiling, min(lo_ceiling, lo_r * 60) + hi_r * t)
        today = min(hi_ceiling, hi_r * (t + 60))
    sched["integrated"].append([round(t, 2), integ])
    sched["today"].append([round(t, 2), today])
R["sched_demo"] = sched

# trim big logs we do not plot
for k, v in list(R.items()):
    if isinstance(v, dict) and "rows" in v:
        for row in v["rows"]:
            row.pop("e_all", None)

real = R["real_808"]
cum = 0
real_pts, tk_pts = [], []
for e in real["trace"]:
    cum += e["admitted"]
    real_pts.append([e["t_ms"], cum])
    tk_pts.append([e["t_ms"], e["tk"]])
R["real_cum"] = real_pts
R["real_tk"] = tk_pts
bm = R["burst_model"]
R["model_fill"] = [[round(t * 1000), a] for t, a in zip(bm["t"], bm["fill"])]
R["model_bound"] = [[round(t * 1000), b] for t, b in zip(bm["t"], bm["bound"])]
del R["burst_model"]
del R["real_808"]

scen = [
    ("fill/base", "16 consumers, 20 ms skew, returns + debt", "design"),
    ("fill/skew2s-flight0.5s", "2 s skew, up to 0.5 s in flight", "design"),
    ("fill/bursty-overload", "bursty on/off, overloaded, 32 consumers", "design"),
    ("fill/L4-wcu-hovering", "4 limits, wcu hovering at full, 950 req/s", "design"),
    ("fill/L4-wcu-extrapolate", "same, extrapolating predictor", "design"),
    ("fill/adversary-returns", "a return lands before every retry", "design"),
    ("fill/soft+hard", "soft limit beside a hard limit", "design"),
    ("fill/adjust two-write", "positive adjustments as forced debits", "design"),
    ("fill/schedule", "5-window schedule 0.1x to 2x, idle gaps", "design"),
    ("fill/schedule-skew1s", "same, 1 s skew", "design"),
    ("fill/bursty-idle", "30 s bursts after 15 min idle", "design"),
    ("BROKEN today", "control: today's fast + slow path (#808)", "broken"),
    ("BROKEN always-set", "control: always set z = t + d", "broken"),
    ("BROKEN adjust z+=d (as proposed)", "finding: adjustment as written in the proof", "broken"),
    ("BROKEN no c<=C guard (sched)", "finding: F write without c <= C", "broken"),
]
bars = []
for key, label, grp in scen:
    v = R[key]
    bars.append(
        {
            "key": key,
            "label": label,
            "group": grp,
            "value": v["worst_ratio"],
            "as_written": v.get("worst_ratio_as_written"),
            "max_writes": v["max_writes"],
            "admitted": v["admitted"],
            "runs": len(v["rows"]),
        }
    )
bars.append(
    {
        "key": "shards",
        "label": "shard splits 1 to 32 racing writes",
        "group": "design",
        "value": R["shards"]["worst_ratio"],
        "as_written": R["shards"]["worst_ratio"],
        "max_writes": None,
        "admitted": None,
        "runs": 40,
    }
)
bars.append(
    {
        "key": "shards_skew",
        "label": "shard splits, 1 s skew",
        "group": "design",
        "value": R["shards_skew"]["worst_ratio"],
        "as_written": R["shards_skew"]["worst_ratio"],
        "max_writes": None,
        "admitted": None,
        "runs": 40,
    }
)
bars.append(
    {
        "key": "shards_naive",
        "label": "control: new shard starts full (ADR-133)",
        "group": "broken",
        "value": R["shards_naive"]["worst_ratio"],
        "as_written": R["shards_naive"]["worst_ratio"],
        "max_writes": None,
        "admitted": None,
        "runs": 40,
    }
)
R["bars"] = bars
for k in [k for k in R if k.startswith("fill/") or k.startswith("BROKEN")]:
    del R[k]
R.pop("hist_L4", None)

html = open("template.html").read().replace("/*__DATA__*/null", json.dumps(R))
open("report.html", "w").write(html)
print("report.html", len(html) // 1024, "KB")
