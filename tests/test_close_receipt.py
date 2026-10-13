"""Close receipt on POST /comments (issue #117, writer half).

The receipt reports what the write stepped into, as read by the gate before
commit. It is never a post-commit re-read: any thread event stales every
vote, so state read after the write is always "0 current, open".
Design converged on Stoa post 23 (comments 336, 346, 348, 349, 355).
"""

from httpx import AsyncClient

from tests.test_close_votes import ALICE, BOB, _comment, _post


async def _soft_close(client: AsyncClient, root: int) -> str:
    """Two participants, two votes: soft-closed. Returns the head token."""
    await _comment(client, BOB, root)
    await client.post(f"/api/posts/{root}/close-votes", headers=ALICE)
    await client.post(f"/api/posts/{root}/close-votes", headers=BOB)
    state = (await client.get(f"/api/posts/{root}/close-state", headers=BOB)).json()
    assert state["soft_closed"] is True
    return f"{state['head_event_kind']}:{state['head_event_id']}"


class TestCloseReceipt:
    async def test_open_thread_receipt_is_null_not_absent(self, client: AsyncClient):
        """null is a value a client can check; absence is ambiguous."""
        root = await _post(client, ALICE)
        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "An ordinary comment on an open thread."},
            headers=ALICE,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "close_receipt" in body
        assert body["close_receipt"] is None

    async def test_receipt_reports_the_state_the_write_stepped_into(self, client: AsyncClient):
        root = await _post(client, ALICE)
        pin = await _soft_close(client, root)

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "A deliberate post-close correction."},
            headers={**BOB, "X-Acknowledge-Soft-Close": pin},
        )
        assert resp.status_code == 201, resp.text
        receipt = resp.json()["close_receipt"]
        assert receipt == {
            "stepped_into": pin,
            "votes_at_write": {"current": 2, "required": 2, "participants": 2},
        }

    async def test_receipt_is_gate_state_not_a_post_commit_reread(self, client: AsyncClient):
        """After the write, close-state says open with every vote stale. The
        receipt must still say what was there when the write went through."""
        root = await _post(client, ALICE)
        pin = await _soft_close(client, root)

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "Writing past the votes, knowingly."},
            headers={**BOB, "X-Acknowledge-Soft-Close": pin},
        )
        receipt = resp.json()["close_receipt"]

        after = (await client.get(f"/api/posts/{root}/close-state", headers=BOB)).json()
        assert after["soft_closed"] is False
        assert after["current_vote_count"] == 0
        assert f"{after['head_event_kind']}:{after['head_event_id']}" != pin
        assert receipt["stepped_into"] == pin
        assert receipt["votes_at_write"]["current"] == 2

    async def test_stepped_into_uses_the_428_vocabulary(self, client: AsyncClient):
        """One token format: the 428's head_event is the 201's stepped_into."""
        root = await _post(client, ALICE)
        await _soft_close(client, root)

        refused = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "No acknowledgement header on this one."},
            headers=BOB,
        )
        assert refused.status_code == 428
        head_event = refused.json()["detail"]["head_event"]

        resp = await client.post(
            f"/api/posts/{root}/comments",
            json={"body_markdown": "Now acknowledged with the token from the 428."},
            headers={**BOB, "X-Acknowledge-Soft-Close": head_event},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["close_receipt"]["stepped_into"] == head_event

    async def test_list_comments_carries_no_receipt_field(self, client: AsyncClient):
        """The receipt is per-write, not a comment property: reads don't carry it."""
        root = await _post(client, ALICE)
        await _comment(client, BOB, root)
        resp = await client.get(f"/api/posts/{root}/comments", headers=ALICE)
        assert resp.status_code == 200
        assert all("close_receipt" not in c for c in resp.json())
