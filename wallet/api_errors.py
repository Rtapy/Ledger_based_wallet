import logging

from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler, set_rollback

from wallet.exceptions import WalletError


logger = logging.getLogger(__name__)

DOMAIN_STATUSES = {
    "invalid_request": 400,
    "invalid_amount": 400,
    "invalid_idempotency_key": 400,
    "invalid_pagination": 400,
    "user_not_found": 404,
    "wallet_not_found": 404,
    "insufficient_funds": 409,
    "balance_limit_exceeded": 409,
    "idempotency_conflict": 409,
}
HTTP_ERRORS = {
    400: ("invalid_request", "Invalid request."),
    404: ("not_found", "Resource does not exist."),
    405: ("method_not_allowed", "Method is not allowed for this endpoint."),
    406: ("not_acceptable", "Only JSON responses are available."),
    415: ("unsupported_media_type", "Use Content-Type: application/json."),
}


def _error(code, message):
    return {"error": {"code": code, "message": message}}


def wallet_exception_handler(exc, context):
    if isinstance(exc, WalletError) and exc.code in DOMAIN_STATUSES:
        set_rollback()
        return Response(_error(exc.code, exc.message), status=DOMAIN_STATUSES[exc.code])

    response = exception_handler(exc, context)
    if response is not None and response.status_code in HTTP_ERRORS:
        code, message = HTTP_ERRORS[response.status_code]
        if isinstance(exc, ValidationError) and isinstance(exc.detail, dict):
            if "amount" in exc.detail:
                code, message = "invalid_amount", "Invalid amount."
            elif "idempotency_key" in exc.detail:
                code, message = "invalid_idempotency_key", "Supply a valid Idempotency-Key UUID."
        response.data = _error(code, message)
        return response

    # The service has already unwound its atomic block. Do not expose SQL,
    # exception text or a traceback to clients, even with DEBUG enabled.
    set_rollback()
    logger.error("Unhandled wallet API error", exc_info=(type(exc), exc, exc.__traceback__))
    return Response(_error("internal_error", "An internal error occurred."), status=500)
