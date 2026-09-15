from django.contrib.auth import get_user_model
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from wallet.api_errors import wallet_exception_handler
from wallet.exceptions import WalletError
from wallet.models import LedgerEntry, Wallet
from wallet.pagination import HistoryPagination
from wallet.parsers import WalletJSONParser
from wallet.serializers import (
    APIErrorSerializer,
    IdempotencyKeySerializer,
    LedgerEntrySerializer,
    PostingSerializer,
    WalletHistorySerializer,
    WalletSerializer,
)
from wallet.services import post_entry


USER_ID_PARAMETER = OpenApiParameter(
    name="user_id",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.PATH,
    description="Django user ID whose wallet is selected.",
)
IDEMPOTENCY_PARAMETER = OpenApiParameter(
    name="Idempotency-Key",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Caller-supplied UUID. Reuse it only when retrying the same operation.",
)
LIMIT_PARAMETER = OpenApiParameter(
    name="limit",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Page size from 1 to 100; defaults to 20.",
)
OFFSET_PARAMETER = OpenApiParameter(
    name="offset",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Nonnegative result offset; defaults to 0.",
)


def posting_schema(summary, operation_id):
    return extend_schema(
        tags=["wallet"],
        summary=summary,
        operation_id=operation_id,
        parameters=[USER_ID_PARAMETER, IDEMPOTENCY_PARAMETER],
        request=PostingSerializer,
        responses={
            200: LedgerEntrySerializer,
            201: LedgerEntrySerializer,
            400: APIErrorSerializer,
            404: APIErrorSerializer,
            409: APIErrorSerializer,
            415: APIErrorSerializer,
        },
    )


def _get_wallet(user_id):
    try:
        return Wallet.objects.get(user_id=user_id)
    except Wallet.DoesNotExist:
        if not get_user_model().objects.filter(pk=user_id).exists():
            raise WalletError("user_not_found", "User does not exist.") from None
        raise WalletError("wallet_not_found", "User has no wallet.") from None


class WalletAPIView(APIView):

    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [WalletJSONParser]
    renderer_classes = [JSONRenderer]

    def get_exception_handler(self):
        return wallet_exception_handler


class PostingView(WalletAPIView):
    entry_type = None

    def post(self, request, user_id):
        body = PostingSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        header = IdempotencyKeySerializer(
            data={"idempotency_key": request.headers.get("Idempotency-Key")}
        )
        header.is_valid(raise_exception=True)
        result = post_entry(
            user_id=user_id,
            entry_type=self.entry_type,
            amount=body.validated_data["amount"],
            idempotency_key=header.validated_data["idempotency_key"],
        )
        return Response(
            LedgerEntrySerializer(result.entry).data,
            status=201 if result.created else 200,
        )


class CreditView(PostingView):
    entry_type = LedgerEntry.EntryType.CREDIT

    @posting_schema("Credit a wallet", "wallet_credit")
    def post(self, request, user_id):
        return super().post(request, user_id)


class DebitView(PostingView):
    entry_type = LedgerEntry.EntryType.DEBIT

    @posting_schema("Debit a wallet", "wallet_debit")
    def post(self, request, user_id):
        return super().post(request, user_id)


class BalanceView(WalletAPIView):
    @extend_schema(
        tags=["wallet"],
        summary="Get the current wallet balance",
        operation_id="wallet_balance",
        parameters=[USER_ID_PARAMETER],
        responses={200: WalletSerializer, 404: APIErrorSerializer},
    )
    def get(self, request, user_id):
        return Response(WalletSerializer(_get_wallet(user_id)).data)


class HistoryView(WalletAPIView):
    @extend_schema(
        tags=["wallet"],
        summary="List wallet ledger entries",
        operation_id="wallet_history",
        parameters=[USER_ID_PARAMETER, LIMIT_PARAMETER, OFFSET_PARAMETER],
        responses={
            200: WalletHistorySerializer,
            400: APIErrorSerializer,
            404: APIErrorSerializer,
        },
    )
    def get(self, request, user_id):
        wallet = _get_wallet(user_id)
        paginator = HistoryPagination()
        page = paginator.paginate_queryset(wallet.entries.order_by("id"), request, self)
        return paginator.get_paginated_response(LedgerEntrySerializer(page, many=True).data)
