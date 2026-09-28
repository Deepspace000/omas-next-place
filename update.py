#!/usr/bin/env python3
"""Update Oma's Next Place from the latest search.

  python update.py                        after rent_search.py: merge the search, rebuild data/site.json
  python update.py --rebuild              only rebuild data/site.json (after Claude adds assessments or contacts)
  python update.py --request-email-retry  note that the search button was pressed, so Claude retries emails
  python update.py --report               print which ads Claude hasn't read and which have no email; change nothing

Who writes what (so the GitHub search and Claude never edit the same file):
  data/latest.json        rent_search.py  this search
  data/listings.json      update.py       every ad seen so far, with first and last seen times
  data/runs.json          update.py       the last 60 searches
  data/requests.json      update.py       when the search button was last pressed
  data/site.json          update.py       what the page shows
  data/assessments.json   Claude          Claude's reading of each ad
  data/contacts/*.json    Claude          emails and phone numbers Claude found, agency details, seniors places
  data/claude-state.json  Claude          when Claude last reviewed ads and retried emails

An ad Claude hasn't read yet gets a rough reading from the rules below, marked "not checked by Claude yet".
"""
import datetime
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
AREAS = ["Redcliffe Peninsula", "Deception Bay / North Lakes", "Sandgate / Brighton", "Northern suburbs"]
BRISBANE = datetime.timezone(datetime.timedelta(hours=10))
TEAM_WORDS = re.compile(r"\b(department|team|office|leasing|rentals?|admin|property|for rent|management|realty|agency|landlord|listings?)\b", re.I)
# addresses that look like contacts but aren't: the private-listing service's catch-all inbox
NOT_A_CONTACT = {"rentals@forsalebyagent.com.au"}

ROOM = re.compile(r"rooming accommodation|boarding house|share ?house|shared house|private room|co-?living|room for rent|form r18", re.I)
NO_PETS = re.compile(r"no pets|not suitable for (?:any )?pets|unsuitable for pets|pets? (?:are )?not (?:allowed|permitted|accepted)|"
                     r"strictly no pets|no (?:couples|children)[^.]{0,40}\bpets\b", re.I)
PETS_OK = re.compile(r"pets? (?:are )?(?:allowed|considered|welcome|negotiable|ok|okay)|pet[- ]friendly|pets? on application", re.I)
GROUND = re.compile(r"ground[- ]floor (?:unit|flat|apartment|studio)|on the ground floor|single[- ]level|single[- ]stor[e]?y|low[- ]?set|"
                    r"no stairs|step[- ]free|level entry", re.I)
LIFT = re.compile(r"\blifts?\b|elevators?", re.I)
UPSTAIRS = re.compile(r"upstairs|first[- ]floor|second[- ]floor|third[- ]floor|top[- ]floor|upper[- ]level|walk[- ]?up|high[- ]?set", re.I)
KINDS = {"flat": "1-bedroom flat", "unit": "1-bedroom unit", "apartment": "1-bedroom apartment", "studio": "studio",
         "villa": "1-bedroom villa", "townhouse": "1-bedroom townhouse", "duplex": "1-bedroom duplex"}


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def now_brisbane():
    return datetime.datetime.now(BRISBANE).replace(microsecond=0)


def phone_fmt(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("61"):
        digits = "0" + digits[2:]
    if len(digits) == 10 and digits.startswith("04"):
        return f"{digits[:4]} {digits[4:7]} {digits[7:]}"
    if len(digits) == 10 and digits.startswith("1300"):
        return f"{digits[:4]} {digits[4:7]} {digits[7:]}"
    if len(digits) == 10 and digits[:2] in ("02", "03", "07", "08"):
        return f"({digits[:2]}) {digits[2:6]} {digits[6:]}"
    if len(digits) == 8:
        return f"(07) {digits[:4]} {digits[4:]}"
    return raw.strip()


def words(name):
    return set(re.findall(r"[a-z0-9]+", (name or "").lower())) - {"real", "estate", "the", "property", "management", "pty", "ltd"}


def agency_for(company, agencies):
    """Find a researched agency whose name matches the ad's agency name."""
    w = words(company)
    if not w:
        return None
    for a in agencies:
        aw = words(a.get("name"))
        if aw and (aw <= w or w <= aw):
            return a
    return None


def default_contacts(r, agencies):
    out = []
    cp, co = r.get("contactPerson") or {}, r.get("company") or {}
    name = (cp.get("name") or "").strip()
    feed = r.get("feedEmail") if r.get("feedEmail") not in NOT_A_CONTACT else None
    if name or cp.get("phone"):
        out.append({"name": name or co.get("name") or "Agent", "role": "Agent on the ad",
                    "email": feed if (not r.get("feedAgentName") or r["feedAgentName"].strip().lower() == name.lower()) else None,
                    "phone": phone_fmt(cp.get("phone")), "source": "rent.com.au ad"})
    a = agency_for(co.get("name"), agencies)
    if a and (a.get("rentalsEmail") or a.get("generalEmail")):
        out.append({"name": a.get("name"), "role": "Agency rentals email (found earlier)",
                    "email": a.get("rentalsEmail") or a.get("generalEmail"), "phone": phone_fmt(a.get("phone")),
                    "source": a.get("source")})
    elif co.get("email") and co["email"] not in NOT_A_CONTACT:
        out.append({"name": co.get("name"), "role": "Agency office", "email": co["email"],
                    "phone": phone_fmt(co.get("phone")), "source": "rent.com.au ad"})
    return out


def rough_assessment(r):
    """A first reading by simple rules, until Claude reads the ad."""
    desc = r.get("description") or ""
    addr = r.get("address") or ""
    if re.match(r"\s*room\b", addr, re.I) or ROOM.search(desc):
        return {"verdict": "no", "reason": "A room in a shared house (not checked by Claude yet)"}
    if NO_PETS.search(desc):
        return {"verdict": "no", "reason": "The ad says no pets (not checked by Claude yet)"}
    if r.get("petsListed") is True or PETS_OK.search(desc):
        cat = {"state": "ok", "note": "The ad says pets are allowed or considered."}
    else:
        cat = {"state": "ask", "note": "The ad doesn't say. Ask about her cat."}
    if GROUND.search(desc):
        stairs = {"state": "none", "note": "The ad mentions ground floor or single level."}
    elif LIFT.search(desc):
        stairs = {"state": "lift", "note": "The ad mentions a lift."}
    elif UPSTAIRS.search(desc):
        stairs = {"state": "ask", "note": "The ad mentions stairs or an upper floor. Ask."}
    else:
        stairs = {"state": "ask", "note": "The ad doesn't say. Ask."}
    headline = (r.get("byline") or "").strip()
    summary = "Claude hasn't read this ad yet."
    if headline:
        summary += f' The ad\'s headline: "{headline[:100]}"'
    kind = KINDS.get((r.get("type") or "").lower(), "1-bedroom place")
    return {"verdict": "maybe", "kind": kind, "summary": summary, "cat": cat, "stairs": stairs}


def walkable(r):
    ws = r.get("walkScore")
    if ws is None:
        return {"state": "some", "note": "Not known. Ask about shops and buses."}
    if ws >= 50:
        return {"state": "close", "note": "Walkable."}
    return {"state": "far", "note": "Not walkable: most errands need a car or bus."}


def merge_search(seen, runs, stamp):
    latest = load(DATA / "latest.json", None)
    if not latest:
        raise SystemExit("data/latest.json is missing: run rent_search.py first")
    failed = {p.split(":")[0].strip() for p in latest.get("problems", []) if not p.startswith("detail ")}
    if latest.get("suburbsSearched") and len(failed) >= latest["suburbsSearched"]:
        raise SystemExit("Every suburb search failed; nothing changed.")
    current = {r["id"]: r for r in latest["listings"]}

    def slug(raw):
        return f"{(raw.get('suburb') or '').lower().replace(' ', '-')}-qld-{raw.get('postcode')}"

    new, gone = [], []
    for _id, r in current.items():
        rec = seen.get(_id)
        if not rec:
            rec = {"firstSeenAt": stamp}
            new.append(_id)
        rec.setdefault("firstSeenAt", (rec.get("firstSeen") or stamp[:10]) + "T06:36:00+10:00")
        rec.update({"raw": r, "lastSeenAt": stamp, "gone": False})
        for k in ("goneSince", "goneRun", "firstSeen", "firstSeenRun", "lastSeen"):
            rec.pop(k, None)
        seen[_id] = rec
    for _id, rec in seen.items():
        if _id not in current and not rec.get("gone") and slug(rec["raw"]) not in failed:
            rec.update({"gone": True, "goneAt": stamp})
            gone.append(_id)
    runs.insert(0, {"at": stamp, "found": latest["count"], "new": new, "gone": gone,
                    "problems": latest.get("problems", []), "trigger": os.environ.get("GITHUB_EVENT_NAME", "manual")})
    del runs[60:]


def build_site(seen, runs, stamp, write=True):
    assess = load(DATA / "assessments.json", {})
    researched, agencies, seniors, register = {}, [], [], None
    for f in sorted((DATA / "contacts").glob("*.json")):
        c = load(f, {})
        for item in c.get("listings", []):
            researched[item["id"]] = item
        agencies.extend(c.get("agencies", []))
        seniors.extend(c.get("places", []))
        register = c.get("housingRegister") or register

    listings = []
    for _id, rec in seen.items():
        r = rec["raw"]
        a = assess.get(_id)
        reviewed = a is not None
        a = a or rough_assessment(r)
        res = researched.get(_id, {})
        contacts = []
        for c in (res.get("contacts") or a.get("contacts") or default_contacts(r, agencies)):
            if c.get("email") in NOT_A_CONTACT:
                c = {**c, "email": None}
                if not c.get("phone"):
                    continue
            contacts.append({**c, "phone": phone_fmt(c.get("phone")),
                             "team": c.get("team", bool(TEAM_WORDS.search(c.get("name") or "")))})
        doc = {
            "id": _id, "url": r.get("url"), "area": r.get("area"),
            "address": re.sub(r"^\s*unit\s+", "", r.get("address") or "", flags=re.I) or "(address not shown)",
            "suburb": r.get("suburb"), "postcode": r.get("postcode"), "type": r.get("type"), "beds": r.get("beds"),
            "price": r.get("price"), "bond": r.get("bond") or None, "available": r.get("available"),
            "photos": r.get("photos") or [],
            "firstSeenAt": rec.get("firstSeenAt"), "lastSeenAt": rec.get("lastSeenAt"),
            "gone": rec.get("gone", False), "goneAt": rec.get("goneAt"),
            "reviewed": reviewed, "verdict": a["verdict"], "reason": a.get("reason"), "summary": a.get("summary"),
            "kind": a.get("kind"), "cat": a.get("cat"), "stairs": a.get("stairs"), "shops": a.get("shops") or walkable(r),
            "smsCode": a.get("smsCode"), "agency": (r.get("company") or {}).get("name"), "contacts": contacts,
            "enquiryForm": res.get("enquiryForm") or a.get("enquiryForm"),
            "otherAds": [o for o in (res.get("otherAds") or []) if "rent.com.au" not in (o.get("url") or "")][:2],
        }
        listings.append({k: v for k, v in doc.items() if v not in (None, "")})

    listings.sort(key=lambda d: (AREAS.index(d["area"]) if d.get("area") in AREAS else 9, d.get("price") or 0))
    site = {
        "generatedAt": stamp,
        "lastSearch": runs[0] if runs else None,
        "runs": runs[:10],
        "requests": load(DATA / "requests.json", {}),
        "claude": load(DATA / "claude-state.json", {}),
        "listings": listings,
        "seniors": seniors,
        "housingRegister": register,
    }
    if write:
        save(DATA / "site.json", site)
    return listings


def main():
    stamp = now_brisbane().isoformat()
    if "--request-email-retry" in sys.argv:
        req = load(DATA / "requests.json", {})
        req["searchButtonAt"] = stamp
        save(DATA / "requests.json", req)
        print("noted: search button pressed at", stamp)
        return
    seen = load(DATA / "listings.json", {})
    runs = load(DATA / "runs.json", [])
    report_only = "--report" in sys.argv   # print what needs Claude, change nothing
    if "--rebuild" not in sys.argv and not report_only:
        merge_search(seen, runs, stamp)
        save(DATA / "listings.json", seen)
        save(DATA / "runs.json", runs)
    listings = build_site(seen, runs, stamp, write=not report_only)
    live = [d for d in listings if not d.get("gone")]
    counts = {v: sum(1 for d in live if d["verdict"] == v) for v in ("good", "maybe", "longshot", "no")}
    unread = [d["id"] for d in live if not d["reviewed"]]
    print(f"{len(live)} ads still advertised: {counts}")
    if runs and "--rebuild" not in sys.argv and not report_only:
        print(f"this search: {len(runs[0]['new'])} new, {len(runs[0]['gone'])} gone")
    if unread:
        print("NOT READ BY CLAUDE YET:", ", ".join(unread))
    no_email = [d["id"] for d in live if d["verdict"] != "no" and not any(c.get("email") for c in d.get("contacts", []))]
    if no_email:
        print("NO EMAIL YET:", ", ".join(no_email))


if __name__ == "__main__":
    main()
