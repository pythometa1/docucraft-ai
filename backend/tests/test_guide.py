"""The authoring guide is served only to signed-in users.

It used to ship in the frontend bundle, where anyone could read it without an
account. These pin the login requirement and the words it must not contain.
"""

import json


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_the_guide_needs_a_login(app_client):
    assert app_client.get("/api/v1/guide").status_code == 401


def test_a_signed_in_user_gets_the_guide(app_client, two_orgs):
    token, *_ = two_orgs
    res = app_client.get("/api/v1/guide", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    ids = [s["id"] for s in body["sections"]]
    assert {"authoring", "conditions", "states", "api-errors", "api-reference"} <= set(ids)
    assert all(s["title"] and s["blocks"] for s in body["sections"])


def test_the_guide_says_what_to_do_not_how_it_works():
    from app.guide import load_guide

    text = json.dumps(load_guide()).lower()
    for word in ("openapi.json", "canary", "deterministic", "confidence band", "engine",
                 "run by run", "embedding", "language model", "llm", "three sample"):
        assert word not in text, word
