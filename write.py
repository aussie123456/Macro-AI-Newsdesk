#!/usr/bin/env python3
"""Macro & AI Newsdesk: write the theme summaries and the second/third-order effects cards with Claude.

Usage:  python write.py --out docs/data
Reads OUT/prices.json, OUT/news.json and the previous OUT/themes.json and OUT/derivatives.json, asks Claude
for new summaries and drivers, validates them against the page's schema, and overwrites the two files.
Needs ANTHROPIC_API_KEY; without it (or on any failure) it leaves the previous files untouched and exits 0.
"""
import argparse, datetime as dt, json, os, re, sys

MODEL = "claude-opus-5-5"
THEME_SPEC = [("brief", 0, "Brief"), ("markets", 1, "Markets wrap and week ahead"), ("ai", 2, "AI and data centres"), ("iran", 3, "US and Iran war"),
              ("geo", 4, "Geopolitics"), ("uspol", 5, "US politics and policy"), ("korea", 6, "Korea"), ("japan", 7, "Japan"),
              ("aus", 8, "Australia"), ("energy", 9, "Energy and commodities")]
FIELDS = {"last", "d1", "w1", "m1", "m3", "ytd"}
OPS = {">", "<", ">=", "<=", "always"}

READER = ("The reader is a capital-markets professional at IREN, an AI data-centre and power company listed in the US and based in Sydney. "
          "Write plain, specific prose. Name the numbers (index moves, yields, oil, FX). No markdown, no bullet characters, no hype.")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def perf_table(prices):
    """Same arithmetic as the page's 'perf' calc: last close and changes vs 1d, 1w, 1m, 3m, end-2025."""
    by = {}
    for r in prices:
        by.setdefault(r["symbol"], {"name": r["name"], "group": r["group"], "unit": r["unit"], "s": []})
        by[r["symbol"]]["s"].append((str(r["date"])[:10], float(r["close"])))
    out = []
    for sym, o in by.items():
        s = sorted(o["s"])
        if not s:
            continue
        last_d, last_c = s[-1]
        def at(iso):
            v = None
            for d, c in s:
                if d <= iso:
                    v = c
                else:
                    break
            return v
        def back(n, unit):
            d = dt.date.fromisoformat(last_d)
            if unit == "d":
                d = d - dt.timedelta(days=n)
            else:
                m = d.month - n
                y = d.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                d = d.replace(year=y, month=m, day=min(d.day, 28))
            return at(d.isoformat())
        bp = sym == "^TNX"
        def chg(ref):
            if ref is None:
                return None
            return round((last_c - ref) * 100) if bp else round(last_c / ref - 1, 4)
        out.append({"symbol": sym, "name": o["name"], "group": o["group"], "unit": o["unit"], "date": last_d, "last": last_c, "kind": "bp" if bp else "pct",
                    "d1": chg(s[-2][1] if len(s) > 1 else None), "w1": chg(back(7, "d")), "m1": chg(back(1, "m")), "m3": chg(back(3, "m")), "ytd": chg(at("2025-12-31"))})
    return out


def fmt_perf(perf):
    lines = []
    for p in perf:
        def f(v):
            if v is None:
                return "n/a"
            return ("%+d bp" % v) if p["kind"] == "bp" else ("%+.1f%%" % (v * 100))
        lines.append("%s (%s, %s): last %.4g on %s; 1d %s, 1w %s, 1m %s, 3m %s, ytd %s" % (p["name"], p["symbol"], p["unit"], p["last"], p["date"], f(p["d1"]), f(p["w1"]), f(p["m1"]), f(p["m3"]), f(p["ytd"])))
    return "\n".join(lines)


def fmt_news(news):
    by = {}
    for n in news:
        by.setdefault(n["theme"], []).append(n)
    parts = []
    for t, _, label in THEME_SPEC:
        if t == "brief":
            continue
        parts.append("## %s (%s)" % (label, t))
        for n in by.get(t, [])[:20]:
            parts.append("- [%s] %s (%s)" % (n["published"][:16].replace("T", " "), n["title"], n["source"]))
    return "\n".join(parts)


def sydney_asof():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        s = now.astimezone(ZoneInfo("Australia/Sydney"))
    except Exception:  # noqa: BLE001
        s = now + dt.timedelta(hours=11)
    h = s.strftime("%I").lstrip("0") or "12"
    return "%s %d %s %d, %s:%s%s %s" % (s.strftime("%A"), s.day, s.strftime("%b"), s.year, h, s.strftime("%M"), s.strftime("%p").lower(), s.tzname() or "AEDT"), now


def extract_json(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in reply: " + text[:200])
    return json.loads(m.group(0))


def ask(client, system, user, max_tokens):
    kwargs = dict(model=MODEL, max_tokens=max_tokens, output_config={"effort": "medium"}, system=system, messages=[{"role": "user", "content": user}])
    try:
        # Server-side refusal fallback: re-runs on another model if a safety classifier declines the request.
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused: %s" % getattr(resp, "stop_details", None))
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("reply cut off at max_tokens")
    return "".join(b.text for b in resp.content if b.type == "text")


def validate_themes(arr, asof_line):
    want = {t: (o, l) for t, o, l in THEME_SPEC}
    out = {}
    for row in arr:
        t = row.get("theme")
        if t not in want:
            continue
        summary = str(row.get("summary", "")).strip()
        if len(summary.split()) < 40:
            raise ValueError("summary too short for " + t)
        out[t] = {"theme": t, "order": want[t][0], "label": want[t][1], "summary": summary}
    missing = [t for t in want if t not in out]
    if missing:
        raise ValueError("themes missing: " + ", ".join(missing))
    out["brief"]["asof"] = asof_line + " US prices are overnight closes; Asian indices, oil, gold, Bitcoin and FX are the latest available prints."
    return [out[t] for t, _, _ in THEME_SPEC]


def validate_derivs(arr, symbols):
    out = []
    for i, d in enumerate(arr):
        sym, field, op = d.get("symbol"), d.get("field"), d.get("op")
        if sym not in symbols or field not in FIELDS or op not in OPS:
            raise ValueError("bad driver %r: symbol/field/op" % d.get("id"))
        thr = d.get("threshold")
        if op == "always":
            thr = None
        elif not isinstance(thr, (int, float)):
            raise ValueError("bad threshold on %r" % d.get("id"))
        for k in ("second", "third", "suggest", "watch"):
            if not isinstance(d.get(k), list) or not d[k]:
                raise ValueError("missing %s on %r" % (k, d.get("id")))
        out.append({"id": re.sub(r"[^a-z0-9_-]", "", str(d.get("id", "d%d" % i)).lower()) or "d%d" % i, "order": i + 1, "driver": str(d.get("driver", "")).strip(),
                    "symbol": sym, "field": field, "op": op, "threshold": thr, "trigger_label": str(d.get("trigger_label", "")).strip(),
                    "second": [str(x) for x in d["second"]][:4], "third": [str(x) for x in d["third"]][:4],
                    "suggest": [str(x) for x in d["suggest"]][:3], "watch": [str(x) for x in d["watch"]][:4]})
    if not 5 <= len(out) <= 10:
        raise ValueError("expected 6 to 9 drivers, got %d" % len(out))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/data")
    a = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set; summaries and effects cards left as they were")
        return 0
    p_prices, p_news, p_themes, p_derivs = (os.path.join(a.out, f) for f in ("prices.json", "news.json", "themes.json", "derivatives.json"))
    prices, news = load(p_prices, []), load(p_news, [])
    prev_themes, prev_derivs = load(p_themes, []), load(p_derivs, [])
    if not prices or not news:
        print("no prices or news to write from; nothing changed")
        return 0
    perf = perf_table(prices)
    symbols = {p["symbol"] for p in perf}
    asof_line, now = sydney_asof()
    import anthropic
    client = anthropic.Anthropic()

    context = "Date and time in Sydney: %s.\n\n# Price moves\n%s\n\n# Curated headlines\n%s" % (asof_line, fmt_perf(perf), fmt_news(news))

    # themes
    try:
        spec = "; ".join("%s (order %d, label '%s')" % (t, o, l) for t, o, l in THEME_SPEC)
        user = (context + "\n\nWrite themes.json: a JSON array with one object per theme {theme, order, label, summary} using exactly these ids, orders and labels: "
                + spec + ". Each summary is 90 to 160 words written from the headlines and the price moves above. The brief row's summary is a 60 to 90 word "
                "takeaway for the whole page naming the two or three things that matter most for an AI data-centre capital-markets reader at IREN. "
                "Reply with the JSON array only.")
        themes = validate_themes(extract_json(ask(client, READER, user, 8000)), asof_line)
        themes[0]["summaries_written_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(p_themes, "w", encoding="utf-8") as f:
            json.dump(themes, f, ensure_ascii=False, indent=1)
        print("themes.json written (%d rows)" % len(themes))
    except Exception as e:  # noqa: BLE001
        print("themes not rewritten:", e, file=sys.stderr)

    # derivatives
    try:
        prev_ids = ", ".join(d.get("id", "") for d in prev_derivs) or "none"
        user = (context + "\n\nPrevious driver ids: " + prev_ids + ".\n\nWrite derivatives.json: 6 to 9 drivers reflecting today's regime as a JSON array of "
                "{id, order, driver, symbol, field, op, threshold, trigger_label, second, third, suggest, watch}. symbol must be one of: " + ", ".join(sorted(symbols)) +
                ". field is one of last, d1, w1, m1, m3, ytd (last = latest close; the others are fractional changes such as -0.05 for -5%; for ^TNX they are basis points). "
                "op is one of '>', '<', '>=', '<=', 'always' (threshold null for always). The page evaluates the trigger against live prices, so choose triggers that "
                "describe when the driver is live (for example ^TNX last > 5; BZ=F last > 100; SOXX w1 < -0.05). second and third are arrays of 2 to 4 one-sentence "
                "effects (second-order: who is hit next, policy and flow consequences; third-order: structural, financing and political consequences, especially for "
                "AI data-centre funding, power procurement, converts and private credit). suggest is 2 to 3 concrete suggestions for an IREN capital-markets reader "
                "(funding windows, what to emphasise with investors, what to hedge or stress-test), never personal investment advice. watch is 2 to 4 short labels. "
                "Keep ids stable where a driver persists (rates, oil, chips, power, russia, japan, aus, korea, credit) and add or retire drivers as the regime changes. "
                "Reply with the JSON array only.")
        derivs = validate_derivs(extract_json(ask(client, READER, user, 8000)), symbols)
        with open(p_derivs, "w", encoding="utf-8") as f:
            json.dump(derivs, f, ensure_ascii=False, indent=1)
        print("derivatives.json written (%d drivers)" % len(derivs))
    except Exception as e:  # noqa: BLE001
        print("derivatives not rewritten:", e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
