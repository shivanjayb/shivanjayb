#!/usr/bin/env python3
"""
Fetch real daily contribution counts for a GitHub user.

Primary method  : GitHub GraphQL API (uses GITHUB_TOKEN — always available in
                  Actions, no extra secret needed).
Fallback method : Scrape the public contributions HTML page (works locally
                  when no token is set).

Writes data/contributions.json with raw days + derived stats:
  current_streak, longest_streak, best_day, monthly totals.
"""
import datetime
import json
import os
import re
import sys

import requests

USERNAME = os.environ.get("GH_PROFILE_USER", "shivanjayb")
TOKEN    = os.environ.get("GITHUB_TOKEN", "")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "contributions.json")

# ── GraphQL (primary) ──────────────────────────────────────────────────────────

_GQL_QUERY = """
query($username: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $username) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""


def fetch_via_graphql():
    today        = datetime.date.today()
    one_year_ago = today - datetime.timedelta(days=365)

    resp = requests.post(
        "https://api.github.com/graphql",
        json={
            "query": _GQL_QUERY,
            "variables": {
                "username": USERNAME,
                "from": one_year_ago.strftime("%Y-%m-%dT00:00:00Z"),
                "to":   today.strftime("%Y-%m-%dT23:59:59Z"),
            },
        },
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type":  "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    if "errors" in body:
        raise ValueError(f"GraphQL errors: {body['errors']}")

    weeks = (body["data"]["user"]["contributionsCollection"]
             ["contributionCalendar"]["weeks"])

    days = []
    for week in weeks:
        for day in week["contributionDays"]:
            days.append({"date": day["date"], "count": day["contributionCount"]})

    days.sort(key=lambda d: d["date"])
    return days


# ── HTML scrape (fallback) ─────────────────────────────────────────────────────

def fetch_via_scraping():
    from bs4 import BeautifulSoup

    url  = f"https://github.com/users/{USERNAME}/contributions"
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; profile-readme-bot/1.0)"},
        timeout=30,
    )
    resp.raise_for_status()
    soup  = BeautifulSoup(resp.text, "html.parser")
    cells = soup.select("td.ContributionCalendar-day")

    if not cells:
        print("no calendar cells found — GitHub markup may have changed",
              file=sys.stderr)
        sys.exit(1)

    days = []
    for td in cells:
        date = td.get("data-date")
        if not date:
            continue

        # 1. aria-label (most reliable without JS)
        label = td.get("aria-label", "")
        m = re.search(r"(\d+)\s+contribution", label, re.I)
        if m:
            count = int(m.group(1))
        else:
            # 2. <tool-tip> sibling element
            td_id = td.get("id")
            tip   = soup.find("tool-tip", attrs={"for": td_id}) if td_id else None
            text  = tip.get_text(strip=True) if tip else ""
            if re.search(r"no contributions", text, re.I):
                count = 0
            else:
                m2    = re.match(r"(\d+)", text)
                count = int(m2.group(1)) if m2 else 0

        days.append({"date": date, "count": count})

    days.sort(key=lambda d: d["date"])
    return days


# ── Stats helpers ──────────────────────────────────────────────────────────────

def compute_current_streak(days):
    idx = len(days) - 1
    if days[idx]["count"] == 0:
        idx -= 1           # today may not be over yet
    streak = end_idx = idx
    while idx >= 0 and days[idx]["count"] > 0:
        idx -= 1
    start_idx = idx + 1
    length    = end_idx - start_idx + 1 if end_idx >= start_idx else 0
    if length == 0:
        return 0, None, None
    return length, days[start_idx]["date"], days[end_idx]["date"]


def compute_longest_streak(days):
    longest = run = 0
    longest_start = longest_end = run_start_idx = None
    for i, d in enumerate(days):
        if d["count"] > 0:
            if run == 0:
                run_start_idx = i
            run += 1
            if run > longest:
                longest       = run
                longest_start = days[run_start_idx]["date"]
                longest_end   = days[i]["date"]
        else:
            run = 0
    return longest, longest_start, longest_end


def build_data(days):
    total       = sum(d["count"] for d in days)
    active_days = sum(1 for d in days if d["count"] > 0)
    best        = max(days, key=lambda d: d["count"])
    cur_len,  cur_start,  cur_end  = compute_current_streak(days)
    long_len, long_start, long_end = compute_longest_streak(days)

    monthly = {}
    for d in days:
        key           = d["date"][:7]
        monthly[key]  = monthly.get(key, 0) + d["count"]
    monthly_list = [{"month": k, "total": v} for k, v in sorted(monthly.items())]

    return {
        "username":            USERNAME,
        "generated_at":        datetime.datetime.now(datetime.timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "range":               {"start": days[0]["date"], "end": days[-1]["date"]},
        "total_contributions": total,
        "active_days":         active_days,
        "avg_per_active_day":  round(total / active_days, 1) if active_days else 0,
        "current_streak":      {"length": cur_len,  "start": cur_start,  "end": cur_end},
        "longest_streak":      {"length": long_len, "start": long_start, "end": long_end},
        "best_day":            {"date": best["date"], "count": best["count"]},
        "monthly":             monthly_list,
        "days":                days,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if TOKEN:
        print(f"Using GraphQL API for @{USERNAME} …")
        days = fetch_via_graphql()
    else:
        print(f"No GITHUB_TOKEN — falling back to HTML scrape for @{USERNAME} …")
        days = fetch_via_scraping()

    data = build_data(days)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(
        f"wrote {OUT_PATH}: {data['total_contributions']} contributions | "
        f"streak {data['current_streak']['length']} | "
        f"longest {data['longest_streak']['length']} | "
        f"best day {data['best_day']['count']} on {data['best_day']['date']}"
    )
