import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_orchestra import lifecycle  # noqa: E402


CONDUCTOR = "mb_cond0001"
ACCORDION = "mb_acc00001"
TRUMPET = "mb_trump001"
ASSIGN = "m_assign00000001"


def _task(recipients=(ACCORDION,), sender=CONDUCTOR, created_at=1000.0):
    return {
        "task": "t_harvest",
        "message_id": ASSIGN,
        "sender": sender,
        "recipients": list(recipients),
        "created_at": created_at,
    }


def _message(index, sender, act, sent_at, typed=None):
    return {
        "id": f"m_{index:014d}",
        "sender": sender,
        "act": act,
        "lifecycle": typed,
        "sent_at": sent_at,
    }


def _view(task, messages, reached=None, deliveries=None, **kwargs):
    owners = lifecycle.derive_owners(task, messages, reached or {}, deliveries or {}, **kwargs)
    return {**task, "owners": owners, "state": lifecycle.aggregate(owners)}


class DeriveOwnersTests(unittest.TestCase):
    def test_delivery_is_reported_beside_lifecycle_and_never_as_it(self):
        view = _view(
            _task(),
            [],
            deliveries={ACCORDION: {"state": "delivered", "delivered_at": 1005.0, "handled_at": None}},
        )
        owner = view["owners"][0]
        self.assertEqual(owner["state"], "pending")
        self.assertEqual(owner["delivery"], "delivered")
        self.assertEqual(owner["delivered_at"], 1005.0)
        self.assertEqual(view["state"], "pending")

    def test_reopen_applies_only_to_the_owners_it_reached(self):
        messages = [
            _message(1, ACCORDION, "done", 1010.0),
            _message(2, TRUMPET, "done", 1011.0),
            _message(3, CONDUCTOR, "ask", 1020.0, "reopened"),
        ]
        view = _view(
            _task((ACCORDION, TRUMPET)), messages, reached={messages[2]["id"]: [ACCORDION]}
        )
        states = {row["id"]: row["state"] for row in view["owners"]}
        self.assertEqual(states, {ACCORDION: "pending", TRUMPET: "done"})
        self.assertEqual(view["state"], "pending")

    def test_cancel_leaves_finished_work_finished(self):
        messages = [
            _message(1, ACCORDION, "done", 1010.0),
            _message(2, CONDUCTOR, "tell", 1020.0, "cancelled"),
        ]
        view = _view(
            _task((ACCORDION, TRUMPET)),
            messages,
            reached={messages[1]["id"]: [ACCORDION, TRUMPET]},
        )
        states = {row["id"]: row["state"] for row in view["owners"]}
        self.assertEqual(states, {ACCORDION: "done", TRUMPET: "cancelled"})
        self.assertEqual(view["state"], "done")

    def test_an_owner_report_after_done_updates_the_report_time_only(self):
        messages = [
            _message(1, ACCORDION, "done", 1010.0),
            _message(2, ACCORDION, "block", 1020.0),
        ]
        owner = _view(_task(), messages)["owners"][0]
        self.assertEqual(owner["state"], "done")
        self.assertEqual(owner["state_message_id"], messages[0]["id"])
        self.assertEqual(owner["last_report_at"], 1020.0)

    def test_aggregate_is_conservative(self):
        cases = [
            ({"done", "blocked"}, "blocked"),
            ({"done", "pending"}, "pending"),
            ({"started", "unknown"}, "unknown"),
            ({"started", "accepted"}, "accepted"),
            ({"started", "done"}, "started"),
            ({"done", "cancelled"}, "done"),
            ({"cancelled"}, "cancelled"),
            (set(), "unknown"),
        ]
        for states, expected in cases:
            with self.subTest(states=states):
                self.assertEqual(
                    lifecycle.aggregate([{"state": state} for state in states]), expected
                )


class AttentionTests(unittest.TestCase):
    def _items(self, view, at, *, member=CONDUCTOR, conductor=CONDUCTOR, within=900, stale=3600):
        return lifecycle.attention(
            [view],
            member_id=member,
            conductor_id=conductor,
            at=at,
            response_within=within,
            stale_after=stale,
        )

    def test_an_unanswered_assignment_waits_for_the_response_window(self):
        view = _view(_task(created_at=1000.0), [])
        self.assertEqual(self._items(view, 1000.0 + 899), [])
        item = self._items(view, 1000.0 + 901)[0]
        self.assertEqual(item["kind"], "no-response")
        self.assertEqual(item["since"], 1000.0)
        self.assertEqual(item["delivery"], "unknown")

    def test_a_stale_started_task_asks_for_status_on_the_same_task_and_never_reruns(self):
        messages = [_message(1, ACCORDION, "status", 1100.0, "started")]
        view = _view(_task(), messages)
        self.assertEqual(self._items(view, 1100.0 + 3599), [])
        item = self._items(view, 1100.0 + 3601)[0]
        self.assertEqual(item["kind"], "stale")
        self.assertEqual(item["since"], 1100.0)
        self.assertIn("ACT ask, TASK t_harvest, RE m_assign00000001, NEED status", item["next"])
        self.assertIn("do not re-assign or re-run", item["next"])

    def test_a_block_needs_no_threshold_and_comes_first(self):
        blocked = _view(_task(), [_message(1, ACCORDION, "block", 1100.0)])
        silent = {**_view(_task(created_at=500.0), []), "task": "t_other"}
        items = lifecycle.attention(
            [silent, blocked],
            member_id=CONDUCTOR,
            conductor_id=CONDUCTOR,
            at=1101.0,
            response_within=60,
            stale_after=3600,
        )
        self.assertEqual([item["kind"] for item in items], ["blocked", "no-response"])

    def test_a_player_answers_only_for_what_it_assigned(self):
        view = _view(_task(sender=TRUMPET), [])
        self.assertEqual(self._items(view, 99999.0, member=ACCORDION, conductor=CONDUCTOR), [])
        self.assertEqual(len(self._items(view, 99999.0, member=TRUMPET, conductor=CONDUCTOR)), 1)
        self.assertEqual(len(self._items(view, 99999.0, member=CONDUCTOR, conductor=CONDUCTOR)), 1)

    def test_a_task_view_without_owners_is_skipped(self):
        legacy = {key: value for key, value in _task().items()}
        self.assertEqual(self._items(legacy, 99999.0), [])

    def test_one_line_stays_short(self):
        view = _view(
            _task(created_at=1000.0),
            [],
            deliveries={ACCORDION: {"state": "delivered", "delivered_at": 1001.0}},
        )
        item = self._items(view, 1000.0 + 38 * 60)[0]
        self.assertEqual(lifecycle.one_line(item), "t_harvest mb_acc00001: no response 38m, delivered")
        item = self._items(view, 1000.0 + 5 * 3600)[0]
        self.assertEqual(lifecycle.one_line(item), "t_harvest mb_acc00001: no response 5h, delivered")


class ThresholdTests(unittest.TestCase):
    def test_defaults_env_and_explicit_values(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lifecycle.RESPONSE_WITHIN_ENV, None)
            os.environ.pop(lifecycle.STALE_AFTER_ENV, None)
            self.assertEqual(lifecycle.thresholds(), (900.0, 3600.0))
            os.environ[lifecycle.RESPONSE_WITHIN_ENV] = "120"
            os.environ[lifecycle.STALE_AFTER_ENV] = "not a number"
            self.assertEqual(lifecycle.thresholds(), (120.0, 3600.0))
            self.assertEqual(lifecycle.thresholds(30, 7200), (30.0, 7200.0))
            self.assertEqual(lifecycle.thresholds(-5, 0), (120.0, 3600.0))


if __name__ == "__main__":
    unittest.main()
