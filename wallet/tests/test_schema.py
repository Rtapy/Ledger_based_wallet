import json

from django.test import SimpleTestCase
from django.urls import reverse


class OpenAPISchemaTests(SimpleTestCase):
    def test_schema_lists_the_four_wallet_operations(self):
        response = self.client.get(
            reverse("api-schema"),
            HTTP_ACCEPT="application/vnd.oai.openapi+json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        schema = json.loads(response.content)
        self.assertEqual(schema["info"]["title"], "Ledger-Based Wallet API")
        self.assertEqual(
            set(schema["paths"]),
            {
                "/api/users/{user_id}/wallet/",
                "/api/users/{user_id}/wallet/credits/",
                "/api/users/{user_id}/wallet/debits/",
                "/api/users/{user_id}/wallet/entries/",
            },
        )
        self.assertEqual(set(schema["paths"]["/api/users/{user_id}/wallet/"]), {"get"})
        self.assertEqual(
            set(schema["paths"]["/api/users/{user_id}/wallet/credits/"]), {"post"}
        )
        credit_parameters = schema["paths"][
            "/api/users/{user_id}/wallet/credits/"
        ]["post"]["parameters"]
        idempotency = next(
            parameter for parameter in credit_parameters
            if parameter["name"] == "Idempotency-Key"
        )
        self.assertTrue(idempotency["required"])
        self.assertEqual(idempotency["schema"]["format"], "uuid")

    def test_swagger_ui_is_available(self):
        response = self.client.get(reverse("swagger-ui"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("api-schema"))
