from django.urls import path

from wallet.views import BalanceView, CreditView, DebitView, HistoryView


app_name = "wallet"
urlpatterns = [
    path("users/<int:user_id>/wallet/", BalanceView.as_view(), name="balance"),
    path("users/<int:user_id>/wallet/credits/", CreditView.as_view(), name="credit"),
    path("users/<int:user_id>/wallet/debits/", DebitView.as_view(), name="debit"),
    path("users/<int:user_id>/wallet/entries/", HistoryView.as_view(), name="history"),
]
