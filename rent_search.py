#!/usr/bin/env python3
"""Search rent.com.au for small 1-bedroom rentals around Redcliffe and Brisbane's north.

Usage:  python rent_search.py            (writes data/latest.json)
        python rent_search.py --quick    (Redcliffe Peninsula only, for testing)

For every matching ad this records the ad's details, the contact details the ad
lists, and words from the description that matter for Mum (stairs, lift, pets,
shops, over-50s). It does not judge anything; the routine reads the output and
decides.
"""
import datetime
import html
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "MumRentalSearch/1.0 (personal rental search for a family member)"
MAX_RENT = 430          # budget is $400; up to $430 is shown as "a bit over"
PAUSE = 1.2             # seconds between requests, to be polite to the site
HERE = pathlib.Path(__file__).resolve().parent

AREAS = {
    "Redcliffe Peninsula": [
        "redcliffe-qld-4020", "scarborough-qld-4020", "newport-qld-4020",
        "margate-qld-4019", "woody-point-qld-4019", "clontarf-qld-4019",
        "kippa-ring-qld-4021", "rothwell-qld-4022",
    ],
    "Deception Bay / North Lakes": [
        "deception-bay-qld-4508", "mango-hill-qld-4509", "north-lakes-qld-4509",
        "griffin-qld-4503", "murrumba-downs-qld-4503", "kallangur-qld-4503",
    ],
    "Sandgate / Brighton": [
        "brighton-qld-4017", "sandgate-qld-4017", "shorncliffe-qld-4017",
        "deagon-qld-4017", "bracken-ridge-qld-4017",
    ],
    "Northern suburbs": [
        "bald-hills-qld-4036", "carseldine-qld-4034", "aspley-qld-4034",
        "zillmere-qld-4034", "boondall-qld-4034", "geebung-qld-4034",
        "taigum-qld-4018", "fitzgibbon-qld-4018", "chermside-qld-4032",
        "chermside-west-qld-4032", "kedron-qld-4031", "gordon-park-qld-4031",
        "lutwyche-qld-4030", "wooloowin-qld-4030", "windsor-qld-4030",
        "wavell-heights-qld-4012", "nundah-qld-4012", "northgate-qld-4013",
        "virginia-qld-4014", "banyo-qld-4014", "nudgee-qld-4014",
        "clayfield-qld-4011", "stafford-qld-4053", "everton-park-qld-4053",
        "albany-creek-qld-4035", "strathpine-qld-4500", "bray-park-qld-4500",
        "lawnton-qld-4501", "petrie-qld-4502",
    ],
}

SKIP_TYPES = {"car space", "parking", "storage", "room", "share house", "commercial"}

FLAG_PATTERNS = {
    "ground": r"ground[- ]floor|ground[- ]level|single[- ]level|single[- ]stor[e]?y|low[- ]?set|"
              r"no stairs|step[- ]free|stair[- ]free|level entry|flat access|wheelchair|"
              r"at street level|no steps",
    "lift": r"\blifts?\b|elevators?",
    "stairs": r"stairs|staircase|stairwell|\bsteps\b|upstairs|downstairs|first[- ]floor|"
              r"second[- ]floor|third[- ]floor|top[- ]floor|upper[- ]level|upper[- ]floor|"
              r"walk[- ]?up|high[- ]?set|\blevel [1-9]\b",
    "pets": r"pets?[- ]friendly|pets? (?:are )?(?:allowed|considered|welcome|negotiable|ok|okay)|"
            r"pets? on application|pet application|no pets|pets? not (?:allowed|permitted)|"
            r"strictly no pets|\bcats?\b|\bdogs?\b|body corporate",
    "seniors": r"over[- ]?50s?|over[- ]?55s?|over[- ]?60s?|\bseniors?\b|retire(?:e|es|d|ment)|"
               r"mature[- ]aged|pensioners?|aged care",
    "shops": r"shops?|shopping|supermarket|woolworths|coles|aldi|\bbus\b|buses|train|"
             r"transport|doctor|medical|hospital|pharmacy|chemist",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def rsc_text(page):
    """The page's data travels as JSON string chunks in self.__next_f.push([1, "..."])."""
    parts = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page, re.S)
    return "".join(json.loads(p) for p in parts)


def text_chunks(text):
    """Long strings (like ad descriptions) are sent as separate rows, `<hex id>:T<hex byte length>,<text>`,
    and the property object only holds a "$<hex id>" pointer to them."""
    raw = text.encode("utf-8")
    out = {}
    for m in re.finditer(rb"(?:^|\n)([0-9a-f]+):T([0-9a-f]+),", raw):
        start = m.end()
        out[m.group(1).decode()] = raw[start:start + int(m.group(2), 16)].decode("utf-8", "replace")
    return out


def resolve(value, chunks):
    if isinstance(value, str):
        m = re.fullmatch(r"\$([0-9a-f]+)", value)
        if m and m.group(1) in chunks:
            return chunks[m.group(1)]
    return value


def grab_obj(text, start):
    """Return the JSON object that starts at text[start] == '{'."""
    depth, in_str, esc = 0, False, False
    for k in range(start, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:k + 1]
    return None


def listing_objects(text):
    out = []
    for m in re.finditer(r'\{"id":(\d+),"url":"https://www\.rent\.com\.au/property/', text):
        raw = grab_obj(text, m.start())
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return out


def rent_number(obj):
    if isinstance(obj.get("weekly_rent_number"), (int, float)):
        return int(obj["weekly_rent_number"])
    m = re.search(r"([\d,]+)", obj.get("weekly_rent") or "")
    return int(m.group(1).replace(",", "")) if m else None


def search_suburb(slug, log):
    found, page = [], 1
    while page <= 6:
        q = urllib.parse.urlencode({"rent_high": MAX_RENT, "surrounding_suburbs": 0, "page": page})
        url = f"https://www.rent.com.au/properties/{slug}?{q}"
        try:
            text = rsc_text(fetch(url))
        except urllib.error.HTTPError as e:
            log.append(f"{slug}: HTTP {e.code}")
            break
        except Exception as e:  # network trouble: note it and move on
            log.append(f"{slug}: {e}")
            break
        time.sleep(PAUSE)
        objs = [o for o in listing_objects(text) if o.get("id")]
        # the search page repeats each ad in a few places; keep one per id
        seen = {o["id"] for o in found}
        new = []
        for o in objs:
            if o["id"] not in seen:
                seen.add(o["id"])
                new.append(o)
        found.extend(new)
        if len({o["id"] for o in objs}) < 25:
            break
        page += 1
    return found


def pick(obj, *keys):
    for k in keys:
        if obj is None:
            return None
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


def detail(url, log):
    try:
        page = fetch(url)
    except Exception as e:
        log.append(f"detail {url}: {e}")
        return None
    time.sleep(PAUSE)
    text = rsc_text(page)
    k = text.find('"property":{"id":')
    if k < 0:
        log.append(f"detail {url}: no property data")
        return None
    obj = json.loads(grab_obj(text, k + len('"property":')))
    chunks = text_chunks(text)
    for key in ("description", "byline"):
        obj[key] = resolve(obj.get(key), chunks)
    # contact details the site passes to its application form (agent name, company, email)
    papf = {}
    for m in re.finditer(r"papf_[a-z]+=[^&\"]*", text):
        key, _, val = m.group(0).partition("=")
        papf[key] = urllib.parse.unquote_plus(val)
    obj["_papf"] = papf
    return obj


def flags_for(description):
    out = {}
    low = description or ""
    for name, pat in FLAG_PATTERNS.items():
        hits = []
        for m in re.finditer(pat, low, re.I):
            a, b = max(0, m.start() - 60), min(len(low), m.end() + 60)
            hits.append(re.sub(r"\s+", " ", low[a:b]).strip())
        if hits:
            out[name] = hits[:6]
    return out


def clean_record(area, card, full):
    src = full or card
    desc = (full or {}).get("description") or ""
    company = pick(full, "company") or pick(card, "contact", "company") or {}
    contact = pick(full, "contact") or pick(card, "contact") or {}
    papf = (full or {}).get("_papf", {})
    person = " ".join(x for x in [contact.get("first_name"), contact.get("last_name")] if x).strip()
    walk = src.get("walkability_score") or {}
    feed_email = papf.get("papf_realestatem") or None
    emails_in_text = sorted(set(EMAIL_RE.findall(desc)))
    street = (src.get("street_address") or "").strip().rstrip(",").strip()
    gallery = (full or {}).get("gallery") or card.get("gallery") or []
    photos = [g["url"] for g in gallery if g.get("url") and g.get("gallery_type", "photo") == "photo"][:3]
    return {
        "photos": photos,
        "id": f"rent-{src['id']}",
        "source": "rent.com.au",
        "url": src.get("url"),
        "area": area,
        "address": street,
        "suburb": (src.get("suburb") or "").title(),
        "postcode": src.get("postcode"),
        "type": src.get("property_type"),
        "beds": src.get("bedrooms"),
        "baths": src.get("bathrooms"),
        "cars": src.get("car_spaces"),
        "price": rent_number(src),
        "bond": src.get("bond"),
        "available": src.get("date_available"),
        "listedAt": src.get("activated_at"),
        "petsListed": src.get("pets_allowed"),
        "furnished": src.get("furnished"),
        "walkScore": walk.get("walk_score"),
        "walkCategory": walk.get("walk_category"),
        "transitScore": walk.get("transit_score"),
        "transitCategory": walk.get("transit_category"),
        "topFeatures": [f.get("description") for f in (src.get("top_4_features") or src.get("top_3_features") or [])],
        "inspections": src.get("next_inspections") or [],
        "lat": src.get("latitude"),
        "lng": src.get("longitude"),
        "byline": src.get("byline"),
        "description": html.unescape(desc),
        "flags": flags_for(desc),
        "contactPerson": {
            "name": person or None,
            "phone": contact.get("mobile_number") or contact.get("phone_number"),
            "phoneAlt": contact.get("phone_number"),
        },
        "company": {
            "name": company.get("trading_name"),
            "phone": company.get("phone"),
            "email": company.get("email_address"),
            "address": company.get("address"),
            "profileUrl": company.get("company_profile_url"),
        },
        "feedAgentName": papf.get("papf_realestateag"),
        "feedCompany": papf.get("papf_realestateco"),
        "feedEmail": feed_email,
        "emailsInText": emails_in_text,
    }


def main():
    quick = "--quick" in sys.argv
    areas = {"Redcliffe Peninsula": AREAS["Redcliffe Peninsula"]} if quick else AREAS
    log, cards = [], {}
    for area, slugs in areas.items():
        for slug in slugs:
            for o in search_suburb(slug, log):
                price = rent_number(o)
                ptype = (o.get("property_type") or "").lower()
                if o.get("bedrooms") != 1 or price is None or price > MAX_RENT:
                    continue
                if ptype in SKIP_TYPES:
                    continue
                if re.match(r"\s*room\b", o.get("street_address") or "", re.I):
                    continue  # a room in a share house, not a place of her own
                cards.setdefault(o["id"], (area, o))
    records = []
    for _id, (area, card) in cards.items():
        full = detail(card["url"], log)
        records.append(clean_record(area, card, full))
    records.sort(key=lambda r: (list(AREAS).index(r["area"]), r["price"] or 0))
    out = {
        "searchedAt": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
        "maxRent": MAX_RENT,
        "suburbsSearched": sum(len(v) for v in areas.values()),
        "count": len(records),
        "problems": log,
        "listings": records,
    }
    data = HERE / "data"
    data.mkdir(exist_ok=True)
    (data / "latest.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(records)} one-bedroom ads at ${MAX_RENT} or less. Problems: {len(log)}")
    for p in log:
        print("  problem:", p)
    for r in records:
        print(f"  {r['area'][:12]:12} ${r['price']:>4}  {r['address']}, {r['suburb']}  "
              f"pets={r['petsListed']}  email={r['feedEmail'] or r['company']['email'] or '-'}")


if __name__ == "__main__":
    main()
