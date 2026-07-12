"""
Job Hunter v11 — API job sources (Remotive, RemoteOK, Muse, Jooble)
===================================================================
New: four API-based sources. Remotive, RemoteOK, and The Muse need NO
keys and work immediately. Jooble needs a free JOOBLE_API_KEY secret
(jooble.org/api/about); skipped if absent, like Adzuna.
Changes from v8:
- Writes docs/email_body.html (NEW postings only) and .new_count for the
  GitHub Actions email step.
- Adzuna API integration (mainstream job aggregator: set ADZUNA_APP_ID and
  ADZUNA_APP_KEY as repo secrets; skipped silently if absent).
- New sources: YiddishJobs, Parnassa Exchange, Joel Paul Group.
- Macher upgraded from homepage to /all (full 2,000+ listing search page).
Changes from v7:
- Remembers every job link it has ever shown in seen_links.json.
  Jobs not in that file get a NEW badge and sort to the top.
- Writes the report to docs/index.html so GitHub Pages serves it at
  https://<username>.github.io/<repo>/  (bookmark that URL).
- Also writes docs/jobs_latest.csv for spreadsheet use.
Previous fixes retained:
Fixes:
1. SALARY: strips 401(k)/403(b) before parsing (the "$401,000" bug was the
   job's retirement plan). Prefers numbers near salary keywords
   (salary/compensation/base/pay/OTE) over anything else on the page —
   which also kills the fake $550k from ab-hires' filter sidebar.
2. LOCATION: precedence order — City/ST in title > known city in title >
   "Location:" line > city early in description. "Remote" only counts in
   the title or opening of the description. Far-state detection (FL, CA...).
3. FIT: executive titles (VP/Chief/President) and non-sales Analyst roles
   get their score halved with an explicit warning in why_fit.
4. COMMUTE: PUBLIC TRANSIT minutes from Edison, NJ (NE Corridor rail).
   Cutoff 90 min. NOTE: Lakewood is ~40 min by car but ~105 by transit,
   so it now gets cut — edit TRANSIT_TIMES below if you'd rather keep it.
5. OUTPUT: styled HTML report (jobs_<stamp>.html) + CSV.
"""

import re
import csv
import html
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

MIN_SALARY = 130_000
MAX_PAGES = 15
LOAD_MORE_CLICKS = 8
MAX_DETAIL_VISITS = 120
MAX_COMMUTE_MIN = 90          # public transit, minutes
HOURS_PER_YEAR = 2080
SEEN_FILE = "seen_links.json"
OUT_DIR = "docs"

# ---- PUBLIC TRANSIT times from Edison, NJ (typical, minutes) ----
# Edison is on NJ Transit's Northeast Corridor line.
TRANSIT_TIMES = {
    "edison": 0, "metuchen": 10, "new brunswick": 10, "iselin": 15,
    "metropark": 15, "woodbridge": 20, "piscataway": 20, "rahway": 20,
    "linden": 25, "somerset": 30, "elizabeth": 30, "cranford": 35,
    "westfield": 35, "princeton": 35, "newark": 35, "union": 40,
    "trenton": 45, "secaucus": 45, "manhattan": 55, "new york": 55,
    "nyc": 55, "jersey city": 60, "hoboken": 60, "harlem": 75,
    "brooklyn": 85, "staten island": 90, "williamsburg": 90,
    "passaic": 90, "queens": 90,
    # beyond the 90-minute transit cutoff:
    "boro park": 100, "borough park": 100, "teaneck": 100,
    "lakewood": 105, "wall township": 120, "monsey": 120,
    "spring valley": 120, "toms river": 140, "rockland": 130,
    "monroe": 150, "long valley": 150, "marlton": 150, "bronx": 95,
    "five towns": 130, "catskills": 240,
    # different metros entirely:
    "los angeles": 9999, "california": 9999, "florida": 9999,
    "chicago": 9999, "texas": 9999, "atlanta": 9999, "boston": 9999,
    "baltimore": 9999, "montreal": 9999, "montréal": 9999, "toronto": 9999,
    "miami": 9999, "denver": 9999, "houston": 9999, "memphis": 9999,
    "charlotte": 9999, "tulsa": 9999, "encino": 9999, "palo alto": 9999,
    "san francisco": 9999, "washington": 9999, "palm beach": 9999,
    "hollywood": 9999, "aspen": 9999, "norfolk": 9999, "west park": 9999,
}
FAR_STATES = [" fl", " ca", " tx", " il", " ga", " ma", " co", " md",
              " tn", " ok", " va", " dc", " az", " wa", " or", " mi",
              " oh", " nc", " sc", " qc", " on"]

ROLE_KEYWORDS = [
    "sales", "business development", "account executive", "account manager",
    "client relations", "customer relations", "client success",
    "customer success", "partnerships", "revenue", "relationship manager",
    "client services", "bd manager", "director of development",
]
SKIP_TITLES = {
    "apply", "apply now", "show sidebar", "clear all", "read more", "view",
    "view job", "view →", "details", "back", "next", "next →", "previous",
    "home", "load more", "load more listings", "sales",
}
WIDGET_MARKERS = [
    "search by keywords", "choose a category", "minimum salary",
    "any category", "any location", "any salary", "filter by",
    "sort by", "choose type of role", "choose country", "active filters",
]
EXEC_TITLE_MARKERS = ["vice president", "vp ", "vp,", "vp-", "svp", "evp",
                      "chief ", "cro,", "president"]

RESUME_MAP = {
    "b2b": (3, "B2B client experience (Star Communications)"),
    "telecom": (3, "telecom/IT background (Star Communications)"),
    "it services": (2, "IT infrastructure support experience"),
    "saas": (2, "tech/software familiarity + AI interest"),
    "automotive": (3, "automotive sales (Mercedes-Benz)"),
    "dealership": (2, "dealership sales experience (Mercedes-Benz)"),
    "luxury": (3, "luxury client services (Feldmar Watch Co.)"),
    "watch": (2, "watch industry experience (Feldmar)"),
    "electronics": (2, "consumer electronics sales (Video & Audio Center)"),
    "home automation": (2, "sold home automation systems"),
    "operations": (2, "ran high-volume service ops (200 units/mo, 7 techs)"),
    "manager": (2, "management experience (Feldmar service center)"),
    "director": (2, "leadership/ops management background"),
    "ai": (2, "strong AI/automation interest, built AI tools"),
    "automation": (2, "workflow automation interest and hands-on projects"),
    "prospecting": (2, "outbound prospecting & lead gen (Mercedes-Benz)"),
    "lead generation": (2, "multi-channel lead nurturing experience"),
    "upsell": (2, "proven upselling (home theater installs)"),
    "negotiation": (2, "sales negotiation experience"),
    "de-escalation": (2, "client de-escalation skills (Feldmar)"),
    "outside sales": (2, "in-person consultative sales background"),
    "inside sales": (2, "phone/email/in-person sales channels"),
    "account": (3, "account/client relationship management"),
    "client": (2, "5+ yrs customer-facing client relations"),
    "customer": (2, "customer needs assessment & service ops"),
    "relationship": (2, "relationship-building track record"),
    "success": (2, "client satisfaction focus (Star Communications)"),
    "business development": (3, "pipeline building & new business generation"),
    "sales": (3, "5+ yrs sales across four industries"),
    "development": (1, "relationship cultivation background"),
    "donor": (1, "high-touch client relations transferable to donors"),
    "fundraising": (1, "relationship sales transferable to fundraising"),
}
RESUME_NEGATIVES = [
    "rn ", "nurse", "bcba", "physical therapist", "cpa", "accountant",
    "controller", "attorney", "lawyer", "paralegal", "engineer",
    "developer", "teacher", "rebbe", "rabbi", "cantor", "dental",
    "medical assistant", "phlebotom", "warehouse", "driver", "electrician",
    "plumber", "therapist", "social worker", "bookkeeper", "payroll",
    "head of school", "principal", "nurse practitioner",
]

SITES = [
    {"name": "jewishstaffing", "kind": "urls", "urls": [
        "https://www.jewishstaffing.com/jobs?salary=100k-150k",
        "https://www.jewishstaffing.com/jobs?salary=150k-200k",
        "https://www.jewishstaffing.com/jobs?salary=200k-300k",
        "https://www.jewishstaffing.com/jobs?salary=300k%2B",
    ]},
    {"name": "jewishjobs", "kind": "urls",
     "urls": ["https://www.jewishjobs.com/search"], "extra_wait": 5000},
    {"name": "macherusa", "kind": "urls",
     "urls": ["https://macherusa.com/all"], "extra_wait": 4000},
    {"name": "yiddishjobs", "kind": "urls",
     "urls": ["https://yiddishjobs.com/"], "extra_wait": 5000},
    {"name": "joelpaul", "kind": "urls",
     "urls": ["https://www.joelpaul.com/jobs/"], "extra_wait": 4000},
    {"name": "jpro", "kind": "search",
     "url": "https://jobs.jpro.org/jobs",
     "queries": ["sales", "business development", "client success"],
     "extra_wait": 4000},
    {"name": "bhired", "kind": "urls",
     "urls": ["https://recruiterflow.com/Bhired/jobs-page-widget"],
     "extra_wait": 4000},
    {"name": "yonah", "kind": "urls",
     "urls": ["https://www.yonah.io/positions"], "extra_wait": 5000},
    {"name": "protalent", "kind": "urls",
     "urls": ["https://protalentsolutions.com/jobs/"], "extra_wait": 3000},
    {"name": "ab-hires", "kind": "urls",
     "urls": ["https://ab-hires.com/jobs/"]},
    {"name": "poelgroup", "kind": "urls",
     "urls": ["https://jobs.poelgroup.com/"]},
    {"name": "staffconnect", "kind": "urls",
     "urls": ["https://staffconnectny.com/job-openings/"], "extra_wait": 4000},
    {"name": "maiplacement", "kind": "urls",
     "urls": ["https://maiplacement.com/job-listings/"], "extra_wait": 5000},
    {"name": "swift", "kind": "pages",
     "template": "https://swiftstaffinggroup.com/positions/?jobs_page={n}",
     "max_pages": 15},
    {"name": "smstaffing", "kind": "pages",
     "template": "https://smstaffing.herokuapp.com/jobs/page/{n}",
     "max_pages": 10, "extra_wait": 3000},
    {"name": "supremetalent", "kind": "urls",
     "urls": ["https://thesupremetalent.com/positions/"]},
    {"name": "blackbird", "kind": "urls",
     "urls": ["https://jobs.blackbirdrecruiting.com"], "extra_wait": 4000},
    {"name": "yidjob", "kind": "loadmore",
     "urls": ["https://yidjob.com/find-a-job/"],
     "loadmore_text": "Load more listings", "extra_wait": 4000},
    {"name": "jobsgemach", "kind": "urls",
     "urls": ["https://www.jobsgemach.com"]},
    {"name": "candibots", "kind": "urls",
     "urls": ["https://candibots.com/jobs/"], "extra_wait": 4000},
    {"name": "pcs", "kind": "pcs",
     "urls": ["https://pcsnynj.org/newsletters/"]},
]

SAL_KEYWORDS = r"(?:salary|compensation|comp|base|pay|earn|ote|package)"


def _scan_salary(t):
    cands = []
    for m in re.findall(
            r"\$?\s*(\d{2,3}(?:\.\d{1,2})?)\s*(?:/|per\s*)h(?:ou)?r", t):
        cands.append(int(float(m) * HOURS_PER_YEAR))
    for a, b in re.findall(r"\$?(\d{2,3})\s*k?\s*[-–]\s*\$?(\d{2,3})\s*k", t):
        cands.append(max(int(a), int(b)) * 1000)
    for m in re.findall(r"\$\s*(\d{2,3})\s*k\b|\b(\d{2,3})\s*k\s*\+", t):
        n = m[0] or m[1]
        cands.append(int(n) * 1000)
    for a, b in re.findall(
            r"\$?\s*(\d{5,7})\s*(?:[-–]|to)\s*\$?\s*(\d{5,7})", t):
        cands.append(max(int(a), int(b)))
    for m in re.findall(r"\$\s*(\d{5,7})\b", t):
        cands.append(int(m))
    return [c for c in cands if 20_000 <= c <= 1_000_000]


def parse_salary(text):
    t = text.lower().replace(",", "")
    # kill retirement-plan tokens BEFORE parsing ($401,000 bug)
    t = re.sub(r"40[13]\s*\(?[kb]\)?", " ", t)
    # 1) prefer numbers near salary keywords
    windows = []
    for m in re.finditer(SAL_KEYWORDS, t):
        windows.append(t[max(0, m.start() - 40): m.end() + 120])
    near = _scan_salary(" | ".join(windows)) if windows else []
    if near:
        return max(near)
    # 2) fall back to whole-text scan
    allc = _scan_salary(t)
    return max(allc) if allc else None


def extract_location(title, text):
    """Precedence: title city/state > Location: line > early-description
    city > remote-in-title/opening. Returns (label, minutes or None)."""
    tl = " " + title.lower() + " "
    body = " " + text.lower()[:900] + " "
    hybrid = "hybrid" in tl or "hybrid" in body

    def match_city(s):
        best = None
        for city, mins in TRANSIT_TIMES.items():
            if re.search(r"\b" + re.escape(city) + r"\b", s):
                if best is None or mins < best[0]:
                    best = (mins, city)
        return best

    def far_state(s):
        return any(re.search(r",\s*" + st.strip() + r"\b", s)
                   for st in FAR_STATES)

    # 1) title
    if far_state(tl):
        return "Out of area", 9999
    hit = match_city(tl)
    if hit:
        label = hit[1].title() + (" (Hybrid)" if hybrid else "")
        return label, hit[0]
    if re.search(r"\bremote\b", tl):
        return "Remote", 0

    # 2) explicit "Location:" line in body
    loc_line = re.search(r"location[:\s]+([^\n]{3,60})", body)
    if loc_line:
        seg = loc_line.group(1)
        if far_state(seg):
            return "Out of area", 9999
        hit = match_city(seg)
        if hit:
            label = hit[1].title() + (" (Hybrid)" if hybrid else "")
            return label, hit[0]
        if "remote" in seg:
            return "Remote", 0

    # 3) early description
    if far_state(body):
        return "Out of area", 9999
    hit = match_city(body)
    if hit:
        label = hit[1].title() + (" (Hybrid)" if hybrid else "")
        return label, hit[0]
    if re.search(r"\bremote\b", body[:400]):
        return "Remote", 0
    return "Unknown", None


def fit_details(title, body):
    blob = (title + " " + body).lower()
    tl = title.lower()
    if any(neg in blob for neg in RESUME_NEGATIVES):
        return 0, ""
    raw, reasons = 0, []
    for kw, (w, reason) in RESUME_MAP.items():
        if kw in blob:
            raw += w
            if kw in tl:
                raw += w
            reasons.append((w, reason))
    score = min(100, raw * 4)
    warn = ""
    if any(m in tl for m in EXEC_TITLE_MARKERS):
        score //= 2
        warn = "⚠ Executive-level role — likely requires 10+ yrs leadership. "
    elif "analyst" in tl and "sales" not in tl:
        score //= 2
        warn = "⚠ Analyst role — not client-facing sales. "
    reasons.sort(key=lambda r: -r[0])
    seen, why = set(), []
    for _, r in reasons:
        if r not in seen:
            seen.add(r)
            why.append(r)
        if len(why) == 3:
            break
    return score, warn + "; ".join(why)


def clean_title(t):
    return re.sub(r"\s+", " ", t).strip()[:120]


def strip_widget_lines(text):
    """Remove sidebar/filter lines from a detail page before parsing."""
    keep = []
    for line in text.split("\n"):
        low = line.lower()
        if any(m in low for m in WIDGET_MARKERS):
            continue
        if re.match(r"^\s*\$\d{2,3}[,.]?\d{0,3}\s*[-–]\s*\$\d{2,3}", low) \
                and len(line) < 30:
            continue  # dropdown salary-range option rows
        keep.append(line)
        if any(x in low for x in ("related jobs", "similar jobs",
                                  "more jobs", "other openings")):
            break
    return "\n".join(keep)


def safe_goto(page, url, wait):
    for attempt in (1, 2):
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            break
        except Exception as e:
            if "Download is starting" in str(e):
                break
            if attempt == 2:
                raise
            page.wait_for_timeout(3000)   # brief pause, then retry once
    page.wait_for_timeout(wait)


def extract_with_retry(page, base_url):
    """Retry once if the page navigates mid-extraction (jobsgemach bug)."""
    try:
        return extract_jobs_from_page(page, base_url)
    except Exception as e:
        if "Execution context was destroyed" not in str(e):
            raise
        page.wait_for_timeout(2500)
        return extract_jobs_from_page(page, base_url)


def make_job(title, context, link):
    score, why = fit_details(title, context)
    city, mins = extract_location(title, context)
    return {
        "title": title,
        "salary": parse_salary(context),
        "city": city,
        "commute_min": mins,
        "fit_score": score,
        "why_fit": why,
        "link": link,
    }


def extract_jobs_from_page(page, base_url):
    jobs, all_hrefs = [], set()
    for a in page.query_selector_all("a"):
        try:
            title = clean_title(a.inner_text() or "")
        except Exception:
            continue
        href = a.get_attribute("href") or ""
        if href.startswith("javascript") or href in ("#", ""):
            continue
        if not title or len(title) < 5 or title.lower() in SKIP_TITLES:
            continue
        if "/job-category/" in href or "/browse/" in href:
            continue
        all_hrefs.add(href)
        if not any(k in title.lower() for k in ROLE_KEYWORDS):
            continue
        card_text = title
        try:
            h = a.evaluate_handle(
                "el => el.closest('li,article,tr,.job,.job-card,.card,"
                ".listing,.job_listing,.position') || el.parentElement")
            card_text = h.evaluate("el => el.innerText") or title
        except Exception:
            pass
        low = card_text.lower()
        if any(m in low for m in WIDGET_MARKERS) or len(card_text) > 1200:
            card_text = title
        jobs.append(make_job(title, card_text[:600],
                             urljoin(base_url, href)))
    return jobs, all_hrefs


def find_pagination_links(page, current_url):
    out = []
    sel = page.query_selector(
        "a[rel='next'], a[aria-label='Next'], .next a, li.next a, "
        "a.next, .pagination-next a")
    if sel:
        href = sel.get_attribute("href")
        if href and not href.startswith(("#", "javascript")):
            out.append(urljoin(current_url, href))
    for a in page.query_selector_all(
            ".pagination a, .page-numbers, nav[aria-label*='agination'] a, "
            ".pager a, .paginate a"):
        try:
            txt = (a.inner_text() or "").strip()
        except Exception:
            continue
        href = a.get_attribute("href") or ""
        if txt.isdigit() and href and not href.startswith(("#", "javascript")):
            out.append(urljoin(current_url, href))
    for a in page.query_selector_all("a"):
        try:
            txt = (a.inner_text() or "").strip().lower()
        except Exception:
            continue
        if txt in ("next", "next →", "→", "older", "next page", "»"):
            href = a.get_attribute("href")
            if href and not href.startswith(("#", "javascript")):
                out.append(urljoin(current_url, href))
    seen, uniq = set(), []
    for u in out:
        if u not in seen and u != current_url:
            seen.add(u)
            uniq.append(u)
    return uniq


def click_load_more(page, label, times):
    for _ in range(times):
        btn = (page.query_selector(f"text='{label}'")
               or page.query_selector("button:has-text('Load more')"))
        if not btn:
            break
        try:
            btn.click()
            page.wait_for_timeout(2000)
        except Exception:
            break


def scrape_site(page, site):
    jobs, visited, seen_hrefs = [], 0, set()
    wait = site.get("extra_wait", 1500)

    def visit(url):
        nonlocal visited
        safe_goto(page, url, wait)
        visited += 1
        new_jobs, hrefs = extract_with_retry(page, url)
        fresh = hrefs - seen_hrefs
        seen_hrefs.update(hrefs)
        return new_jobs, len(fresh)

    try:
        if site["kind"] == "pages":
            for n in range(1, site.get("max_pages", MAX_PAGES) + 1):
                new, fresh = visit(site["template"].format(n=n))
                jobs += new
                if fresh == 0 and n > 1:
                    break
        elif site["kind"] == "loadmore":
            for url in site["urls"]:
                safe_goto(page, url, wait)
                click_load_more(page, site["loadmore_text"], LOAD_MORE_CLICKS)
                new, _ = extract_with_retry(page, url)
                jobs += new
                visited += 1
        elif site["kind"] == "search":
            for q in site["queries"]:
                safe_goto(page, site["url"], wait)
                box = (page.query_selector("input[type='search']")
                       or page.query_selector("input[placeholder*='earch']")
                       or page.query_selector("input[type='text']"))
                if box:
                    box.fill(q)
                    box.press("Enter")
                    page.wait_for_timeout(3000)
                    click_load_more(page, "Load more", 4)
                new, _ = extract_jobs_from_page(page, site["url"])
                jobs += new
                visited += 1
        elif site["kind"] == "pcs":
            safe_goto(page, site["urls"][0], wait)
            post_links = []
            for a in page.query_selector_all("a"):
                href = a.get_attribute("href") or ""
                if "/newsletters/" in href and href.rstrip("/") != \
                        site["urls"][0].rstrip("/") and href not in post_links:
                    post_links.append(urljoin(site["urls"][0], href))
            for url in post_links[:3]:
                safe_goto(page, url, 1500)
                body = page.inner_text("body")
                for chunk in re.split(r"\n\s*\d+\.\s*|\n\s*[•▪]\s*", body):
                    first = clean_title(chunk.split("\n")[0])[:100]
                    if not any(k in first.lower() for k in ROLE_KEYWORDS):
                        continue
                    jobs.append(make_job(first, chunk[:500], url))
                visited += 1
        else:
            queue = list(site["urls"])
            done = set()
            while queue and visited < site.get("max_pages", MAX_PAGES):
                url = queue.pop(0)
                if url in done:
                    continue
                done.add(url)
                new, fresh = visit(url)
                jobs += new
                if fresh > 0:
                    for nxt in find_pagination_links(page, url):
                        if nxt not in done:
                            queue.append(nxt)
    except Exception as e:
        return jobs, f"PARTIAL ({visited}p, {len(jobs)}j): {e}"
    return jobs, f"OK ({visited} pages, {len(jobs)} role-matches)"


def enrich(page, job):
    try:
        safe_goto(page, job["link"], 2500)
        body = ""
        try:
            body = page.inner_text("main") or ""
        except Exception:
            pass
        if len(body) < 200:
            body = page.inner_text("body") or ""
        body = strip_widget_lines(body)[:4000]
    except Exception:
        return job
    sal = parse_salary(body)
    if sal:
        job["salary"] = sal
    city, mins = extract_location(job["title"], body)
    if city != "Unknown":
        job["city"], job["commute_min"] = city, mins
    score, why = fit_details(job["title"], body)
    if score > job["fit_score"]:
        job["fit_score"], job["why_fit"] = score, why
    job["enriched"] = "yes"
    return job



# ============================================================
# ADZUNA API (mainstream aggregator; needs free API keys)
# ============================================================

ADZUNA_QUERIES = ["sales manager", "business development",
                  "account executive", "client success manager"]

def fetch_adzuna():
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        return [], "SKIPPED (no ADZUNA_APP_ID/ADZUNA_APP_KEY set)"
    jobs = []
    for what in ADZUNA_QUERIES:
        url = ("https://api.adzuna.com/v1/api/jobs/us/search/1"
               f"?app_id={app_id}&app_key={app_key}"
               "&results_per_page=50&distance=40&salary_min=130000"
               "&where=Edison%2C%20New%20Jersey"
               f"&what={urllib.parse.quote(what)}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
        except Exception as e:
            return jobs, f"PARTIAL: {e}"
        for res in data.get("results", []):
            title = clean_title(res.get("title", ""))
            if not title:
                continue
            area = res.get("location", {}).get("area", [])
            loc = ", ".join(area[-2:]) if area else ""
            desc = res.get("description", "")
            j = make_job(title, f"{title} {loc} {desc}",
                         res.get("redirect_url", ""))
            sal = res.get("salary_max") or res.get("salary_min")
            if sal:
                j["salary"] = int(sal)
            jobs.append(j)
    return jobs, f"OK ({len(jobs)} API results)"



def _http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, headers=headers or
                                 {"User-Agent": "Mozilla/5.0"})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_remotive():
    jobs = []
    try:
        data = _http_json(
            "https://remotive.com/api/remote-jobs?category=sales")
    except Exception as e:
        return jobs, f"FAILED: {e}"
    for res in data.get("jobs", []):
        loc = (res.get("candidate_required_location") or "").lower()
        if loc and not any(x in loc for x in
                           ("usa", "united states", "worldwide", "americas",
                            "north america", "anywhere")):
            continue
        title = clean_title(res.get("title", ""))
        desc = re.sub(r"<[^>]+>", " ", res.get("description", ""))[:1500]
        j = make_job(title, f"{title} Remote {res.get('salary','')} {desc}",
                     res.get("url", ""))
        j["city"], j["commute_min"] = "Remote", 0
        jobs.append(j)
    return jobs, f"OK ({len(jobs)} API results)"


def fetch_remoteok():
    jobs = []
    try:
        data = _http_json("https://remoteok.com/api")
    except Exception as e:
        return jobs, f"FAILED: {e}"
    for res in data:
        if not isinstance(res, dict) or not res.get("position"):
            continue
        blob = " ".join(res.get("tags", [])) + " " + res.get("position", "")
        if not any(k in blob.lower() for k in ROLE_KEYWORDS):
            continue
        title = clean_title(res.get("position", ""))
        desc = re.sub(r"<[^>]+>", " ", res.get("description", ""))[:1500]
        j = make_job(title, f"{title} Remote {desc}",
                     res.get("url", ""))
        j["city"], j["commute_min"] = "Remote", 0
        smax = res.get("salary_max") or res.get("salary_min")
        if smax:
            j["salary"] = int(smax)
        jobs.append(j)
    return jobs, f"OK ({len(jobs)} API results)"


def fetch_themuse():
    jobs = []
    locs = ["New York, NY", "Flexible / Remote"]
    try:
        for loc in locs:
            for pg in (1, 2):
                url = ("https://www.themuse.com/api/public/jobs"
                       f"?category=Sales&page={pg}&location="
                       + urllib.parse.quote(loc))
                data = _http_json(url)
                for res in data.get("results", []):
                    title = clean_title(res.get("name", ""))
                    desc = re.sub(r"<[^>]+>", " ",
                                  res.get("contents", ""))[:1500]
                    where = "; ".join(l.get("name", "") for l in
                                      res.get("locations", []))
                    link = (res.get("refs", {}) or {}).get(
                        "landing_page", "")
                    jobs.append(make_job(
                        title, f"{title} {where} {desc}", link))
    except Exception as e:
        return jobs, f"PARTIAL: {e}"
    return jobs, f"OK ({len(jobs)} API results)"


def fetch_jooble():
    key = os.environ.get("JOOBLE_API_KEY", "")
    if not key:
        return [], "SKIPPED (no JOOBLE_API_KEY set)"
    jobs = []
    try:
        for what in ("sales manager", "business development",
                     "account executive"):
            data = _http_json(
                "https://jooble.org/api/" + key,
                data={"keywords": what, "location": "Edison, NJ",
                      "salary": "130000", "page": "1"})
            for res in data.get("jobs", []):
                title = clean_title(res.get("title", ""))
                snippet = re.sub(r"<[^>]+>", " ",
                                 res.get("snippet", ""))[:1200]
                ctx = (f"{title} {res.get('location','')} "
                       f"{res.get('salary','')} {snippet}")
                jobs.append(make_job(title, ctx, res.get("link", "")))
    except Exception as e:
        return jobs, f"PARTIAL: {e}"
    return jobs, f"OK ({len(jobs)} API results)"


API_SOURCES = [("adzuna", fetch_adzuna), ("remotive", fetch_remotive),
               ("remoteok", fetch_remoteok), ("themuse", fetch_themuse),
               ("jooble", fetch_jooble)]


def passes(j):
    if j["commute_min"] is not None and j["commute_min"] > MAX_COMMUTE_MIN:
        return False
    if j["salary"] is not None and j["salary"] < MIN_SALARY:
        return False
    return j["fit_score"] >= 12


def dedupe(jobs):
    seen, out = set(), []
    for j in sorted(jobs, key=lambda x: -x["fit_score"]):
        key = j["link"].rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


# ============================================================
# HTML REPORT
# ============================================================

def write_html(jobs, path, stamp):
    def esc(s):
        return html.escape(str(s or ""))

    def card_html(j):
        new_badge = '<span class="new">NEW</span> ' if j.get("is_new") else ""
        sal = f"${j['salary']:,}" if j["salary"] else "Salary not listed"
        commute = ("Remote" if j["city"] == "Remote" else
                   f"{j['city']} · ~{j['commute_min']} min transit"
                   if j["commute_min"] is not None else j["city"])
        badge = ("high" if j["fit_score"] >= 60 else
                 "mid" if j["fit_score"] >= 35 else "low")
        added = j.get("date_added", "")
        return f"""
    <a class="card" href="{esc(j['link'])}" target="_blank">
      <div class="row">
        <span class="fit {badge}">{j['fit_score']}</span>
        <div class="body">
          <div class="title">{new_badge}{esc(j['title'])}</div>
          <div class="meta">{esc(sal)} &nbsp;·&nbsp; {esc(commute)}
            &nbsp;·&nbsp; <span class="board">{esc(j['board'])}</span>
            &nbsp;·&nbsp; added {esc(added)}</div>
          <div class="why">{esc(j['why_fit'])}</div>
        </div>
      </div>
    </a>"""

    new_jobs = [j for j in jobs if j.get("is_new")]
    old_jobs = [j for j in jobs if not j.get("is_new")]
    sections = []
    if new_jobs:
        sections.append(f'<h2 class="sect">🆕 New today ({len(new_jobs)})</h2>'
                        + "".join(card_html(j) for j in new_jobs))
    label = "Earlier postings" if new_jobs else "All postings"
    if old_jobs:
        sections.append(f'<h2 class="sect">{label} ({len(old_jobs)})</h2>'
                        + "".join(card_html(j) for j in old_jobs))
    cards = sections

    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Matches — {stamp}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #f2f2f7; color: #111; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #000; color: #eee; }}
    .card {{ background: #1c1c1e !important; }}
    .meta, .why {{ color: #98989f !important; }}
  }}
  header {{ padding: 20px 16px 8px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #6e6e73; font-size: 14px; }}
  .list {{ padding: 8px 12px 40px; max-width: 720px; margin: 0 auto; }}
  .card {{ display: block; background: #fff; border-radius: 14px;
          padding: 14px; margin: 10px 0; text-decoration: none;
          color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .row {{ display: flex; gap: 12px; align-items: flex-start; }}
  .fit {{ min-width: 44px; height: 44px; border-radius: 10px;
         display: flex; align-items: center; justify-content: center;
         font-weight: 700; font-size: 17px; color: #fff; }}
  .fit.high {{ background: #34c759; }}
  .fit.mid  {{ background: #ff9f0a; }}
  .fit.low  {{ background: #8e8e93; }}
  .title {{ font-weight: 600; font-size: 16px; line-height: 1.3; }}
  .meta {{ font-size: 14px; color: #6e6e73; margin-top: 3px; }}
  .board {{ text-transform: capitalize; }}
  .why {{ font-size: 13px; color: #6e6e73; margin-top: 6px;
         line-height: 1.35; }}
  .sect {{ font-size: 17px; margin: 22px 4px 6px; }}
  .new {{ background: #ff3b30; color: #fff; font-size: 11px;
         font-weight: 700; padding: 2px 6px; border-radius: 6px;
         vertical-align: 2px; }}
</style></head>
<body>
<header>
  <h1>Job Matches</h1>
  <div class="sub">{sum(1 for j in jobs if j.get("is_new"))} new ·
    {len(jobs)} total · $130k+ or unlisted ·
    ≤90 min transit from Edison, NJ · updated {stamp}</div>
</header>
<div class="list">{''.join(cards)}
</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    all_jobs, log = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for site in SITES:
            print(f"[{site['name']}] scraping ...")
            ctx = browser.new_context(accept_downloads=False)
            pg = ctx.new_page()
            try:
                jobs, status = scrape_site(pg, site)
            finally:
                ctx.close()
            for j in jobs:
                j["board"] = site["name"]
                j["enriched"] = "no"
            log.append(f"[{site['name']}] {status}")
            all_jobs.extend(jobs)

        for api_name, fetcher in API_SOURCES:
            print(f"[{api_name}] querying API ...")
            api_jobs, api_status = fetcher()
            filtered = []
            for j in api_jobs:
                tl = j["title"].lower()
                if re.search(r"\d+\s+open positions|^sales\s*\(\d+\)", tl):
                    continue   # category rows, not jobs
                if not (any(k in tl for k in ROLE_KEYWORDS)
                        or j["fit_score"] >= 30):
                    continue   # API returned an unrelated role
                j["board"] = api_name
                j["enriched"] = "yes"  # APIs already include descriptions
                filtered.append(j)
            log.append(f"[{api_name}] {api_status} "
                       f"-> {len(filtered)} after role filter")
            all_jobs.extend(filtered)

        candidates = dedupe([j for j in all_jobs if passes(j)])
        order = sorted(candidates,
                       key=lambda j: (j["salary"] is not None,
                                      -j["fit_score"]))
        print(f"\nEnriching {min(len(order), MAX_DETAIL_VISITS)} "
              f"job detail pages ...")
        ctx = browser.new_context(accept_downloads=False)
        pg = ctx.new_page()
        for i, job in enumerate(order[:MAX_DETAIL_VISITS]):
            print(f"  ({i+1}) {job['title'][:60]}")
            enrich(pg, job)
        ctx.close()
        browser.close()

    kept = [j for j in candidates if passes(j)]

    # ---- NEW-posting tracking with first-seen dates ----
    today = datetime.now().strftime("%Y-%m-%d")
    seen = {}
    if os.path.exists(SEEN_FILE):
        try:
            raw = json.load(open(SEEN_FILE))
            if isinstance(raw, list):      # migrate old list format
                seen = {link: today for link in raw}
            else:
                seen = raw
        except Exception:
            seen = {}
    first_run = not seen
    for j in kept:
        key = j["link"].rstrip("/")
        j["is_new"] = (not first_run) and (key not in seen)
        j["date_added"] = seen.get(key, today)
    for j in kept:
        seen.setdefault(j["link"].rstrip("/"), today)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=0, sort_keys=True)

    # NEW first, then most recently added, then fit, then salary
    kept.sort(key=lambda j: (j.get("is_new", False),
                             j.get("date_added", ""), j["fit_score"],
                             j["salary"] or 0), reverse=True)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUT_DIR, "jobs_latest.csv")
    html_path = os.path.join(OUT_DIR, "index.html")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "is_new", "date_added", "fit_score", "title", "salary", "city",
            "commute_min", "why_fit", "enriched", "board", "link"])
        w.writeheader()
        for j in kept:
            w.writerow({k: j.get(k, "") for k in w.fieldnames})
    write_html(kept, html_path, stamp)

    # ---- email body (NEW postings only) + count for the workflow ----
    new_jobs = [j for j in kept if j.get("is_new")]
    rows = []
    for j in new_jobs[:25]:
        sal = f"${j['salary']:,}" if j["salary"] else "salary not listed"
        loc = (j["city"] if j["commute_min"] is None
               else f"{j['city']} (~{j['commute_min']} min transit)")
        rows.append(
            f'<p style="margin:0 0 14px">'
            f'<a href="{html.escape(j["link"])}" '
            f'style="font-size:16px;font-weight:600">'
            f'{html.escape(j["title"])}</a><br>'
            f'<span style="color:#555">[fit {j["fit_score"]}] {sal} · '
            f'{html.escape(loc)} · {html.escape(j["board"])}</span><br>'
            f'<span style="color:#777;font-size:13px">'
            f'{html.escape(j["why_fit"])}</span></p>')
    email_html = ("<h2>%d new job posting%s today</h2>%s"
                  "<p><a href='https://JACKS-GITHUB-USERNAME.github.io/"
                  "job-hunter/'>Open the full report</a></p>"
                  % (len(new_jobs), "s" if len(new_jobs) != 1 else "",
                     "".join(rows)))
    with open(os.path.join(OUT_DIR, "email_body.html"), "w",
              encoding="utf-8") as f:
        f.write(email_html)
    with open(".new_count", "w") as f:
        f.write(str(len(new_jobs)))

    n_new = sum(1 for j in kept if j.get("is_new"))
    print("\n--- SCRAPE LOG ---")
    print("\n".join(log))
    print(f"\n{len(kept)} matching roles ({n_new} new since last run)")
    print(f"  CSV : {csv_path}")
    print(f"  HTML: {html_path}")


if __name__ == "__main__":
    main()
