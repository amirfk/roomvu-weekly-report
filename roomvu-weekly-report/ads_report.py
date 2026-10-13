"""Weekly "Ads Report" deck — mirrors the team's Google Slides report.

Every number is computed here from three sources, with ONE definition per
metric (see report.yaml header):
  * Meta spend   ad_analytics (db 74); days it hasn't loaded -> Supermetrics FA
  * Google spend Supermetrics Google Ads (cached daily dataset)
  * Users / revenue  roomview-website (db 6)
All weekly grids are Wednesday–Tuesday, anchored on 2026-01-07.
"""
import datetime as dt
import re
import sys
from pathlib import Path

import yaml

from metabase_client import execute_sql
import supermetrics_client as sm

ROOT = Path(__file__).parent
ASSETS = "roomvu-weekly-report/assets/report"      # relative to index.html (repo root)

A = dt.date(2026, 1, 7)                             # Wednesday anchor of every grid
SUCC = "('charge.succeeded','SUBSCRIPTION','CHARGE','subcription.succeeded','payment_intent.succeeded')"
CAMP = "substr(u.utm_campaign,1,position('-' in u.utm_campaign)-1)"
ADSET = "substr(u.utm_campaign,position('-' in u.utm_campaign)+1)"
LIVE = "u.user_type_id != 1 and u.deleted_at is null"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "June", "July", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def wk(d):
    return (d - A).days // 7


def wstart(k):
    return A + dt.timedelta(days=7 * k)


def wk_sql(col):
    return f"floor(datediff({col}, '{A}')/7)"


def ids_sql(xs):
    return ",".join(f"'{x}'" for x in xs) or "''"


def week_label(k):
    """Manual-deck convention: a week is named by its LAST day (Tuesday),
    e.g. 30 Sep - 6 Oct = "Oct W1", 24 Dec - 30 Dec = "Dec W5"."""
    e = wstart(k) + dt.timedelta(days=6)
    return f"{MONTHS[e.month - 1]} W{(e.day - 1) // 7 + 1}"


def short_range(k):
    s = wstart(k)
    e = s + dt.timedelta(days=6)
    return f"{s.day} {s:%b} - {e.day} {e:%b}"


def long_range(k):
    s = wstart(k)
    e = s + dt.timedelta(days=6)
    return f"{s.day} {MONTHS_LONG[s.month - 1]} to {e.day} {MONTHS_LONG[e.month - 1]}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money(v, dec=0, dash="$-"):
    if v is None or abs(v) < 0.005:
        return dash
    return f"${v:,.{dec}f}"


def count(v, dash="-"):
    if not v:
        return dash
    return f"{int(round(v)):,}"


def pct(v, dec=0, dash="-"):
    if v is None:
        return dash
    return f"{v:.{dec}f}%"


def roi_color(p):
    c = max(0.0, min(150.0, p))
    if c <= 100:
        t = c / 100
        r, g, b = 220, int(20 + t * 200), int(20 + t * 160)
    else:
        t = (c - 100) / 50
        r, g, b = int(220 - t * 200), 210, int(180 - t * 160)
    txt = "#111" if 0.299 * r + 0.587 * g + 0.114 * b > 140 else "#fff"
    return f"background:rgb({r},{g},{b});color:{txt}"


class Report:
    def __init__(self, cfg, url_env, key_env, today=None):
        self.cfg = cfg
        self.url_env, self.key_env = url_env, key_env
        self.today = today or dt.date.today()
        back = (self.today.weekday() - 1) % 7 or 7          # latest COMPLETE Wed–Tue week
        self.we = self.today - dt.timedelta(days=back)
        self.ws = self.we - dt.timedelta(days=6)
        self.k = wk(self.ws)
        assert wstart(self.k) == self.ws, "report week must be on the Wednesday grid"
        r = cfg.get("rates", {})
        self.rev_usd = float(r.get("revenue_usd_to_cad", 1.3))
        self.spend_usd = float(r.get("spend_usd_to_cad", 1.39))
        self.groups = {k: [str(x) for x in v] for k, v in cfg["campaign_groups"].items()}
        self.segments = cfg["segments"]
        self.conv = f"if(f.currency='CAD', f.amount, f.amount*{self.rev_usd})"
        self.warnings = []
        self._meta = None
        self._google = None
        self._user = {}
        self._names = None
        self.fa_rows = []
        mi = ROOT / "manual_inputs.yaml"
        allm = (yaml.safe_load(mi.read_text(encoding="utf-8")) if mi.exists() else None) or {}
        self.manual = allm.get(self.ws.isoformat()) or allm.get(str(self.ws)) or {}

    # ── helpers ─────────────────────────────────────────────────────────────
    def q(self, sql, db=6):
        return execute_sql(sql, db, self.url_env, self.key_env)

    def warn(self, msg):
        print(f"  [WARN] {msg}")
        self.warnings.append(msg)

    def resolve(self, items):
        out = []
        for x in items or []:
            out += self.groups.get(str(x), [str(x)])
        return out

    def seg(self, name):
        if isinstance(name, dict):
            return name
        if name not in self.segments:
            raise KeyError(f"unknown segment '{name}'")
        return self.segments[name]

    def seg_where(self, spec):
        spec = self.seg(spec)
        parts = []
        if spec.get("meta"):
            parts.append(f"(u.utm_source='facebook' and {CAMP} in ({ids_sql(self.resolve(spec['meta']))}))")
        if spec.get("meta_exclude"):
            parts.append(f"(u.utm_source='facebook' and coalesce({CAMP},'') not in "
                         f"({ids_sql(self.resolve(spec['meta_exclude']))}))")
        g = spec.get("google")
        if g == "all":
            parts.append("(u.utm_source='google')")
        elif g:
            parts.append(f"(u.utm_source='google' and {CAMP} in ({ids_sql(self.resolve(g))}))")
        if spec.get("sql"):
            parts.append(f"({spec['sql']})")
        return " or ".join(parts) or "1=0"

    # ── spend layer ─────────────────────────────────────────────────────────
    def meta(self):
        """{(k, campaign): cad} and {(k, campaign): clicks} for every week since Apr 2025."""
        if self._meta is not None:
            return self._meta
        rows = self.q(f"""
select {wk_sql('aa.date')} k, c.campaign_id cid,
       sum(if(c.account_name='facebook', aa.spend, aa.spend*{self.spend_usd})) cad,
       sum(aa.inline_link_clicks) clicks
from ad_analytics aa join ads a on aa.ad_id = a.id join campaigns c on a.campaign_id = c.id
where aa.date >= '2025-04-02' group by 1, 2""", db=74)
        last = self.q("select max(date(date)) mx from ad_analytics", db=74)[0]["mx"]
        aa_complete = dt.date.fromisoformat(str(last)[:10]) - dt.timedelta(days=1)
        spend, clicks = {}, {}
        for r in rows:
            key = (int(_num(r["k"])), str(r["cid"]))
            spend[key] = _num(r["cad"]) or 0.0
            clicks[key] = _num(r["clicks"]) or 0.0
        # Weeks to take from Supermetrics Facebook Ads (Facebook's own numbers):
        #  * every week from meta_supermetrics_from on (ad_analytics kept phantom
        #    rows from its 30 Sep 2026 outage that the backfill never corrected), and
        #  * any week ad_analytics hasn't fully loaded.
        gap = {k for k in range(wk(aa_complete + dt.timedelta(days=1)), self.k + 1)
               if wstart(k) + dt.timedelta(days=6) > aa_complete}
        fa_from = self.cfg.get("meta_supermetrics_from")
        prefer = set(range(wk(dt.date.fromisoformat(fa_from)), self.k + 1)) if fa_from else set()
        targets = gap | prefer
        fa_weeks = set()
        if targets:
            start = wstart(min(targets))
            try:
                ds = sm.ensure_meta_daily(self.cfg["meta_fallback_accounts"], start.isoformat())
                fa_complete = dt.date.fromisoformat(ds["end"]) - dt.timedelta(days=1)
                fa = {}
                for d, c, _a, cur, cost in ds["rows"]:
                    kk = wk(dt.date.fromisoformat(d))
                    if kk in targets:
                        cad = cost * (1.0 if str(cur).upper() == "CAD" else self.spend_usd)
                        fa[(kk, str(c))] = fa.get((kk, str(c)), 0.0) + cad
                for kk in sorted(targets):
                    if wstart(kk) + dt.timedelta(days=6) > fa_complete:
                        continue
                    for key in [x for x in spend if x[0] == kk]:
                        spend.pop(key, None)
                        if kk in gap:
                            clicks.pop(key, None)     # FA dataset has no clicks
                    for (k2, c), v in fa.items():
                        if k2 == kk:
                            spend[(k2, c)] = v
                    fa_weeks.add(kk)
                self.fa_rows = ds["rows"]
                print(f"  [INFO] Meta spend from Supermetrics Facebook Ads for {len(fa_weeks)} week(s) "
                      f"from {wstart(min(fa_weeks)) if fa_weeks else '-'} (Facebook data through {fa_complete})")
            except Exception as exc:
                self.warn(f"Supermetrics Facebook Ads unavailable ({str(exc)[:120]}); "
                          f"using ad_analytics (complete through {aa_complete})")
        missing = {k for k in gap if k not in fa_weeks}
        for kk in sorted(missing):
            self.warn(f"Meta spend for {short_range(kk)} is incomplete (ad_analytics through {aa_complete})")
        self._meta = {"spend": spend, "clicks": clicks, "aa_complete": aa_complete,
                      "missing": missing, "fa_weeks": fa_weeks}
        print(f"  [INFO] Meta spend: ad_analytics complete through {aa_complete}")
        return self._meta

    def google(self):
        """{(k, campaign): cost} from Supermetrics, only for fully covered weeks."""
        if self._google is not None:
            return self._google
        cover = sm.google_complete_through()
        end = min(self.we, cover) if cover else self.we
        rows = sm.fetch_google_ads(["Date", "Campaignid", "Cost"],
                                   start_date="2025-09-10", end_date=end.isoformat())
        spend = {}
        for r in rows:
            day = dt.date.fromisoformat(str(r["Date"])[:10])
            key = (wk(day), str(r["Campaignid"]))
            spend[key] = spend.get(key, 0.0) + float(r.get("Cost") or 0)
        last_k = wk(end + dt.timedelta(days=1)) - 1     # last week fully inside coverage
        if last_k < self.k:
            self.warn(f"Google spend only complete through {end}; week {short_range(self.k)} incomplete")
        self._google = {"spend": spend, "last_k": last_k, "end": end}
        return self._google

    def google_clicks(self, groups):
        cids = set(self.resolve(groups))
        rows = sm.fetch_google_ads(["Campaignid", "Clicks", "Cost"],
                                   start_date=self.ws.isoformat(), end_date=self.we.isoformat())
        return sum(float(r.get("Clicks") or 0) for r in rows if str(r["Campaignid"]) in cids)

    def spend(self, segname, k):
        spec = self.seg(segname)
        total = 0.0
        if spec.get("meta"):
            m = self.meta()
            if k in m["missing"]:
                return None
            cids = set(self.resolve(spec["meta"]))
            total += sum(v for (kk, c), v in m["spend"].items() if kk == k and c in cids)
        g = spec.get("google")
        if g:
            G = self.google()
            if k > G["last_k"]:
                return None
            cids = None if g == "all" else set(self.resolve(g))
            total += sum(v for (kk, c), v in G["spend"].items()
                         if kk == k and (cids is None or c in cids))
        return total

    def meta_clicks(self, groups, k):
        m = self.meta()
        cids = set(self.resolve(groups))
        vals = [v for (kk, c), v in m["clicks"].items() if kk == k and c in cids]
        return sum(vals) if vals else None

    # ── users / revenue layer ───────────────────────────────────────────────
    def users(self, segname, since="2025-04-02"):
        key = (str(segname), since)
        if key in self._user:
            return self._user[key]
        where = self.seg_where(segname)
        end = (self.we + dt.timedelta(days=1)).isoformat()
        sub_paid = (f"f.user_id=u.id and f.gateway_transaction_type in {SUCC} and f.amount>=1 "
                    f"and f.morph_type like 'SUBSCRIPTION%'")
        regs = self.q(f"""
select {wk_sql('u.created_at')} k, count(*) regs,
  sum(exists(select 1 from financials f where {sub_paid})) subs_ever,
  sum(exists(select 1 from financials f where {sub_paid}
             and {wk_sql('f.created_at')} = {wk_sql('u.created_at')})) subs_wk
from users u where {LIVE} and ({where}) and u.created_at >= '{since}' and u.created_at < '{end}'
group by 1""")
        imm = self.q(f"""
select {wk_sql('u.created_at')} k, sum({self.conv}) imm
from users u join financials f on f.user_id = u.id
where {LIVE} and ({where}) and f.gateway_transaction_type in {SUCC} and f.amount >= 1
  and f.morph_type like 'SUBSCRIPTION%' and {wk_sql('f.created_at')} = {wk_sql('u.created_at')}
  and u.created_at >= '{since}' and u.created_at < '{end}'
group by 1""")
        rev = self.q(f"""
select {wk_sql('f.created_at')} k, sum({self.conv}) total, sum(f.amount) raw
from users u join financials f on f.user_id = u.id
where {LIVE} and ({where}) and f.gateway_transaction_type in {SUCC} and f.amount >= 1
  and f.created_at >= '{since}' and f.created_at < '{end}'
group by 1""")
        out = {}

        def put(k, field, v):
            out.setdefault(int(_num(k)), {})[field] = _num(v) or 0.0

        for r in regs:
            for f in ("regs", "subs_ever", "subs_wk"):
                put(r["k"], f, r[f])
        for r in imm:
            put(r["k"], "imm", r["imm"])
        for r in rev:
            put(r["k"], "total", r["total"])
            put(r["k"], "raw", r["raw"])
        self._user[key] = out
        return out

    def u(self, segname, k, field):
        return self.users(segname).get(k, {}).get(field, 0.0)

    # ── metric evaluation ───────────────────────────────────────────────────
    def metric(self, spec, k):
        m = spec["metric"]
        seg = spec.get("segment")
        if m == "spend":
            return self.spend(seg, k)
        if m == "regs":
            return self.u(seg, k, "regs")
        if m == "subs":
            return self.u(seg, k, "subs_wk")
        if m == "imm":
            return self.u(seg, k, "imm")
        if m == "revenue":
            return self.u(seg, k, "total")
        if m == "clicks":
            s = self.seg(seg)
            total, have = 0.0, False
            if s.get("meta"):
                v = self.meta_clicks(s["meta"], k)
                if v is not None:
                    total, have = total + v, True
            if s.get("google") and s["google"] != "all" and k == self.k:
                total, have = total + self.google_clicks(s["google"]), True
            return total if have else None
        sp = self.spend(seg, k)
        if m == "cpa":
            r = self.u(seg, k, "regs")
            return sp / r if (sp is not None and r) else None
        if m == "sub_rate":
            r = self.u(seg, k, "regs")
            return 100 * self.u(seg, k, "subs_ever") / r if r else None
        if m == "imm_rate":
            return 100 * self.u(seg, k, "imm") / sp if sp else None
        if m == "return_rate":
            return 100 * self.u(spec["revenue_segment"], k, "total") / sp if sp else None
        if m == "raw_return_rate":
            return 100 * self.u(spec["revenue_segment"], k, "raw") / sp if sp else None
        raise KeyError(f"unknown metric '{m}'")

    # ── slide builders ──────────────────────────────────────────────────────
    def chart(self, c):
        k0 = wk(dt.date.fromisoformat(c["since"]))
        ks = list(range(k0, self.k + 1))
        series = []
        palette = ["#4285f4", "#ea4335", "#fbbc04", "#34a853"]
        for i, s in enumerate(c["series"]):
            vals = []
            for k in ks:
                v = self.metric(s, k)
                vals.append(None if v is None else round(v, 4))
            series.append({"label": s["label"], "data": vals,
                           "color": s.get("color") or c.get("color") or palette[i % 4]})
        # drop trailing weeks where every series is missing (data not loaded yet)
        while ks and all(sr["data"][len(ks) - 1] is None for sr in series):
            ks.pop()
            for sr in series:
                sr["data"].pop()
        lab = short_range if c.get("label_style") == "range" else week_label
        return {"title": c.get("title", ""), "labels": [lab(k) for k in ks],
                "series": series, "format": c.get("format", "number"),
                "decimals": c.get("decimals", 0 if c.get("format") != "percent" else 2)}

    def kpi_values(self, kpis):
        out = []
        for spec in kpis:
            m = spec["metric"]
            v = self.metric(spec, self.k)
            if m in ("regs", "subs", "clicks"):
                txt = count(v)
            elif m in ("cpa",):
                txt = money(v)
            elif m in ("spend", "imm", "revenue"):
                txt = money(v)
            elif m.endswith("rate"):
                txt = pct(v)
            else:
                txt = str(v)
            out.append({"label": spec["label"], "value": txt})
        return out

    def b_charts(self, s):
        return {"type": "charts", "layout": s.get("layout", "auto"),
                "charts": [self.chart(c) for c in s["charts"]], "note": s.get("note")}

    def b_kpi_chart(self, s):
        return {"type": "kpi_chart", "kpis": self.kpi_values(s["kpis"]), "chart": self.chart(s["chart"])}

    def b_kpi(self, s):
        return {"type": "kpi", "kpis": self.kpi_values(s["kpis"]),
                "creative": f"{ASSETS}/{s['creative']}" if s.get("creative") else None,
                "creative_wide": s.get("creative_wide", False), "side_text": s.get("side_text")}

    def b_manual_kpi(self, s):
        vals = self.manual.get(s["key"]) or {}
        kpis, missing = [], []
        for label in s["kpis"]:
            key = label.lower().replace(".", "").replace(" ", "_")
            v = vals.get(key)
            if v is None:
                missing.append(label)
            money_like = label in ("Cost", "CPA", "CPC", "Im. Revenue", "Total Revenue")
            txt = (money(_num(v), 1 if label == "CPC" else 0) if money_like else count(_num(v))) \
                if v is not None else "—"
            kpis.append({"label": label, "value": txt})
        note = None
        if missing:
            note = (f"Not in our data sources (platform-reported only). Enter this week's values under "
                    f"'{self.ws.isoformat()}: {s['key']}' in manual_inputs.yaml.")
            self.warn(f"{s['title']}: no values for {', '.join(missing)}")
        return {"type": "kpi", "kpis": kpis, "note": note,
                "creative": f"{ASSETS}/{s['creative']}" if s.get("creative") else None}

    def b_cohort(self, s):
        spec = self.seg(s["segment"])
        where = self.seg_where(spec)
        nrows = int(s.get("rows", 5))
        first = wstart(self.k - nrows + 1)
        end = (self.we + dt.timedelta(days=1)).isoformat()
        wins = ", ".join(f"sum(if(f.created_at < cu.cs + interval {7 * n + 1} day, {self.conv}, 0)) w{n}"
                         for n in range(1, 6))
        rows = self.q(f"""
with cu as (
  select u.id uid, {wk_sql('u.created_at')} k,
         date_add('{A}', interval {wk_sql('u.created_at')}*7 day) cs
  from users u where {LIVE} and ({where}) and u.created_at >= '{first}' and u.created_at < '{end}'),
reg as (select k, count(*) regs from cu group by k),
rev as (select cu.k, {wins} from cu join financials f on f.user_id = cu.uid
        where f.gateway_transaction_type in {SUCC} and f.amount >= 1 group by cu.k)
select reg.k, reg.regs, rev.w1, rev.w2, rev.w3, rev.w4, rev.w5
from reg left join rev on rev.k = reg.k order by reg.k""")
        cols = [f"W{n}" for n in range(1, int(s.get("max_weeks", 5)) + 1)]
        out = []
        for r in rows:
            k = int(_num(r["k"]))
            sp = self.spend(spec, k) or 0.0
            cells = []
            for n in range(1, len(cols) + 1):
                mature = self.today >= wstart(k) + dt.timedelta(days=7 * n)
                rev = _num(r[f"w{n}"]) or 0.0
                if not mature or not sp:
                    cells.append({"text": "", "style": None})
                else:
                    p = 100 * rev / sp
                    cells.append({"text": f"{p:.0f}%", "style": roi_color(p) if s.get("style") == "navy" else None})
            label = long_range(k) if s.get("week_label_style") == "long" else short_range(k)
            out.append({"week": label, "regs": f"{int(_num(r['regs'])):,}", "spend": money(sp), "cells": cells})
        return {"type": "cohort", "style": s.get("style", "navy"), "cols": cols, "rows": out}

    # regions -----------------------------------------------------------------
    def _adset_names(self):
        if self._names is None:
            self._names = {str(r["adset_id"]): r["name"] for r in
                           self.q("select adset_id, name from ad_sets", db=74)}
        return self._names

    @staticmethod
    def _region(name, test_label):
        n = name or ""
        if "test" in n.lower():
            return test_label
        m = re.search(r"\[Register ([^\]]+)\]", n)
        if m:
            return m.group(1).strip()
        return None

    def _region_week(self, k, test_label):
        cids = self.resolve(["meta_realtor"])
        s, e = wstart(k), wstart(k) + dt.timedelta(days=7)
        names = self._adset_names()
        skip = {str(x) for x in self.cfg.get("region_exclude_adsets", [])}
        old_map = {str(a): v for a, v in (self.cfg.get("region_adset_map") or {}).items()}

        def region_of(adset):
            if adset in skip:
                return None
            return self._region(names.get(adset, ""), test_label) or old_map.get(adset) or "Other"

        sp_rows = self.q(f"""
select st.adset_id adset, sum(if(c.account_name='facebook', aa.spend, aa.spend*{self.spend_usd})) cad
from ad_analytics aa join ads a on aa.ad_id = a.id join ad_sets st on a.ad_set_id = st.id
join campaigns c on a.campaign_id = c.id
where c.campaign_id in ({ids_sql(cids)}) and aa.date >= '{s}' and aa.date < '{e}' group by 1""", db=74)
        if k in self.meta()["fa_weeks"]:
            agg = {}
            for d, c, a, cur, cost in self.fa_rows:
                if str(c) in cids and s.isoformat() <= d < e.isoformat():
                    agg[str(a)] = agg.get(str(a), 0.0) + cost * (1.0 if str(cur).upper() == "CAD" else self.spend_usd)
            sp_rows = [{"adset": a, "cad": v} for a, v in agg.items()]
        where = f"u.utm_source='facebook' and {CAMP} in ({ids_sql(cids)})"
        sub_paid = (f"f.user_id=u.id and f.gateway_transaction_type in {SUCC} and f.amount>=1 "
                    f"and f.morph_type like 'SUBSCRIPTION%'")
        ur = self.q(f"""
select {ADSET} adset, count(*) regs,
  sum(exists(select 1 from financials f where {sub_paid} and f.created_at < '{e}')) paid,
  sum(exists(select 1 from calendly_events c where c.user_id = u.id and c.created_at < '{e}')) demo,
  sum((select coalesce(sum({self.conv}),0) from financials f where {sub_paid} and f.created_at < '{e}')) imm
from users u where {LIVE} and {where} and u.created_at >= '{s}' and u.created_at < '{e}' group by 1""")
        tot = self.q(f"""
select {ADSET} adset, count(distinct us.user_id) n from users u join user_subscription us on us.user_id = u.id
where {LIVE} and {where} and us.status = 1 group by 1""")
        R = {}

        def row(region):
            return R.setdefault(region, {"region": region, "spend": 0.0, "regs": 0, "paid": 0,
                                         "demo": 0, "imm": 0.0, "tot_subs": 0})
        for r in sp_rows:
            reg = region_of(str(r["adset"]))
            if reg:
                row(reg)["spend"] += _num(r["cad"]) or 0.0
        for r in ur:
            reg = region_of(str(r["adset"]))
            if reg:
                x = row(reg)
                x["regs"] += int(_num(r["regs"]) or 0)
                x["paid"] += int(_num(r["paid"]) or 0)
                x["demo"] += int(_num(r["demo"]) or 0)
                x["imm"] += _num(r["imm"]) or 0.0
        active = set(R)
        for r in tot:
            reg = region_of(str(r["adset"]))
            if reg in active:
                row(reg)["tot_subs"] += int(_num(r["n"]) or 0)
        rows = [x for x in R.values() if x["spend"] > 0.5 or x["regs"]]
        for x in rows:
            x["cpa"] = x["spend"] / x["regs"] if x["regs"] else None
            x["roas"] = 100 * x["imm"] / x["spend"] if x["spend"] else None
        return rows

    def b_regions(self, s):
        if s.get("style") == "summary":
            rows = self._region_week(self.k, "Test Ads")
            rows.sort(key=lambda x: (x["cpa"] is None, x["cpa"] or 0))
            T = {"spend": sum(x["spend"] for x in rows), "regs": sum(x["regs"] for x in rows),
                 "paid": sum(x["paid"] for x in rows), "demo": sum(x["demo"] for x in rows),
                 "imm": sum(x["imm"] for x in rows)}
            T["cpa"] = T["spend"] / T["regs"] if T["regs"] else None
            T["roas"] = 100 * T["imm"] / T["spend"] if T["spend"] else None
            fmt = lambda x: {
                "region": x.get("region", "Total"), "spend": money(x["spend"]), "regs": count(x["regs"], "0"),
                "cpa": money(x["cpa"], 1), "demo": (f"{100 * x['demo'] / x['regs']:.0f}% ({x['demo']})"
                                                    if x["regs"] else "-"),
                "paid": str(x["paid"]), "imm": money(x["imm"], 0, "-"),
                "roas": pct(x["roas"], 0, "0%")}
            return {"type": "regions_summary", "rows": [fmt(x) for x in rows], "total": fmt(T)}
        tables = []
        for k, label in ((self.k, None), (self.k - 1, "Last Week")):
            rows = sorted(self._region_week(k, "Test"), key=lambda x: x["region"])
            tables.append({"label": label, "rows": [{
                "region": x["region"], "spend": money(x["spend"]), "regs": count(x["regs"], "0"),
                "cpa": money(x["cpa"], 1), "paid": str(x["paid"]), "tot_subs": str(x["tot_subs"]),
                "imm": money(x["imm"], 0, "-"), "rate": pct(x["roas"], 0, "-") if x["imm"] else "-"}
                for x in rows]})
        return {"type": "regions_compare", "tables": tables}

    def b_retargeting(self, s):
        names = {str(r["campaign_id"]): r["name"] for r in
                 self.q("select campaign_id, name from campaigns", db=74)}
        rows = []
        for cid in self.resolve(s["meta_groups"]):
            sp = self.meta()["spend"].get((self.k, cid))
            if not sp:
                continue
            cl = self.meta()["clicks"].get((self.k, cid))
            rows.append({"campaign": names.get(cid, cid), "spend": money(sp),
                         "clicks": count(cl, "—"), "cpc": money(sp / cl, 2) if cl else "—"})
        return {"type": "retargeting", "rows": rows,
                "creative": f"{ASSETS}/{s['creative']}" if s.get("creative") else None}

    # financial tables ----------------------------------------------------------
    def _sp(self, seg):
        return self.spend(seg, self.k) or 0.0

    def b_fin_re(self, s):
        k = self.k
        li = {"cost": self.manual.get("linkedin_spend"), "registrations": self.manual.get("linkedin_registrations")}
        openai = _num(self.manual.get("openai_spend"))
        chat = self.q(f"""select count(*) n from users u where {LIVE} and u.utm_source in ('chatgpt','chatgpt.com')
            and u.created_at >= '{self.ws}' and u.created_at < '{self.we + dt.timedelta(days=1)}'""")[0]["n"]
        seg_rt = {"meta": ["meta_re_retargeting"], "google": ["google_retargeting"]}
        rows = {
            "fb": (self._sp("realtor_mb"), self.u("realtor_mb", k, "regs")),
            "google": (self._sp("google_acq"), self.u("google_acq", k, "regs")),
            "rt": (self._sp(seg_rt), None),
            "li": (_num(li.get("cost")), _num(li.get("registrations"))),
            "tests": (self._sp("re_tests_meta") + self._sp("google_tests") + (openai or 0.0),
                      self.u("re_tests_meta", k, "regs") + self.u("google_tests", k, "regs") + (_num(chat) or 0)),
            "aw_fb": (0.0, None),
            "aw_g": (self._sp({"google": ["google_awareness"]}), None),
        }
        total_spend = sum(v[0] or 0.0 for v in rows.values())
        total_reg = sum(v[1] or 0 for key, v in rows.items() if key in ("fb", "google", "li", "tests"))
        revenue = self.u("re_mb_all_revenue", k, "total")
        imm = self.u("re_mb_all", k, "imm")
        f = {key: {"spend": money(v[0]), "reg": count(v[1]) if v[1] is not None else "-"} for key, v in rows.items()}
        if openai is None:
            self.warn("Financial report RE&MB: Open AI test spend not provided (manual_inputs.yaml: openai_spend)")
        return {"type": "fin_re", "r": f, "total_spend": money(total_spend), "revenue": money(revenue),
                "imm_return": pct(100 * imm / total_spend if total_spend else None),
                "total_reg": count(total_reg)}

    def b_fin_ins(self, s):
        k = self.k
        out = []
        for label, seg in (("FB (Insurance)", "insurance"), ("FB (Financial)", "financial")):
            sp = self._sp(seg)
            tot = self.u(seg, k, "total")          # payments in the week by the segment's users
            imm = self.u(seg, k, "imm")
            out.append({"channel": label, "spend": money(sp), "revenue": money(_num(tot), 0, "-"),
                        "imm_return": pct(100 * imm / sp, 0) if (sp and imm) else "-",
                        "reg": count(self.u(seg, k, "regs"))})
        total_reg = sum(self.u(seg, k, "regs") for seg in ("insurance", "financial"))
        return {"type": "fin_ins", "rows": out, "total_reg": count(total_reg)}

    # ── run ───────────────────────────────────────────────────────────────────
    def build_slides(self):
        out = []
        for s in self.cfg["slides"]:
            if s.get("hidden"):
                continue
            t = s["type"]
            base = {"type": t, "title": s.get("title", ""), "icon": s.get("icon"), "header": s.get("header")}
            try:
                if t in ("title", "section", "thanks"):
                    data = {}
                else:
                    data = getattr(self, f"b_{t}")(s)
                base.update(data)
                print(f"  [OK]   {t:<13} {s.get('title', '')}")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  [ERR]  {t:<13} {s.get('title', '')} — {exc}", file=sys.stderr)
                base.update({"type": "error", "error": str(exc)})
            out.append(base)
        return out


def run(cfg, url_env, key_env, env, output_path):
    rep = Report(cfg, url_env, key_env)
    print(f"Ads Report for {rep.ws} .. {rep.we} (built {rep.today})")
    slides = rep.build_slides()
    date_label = f"{rep.ws:%b}. {rep.ws.day} - {rep.we:%b}. {rep.we.day}"
    html = env.get_template("ads_report.html.j2").render(
        slides=slides, date_label=date_label, assets=ASSETS,
        deck_title=f"Ads Report {date_label}",
        generated=dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        warnings=rep.warnings)
    Path(output_path).write_text(html, encoding="utf-8")
    errs = sum(1 for s in slides if s["type"] == "error")
    print(f"\nWrote {output_path}")
    print(f"Build summary: {len(slides) - errs} slide(s) built, {errs} failed, {len(rep.warnings)} warning(s).")
    return rep, slides
