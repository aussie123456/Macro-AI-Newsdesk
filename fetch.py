#!/usr/bin/env python3
"""Macro & AI Newsdesk: fetch prices and headlines. Standard library only, no LLM.

Usage:  python fetch.py --out docs/data

Writes into OUT:
  prices.json   one year of daily closes for 30 instruments, long format {date, symbol, name, group, unit, close}
  news.json     curated Google News headlines per theme {theme, published, source, title, url}
  meta.json     what was refreshed when, and anything that failed
and rewrites the 'asof' line of the brief row in themes.json so the page says what is fresh.

If Yahoo fails for a symbol, that symbol's rows are carried over from the previous prices.json.
If Google News fails for a theme, that theme's headlines are carried over from the previous news.json.
"""
import argparse, datetime as dt, html, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

# symbol -> (name, group, unit). The page matches on these exact symbol strings.
SYMBOLS = [
    ("^GSPC", "S&P 500", "broad", "United States"), ("^IXIC", "Nasdaq Composite", "broad", "United States"),
    ("^AXJO", "ASX 200", "broad", "Australia"), ("^KS11", "KOSPI", "broad", "Korea"), ("^N225", "Nikkei 225", "broad", "Japan"),
    ("CL=F", "WTI crude", "macro", "USD/bbl"), ("BZ=F", "Brent crude", "macro", "USD/bbl"), ("^TNX", "US 10-year yield", "macro", "%"),
    ("GC=F", "Gold", "macro", "USD/oz"), ("BTC-USD", "Bitcoin", "macro", "USD"), ("^VIX", "VIX", "macro", "index"),
    ("AUDUSD=X", "AUD/USD", "fx", "USD"), ("USDJPY=X", "USD/JPY", "fx", "JPY"), ("KRW=X", "USD/KRW", "fx", "KRW"),
    ("IGV", "Software (IGV)", "software", "ETF"), ("XPH", "Pharma (XPH)", "pharma", "ETF"), ("SOXX", "Semis (SOXX)", "semis", "ETF"),
    ("DTCR", "Data centre infra (DTCR)", "infra", "ETF"), ("VRT", "Vertiv", "infra", "stock"), ("ETN", "Eaton", "infra", "stock"),
    ("DLR", "Digital Realty", "infra", "stock"), ("EQIX", "Equinix", "infra", "stock"),
    ("URA", "Uranium (URA)", "energy", "ETF"), ("UNG", "US natural gas (UNG)", "energy", "ETF"), ("NG=F", "Henry Hub gas", "energy", "USD/MMBtu"),
    ("ICLN", "Clean energy (ICLN)", "energy", "ETF"),
    ("BTU", "Peabody", "coal", "stock"), ("CNR", "Core Natural Resources", "coal", "stock"), ("WHC.AX", "Whitehaven", "coal", "stock"), ("YAL.AX", "Yancoal", "coal", "stock"),
]

THEMES = ["markets", "ai", "iran", "geo", "uspol", "korea", "japan", "aus", "energy"]
QUERIES = {  # Google News RSS searches per theme (recency added below)
    "markets": ["Wall Street \"S&P 500\" stocks Treasury yields", "stocks bonds dollar week ahead markets"],
    "ai": ["AI \"data center\" OR \"data centre\"", "AI infrastructure financing debt bond \"private credit\"",
           "Nvidia OR OpenAI OR Anthropic OR hyperscaler capex", "IREN OR CoreWeave OR Nebius OR Oracle \"data center\""],
    "iran": ["Iran \"United States\" war strikes ceasefire talks", "Iran Hormuz oil tanker Israel"],
    "geo": ["geopolitics Taiwan Ukraine Russia China tensions", "Russia Ukraine peace talks sanctions"],
    "uspol": ["Trump tariffs Fed \"Federal Reserve\" Congress", "Supreme Court tariffs Treasury Bessent shutdown"],
    "korea": ["Korea KOSPI \"Bank of Korea\" Samsung \"SK Hynix\" economy", "South Korea won exports chips"],
    "japan": ["Japan Nikkei \"Bank of Japan\" yen economy", "Japan Takaichi stimulus JGB yields"],
    "aus": ["Australia ASX RBA Chalmers Albanese economy", "Australia inflation RBA rates ASX 200"],
    "energy": ["oil prices OPEC Brent", "uranium LNG \"natural gas\" coal renewables market"],
}
RECENCY = "when:3d"
PER_THEME = 20
PREFERRED = ["Reuters", "Bloomberg", "Financial Times", "FT", "The Wall Street Journal", "WSJ", "AFR", "Australian Financial Review",
             "Nikkei Asia", "The Korea Times", "Korea JoongAng Daily", "Yonhap", "The Japan Times", "ABC News", "The Sydney Morning Herald",
             "The Australian", "CNBC", "The Economist", "The New York Times", "The Guardian", "Al Jazeera", "South China Morning Post",
             "Chosunbiz", "The Korea Herald", "Kyodo News", "Barron's", "MarketWatch", "Associated Press", "AP News"]
BLOCKED = ["TradingView", "Investing.com", "MSN", "Yahoo Finance", "Yahoo Finance Australia", "Benzinga", "Motley Fool", "Seeking Alpha",
           "Simply Wall St", "MarketBeat", "Zacks", "TipRanks", "GuruFocus", "Newsweek", "Daily Mail", "New York Post", "Fox News", "Sky News Australia"]


def log(msg):
    print(msg, flush=True)


def get(url, tries=2, timeout=30):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def dump_json(path, obj, compact=True):
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ prices
def fetch_prices(prev_rows):
    rows, failed = [], []
    prev_by = {}
    for r in prev_rows:
        prev_by.setdefault(r.get("symbol"), []).append(r)
    for sym, name, group, unit in SYMBOLS:
        q = urllib.parse.quote(sym, safe="")
        got = None
        for host in ("query1", "query2"):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d" % (host, q)
            try:
                j = json.loads(get(url))
                res = j["chart"]["result"][0]
                ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
                got = []
                for t, c in zip(ts, cl):
                    if c is None:
                        continue
                    d = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
                    if got and got[-1]["date"] == d:  # one bar per session
                        got[-1]["close"] = round(float(c), 4)
                    else:
                        got.append({"date": d, "symbol": sym, "name": name, "group": group, "unit": unit, "close": round(float(c), 4)})
                if got:
                    break
            except Exception as e:  # noqa: BLE001
                log("  %s via %s failed: %s" % (sym, host, e))
        if got:
            rows.extend(got)
            log("  %s %d closes to %s" % (sym, len(got), got[-1]["date"]))
        elif sym in prev_by:
            rows.extend(prev_by[sym])
            failed.append(sym)
            log("  %s carried over from previous file" % sym)
        else:
            failed.append(sym)
            log("  %s missing" % sym)
        time.sleep(0.3)
    return rows, failed


# -------------------------------------------------------------------- news
def norm_title(t):
    t = re.sub(r"\s+-\s+[^-]+$", "", t)  # strip trailing " - Source"
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:70].strip()


def parse_rss(xml_bytes):
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = html.unescape((src_el.text or "").strip()) if src_el is not None and src_el.text else ""
        if not source and " - " in title:
            source = title.rsplit(" - ", 1)[1].strip()
        title = re.sub(r"\s+-\s+" + re.escape(source) + r"$", "", title).strip() if source else title
        try:
            published = dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=dt.timezone.utc)
        except Exception:  # noqa: BLE001
            try:
                published = dt.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=dt.timezone.utc)
            except Exception:  # noqa: BLE001
                published = None
        if title and link:
            out.append({"title": title, "url": link, "source": source, "published": published})
    return out


def rank(item):
    src = item["source"]
    pref = 0
    for i, p in enumerate(PREFERRED):
        if p.lower() in src.lower():
            pref = len(PREFERRED) - i
            break
    ts = item["published"].timestamp() if item["published"] else 0
    return (pref, ts)


def fetch_news(prev_rows):
    out, failed = [], []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    seen_global = set()
    for theme in THEMES:
        items = []
        ok = False
        for q in QUERIES[theme]:
            url = "https://news.google.com/rss/search?q=%s&hl=en-AU&gl=AU&ceid=AU:en" % urllib.parse.quote(q + " " + RECENCY)
            try:
                items.extend(parse_rss(get(url)))
                ok = True
            except Exception as e:  # noqa: BLE001
                log("  %s query failed: %s" % (theme, e))
            time.sleep(0.5)
        if not ok:
            keep = [r for r in prev_rows if r.get("theme") == theme]
            out.extend(keep)
            failed.append(theme)
            log("  %s: all queries failed, carried over %d headlines" % (theme, len(keep)))
            continue
        items = [i for i in items if i["published"] is None or i["published"] >= cutoff]
        items = [i for i in items if not any(b.lower() == i["source"].lower() or b.lower() in i["source"].lower() for b in BLOCKED)]
        items.sort(key=rank, reverse=True)
        picked, seen = [], set()
        for i in items:
            k = norm_title(i["title"])
            if not k or k in seen or k in seen_global:
                continue
            seen.add(k)
            picked.append(i)
            if len(picked) >= PER_THEME:
                break
        seen_global |= seen
        picked.sort(key=lambda i: i["published"].timestamp() if i["published"] else 0, reverse=True)
        for i in picked:
            out.append({"theme": theme, "published": (i["published"] or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": i["source"], "title": i["title"], "url": i["url"]})
        log("  %s: %d headlines from %d items" % (theme, len(picked), len(items)))
    return out, failed


# ------------------------------------------------------------------- asof
def sydney_now():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("Australia/Sydney")), now
    except Exception:  # noqa: BLE001
        return now + dt.timedelta(hours=11), now


def fmt_syd(d):
    h = d.strftime("%I").lstrip("0") or "12"
    return "%s %s %s %s, %s:%s%s %s" % (d.strftime("%A"), d.day, d.strftime("%b"), d.year, h, d.strftime("%M"), d.strftime("%p").lower(), d.tzname() or "AEDT")


def stamp_brief(themes, failed_syms, failed_themes):
    syd, now = sydney_now()
    for t in themes:
        if t.get("theme") != "brief":
            continue
        written = t.get("summaries_written_at")
        try:
            wd = dt.datetime.strptime(written, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            from zoneinfo import ZoneInfo
            wtxt = fmt_syd(wd.astimezone(ZoneInfo("Australia/Sydney")))
        except Exception:  # noqa: BLE001
            wtxt = written or "an earlier run"
        line = "%s. Prices and headlines refreshed; written summaries and effects cards from %s." % (fmt_syd(syd), wtxt)
        if failed_syms:
            line += " Prices carried over for: %s." % ", ".join(failed_syms)
        if failed_themes:
            line += " Headlines carried over for: %s." % ", ".join(failed_themes)
        t["asof"] = line
    return themes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/data")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    p_prices, p_news, p_themes, p_meta = (os.path.join(a.out, f) for f in ("prices.json", "news.json", "themes.json", "meta.json"))
    prev_prices, prev_news, themes = load_json(p_prices, []), load_json(p_news, []), load_json(p_themes, [])

    log("prices")
    rows, failed_syms = fetch_prices(prev_prices)
    if rows:
        dump_json(p_prices, rows)
    else:
        log("  no prices fetched at all; previous file kept")

    log("news")
    news, failed_themes = fetch_news(prev_news)
    if news:
        dump_json(p_news, news, compact=False)
    else:
        log("  no headlines fetched at all; previous file kept")

    if themes:
        dump_json(p_themes, stamp_brief(themes, failed_syms, failed_themes), compact=False)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dump_json(p_meta, {"prices_at": now if rows else None, "news_at": now if news else None,
                       "prices_last_date": max((r["date"] for r in rows), default=None),
                       "failed_symbols": failed_syms, "failed_themes": failed_themes, "by": "GitHub Actions fetch.py"}, compact=False)
    log("done: %d price rows (%d symbols failed), %d headlines (%d themes failed)" % (len(rows), len(failed_syms), len(news), len(failed_themes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
