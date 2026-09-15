from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from wallet.models import LedgerEntry, Wallet
from wallet.services import post_entry


User = get_user_model()


class WalletAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="api-alice")
        cls.wallet = Wallet.objects.create(user=cls.user)
        cls.other_user = User.objects.create_user(username="api-bob")
        cls.other_wallet = Wallet.objects.create(user=cls.other_user)

    def _url(self, name, user_id=None):
        return reverse(f"wallet:{name}", kwargs={"user_id": user_id or self.user.pk})

    def _post(self, name="credit", amount="10", key=None, user_id=None):
        return self.client.post(
            self._url(name, user_id), {"amount": amount}, format="json",
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def _seed(self, amount="10", user_id=None):
        return post_entry(
            user_id=user_id or self.user.pk, entry_type="credit",
            amount=Decimal(amount), idempotency_key=uuid4(),
        ).entry

    def _snapshot(self):
        return (
            list(Wallet.objects.order_by("pk").values()),
            list(LedgerEntry.objects.order_by("pk").values()),
        )

    def _assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response["Content-Type"], "application/json")
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertTrue(body["error"]["message"])

    def test_all_four_documented_routes_are_available(self):
        root = f"/api/users/{self.user.pk}/wallet/"
        for name, suffix in (
            ("balance", ""), ("credit", "credits/"),
            ("debit", "debits/"), ("history", "entries/"),
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(self._url(name), root + suffix)

        credit = self._post(amount="10.1")
        debit = self._post("debit", amount="0.1")
        self.assertEqual(credit.status_code, 201)
        self.assertEqual(debit.status_code, 201)
        balance = self.client.get(self._url("balance"))
        self.assertEqual(balance.status_code, 200)
        self.assertEqual(balance.json()["balance"], "10.00000000")
        self.assertEqual(set(balance.json()), {"id", "user_id", "balance", "updated_at"})
        history = self.client.get(self._url("history"))
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["results"], [credit.json(), debit.json()])

    def test_entry_response_has_public_fields_fixed_decimals_and_utc_time(self):
        key = uuid4()
        response = self._post(amount="1.12345678", key=key)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(set(body), {
            "id", "wallet_id", "type", "amount", "balance_before",
            "balance_after", "idempotency_key", "created_at",
        })
        self.assertEqual(body["wallet_id"], self.wallet.pk)
        self.assertEqual(body["type"], "credit")
        self.assertEqual(body["amount"], "1.12345678")
        self.assertEqual(body["balance_before"], "0.00000000")
        self.assertEqual(body["balance_after"], "1.12345678")
        self.assertEqual(body["idempotency_key"], str(key))
        self.assertTrue(body["created_at"].endswith("Z"))

    def test_api_quantizes_before_calling_service_and_parses_uuid(self):
        key = uuid4()
        with patch("wallet.views.post_entry", wraps=post_entry) as posting:
            response = self._post(amount="00010.1", key=str(key).upper())
        self.assertEqual(response.status_code, 201)
        posting.assert_called_once()
        arguments = posting.call_args.kwargs
        self.assertIsInstance(arguments["amount"], Decimal)
        self.assertEqual(arguments["amount"], Decimal("10.10000000"))
        self.assertEqual(arguments["amount"].as_tuple().exponent, -8)
        self.assertIsInstance(arguments["idempotency_key"], UUID)
        self.assertEqual(arguments["idempotency_key"], key)

    def test_invalid_amounts_on_both_operations_leave_all_data_unchanged(self):
        amounts = (
            "0", "0.00000000", "-0", "-1", "+1", " 1", "1 ", "1\n",
            "1e2", "1E-8", ".1", "1.", "1,000", "1_000", "۱", "١.٢",
            "NaN", "Infinity", "-Infinity", "", "0.000000001",
            "1.000000000", "1.123456789", "1000000000000",
            "9" * 100, 1, 1.25, True, False, None, [], {},
        )
        for name in ("credit", "debit"):
            for amount in amounts:
                with self.subTest(endpoint=name, amount=amount):
                    before = self._snapshot()
                    self._assert_error(self._post(name, amount), 400, "invalid_amount")
                    self.assertEqual(self._snapshot(), before)

    def test_body_shape_missing_amount_and_unknown_fields_are_rejected(self):
        for data in ({}, [], "10", {"amount": "10", "user_id": self.other_user.pk},
                     {"amount": "10", "wallet_id": self.other_wallet.pk},
                     {"amount": "10", "type": "debit"}):
            with self.subTest(data=data):
                before = self._snapshot()
                response = self.client.post(
                    self._url("credit"), data, format="json",
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                self._assert_error(response, 400, "invalid_request")
                self.assertEqual(self._snapshot(), before)

    def test_malformed_duplicate_and_nonstandard_json_are_rejected(self):
        for body in (
            '{"amount":', '{"amount":"1","amount":"2"}',
            '{"amount":NaN}', '{"amount":Infinity}', 'null', '',
        ):
            with self.subTest(body=body):
                before = self._snapshot()
                response = self.client.generic(
                    "POST", self._url("credit"), body, content_type="application/json",
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                self._assert_error(response, 400, "invalid_request")
                self.assertEqual(self._snapshot(), before)

    def test_missing_or_invalid_idempotency_headers_are_rejected(self):
        for key in (None, "", "abc", "123", " " + str(uuid4()),
                    f"{uuid4()},{uuid4()}"):
            with self.subTest(key=key):
                headers = {} if key is None else {"HTTP_IDEMPOTENCY_KEY": key}
                before = self._snapshot()
                response = self.client.post(
                    self._url("credit"), {"amount": "10"}, format="json", **headers
                )
                self._assert_error(response, 400, "invalid_idempotency_key")
                self.assertEqual(self._snapshot(), before)

    def test_minimum_and_maximum_amounts_and_debit_to_zero(self):
        for amount in ("0.00000001", "999999999999.99999999"):
            with self.subTest(amount=amount):
                credit = self._post(amount=amount)
                self.assertEqual(credit.status_code, 201, credit.content)
                debit = self._post("debit", amount=amount)
                self.assertEqual(debit.status_code, 201, debit.content)
                self.assertEqual(debit.json()["balance_after"], "0.00000000")

    def test_insufficient_funds_and_balance_overflow_are_conflicts_without_writes(self):
        before = self._snapshot()
        self._assert_error(self._post("debit"), 409, "insufficient_funds")
        self.assertEqual(self._snapshot(), before)
        self._seed("999999999999.99999999")
        before = self._snapshot()
        self._assert_error(self._post(amount="0.00000001"), 409, "balance_limit_exceeded")
        self.assertEqual(self._snapshot(), before)

    def test_retry_returns_original_json_and_200_without_mutation(self):
        key = uuid4()
        first = self._post(key=key)
        self._post(amount="20")
        before = self._snapshot()
        replay = self._post(amount="10.00000000", key=str(key).upper())
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self._snapshot(), before)

    def test_debit_retry_succeeds_with_insufficient_current_funds(self):
        self._seed()
        key = uuid4()
        first = self._post("debit", key=key)
        before = self._snapshot()
        replay = self._post("debit", key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self._snapshot(), before)

    def test_changed_payload_with_same_key_is_a_conflict(self):
        key = uuid4()
        self._post(key=key)
        for name, amount in (("credit", "11"), ("debit", "10")):
            with self.subTest(endpoint=name):
                before = self._snapshot()
                self._assert_error(self._post(name, amount, key), 409, "idempotency_conflict")
                self.assertEqual(self._snapshot(), before)

    def test_rejected_request_can_retry_with_the_same_key(self):
        key = uuid4()
        self._assert_error(self._post("debit", key=key), 409, "insufficient_funds")
        self._post()
        self.assertEqual(self._post("debit", key=key).status_code, 201)
        self.assertEqual(self._post("debit", key=key).status_code, 200)

    def test_reads_writes_and_replay_are_scoped_to_the_url_user(self):
        key = uuid4()
        own = self._post(key=key)
        before_other = Wallet.objects.filter(pk=self.other_wallet.pk).values().get()
        self._post("debit", amount="1")
        self.assertEqual(
            Wallet.objects.filter(pk=self.other_wallet.pk).values().get(), before_other
        )
        other = self._post(amount="50", key=key, user_id=self.other_user.pk)
        self.assertEqual(other.status_code, 201)
        self.assertNotEqual(own.json()["id"], other.json()["id"])
        self.assertEqual(other.json()["wallet_id"], self.other_wallet.pk)

        for user, wallet, balance, count in (
            (self.user, self.wallet, "9.00000000", 2),
            (self.other_user, self.other_wallet, "50.00000000", 1),
        ):
            with self.subTest(user=user.pk):
                state = self.client.get(self._url("balance", user.pk)).json()
                self.assertEqual(state["id"], wallet.pk)
                self.assertEqual(state["user_id"], user.pk)
                self.assertEqual(state["balance"], balance)
                history = self.client.get(self._url("history", user.pk)).json()
                self.assertEqual(history["count"], count)
                self.assertTrue(all(row["wallet_id"] == wallet.pk for row in history["results"]))

    def test_missing_user_and_missing_wallet_return_404_on_all_endpoints(self):
        without_wallet = User.objects.create_user(username="api-no-wallet")
        missing_id = without_wallet.pk + 1
        for user_id, code in (
            (missing_id, "user_not_found"), (without_wallet.pk, "wallet_not_found")
        ):
            for name in ("credit", "debit", "balance", "history"):
                with self.subTest(user=user_id, endpoint=name):
                    before = self._snapshot()
                    response = (
                        self._post(name, user_id=user_id) if name in ("credit", "debit")
                        else self.client.get(self._url(name, user_id))
                    )
                    self._assert_error(response, 404, code)
                    self.assertEqual(self._snapshot(), before)

    def test_empty_wallet_has_zero_balance_and_empty_history(self):
        self.assertEqual(self.client.get(self._url("balance")).json()["balance"], "0.00000000")
        self.assertEqual(self.client.get(self._url("history")).json(), {
            "count": 0, "next": None, "previous": None, "results": [],
        })

    def test_history_paginates_complete_ordered_wallet_scoped_data(self):
        expected = []
        for _ in range(5):
            expected.append(self._seed().pk)
            self._seed(user_id=self.other_user.pk)
        url = self._url("history") + "?limit=2"
        seen = []
        previous_expected = False
        while url:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(set(body), {"count", "next", "previous", "results"})
            self.assertEqual(body["count"], 5)
            self.assertEqual(body["previous"] is not None, previous_expected)
            self.assertTrue(all(row["wallet_id"] == self.wallet.pk for row in body["results"]))
            seen.extend(row["id"] for row in body["results"])
            if body["next"]:
                self.assertEqual(urlsplit(body["next"]).path, self._url("history"))
            url = body["next"]
            previous_expected = True
        self.assertEqual(seen, expected)

    def test_history_default_limit_and_upper_limit(self):
        for _ in range(21):
            self._seed()
        default = self.client.get(self._url("history")).json()
        self.assertEqual(default["count"], 21)
        self.assertEqual(len(default["results"]), 20)
        maximum = self.client.get(self._url("history"), {"limit": 100})
        self.assertEqual(maximum.status_code, 200)
        self.assertEqual(len(maximum.json()["results"]), 21)

    def test_invalid_pagination_is_rejected_instead_of_silently_defaulting(self):
        queries = (
            "limit=0", "limit=101", "limit=-1", "limit=1.0", "limit=", "limit=abc",
            "offset=-1", "offset=1.0", "offset=", "offset=abc", "offset=%2B1",
            "limit=1&limit=2", "offset=0&offset=1", "limit=%201", "offset=١",
            f"user_id={self.other_user.pk}", "ordering=-id",
        )
        for query in queries:
            with self.subTest(query=query):
                before = self._snapshot()
                response = self.client.get(self._url("history") + "?" + query)
                self._assert_error(response, 400, "invalid_pagination")
                self.assertEqual(self._snapshot(), before)

    def test_offset_beyond_end_including_large_integers_returns_empty_results(self):
        self._seed()
        for offset in (1, 100, 10**30):
            with self.subTest(offset=offset):
                response = self.client.get(self._url("history"), {"offset": offset})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["count"], 1)
                self.assertEqual(response.json()["results"], [])

    def test_wrong_methods_return_json_405(self):
        for name, method in (
            ("credit", "get"), ("debit", "get"), ("balance", "post"),
            ("history", "post"), ("balance", "delete"), ("history", "patch"),
        ):
            with self.subTest(endpoint=name, method=method):
                before = self._snapshot()
                response = getattr(self.client, method)(self._url(name))
                self._assert_error(response, 405, "method_not_allowed")
                self.assertIn("Allow", response)
                self.assertEqual(self._snapshot(), before)

    def test_unsupported_content_type_and_accept_return_json_errors(self):
        before = self._snapshot()
        response = self.client.generic(
            "POST", self._url("credit"), "amount=10", content_type="text/plain",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        self._assert_error(response, 415, "unsupported_media_type")
        response = self.client.get(self._url("balance"), HTTP_ACCEPT="text/html")
        self._assert_error(response, 406, "not_acceptable")
        self.assertEqual(self._snapshot(), before)

    def test_evaluation_api_does_not_enable_session_or_basic_authentication(self):
        # This documents the no-auth scope; it is not an ownership guarantee.
        self.client.force_login(self.other_user)
        response = self.client.post(
            self._url("credit"), {"amount": "10"}, format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid4()), HTTP_AUTHORIZATION="Basic invalid",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["wallet_id"], self.wallet.pk)

    def test_unexpected_error_is_logged_and_hidden_even_with_debug(self):
        before = self._snapshot()
        with self.settings(DEBUG=True):
            with patch("wallet.views.post_entry", side_effect=RuntimeError("private SQL details")):
                with self.assertLogs("wallet.api_errors", level="ERROR"):
                    response = self._post()
        self._assert_error(response, 500, "internal_error")
        self.assertNotIn("private SQL details", response.content.decode())
        self.assertEqual(self._snapshot(), before)

    def test_persistence_failure_returns_500_rolls_back_and_allows_retry(self):
        self._seed()
        before = self._snapshot()
        key = uuid4()
        with patch.object(LedgerEntry, "save", side_effect=RuntimeError("insert failed")):
            with self.assertLogs("wallet.api_errors", level="ERROR"):
                response = self._post(key=key)
        self._assert_error(response, 500, "internal_error")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._post(key=key).status_code, 201)
