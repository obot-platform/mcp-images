#!/usr/bin/env python3
"""Remove obsolete main-branch Trivy categories; dry-run unless --apply is set."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import yaml


MAIN_REF = "refs/heads/main"
MANIFESTS = ("repackaging/images.yaml", "repository-images.yaml")


class CleanupError(RuntimeError):
    pass


class APIError(CleanupError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, repo, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise CleanupError("expected repository in owner/name form")
        self.prefix = f"repos/{repo}/"
        self.token = token

    def request(self, path, method="GET"):
        if not path.startswith(self.prefix):
            raise CleanupError("refusing an API request outside the selected repository")
        request = Request("https://api.github.com/" + path, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mcp-images-trivy-cleanup",
        })
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                message = exc.read().decode("utf-8", errors="replace")
                exc.close()
                limited = exc.code == 429 or (exc.code == 403 and (
                    exc.headers.get("X-RateLimit-Remaining") == "0"
                    or "rate limit" in message.lower()
                    or exc.headers.get("Retry-After") is not None
                ))
                delay = 2 ** attempt
                if limited:
                    delay = max(1, int(exc.headers.get("Retry-After", delay)))
                    if exc.headers.get("X-RateLimit-Remaining") == "0":
                        delay = max(delay, int(exc.headers.get("X-RateLimit-Reset", 0)) - int(time.time()) + 1)
                if (limited or exc.code in (500, 502, 503, 504)) and attempt < 3 and delay <= 60:
                    print(f"GitHub returned {exc.code}; retrying in {delay}s", file=sys.stderr, flush=True)
                    time.sleep(delay)
                    continue
                suffix = "; rerun with the same arguments to resume" if limited else ""
                raise APIError(exc.code, f"GitHub {method} returned {exc.code}: {message}{suffix}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt == 3:
                    raise CleanupError("GitHub request failed after four attempts; rerun to resume") from exc
                time.sleep(2 ** attempt)

    def pages(self, path):
        result = []
        for page in range(1, 10001):
            separator = "&" if "?" in path else "?"
            batch = self.request(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise CleanupError("expected a paginated GitHub list")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise CleanupError("GitHub pagination did not terminate")


def active_categories(documents):
    names = {"base-node", "base-python"}
    for contents in documents:
        try:
            document = yaml.safe_load(contents)
        except yaml.YAMLError as exc:
            raise CleanupError("invalid image manifest YAML") from exc
        images = document.get("images") if isinstance(document, dict) else None
        if not isinstance(images, list) or not images:
            raise CleanupError("image manifests must contain a nonempty images list")
        for entry in images:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                raise CleanupError("invalid image name in manifest")
            if name in names:
                raise CleanupError(f"duplicate image name: {name}")
            names.add(name)
    return {"trivy-" + name for name in names}


def is_obsolete(analysis, active):
    category = analysis.get("category") or ""
    owned = re.fullmatch(r"trivy-[a-z0-9][a-zA-Z0-9_.-]*", category) or re.fullmatch(
        r"\.github/workflows/[a-zA-Z0-9_.-]+\.ya?ml:[^\r\n]+", category
    )
    return bool(analysis.get("tool", {}).get("name") == "Trivy"
                and analysis.get("ref") == MAIN_REF and owned and category not in active)


def require_current_main(api, expected_sha):
    if api.request(api.prefix + "commits/main")["sha"] != expected_sha:
        raise CleanupError("main changed or this is an outdated checkout; refresh main and rerun")


def protected_alerts(api, active):
    alerts = api.pages(api.prefix + "code-scanning/alerts?state=open")
    protected = []
    for alert in alerts:
        instances = api.pages(api.prefix + f"code-scanning/alerts/{alert['number']}/instances")
        if any(i.get("state") == "open" and i.get("ref") == MAIN_REF
               and i.get("category") in active for i in instances):
            protected.append(alert["number"])
    return alerts, protected


def next_analysis_id(url, api):
    if url is None:
        return None
    parsed = urlparse(url)
    prefix = "/" + api.prefix + "code-scanning/analyses/"
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or not parsed.path.startswith(prefix):
        raise CleanupError("unexpected next-analysis URL from GitHub")
    identifier = parsed.path[len(prefix):]
    if not identifier.isdigit():
        raise CleanupError("invalid next-analysis ID from GitHub")
    return int(identifier)


def delete_analyses(api, candidates, expected_sha, record):
    # Only IDs in the reviewed inventory can be deleted, even if GitHub returns
    # a continuation link for a newly uploaded or unrelated analysis.
    categories = {}
    for analysis in candidates:
        categories.setdefault(analysis["category"], {})[analysis["id"]] = dict(analysis)
    deleted = []
    stop = threading.Event()
    guard_lock = threading.Lock()
    audit_lock = threading.Lock()
    require_current_main(api, expected_sha)
    last_guard = time.monotonic()

    def delete_category(remaining):
        nonlocal last_guard
        try:
            while remaining and not stop.is_set():
                with guard_lock:
                    if time.monotonic() - last_guard >= 30:
                        require_current_main(api, expected_sha)
                        last_guard = time.monotonic()
                heads = [a for a in remaining.values() if a.get("deletable")]
                if not heads:
                    raise CleanupError("no deletable analysis in the inventory; rerun to refresh it")
                analysis = max(heads, key=lambda a: (a["created_at"], a["id"]))
                path = api.prefix + f"code-scanning/analyses/{analysis['id']}?confirm_delete=true"
                try:
                    response = api.request(path, "DELETE")
                except APIError as exc:
                    if exc.status != 404:
                        raise
                    # A lost DELETE response may leave this exact approved ID
                    # already absent. A rerun refreshes any remaining chain.
                    response = {}
                remaining.pop(analysis["id"])
                with audit_lock:
                    deleted.append(analysis["id"])
                    record({"event": "deleted", "id": analysis["id"], "category": analysis["category"]})
                    if len(deleted) % 50 == 0:
                        print(f"Deleted {len(deleted)}/{len(candidates)} obsolete analyses", flush=True)
                following = next_analysis_id(response.get("next_analysis_url") or response.get("confirm_delete_url"), api)
                if following is not None:
                    next_item = remaining.get(following)
                    if next_item is None:
                        raise CleanupError("next analysis is outside the approved category inventory; rerun to refresh")
                    next_item["deletable"] = True
                elif remaining:
                    # Some DELETE responses omit continuation URLs. Continue
                    # newest-first within this approved category only; GitHub
                    # rejects deletion if a newer analysis appeared meanwhile.
                    next_item = max(remaining.values(), key=lambda a: (a["created_at"], a["id"]))
                    next_item["deletable"] = True
        except BaseException:
            stop.set()
            raise

    # Categories are independent API deletion chains. Keep each chain strictly
    # sequential, with at most four requests in flight across categories.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(delete_category, items) for items in categories.values()]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            stop.set()
            raise
    require_current_main(api, expected_sha)
    return deleted


def cleanup(api, root, expected_sha, audit_dir, apply=False):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise CleanupError("expected-sha must be a full commit SHA")
    require_current_main(api, expected_sha)
    active = active_categories([(root / name).read_text() for name in MANIFESTS])
    remote = []
    for name in MANIFESTS:
        data = api.request(api.prefix + f"contents/{name}?" + urlencode({"ref": expected_sha}))
        if data.get("encoding") != "base64":
            raise CleanupError("unexpected manifest encoding from GitHub")
        remote.append(base64.b64decode(data["content"]).decode())
    if active_categories(remote) != active:
        raise CleanupError("local active image categories differ from the current main manifests")
    analyses = api.pages(api.prefix + "code-scanning/analyses")
    candidates = [a for a in analyses if is_obsolete(a, active)]
    preserved = {a["id"] for a in analyses if not is_obsolete(a, active)}
    alerts, protected = protected_alerts(api, active)
    audit_dir.mkdir(parents=True, exist_ok=True)
    plan = {"expected_sha": expected_sha, "active_categories": sorted(active),
            "obsolete_categories": sorted({a["category"] for a in candidates}),
            "candidates": candidates, "preserved_ids": sorted(preserved),
            "open_alerts_before": len(alerts), "protected_alert_numbers": protected}
    (audit_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(f"{len(candidates)} obsolete analyses in {len(plan['obsolete_categories'])} categories; "
          f"preserving {len(active)} active categories and {len(preserved)} other analyses", flush=True)
    if not apply:
        print("Dry run: no analyses deleted", flush=True)
        return plan
    with (audit_dir / "events.jsonl").open("a") as events:
        def record(event):
            events.write(json.dumps({"time": int(time.time()), **event}) + "\n")
            events.flush()
        try:
            deleted = delete_analyses(api, candidates, expected_sha, record)
            after = api.pages(api.prefix + "code-scanning/analyses")
            after_alerts = api.pages(api.prefix + "code-scanning/alerts?state=open")
            missing = sorted(preserved - {a["id"] for a in after})
            remaining = [a["id"] for a in after if is_obsolete(a, active)]
            missing_alerts = sorted(set(protected) - {a["number"] for a in after_alerts})
            verification = {"deleted": len(deleted), "remaining_obsolete_ids": remaining,
                            "missing_preserved_ids": missing, "missing_protected_alerts": missing_alerts,
                            "open_alerts_before": len(alerts), "open_alerts_after": len(after_alerts)}
            (audit_dir / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
            if remaining or missing or missing_alerts:
                raise CleanupError("cleanup verification failed; inspect verification.json")
            print(json.dumps(verification), flush=True)
            record({"event": "complete", **verification})
            return verification
        except BaseException as exc:
            record({"event": "interrupted", "error": str(exc)})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "obot-platform/mcp-images"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            token = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                                   check=True, capture_output=True, text=True).stdout.strip()
        cleanup(GitHub(args.repo, token), args.root, args.expected_sha, args.audit_dir, args.apply)
    except (CleanupError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Cleanup stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
