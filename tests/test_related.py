"""Finding and reusing earlier tickets for the same problem.

The failure these guard against is silent: a search that matches nothing looks
exactly like "this has never happened before", and the agent then re-diagnoses
a bug that already has a merged fix.
"""

from __future__ import annotations

import httpx
import respx
from test_safety import BASE_REPORT

from triage.clients.jira import JiraClient, mentioned_keys
from triage.config import JiraConfig
from triage.schema import render_markdown, validate_report
from triage.tools import _phrases, related_ticket_queries, select_comments

JIRA_CFG = JiraConfig(base_url="https://x.atlassian.net", email="e", api_token="t")


def test_search_is_not_pinned_to_one_project() -> None:
    """Pinning to JIRA_PROJECT_KEY is what hid the EPS tickets from a CAM one."""
    queries = related_ticket_queries(["Split Testing"], exclude="CAM-7674")
    assert all("project" not in jql for _, _, jql in queries)
    assert all("key != CAM-7674" in jql for _, _, jql in queries)

    scoped = related_ticket_queries(["Split Testing"], projects=("EPS", "CAM"))
    assert all("project in (EPS, CAM)" in jql for _, _, jql in scoped)


def test_each_phrase_is_searched_on_its_own_and_together() -> None:
    queries = related_ticket_queries(["Campaign Performance", "Split Testing"])
    labels = [label for label, _, _ in queries]
    assert labels[0] == "all phrases"
    assert '"Campaign Performance"' in labels and '"Split Testing"' in labels
    # An exact-phrase clause, quoted inside the JQL string.
    assert 'text ~ "\\"Split Testing\\""' in queries[0][2]


def test_phrases_strip_jql_reserved_punctuation_but_keep_apostrophes() -> None:
    assert _phrases("Torchy's Tacos\nNullPointerException: (foo) [bar]\n;x") == [
        "Torchy's Tacos",
        "NullPointerException foo bar",
    ]


def test_release_names_are_not_ticket_keys() -> None:
    text = "same as EPS-11825, fix in EPS-09-29-2026 and EPS-09.29.2026; see CAM-7305"
    assert mentioned_keys(text, exclude="CAM-7674") == ["EPS-11825", "CAM-7305"]


def test_comment_selection_keeps_the_diagnosis_over_the_chasers() -> None:
    finding = {"author": "Eng", "created": "2026-08-11",
               "body": "On investigating the code we found performance metrics are "
                       "not being routed for split tests with both variants."}
    chasers = [{"author": "Support", "created": f"2026-08-{d:02d}",
                "body": "Just wanted to check here - any further updates on this? Thank you."}
               for d in range(12, 30)]
    bot = {"author": "Automation for Jira", "created": "2026-09-17",
           "body": "This ticket is QA complete and hence moved to Ready for Deployment."}
    kept = select_comments([finding, *chasers, bot])
    assert kept == [finding]


@respx.mock
def test_pull_requests_use_the_instance_type_from_the_summary() -> None:
    """The detail call returns nothing for applicationType=GitHub; it needs the
    exact instance-type key the summary reports."""
    respx.get("https://x.atlassian.net/rest/dev-status/latest/issue/summary").mock(
        return_value=httpx.Response(200, json={"summary": {"pullrequest": {
            "byInstanceType": {"oAuth-com.github.integration.production": {"count": 1}}}}})
    )
    detail = respx.get("https://x.atlassian.net/rest/dev-status/latest/issue/detail").mock(
        return_value=httpx.Response(200, json={"detail": [{"pullRequests": [{
            "name": "[EPS-11825] Add variant notification stats", "status": "MERGED",
            "url": "https://github.com/punchh/csp-foundation/pull/127",
            "repositoryName": "punchh/csp-foundation"}]}]})
    )
    prs = JiraClient(JIRA_CFG).pull_requests("375127")
    assert prs[0]["url"].endswith("/pull/127") and prs[0]["status"] == "MERGED"
    assert detail.calls[0].request.url.params["applicationType"] == (
        "oAuth-com.github.integration.production"
    )


@respx.mock
def test_pull_request_lookup_failure_is_not_fatal() -> None:
    respx.get("https://x.atlassian.net/rest/dev-status/latest/issue/summary").mock(
        return_value=httpx.Response(403)
    )
    assert JiraClient(JIRA_CFG).pull_requests("1") == []


SAME_ISSUE = {
    "key": "EPS-11825",
    "url": "https://x.atlassian.net/browse/EPS-11825",
    "match": "same_issue",
    "reason": "Same split-test notification campaign, same missing sales metrics.",
    "their_resolution": "performanceMetrics was not routed for notification-only split tests.",
    "fix_status": "fix_merged_not_released",
    "pull_requests": ["https://github.com/punchh/csp-foundation/pull/127"],
}
JIRA_ONLY = {
    **BASE_REPORT,
    "evidence": [{"source": "jira", "claim": "c", "detail": "d",
                  "query_or_path": "EPS-11825"}],
}


def test_same_issue_match_keeps_the_verdict_at_medium() -> None:
    r, notes = validate_report({**JIRA_ONLY, "similar_tickets": [SAME_ISSUE]})
    assert r["verdict"] == "root_cause_identified"
    assert r["confidence"] == "medium"
    assert any("EPS-11825" in n for n in notes)


def test_jira_only_without_a_match_is_still_hearsay() -> None:
    related = {**SAME_ISSUE, "match": "related"}
    r, _ = validate_report({**JIRA_ONLY, "similar_tickets": [related]})
    assert r["verdict"] == "narrowed_not_confirmed"
    assert r["confidence"] == "low"


def test_comment_links_the_reference_ticket_and_its_pr() -> None:
    different = {"key": "EPS-11906", "url": "https://x.atlassian.net/browse/EPS-11906",
                 "match": "different", "reason": "Coupon export discrepancy."}
    md = render_markdown(
        {**BASE_REPORT, "similar_tickets": [different, SAME_ISSUE]}, "", "run1"
    )
    assert "**Based on [EPS-11825](https://x.atlassian.net/browse/EPS-11825)**" in md
    assert "fix merged, not yet released" in md
    assert "[punchh/csp-foundation#127](https://github.com/punchh/csp-foundation/pull/127)" in md
    # The same-issue match is listed before the ruled-out one.
    assert md.index("Same issue:") < md.index("Checked — different problem:")
