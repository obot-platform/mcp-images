import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import cleanup_trivy_analyses as cleanup


SHA = "a" * 40
DOCUMENTS = ("images:\n  - name: retained\n", "images:\n  - name: utility\n")
ACTIVE = {"trivy-retained", "trivy-utility", "trivy-base-node", "trivy-base-python"}


def analysis(identifier, category="trivy-retired", ref=cleanup.MAIN_REF, tool="Trivy", deletable=True):
    return {"id": identifier, "category": category, "ref": ref, "tool": {"name": tool},
            "created_at": f"2026-10-{identifier:02d}T00:00:00Z", "deletable": deletable}


class FakeGitHub:
    prefix = "repos/example/images/"

    def __init__(self, analyses):
        self.analyses = copy.deepcopy(analyses)
        self.sha = SHA
        self.deleted = []
        self.fail_id = None
        self.documents = DOCUMENTS
        self.alerts = [{"number": 7}]
        # The same alert is shared by an obsolete and an active category.
        self.instances = [{"ref": cleanup.MAIN_REF, "category": category, "state": "open"}
                          for category in ("trivy-retired", "trivy-retained")]

    def request(self, path, method="GET"):
        if path == self.prefix + "commits/main":
            return {"sha": self.sha}
        for name, contents in zip(cleanup.MANIFESTS, self.documents):
            if path.startswith(self.prefix + "contents/" + name + "?"):
                return {"encoding": "base64", "content": base64.b64encode(contents.encode()).decode()}
        if method == "DELETE":
            identifier = int(path.split("/")[-1].split("?")[0])
            if identifier == self.fail_id:
                raise cleanup.APIError(503, "interrupted")
            current = next(a for a in self.analyses if a["id"] == identifier)
            self.analyses.remove(current)
            self.deleted.append(identifier)
            same = [a for a in self.analyses if (a["ref"], a["category"], a["tool"])
                    == (current["ref"], current["category"], current["tool"])]
            following = max(same, key=lambda a: a["id"]) if same else None
            if following:
                following["deletable"] = True
            return {"next_analysis_url": ("https://api.github.com/" + self.prefix
                    + f"code-scanning/analyses/{following['id']}") if following else None}
        raise AssertionError((path, method))

    def pages(self, path):
        if path.endswith("code-scanning/analyses"):
            return copy.deepcopy(self.analyses)
        if path.endswith("alerts?state=open"):
            return copy.deepcopy(self.alerts)
        if path.endswith("/instances"):
            return copy.deepcopy(self.instances)
        raise AssertionError(path)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, contents in zip(cleanup.MANIFESTS, DOCUMENTS):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self.audit = self.root / "audit"

    def run_cleanup(self, api, apply=False):
        with patch("sys.stdout", new_callable=io.StringIO):
            return cleanup.cleanup(api, self.root, SHA, self.audit, apply)

    def test_manifest_categories_include_both_families_and_bases(self):
        self.assertEqual(cleanup.active_categories(DOCUMENTS), ACTIVE)

    def test_malformed_empty_and_duplicate_manifests_fail_closed(self):
        for document in ("[", "{}", "images: []", "images: [null]", "images: [{name: ../bad}]",
                         "images: [{name: base-python}]", "images: [{name: repeated}, {name: repeated}]"):
            with self.subTest(document=document), self.assertRaises(cleanup.CleanupError):
                cleanup.active_categories((document, DOCUMENTS[1]))
        with self.assertRaises(cleanup.CleanupError):
            cleanup.active_categories((DOCUMENTS[0], DOCUMENTS[0]))

    def test_only_owned_obsolete_main_trivy_categories_are_selected(self):
        for category in ("trivy-retired", "trivy-retained-1.0.0-obot1",
                         ".github/workflows/old.yml:scan/command:--transport streamable-http/name:retired",
                         ".github/workflows/old.yml:scan/image-identifier:retained"):
            self.assertTrue(cleanup.is_obsolete(analysis(1, category), ACTIVE))
        for item in (analysis(1, "trivy-retained"), analysis(2, "custom-scanner"),
                     analysis(3, ref="refs/tags/v1"), analysis(4, ref="refs/pull/1/head"),
                     analysis(5, tool="CodeQL"), analysis(6, ref="refs/heads/other")):
            with self.subTest(item=item):
                self.assertFalse(cleanup.is_obsolete(item, ACTIVE))

    def test_dry_run_keeps_every_analysis_and_records_shared_alert(self):
        api = FakeGitHub([analysis(1), analysis(2, "trivy-retained")])
        result = self.run_cleanup(api)
        self.assertEqual(api.deleted, [])
        self.assertEqual(result["protected_alert_numbers"], [7])
        self.assertEqual(result["preserved_ids"], [2])

    def test_apply_deletes_newest_first_and_preserves_other_history(self):
        protected = [analysis(3, "trivy-retained"), analysis(4, ref="refs/tags/v1"),
                     analysis(5, tool="CodeQL"), analysis(6, ref="refs/pull/1/head")]
        api = FakeGitHub([analysis(1, deletable=False), analysis(2), *protected])
        result = self.run_cleanup(api, True)
        self.assertEqual(api.deleted, [2, 1])
        self.assertEqual(api.analyses, protected)
        self.assertEqual(result["open_alerts_before"], result["open_alerts_after"])
        self.assertEqual(result["missing_protected_alerts"], [])

    def test_changed_main_or_manifest_mismatch_never_deletes(self):
        api = FakeGitHub([analysis(1)])
        api.sha = "b" * 40
        with self.assertRaisesRegex(cleanup.CleanupError, "outdated checkout"):
            self.run_cleanup(api, True)
        api.sha = SHA
        api.documents = ("images: [{name: another-active-image}]", DOCUMENTS[1])
        with self.assertRaisesRegex(cleanup.CleanupError, "categories differ"):
            self.run_cleanup(api, True)
        self.assertEqual(api.deleted, [])

    def test_independent_categories_each_delete_newest_first(self):
        api = FakeGitHub([analysis(1, deletable=False), analysis(2),
                         analysis(3, "trivy-other", deletable=False), analysis(4, "trivy-other")])
        self.run_cleanup(api, True)
        self.assertEqual(set(api.deleted), {1, 2, 3, 4})
        self.assertLess(api.deleted.index(2), api.deleted.index(1))
        self.assertLess(api.deleted.index(4), api.deleted.index(3))

    def test_missing_continuation_still_follows_approved_newest_first_order(self):
        api = FakeGitHub([analysis(1, deletable=False), analysis(2, deletable=False), analysis(3)])
        original = api.request
        def request(path, method="GET"):
            result = original(path, method)
            return {} if method == "DELETE" else result
        api.request = request
        self.run_cleanup(api, True)
        self.assertEqual(api.deleted, [3, 2, 1])

    def test_interrupted_cleanup_resumes_from_server_inventory(self):
        api = FakeGitHub([analysis(1, deletable=False), analysis(2)])
        api.fail_id = 1
        with self.assertRaises(cleanup.APIError):
            self.run_cleanup(api, True)
        self.assertEqual(api.deleted, [2])
        api.fail_id = None
        self.run_cleanup(api, True)
        self.assertEqual(api.deleted, [2, 1])
        events = [json.loads(line) for line in (self.audit / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events], ["deleted", "interrupted", "deleted", "complete"])

    def test_continuation_cannot_delete_an_active_or_uninventoried_analysis(self):
        api = FakeGitHub([analysis(1)])
        original = api.request
        def request(path, method="GET"):
            response = original(path, method)
            if method == "DELETE":
                response["next_analysis_url"] = "https://api.github.com/" + api.prefix + "code-scanning/analyses/999"
            return response
        api.request = request
        with self.assertRaisesRegex(cleanup.CleanupError, "approved category inventory"):
            self.run_cleanup(api, True)
        self.assertEqual(api.deleted, [1])

    def test_continuation_urls_are_restricted_to_the_selected_repository(self):
        api = FakeGitHub([])
        for url in ("https://evil.example/1", "https://api.github.com/repos/other/repo/code-scanning/analyses/1",
                    "https://api.github.com/" + api.prefix + "code-scanning/analyses/not-an-id"):
            with self.assertRaises(cleanup.CleanupError):
                cleanup.next_analysis_id(url, api)

    def test_missing_preserved_history_fails_verification(self):
        api = FakeGitHub([analysis(1), analysis(2, "trivy-retained")])
        original = api.pages
        def pages(path):
            items = original(path)
            return [] if path.endswith("analyses") and api.deleted else items
        api.pages = pages
        with self.assertRaisesRegex(cleanup.CleanupError, "verification failed"):
            self.run_cleanup(api, True)
        verification = json.loads((self.audit / "verification.json").read_text())
        self.assertEqual(verification["missing_preserved_ids"], [2])


class GitHubTests(unittest.TestCase):
    def test_pagination_fetches_all_pages_and_preserves_filters(self):
        api = cleanup.GitHub("example/images", "unused")
        with patch.object(api, "request", side_effect=[list(range(100)), [100]]) as request:
            self.assertEqual(len(api.pages(api.prefix + "code-scanning/alerts?state=open")), 101)
            self.assertIn("state=open&per_page=100&page=2", request.call_args.args[0])

    def test_retries_are_bounded(self):
        api = cleanup.GitHub("example/images", "unused")
        def failure(*args, **kwargs):
            raise HTTPError("https://api.github.com/", 503, "unavailable", {}, io.BytesIO(b"unavailable"))
        with patch.object(cleanup, "urlopen", side_effect=failure) as request, patch.object(cleanup.time, "sleep") as sleep:
            with self.assertRaises(cleanup.APIError):
                api.request(api.prefix + "commits/main")
        self.assertEqual(request.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])

    def test_long_rate_limit_stops_for_resumable_rerun(self):
        api = cleanup.GitHub("example/images", "unused")
        error = HTTPError("https://api.github.com/", 429, "rate limit", {"Retry-After": "120"}, io.BytesIO(b"limited"))
        with patch.object(cleanup, "urlopen", side_effect=error), patch.object(cleanup.time, "sleep") as sleep:
            with self.assertRaisesRegex(cleanup.APIError, "rerun with the same arguments"):
                api.request(api.prefix + "commits/main")
        sleep.assert_not_called()

    def test_short_rate_limit_retries_then_succeeds(self):
        api = cleanup.GitHub("example/images", "unused")
        error = HTTPError("https://api.github.com/", 429, "rate limit", {"Retry-After": "2"}, io.BytesIO(b"limited"))
        with patch.object(cleanup, "urlopen", side_effect=[error, io.BytesIO(b'{"sha": "ok"}')]), patch.object(cleanup.time, "sleep") as sleep:
            self.assertEqual(api.request(api.prefix + "commits/main"), {"sha": "ok"})
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
