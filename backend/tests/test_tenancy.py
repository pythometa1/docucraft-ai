"""Tenancy guards.

Multi-tenancy is enforced per handler, so the risk is a handler that filters on
the path parameter alone. These tests drive org B's token at org A's ids and
assert a 404 -- indistinguishable from a genuinely missing row, so the endpoint
cannot be used to probe for existence either.
"""

import pytest

UNKNOWN = "00000000-0000-0000-0000-000000000000"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path_template", [
    "/api/v1/projects/{pid}",
    "/api/v1/projects/{pid}/templates",
    "/api/v1/projects/{pid}/sources",
    "/api/v1/projects/{pid}/documents",
    "/api/v1/projects/{pid}/template-clusters",
])
def test_other_orgs_project_is_not_readable(app_client, two_orgs, path_template):
    token_a, project_a, token_b, _project_b = two_orgs

    mine = app_client.get(path_template.format(pid=project_a), headers=_auth(token_a))
    assert mine.status_code == 200, f"own project should still be readable: {mine.text}"

    theirs = app_client.get(path_template.format(pid=project_a), headers=_auth(token_b))
    assert theirs.status_code == 404, f"cross-tenant read returned {theirs.status_code}"


@pytest.mark.parametrize("path", [
    "/api/v1/projects/{u}/templates",
    "/api/v1/projects/{u}/sources",
    "/api/v1/template-versions/{u}/sections",
    "/api/v1/source-versions/{u}/chunks",
    "/api/v1/documents/{u}/versions",
    "/api/v1/document-versions/{u}",
])
def test_unknown_ids_404_rather_than_returning_empty(app_client, two_orgs, path):
    """These previously returned 200 with an empty list, which leaks the fact
    that the endpoint does no ownership check at all."""
    token_a, *_ = two_orgs
    res = app_client.get(path.format(u=UNKNOWN), headers=_auth(token_a))
    assert res.status_code == 404, f"{path} returned {res.status_code}"


def test_error_envelope_shape(app_client, two_orgs):
    """The frontend unwraps `detail.error`; lock the shape so a refactor that
    changes it fails here rather than silently rendering '[object Object]'."""
    token_a, *_ = two_orgs
    res = app_client.get(f"/api/v1/projects/{UNKNOWN}", headers=_auth(token_a))
    assert res.status_code == 404
    body = res.json()
    assert body["detail"]["error"]["code"] == "PROJECT_NOT_FOUND"
    assert body["detail"]["error"]["message"]


def test_missing_token_is_rejected(app_client):
    res = app_client.get("/api/v1/projects")
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["code"] == "TOKEN_MISSING"
