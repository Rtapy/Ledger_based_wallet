import re

from rest_framework.pagination import LimitOffsetPagination

from wallet.exceptions import WalletError


class HistoryPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 100

    def paginate_queryset(self, queryset, request, view=None):
        if set(request.query_params) - {"limit", "offset"}:
            raise WalletError("invalid_pagination", "Only limit and offset are supported.")
        return super().paginate_queryset(queryset, request, view)

    def _integer(self, request, name, default, minimum, maximum=None):
        values = request.query_params.getlist(name)
        if not values:
            return default
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            raise WalletError("invalid_pagination", f"{name} must be a single integer.")
        try:
            value = int(values[0])
        except ValueError:
            raise WalletError("invalid_pagination", f"{name} is too large.") from None
        if value < minimum or (maximum is not None and value > maximum):
            raise WalletError("invalid_pagination", f"{name} is outside the supported range.")
        return value

    def get_limit(self, request):
        return self._integer(request, "limit", self.default_limit, 1, self.max_limit)

    def get_offset(self, request):
        return self._integer(request, "offset", 0, 0)
