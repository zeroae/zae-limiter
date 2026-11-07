"""Fuzz campaign over sim.py. Writes results.json for the report."""

import json
import time

from sim import (
    Cfg,
    burst_808,
    kink_data,
    run,
    run_shards,
    schedule_boundary,
    sliding_max,
    theorem3c_adversary,
    theorem4_adversary,
)

R = {}
t0 = time.time()


def summarise(name, cfgs):
    rows = []
    for cfg in cfgs:
        res = run(cfg)
        rows.append(
            {
                "seed": cfg.seed,
                "s": res["s"],
                "ratio": [lim["ratio"] for lim in res["limits"]],
                "ratio_as_written": [lim["ratio_as_written"] for lim in res["limits"]],
                "excess": [lim["excess"] for lim in res["limits"]],
                "two_r_s": [lim["two_r_s"] for lim in res["limits"]],
                "max_writes": res["max_writes"],
                "outcomes": res["outcomes"],
                "first_wrong": res["first_wrong"],
                "e_over_s": res["e_max"] / res["s"] if res["s"] else None,
            }
        )
    hard = [i for i, sft in enumerate(cfgs[0].soft) if not sft]
    worst = max(r["ratio"][i] for r in rows for i in hard)
    worst_w = max(r["ratio_as_written"][i] for r in rows for i in hard)
    R[name] = {
        "rows": rows,
        "worst_ratio": worst,
        "worst_ratio_as_written": worst_w,
        "max_writes": max(r["max_writes"] for r in rows),
        "contended": sum(r["outcomes"]["contended"] for r in rows),
        "admitted": sum(r["outcomes"]["admitted"] for r in rows),
    }
    print(
        f"{name:34s} worst excess/2rs={worst:9.3f} (as written {worst_w:9.3f})  "
        f"max writes={R[name]['max_writes']}  admitted={R[name]['admitted']} "
        f"contended={R[name]['contended']}  ({time.time() - t0:.0f}s)"
    )
    return rows


N = 40
# 1. baseline: 16 consumers, 20 ms skew, returns + debt, idle-heavy Poisson
summarise("fill/base", [Cfg(seed=s) for s in range(N)])
# 2. broken variants on the same traffic
summarise(
    "BROKEN today",
    [Cfg(seed=s, variant="today", bursty=True, burst=(30.0, 900.0), rate=1.0) for s in range(N)],
)
summarise(
    "fill/bursty-idle", [Cfg(seed=s, bursty=True, burst=(30.0, 900.0), rate=1.0) for s in range(N)]
)
summarise("BROKEN always-set", [Cfg(seed=s, variant="set") for s in range(N)])
summarise(
    "BROKEN adjust z+=d (as proposed)",
    [
        Cfg(
            seed=s,
            n_lim=2,
            ceiling=(100.0, 100.0),
            r=(100 / 60, 100 / 60),
            soft=(False, False),
            cost=((1, 20), (1, 20)),
            rate=0.3,
            p_adjust=0.3,
            adjust_mode="add",
        )
        for s in range(N)
    ],
)
summarise(
    "fill/adjust two-write",
    [
        Cfg(
            seed=s,
            n_lim=2,
            ceiling=(100.0, 100.0),
            r=(100 / 60, 100 / 60),
            soft=(False, False),
            cost=((1, 20), (1, 20)),
            rate=0.3,
            p_adjust=0.3,
        )
        for s in range(N)
    ],
)
# 3. large/variable flight times, big skew, slow clocks
summarise("fill/skew2s-flight0.5s", [Cfg(seed=s, skew=2.0, one_way=(0.0, 0.5)) for s in range(N)])
# 4. overload + bursty
summarise(
    "fill/bursty-overload", [Cfg(seed=s, bursty=True, rate=0.5, consumers=32) for s in range(N)]
)
summarise(
    "BROKEN no c<=C guard (sched)",
    [
        Cfg(
            seed=s,
            schedule=(300.0, [0.1, 1.0, 0.5, 2.0, 0.25]),
            bursty=True,
            burst=(300.0, 900.0),
            rate=0.4,
            guard_c_le_ceiling=False,
        )
        for s in range(N)
    ],
)
# 5. four limits incl. wcu hovering at full (u ~ 1)
summarise(
    "fill/L4-wcu-hovering",
    [
        Cfg(
            seed=s,
            n_lim=4,
            ceiling=(3000.0, 50_000.0, 600.0, 1000.0),
            r=(3000 / 60, 50_000 / 60, 600 / 60, 1000.0),
            soft=(False,) * 4,
            cost=((1, 5), (10, 100), (1, 1), (1, 1)),
            subset_p=0.9,
            rate=950.0,
            duration=40.0,
            consumers=32,
            p_return=0.2,
            p_adjust=0.05,
            hold=(0, 0.2),
            predictor="image",
        )
        for s in range(12)
    ],
)
summarise(
    "fill/L4-wcu-extrapolate",
    [
        Cfg(
            seed=s,
            n_lim=4,
            ceiling=(3000.0, 50_000.0, 600.0, 1000.0),
            r=(3000 / 60, 50_000 / 60, 600 / 60, 1000.0),
            soft=(False,) * 4,
            cost=((1, 5), (10, 100), (1, 1), (1, 1)),
            subset_p=0.9,
            rate=950.0,
            duration=40.0,
            consumers=32,
            p_return=0.0,
            p_adjust=0.0,
            predictor="extrapolate",
            cap=50,
        )
        for s in range(12)
    ],
)
# 6. adversarial returns between every retry: must cap and never over-admit
summarise(
    "fill/adversary-returns",
    [Cfg(seed=s, adversary_returns=True, rate=2.0, cap=6, consumers=32) for s in range(N)],
)
# 7. soft limit beside a hard limit on the same write: the hard one must hold
summarise(
    "fill/soft+hard",
    [
        Cfg(
            seed=s,
            n_lim=2,
            ceiling=(100.0, 100.0),
            r=(100 / 60, 100 / 60),
            soft=(True, False),
            cost=((1, 20), (1, 20)),
            rate=0.3,
        )
        for s in range(N)
    ],
)
# 8. schedule windows, several boundaries per idle gap, pending credits across them
SCH = (300.0, [0.1, 1.0, 0.5, 2.0, 0.25])
summarise(
    "fill/schedule",
    [Cfg(seed=s, schedule=SCH, bursty=True, burst=(300.0, 900.0), rate=0.4) for s in range(N)],
)
summarise(
    "fill/schedule-skew1s",
    [
        Cfg(
            seed=s,
            schedule=SCH,
            bursty=True,
            burst=(300.0, 900.0),
            rate=0.4,
            skew=1.0,
            one_way=(0.0, 0.3),
        )
        for s in range(N)
    ],
)

# Theorem 1 trace for the chart (one run, fixed params)
r = run(
    Cfg(
        seed=7,
        skew=0.5,
        one_way=(0.0, 0.2),
        bursty=True,
        burst=(120.0, 240.0),
        rate=0.12,
        consumers=24,
    )
)
step = max(1, len(r["e_trace"]) // 1500)
R["e_trace"] = {"s": r["s"], "pts": r["e_trace"][::step]}

# Sliding-window curves: fill vs broken on identical traffic
widths = [0.01, 0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200]
curves = {}
for v in ("fill", "today", "set"):
    res = run(Cfg(seed=3, variant=v, bursty=True, burst=(30.0, 900.0), rate=1.0))
    curves[v] = sliding_max(res["log0"], widths)
    curves[v + "_s"] = res["s"]
R["sliding"] = {"widths": widths, "C": 100.0, "r": 100 / 60, **curves}

# Writes histograms
R["hist_L4"] = R["fill/L4-wcu-hovering"]["rows"][0]
R["thm3c"] = theorem3c_adversary(12)
R["thm4"] = [
    {
        "L": n_lim,
        "full_first": theorem4_adversary(n_lim, "full"),
        "notfull_first": theorem4_adversary(n_lim, "notfull"),
    }
    for n_lim in range(1, 7)
]

# 9. shards: splits racing fast writes, with skew
sh = [run_shards(seed=s) for s in range(N)]
R["shards"] = {
    "worst_ratio": max(x["ratio"] for x in sh),
    "splits": sum(x["splits"] for x in sh),
    "retries": sum(x["split_retries"] for x in sh),
    "max_conservation_err": max(
        abs(c["before"] - c["after"]) for x in sh for c in x["conservation"]
    ),
    "conservation": sh[0]["conservation"],
}
shn = [run_shards(seed=s, naive_split=True) for s in range(N)]
R["shards_naive"] = {
    "worst_ratio": max(x["ratio"] for x in shn),
    "excess": max(x["excess"] for x in shn),
}
shs = [
    run_shards(seed=s, skew=1.0, one_way=(0.0, 0.3), rate=5.0, split_every=300.0) for s in range(N)
]
R["shards_skew"] = {"worst_ratio": max(x["ratio"] for x in shs)}
print(
    "shards",
    R["shards"]["worst_ratio"],
    R["shards"]["splits"],
    R["shards"]["retries"],
    R["shards"]["max_conservation_err"],
    "naive",
    R["shards_naive"],
    "skew",
    R["shards_skew"],
)

# 10. predictor table (proof §6): 300/4h, 1-token, instantaneous writes, no skew
pred = []
r_ = 300 / 14_400
for u in (0.1, 0.5, 0.9):
    for n in (4, 32):
        row = {"u": u, "consumers": n}
        for p in ("image", "notfull", "extrapolate"):
            res = run(
                Cfg(
                    seed=11,
                    ceiling=(300.0,),
                    r=(r_,),
                    cost=((1, 1),),
                    rate=u * r_,
                    duration=60_000 / (u * r_),
                    consumers=n,
                    skew=0.0,
                    one_way=(1e-6, 2e-6),
                    p_return=0.0,
                    p_adjust=0.0,
                    predictor=p,
                )
            )
            row[p] = res["first_wrong"]
        pred.append(row)
        print("pred", row)
R["pred"] = pred
# bursty variant of the predictor table
predb = []
for n in (4, 32):
    row = {"consumers": n}
    for p in ("image", "notfull", "extrapolate"):
        res = run(
            Cfg(
                seed=12,
                ceiling=(300.0,),
                r=(r_,),
                cost=((1, 1),),
                rate=0.5 * r_,
                duration=60_000 / (0.5 * r_),
                consumers=n,
                skew=0.0,
                one_way=(1e-6, 2e-6),
                p_return=0.0,
                p_adjust=0.0,
                predictor=p,
                bursty=True,
                burst=(14_400.0, 43_200.0),
            )
        )
        row[p] = res["first_wrong"]
    predb.append(row)
    print("pred-bursty", row)
R["pred_bursty"] = predb

R["kink"] = kink_data()
R["burst_model"] = burst_808()
R["real_808"] = json.load(open("real_808.json"))
R["schedule_boundary"] = schedule_boundary()
json.dump(R, open("results.json", "w"))
print("done", time.time() - t0)
