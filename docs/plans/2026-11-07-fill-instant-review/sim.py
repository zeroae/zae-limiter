"""Independent model of the ADR-154/155 fill-instant design, written to try to break it.

Everything is over the reals, as in docs/contributing/fill-instant-proofs.md. Time in seconds.
A limit's state on one item is its fill instant z: balance b(t) = C - r*max(0, z - t).

Writes (proof §2):
  N_t : cond t <= z <= t + (C-c)/r      update z += c/r
  F_t : cond z < t                       update z = t + c/r
  ret : no cond                          update z -= c/r
Broken variants (to show the checker has teeth):
  "tk"  : admit iff z <= t + slack, always z += c/r   (today's fast path, #808)
  "set" : admit iff z <= t + slack, always z = t + c/r
"""

from __future__ import annotations

import heapq
import json
import math
import random
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Window checker: the ADR-153 bound, measured exactly from an event log.
# ---------------------------------------------------------------------------


def window_excess(events, cap_at, rcum):
    """Max over windows [T_i, T_j] (i a debit, j an admission) of
    net(i..j) - C(T_i) - R(T_i, T_j).  events: list of (T, delta, is_admission)
    sorted by T; delta > 0 debit, < 0 return.  Returns (excess, (Ti, Tj))."""
    best = -math.inf
    where = None
    running_min = math.inf
    arg_min_t = None
    prefix = 0.0
    for tr, delta, is_adm in events:
        if delta > 0:
            v = prefix - rcum(tr) + cap_at(tr)
            if v < running_min:
                running_min, arg_min_t = v, tr
        prefix += delta
        if is_adm and running_min < math.inf:
            ex = prefix - rcum(tr) - running_min
            if ex > best:
                best, where = ex, (arg_min_t, tr)
    return best, where


def sliding_max(events, widths):
    """Max net debit in any window of each width ending at an admission."""
    out = []
    times = [e[0] for e in events]
    for w in widths:
        lo, net, best = 0, 0.0, 0.0
        for hi, (tr, delta, is_adm) in enumerate(events):
            net += delta
            while times[lo] < tr - w:
                net -= events[lo][1]
                lo += 1
            if is_adm:
                best = max(best, net)
        out.append(best)
    return out


# ---------------------------------------------------------------------------
# Experiment 1: the kink (Theorem 2) - static geometry.
# ---------------------------------------------------------------------------


def kink_data(ceiling=10.0, r=1.0, c=3.0):
    d, slack = c / r, (ceiling - c) / r
    xs = [x / 20 for x in range(-200, int(slack * 20) + 1)]  # z - t
    return {
        "C": ceiling,
        "r": r,
        "c": c,
        "d": d,
        "slack": slack,
        "x": xs,
        "exact": [max(x, 0) + d for x in xs],
        "N": [x + d for x in xs],
        "F": [d for _ in xs],
        "safe1": [ceiling / r for _ in xs],
    }


# ---------------------------------------------------------------------------
# Experiment 2: #808, one consumer, today's tk/rf model vs strict vs fill instant.
# ---------------------------------------------------------------------------


def burst_808(ceiling=300.0, r=300 / 14_400, n=700, dt=0.001):
    """Idle a day, then n requests at 1/dt per second."""
    # today's model: fast path ADD -c under tk>=c (rf untouched); slow path clamps
    tk, rf = ceiling, -86_400.0
    zi = -86_400.0  # fill instant (bucket full)
    b_strict, t_strict = ceiling, 0.0
    out = {"t": [], "today": [], "fill": [], "strict": [], "bound": []}
    a_today = a_fill = a_strict = 0
    for k in range(1, n + 1):
        t = k * dt
        # today
        if tk >= 1:
            tk -= 1
            a_today += 1
        else:
            tk2 = min(ceiling, tk + r * (t - rf))
            rf = t
            if tk2 >= 1:
                tk = tk2 - 1
                a_today += 1
            else:
                tk = tk2
        # fill instant
        if zi <= t + (ceiling - 1) / r:
            zi = max(zi, t) + 1 / r
            a_fill += 1
        # strict reference
        b_strict = min(ceiling, b_strict + r * (t - t_strict))
        t_strict = t
        if b_strict >= 1:
            b_strict -= 1
            a_strict += 1
        out["t"].append(t)
        out["today"].append(a_today)
        out["fill"].append(a_fill)
        out["strict"].append(a_strict)
        out["bound"].append(ceiling + r * t)
    return out


# ---------------------------------------------------------------------------
# Experiment 3: many consumers, one item, L limits, clock offsets, latency,
# returns, optional schedule windows.  Event-driven.
# ---------------------------------------------------------------------------


@dataclass
class Cfg:
    seed: int = 0
    consumers: int = 16
    n_lim: int = 1
    ceiling: tuple = (100.0,)
    r: tuple = (100 / 60,)
    soft: tuple = (False,)
    cost: tuple = ((1, 20),)  # per limit (lo, hi), uniform int
    subset_p: float = 1.0  # probability a request names each limit beyond the first
    duration: float = 3_600.0
    rate: float = 0.2  # mean request rate (1/s), Poisson
    bursty: bool = False  # on/off: idle gaps with dense bursts
    skew: float = 0.020  # clock offset uniform in [-skew, skew]
    one_way: tuple = (0.001, 0.015)  # one-way delay, uniform
    p_return: float = 0.3  # an admitted lease returns part of its cost
    p_adjust: float = 0.1  # an admitted lease adds debt
    hold: tuple = (0.0, 5.0)
    predictor: str = "image"  # image | extrapolate | full | notfull
    variant: str = "fill"  # fill | tk | set
    cap: int = 6
    # schedule: list of (period_s, [scale,...]) cycling; None = fixed params
    schedule: tuple | None = None
    adversary_returns: bool = False  # land a return between each retry of every request
    adjust_mode: str = (
        "fn"  # add: z += d (as proposed) | fn: forced debit via N'/F' (z = max(z,t) + d)
    )
    guard_c_le_ceiling: bool = (
        True  # client refuses c > C before sending F (proof §1 assumes c < C)
    )
    burst: tuple = (600.0, 5400.0)  # bursty: (on seconds, off seconds)


def scale_at(cfg, t):
    if cfg.schedule is None:
        return 1.0, 0
    period, scales = cfg.schedule
    k = math.floor(t / period)
    return scales[k % len(scales)], k


def params(cfg, i, k):
    if cfg.schedule is None:
        return cfg.ceiling[i], cfg.r[i]
    s = cfg.schedule[1][k % len(cfg.schedule[1])]
    return cfg.ceiling[i] * s, cfg.r[i] * s


@dataclass
class Item:
    z: list
    k: int  # schedule window the z's are expressed in (the pin)
    version: int = 0


def reexpress(cfg, z, k_from, k_to):
    """ADR-155 re-expression at every boundary between windows k_from and k_to."""
    z = list(z)
    if cfg.schedule is None:
        return z
    period = cfg.schedule[0]
    for k in range(k_from + 1, k_to + 1):
        tau = k * period
        for i in range(len(z)):
            ceiling, r = params(cfg, i, k - 1)
            ceiling2, r2 = params(cfg, i, k)
            b = ceiling - r * max(0.0, z[i] - tau)
            z[i] = tau + (ceiling2 - min(b, ceiling2)) / r2
    return z


def run(cfg: Cfg):
    rng = random.Random(cfg.seed)
    n_lim = cfg.n_lim
    item = Item(z=[0.0] * n_lim, k=0)
    tk = [cfg.ceiling[i] for i in range(n_lim)]  # variant "today": stored balance
    rf = [0.0] * n_lim  # and last refill (shared rf on the real item; per limit here)
    theta = [rng.uniform(-cfg.skew, cfg.skew) for _ in range(cfg.consumers)]
    images = [None] * cfg.consumers  # (z list, k, real T of image, stamp-ish)
    q = []
    seq = [0]

    def push(tr, kind, data):
        seq[0] += 1
        heapq.heappush(q, (tr, seq[0], kind, data))

    # arrivals
    tr = 0.0
    while True:
        if cfg.bursty:
            # on/off: ON for burst[0] s at a rate that keeps the long-run mean at cfg.rate
            on, off = cfg.burst
            per = math.floor(tr / (on + off))
            mult = random.Random(cfg.seed * 100_003 + per).choice(
                [0.003, 1.0, 2.0]
            )  # light/normal/heavy
            tr += rng.expovariate(cfg.rate * mult * (on + off) / on)
            if (tr % (on + off)) > on:
                tr += (on + off) - (tr % (on + off))
        else:
            tr += rng.expovariate(cfg.rate)
        if tr > cfg.duration:
            break
        push(tr, "issue", None)

    log = [[] for _ in range(n_lim)]  # per limit (T, delta, is_adm)
    offsets = []
    e_trace = []  # (T, e) limit 0, fixed params only
    e_all = []
    y = [0.0] * n_lim  # reference buckets (fixed params only)
    writes_hist = {}
    outcomes = {"admitted": 0, "rejected": 0, "contended": 0}
    side_wrong = 0
    first_writes = 0
    util_est = [0.0] * n_lim

    def one_way():
        return rng.uniform(*cfg.one_way)

    def record_applied(t_land, t, kind, i, cost):
        o = t - t_land
        offsets.append(abs(o))
        if cfg.schedule is None:
            d = cost / cfg.r[i]
            if kind == "adm":
                y[i] = max(y[i], t_land) + d
            elif kind == "adj":
                y[i] = max(y[i], t_land) + d  # strict: b <- b - c
            else:
                y[i] -= d
            if i == 0:
                e_trace.append((t_land, item.z[0] - y[0]))
            e_all.append((i, t_land, item.z[i] - y[i], kind, o))

    def choose(img_z, img_tr, t, costs, guess_mode):
        kinds = {}
        for i in costs:
            zi = img_z[i]
            if guess_mode == "full":
                kinds[i] = "F"
            elif guess_mode == "notfull":
                kinds[i] = "N"
            elif guess_mode == "extrapolate" and img_tr is not None:
                u = min(0.99, util_est[i])
                kinds[i] = "F" if zi + u * max(0.0, t - img_tr) < t else "N"
            else:
                kinds[i] = "F" if zi < t else "N"
        return kinds

    # requests are coroutine-like state dicts
    while q:
        tn, _, kind, data = heapq.heappop(q)
        if kind == "issue":
            cons = rng.randrange(cfg.consumers)
            t = tn + theta[cons]
            costs = {0: rng.randint(*cfg.cost[0])}
            for i in range(1, n_lim):
                if rng.random() < cfg.subset_p:
                    costs[i] = rng.randint(*cfg.cost[i])
            img = images[cons]
            if img is None or img[1] != scale_at(cfg, t)[1]:
                kinds = choose([-1e18] * n_lim, None, t, costs, "full")
            else:
                kinds = choose(img[0], img[2], t, costs, cfg.predictor)
            req = {
                "cons": cons,
                "t": t,
                "costs": costs,
                "kinds": kinds,
                "n": 0,
                "k": scale_at(cfg, t)[1],
                "first": True,
            }
            push(tn + one_way(), "land", req)
        elif kind == "land":
            req = data
            req["n"] += 1
            t, costs = req["t"], req["costs"]
            k_t = req["k"]
            ok = True
            why = None
            if cfg.variant == "today":
                # fast path: ADD -c under tk >= c, rf untouched; else the slow path clamps
                fast = all(tk[i] >= c for i, c in costs.items())
                if not fast:
                    for i in costs:
                        ceiling, r = params(cfg, i, k_t)
                        tk[i] = min(ceiling, tk[i] + r * max(0.0, t - rf[i]))
                        rf[i] = max(rf[i], t)
                if all(tk[i] >= c for i, c in costs.items()):
                    offsets.append(abs(t - tn))
                    for i, c in costs.items():
                        tk[i] -= c
                        log[i].append((tn, float(c), True))
                    outcomes["admitted"] += 1
                    writes_hist[1] = writes_hist.get(1, 0) + 1
                else:
                    outcomes["rejected"] += 1
                continue
            if item.k != k_t:
                ok, why = False, "pin"
            else:
                for i, c in costs.items():
                    ceiling, r = params(cfg, i, k_t)
                    d, slack = c / r, (ceiling - c) / r
                    zi = item.z[i]
                    soft = cfg.soft[i]
                    if cfg.variant == "fill":
                        if req["kinds"][i] == "N":
                            good = t <= zi and (soft or zi <= t + slack)
                        else:
                            good = zi < t and (soft or not cfg.guard_c_le_ceiling or c <= ceiling)
                    else:
                        good = soft or zi <= t + slack
                    if not good:
                        ok = False
                        break
            if req.get("first"):
                first_writes += 1
                if not ok and why != "pin":
                    side_wrong += 1
                req["first"] = False
            if ok:
                for i, c in costs.items():
                    ceiling, r = params(cfg, i, k_t)
                    d = c / r
                    if cfg.variant == "tk":
                        item.z[i] += d
                    elif cfg.variant == "set":
                        item.z[i] = t + d
                    elif req["kinds"][i] == "N":
                        item.z[i] += d
                    else:
                        item.z[i] = t + d
                    log[i].append((tn, float(c), True))
                    record_applied(tn, t, "adm", i, c)
                item.version += 1
                outcomes["admitted"] += 1
                writes_hist[req["n"]] = writes_hist.get(req["n"], 0) + 1
                images[req["cons"]] = (list(item.z), item.k, tn)
                # lease ends: maybe return / adjust
                if rng.random() < cfg.p_return or rng.random() < cfg.p_adjust:
                    sign = -1 if rng.random() < cfg.p_return / (cfg.p_return + cfg.p_adjust) else 1
                    amt = {i: max(1, int(c * rng.uniform(0.1, 1.0))) for i, c in costs.items()}
                    push(
                        tn + rng.uniform(*cfg.hold) + one_way(),
                        "credit",
                        {"cons": req["cons"], "sign": sign, "amt": amt, "k": k_t, "tries": 0},
                    )
                continue
            # failed: image of the item comes back
            img_z, img_k = list(item.z), item.k
            images[req["cons"]] = (img_z, img_k, tn)
            if why == "pin":
                # ADR-155: re-express from the image in a pinned write, then retry
                push(
                    tn + one_way() + one_way(),
                    "reexpress",
                    {"req": req, "z": img_z, "k": img_k, "v": item.version},
                )
                continue
            rejected = False
            for i, c in costs.items():
                ceiling, r = params(cfg, i, k_t)
                if not cfg.soft[i] and img_z[i] > t + (ceiling - c) / r:
                    rejected = True
            if rejected:
                outcomes["rejected"] += 1
                writes_hist[req["n"]] = writes_hist.get(req["n"], 0) + 1
                continue
            if req["n"] >= cfg.cap:
                outcomes["contended"] += 1
                writes_hist[req["n"]] = writes_hist.get(req["n"], 0) + 1
                continue
            req["kinds"] = {i: ("F" if img_z[i] < t else "N") for i in costs}
            if cfg.adversary_returns:
                # an adversary lands a return of the full cost just before the retry
                push(tn + one_way() * 0.999, "advret", {"costs": costs, "t": t})
            push(tn + one_way() + one_way(), "land", req)
        elif kind == "advret":
            for i, c in data["costs"].items():
                ceiling, r = params(cfg, i, item.k)
                item.z[i] -= c / r
                log[i].append((tn, -float(c), False))
                record_applied(tn, data["t"], "ret", i, c)
            item.version += 1
        elif kind == "reexpress":
            req, z0, k0, v0 = data["req"], data["z"], data["k"], data["v"]
            if item.version == v0 and item.k == k0:
                if k0 < req["k"]:
                    item.z = reexpress(cfg, z0, k0, req["k"])
                    item.k = req["k"]
                    item.version += 1
                # if the item is in a later window than the request's stamp, the
                # request's stamp is behind the item: re-stamp the request (new t)
            if item.k > req["k"]:
                cons = req["cons"]
                req["t"] = tn + theta[cons]
                req["k"] = scale_at(cfg, req["t"])[1]
            req["kinds"] = {i: ("F" if item.z[i] < req["t"] else "N") for i in req["costs"]}
            images[req["cons"]] = (list(item.z), item.k, tn)
            push(tn + one_way(), "land", req)
        elif kind == "credit":
            t_c = data.setdefault("t", tn + theta[data["cons"]])
            if cfg.variant == "today":
                for i, a in data["amt"].items():
                    tk[i] -= data["sign"] * a
                    log[i].append((tn, data["sign"] * float(a), False))
                continue
            k_c = scale_at(cfg, t_c)[1]  # ADR-155: pinned on params, with vu after its stamp
            if item.k != k_c:
                if item.k < k_c:  # re-express first (a pinned read + write), then resend
                    item.z = reexpress(cfg, item.z, item.k, k_c)
                    item.k = k_c
                    item.version += 1
                else:  # the item is ahead of this stamp: take a fresh stamp
                    data["t"] = tn + theta[data["cons"]]
                data["tries"] += 1
                push(tn + 2 * one_way(), "credit", data)
                continue
            if data["sign"] > 0 and cfg.adjust_mode == "fn":
                img = images[data["cons"]]
                guess = data.get("kinds") or {
                    i: ("F" if (img and img[0][i] < t_c) else "N") for i in data["amt"]
                }
                good = all(
                    (item.z[i] < t_c) if guess[i] == "F" else (item.z[i] >= t_c)
                    for i in data["amt"]
                )
                if not good:
                    data["kinds"] = {i: ("F" if item.z[i] < t_c else "N") for i in data["amt"]}
                    data["writes"] = data.get("writes", 1) + 1
                    push(tn + 2 * one_way(), "credit", data)
                    continue
                for i, a in data["amt"].items():
                    ceiling, r = params(cfg, i, k_c)
                    item.z[i] = max(item.z[i], t_c) + a / r
                    log[i].append((tn, float(a), False))
                    record_applied(tn, t_c, "adj", i, a)
                item.version += 1
                continue
            for i, a in data["amt"].items():
                ceiling, r = params(cfg, i, k_c)
                item.z[i] += data["sign"] * a / r
                log[i].append((tn, data["sign"] * float(a), False))
                record_applied(tn, t_c, "adj" if data["sign"] > 0 else "ret", i, a)
            item.version += 1
        # utilisation estimate (for the extrapolate predictor): true long-run u
        for i in range(n_lim):
            util_est[i] = cfg.rate * (sum(cfg.cost[i]) / 2) / cfg.r[i]

    s = max(offsets) if offsets else 0.0
    results = []
    for i in range(n_lim):
        ev = sorted(log[i])
        if cfg.schedule is None:
            ceiling, r = cfg.ceiling[i], cfg.r[i]
            ex, where = window_excess(ev, lambda tr: ceiling, lambda tr: r * tr)
            ex_written = ex
            rmax = r
        else:
            period, scales = cfg.schedule

            def cap_at(tr, i=i):
                return params(cfg, i, math.floor(tr / period))[0]

            def rcum(tr, i=i):
                k = math.floor(tr / period)
                full = sum(params(cfg, i, j)[1] * period for j in range(k)) if k < 2000 else None
                return full + params(cfg, i, k)[1] * (tr - k * period)

            # precompute cumulative refill for speed
            kmax = int(cfg.duration / period) + 2
            cum = [0.0]
            for j in range(kmax):
                cum.append(cum[-1] + params(cfg, i, j)[1] * period)

            def rcum_fast(tr, i=i, cum=cum):
                k = math.floor(tr / period)
                return cum[k] + params(cfg, i, k)[1] * (tr - k * period)

            ex_written, _ = window_excess(ev, cap_at, rcum_fast)

            def cap_back(tr, i=i):
                return max(cap_at(tr), cap_at(tr - s))

            ex, where = window_excess(ev, cap_back, rcum_fast)
            rmax = max(params(cfg, i, j)[1] for j in range(len(scales)))
        results.append(
            {
                "soft": cfg.soft[i],
                "excess": ex,
                "ratio_as_written": ((ex_written if cfg.schedule else ex) / (2 * rmax * s))
                if s
                else None,
                "two_r_s": 2 * rmax * s,
                "ratio": (ex / (2 * rmax * s)) if s else None,
                "where": where,
            }
        )
    return {
        "cfg": {k: v for k, v in cfg.__dict__.items()},
        "s": s,
        "limits": results,
        "outcomes": outcomes,
        "writes_hist": dict(sorted(writes_hist.items())),
        "max_writes": max(writes_hist) if writes_hist else 0,
        "first_wrong": side_wrong / max(1, first_writes),
        "e_trace": e_trace,
        "e_all": e_all,
        "e_max": max((abs(e) for _, e in e_trace), default=0.0),
        "log0": sorted(log[0]),
        "logs": [sorted(lg) for lg in log],
    }


# ---------------------------------------------------------------------------
# Experiment 4: Theorem 4 tightness, deterministic adversary.
# ---------------------------------------------------------------------------


def theorem3c_adversary(k):
    """Theorem 3(c): B alternates an admission and a return between A's writes, so A's
    write is always on the wrong side. Returns A's outcome after k writes."""
    ceiling, r, c, c_b = 10.0, 1.0, 1.0, 2.0
    t = 100.0
    z = t - 0.1  # full, barely
    trace = []
    for n in range(1, k + 1):
        kind = "F" if z < t else "N"  # A chooses from the image it just got
        if kind == "F":
            z = (
                max(z, t - 0.05) + c_b / r if z < t - 0.05 else z + c_b / r
            )  # B admits: not full now
        else:
            z -= c_b / r  # B returns what it took: full again
        ok = (z < t) if kind == "F" else (t <= z <= t + (ceiling - c) / r)
        trace.append({"write": n, "A_sends": kind, "z_minus_t": z - t, "applied": ok})
        if ok:
            return trace
    return trace


def theorem4_adversary(n_lim, first_guess):
    """Victim A on L limits, every limit full in A's image. Before each of A's
    writes, adversary B admits on one limit A believes full, flipping it to not full.
    Returns the number of writes A needs."""
    ceiling, r, c = 10.0, 1.0, 1.0
    t = 100.0
    z = [t - 50.0] * n_lim  # all full, long idle
    believed = {i: ("F" if first_guess == "full" else "N") for i in range(n_lim)}
    flips_left = list(range(n_lim))
    writes = 0
    while True:
        # B flips one limit A currently believes full (if any remain)
        cand = [i for i in flips_left if believed[i] == "F" and z[i] < t]
        if cand:
            i = cand[0]
            z[i] = t - 0.5 + 2.0  # B (stamp t-0.5) admits cost 2: F write -> t+1.5
            flips_left.remove(i)
        writes += 1
        ok = all(
            (t <= z[i] <= t + (ceiling - c) / r) if believed[i] == "N" else (z[i] < t)
            for i in range(n_lim)
        )
        if ok:
            return writes
        believed = {i: ("F" if z[i] < t else "N") for i in range(n_lim)}


# ---------------------------------------------------------------------------
# Experiment 5: sharded entity, splits racing fast writes (Theorem 5).
# ---------------------------------------------------------------------------


def run_shards(
    seed=0,
    ceiling=1000.0,
    r=1000 / 3600,
    consumers=16,
    duration=7200.0,
    rate=2.0,
    skew=0.02,
    one_way=(0.001, 0.015),
    split_every=600.0,
    max_shards=32,
    cost=(1, 10),
    p_return=0.3,
    naive_split=False,
):
    """Each shard: z, share C/S, rate r/S. A split reads every shard (stamp tau) and, in
    one transaction pinned on what it read, re-expresses each at the new share and
    creates its sibling holding the balance above the new share.  naive_split=True gives
    each new shard a full share (ADR-133 today) to show the checker catches it."""
    rng = random.Random(seed)
    n_sh = 1
    shards = [0.0]  # fill instants; all full at t=0
    version = 0
    theta = [rng.uniform(-skew, skew) for _ in range(consumers)]
    q, seq = [], [0]

    def push(tr, kind, data):
        seq[0] += 1
        heapq.heappush(q, (tr, seq[0], kind, data))

    tr = 0.0
    while True:
        tr += rng.expovariate(rate)
        if tr > duration:
            break
        push(tr, "issue", None)
    tr = split_every
    while tr < duration:
        push(tr, "split", None)
        tr += split_every
    log, offsets, splits_done, split_retries = [], [], 0, 0
    total_hist = []
    out = {"admitted": 0, "rejected": 0, "contended": 0}

    def bal(i, tau, n_shx=None):
        n_shx = n_shx or n_sh
        return ceiling / n_shx - (r / n_shx) * max(0.0, shards[i] - tau)

    while q:
        tn, _, kind, data = heapq.heappop(q)
        if kind == "issue":
            cons = rng.randrange(consumers)
            req = {
                "cons": cons,
                "t": tn + theta[cons],
                "c": rng.randint(*cost),
                "S": n_sh,
                "sh": rng.randrange(n_sh),
                "n": 0,
                "kind": None,
            }
            push(tn + rng.uniform(*one_way), "land", req)
        elif kind == "land":
            req = data
            req["n"] += 1
            if req["S"] != n_sh:  # pinned on shard count: fails, re-draw
                req["S"] = n_sh
                req["sh"] = rng.randrange(n_sh)
                req["kind"] = None
                push(tn + 2 * rng.uniform(*one_way), "land", req)
                continue
            i, t, c = req["sh"], req["t"], req["c"]
            ceiling_s, rs = ceiling / n_sh, r / n_sh
            z = shards[i]
            if req["kind"] is None:
                req["kind"] = "F" if z < t else "N"  # exact image (worst for nothing)
            k = req["kind"]
            good = (t <= z <= t + (ceiling_s - c) / rs) if k == "N" else (z < t)
            if good:
                shards[i] = (z + c / rs) if k == "N" else (t + c / rs)
                version += 1
                offsets.append(abs(t - tn))
                log.append((tn, float(c), True))
                out["admitted"] += 1
                if rng.random() < p_return:
                    push(
                        tn + rng.uniform(0, 5) + rng.uniform(*one_way),
                        "ret",
                        {
                            "cons": req["cons"],
                            "sh": i,
                            "S": n_sh,
                            "a": max(1, int(c * rng.random())),
                        },
                    )
                continue
            if z > t + (ceiling_s - c) / rs:
                out["rejected"] += 1
                continue
            if req["n"] >= 6:
                out["contended"] += 1
                continue
            req["kind"] = "F" if z < t else "N"
            push(tn + 2 * rng.uniform(*one_way), "land", req)
        elif kind == "ret":
            if data["S"] != n_sh:  # pin failed: resend at the current count, same shard id
                data["S"] = n_sh
                push(tn + 2 * rng.uniform(*one_way), "ret", data)
                continue
            shards[data["sh"]] -= data["a"] / (r / n_sh)
            version += 1
            log.append((tn, -float(data["a"]), False))
        elif kind == "split":
            if n_sh >= max_shards:
                continue
            # read every shard now (a strongly consistent read), transaction lands later
            snap = list(shards)
            v0 = version
            tau = tn + rng.uniform(-skew, skew)
            push(tn + 2 * rng.uniform(*one_way), "split_tx", {"snap": snap, "v": v0, "tau": tau})
        elif kind == "split_tx":
            if version != data["v"]:
                split_retries += 1
                push(tn, "split", None)  # re-read and retry
                continue
            tau = data["tau"]
            n_sh2 = 2 * n_sh
            ceiling2, r2 = ceiling / n_sh2, r / n_sh2
            new = [0.0] * n_sh2
            for i in range(n_sh):
                b = ceiling / n_sh - (r / n_sh) * max(0.0, data["snap"][i] - tau)
                keep = min(b, ceiling2)
                give = max(0.0, b - ceiling2) if not naive_split else ceiling2
                new[i] = tau + (ceiling2 - keep) / r2
                new[i + n_sh] = tau + (ceiling2 - give) / r2
            before = sum(ceiling / n_sh - (r / n_sh) * max(0.0, z - tau) for z in data["snap"])
            after = sum(ceiling2 - r2 * max(0.0, z - tau) for z in new)
            total_hist.append(
                {"T": tn, "S_from": n_sh, "S_to": n_sh2, "before": before, "after": after}
            )
            shards, n_sh = new, n_sh2
            version += 1
            offsets.append(abs(tau - tn))
            splits_done += 1
    s = max(offsets)
    ev = sorted(log)
    ex, where = window_excess(ev, lambda tr: ceiling, lambda tr: r * tr)
    return {
        "s": s,
        "excess": ex,
        "two_r_s": 2 * r * s,
        "ratio": ex / (2 * r * s),
        "splits": splits_done,
        "split_retries": split_retries,
        "final_S": n_sh,
        "conservation": total_hist,
        "outcomes": out,
        "log": ev,
        "where": where,
    }


# ---------------------------------------------------------------------------
# Experiment 6: schedule boundary, today's credit-at-end vs integrated.
# ---------------------------------------------------------------------------


def schedule_boundary():
    """per_minute(rpm, 1000) with scale 0.1 until 06:00; drained at 05:59:00."""
    ceiling, r = 1000.0, 1000 / 60
    t_drain, t_b, t_q = -60.0, 0.0, 1.0  # seconds relative to 06:00
    # today: credit (t_q - t_drain) at the rate in force at t_q, capped at the ceiling then
    today = min(ceiling, 0 + r * (t_q - t_drain))
    # integrated: 60 s at 0.1x (cap 100) then 1 s at 1x
    b = min(0.1 * ceiling, 0 + 0.1 * r * (t_b - t_drain))
    b = min(ceiling, b + r * (t_q - t_b))
    return {"today": today, "integrated": b}


if __name__ == "__main__":
    import sys

    print(json.dumps(schedule_boundary()))
    print(
        "thm4",
        [
            (n_lim, theorem4_adversary(n_lim, "full"), theorem4_adversary(n_lim, "notfull"))
            for n_lim in range(1, 6)
        ],
    )
    r = run(Cfg(seed=1))
    print("base", r["s"], r["limits"], r["outcomes"], r["max_writes"], r["e_max"])
    sys.exit(0)
