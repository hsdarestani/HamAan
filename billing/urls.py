from django.urls import path

from . import views

urlpatterns = [
    path("wallet/", views.WalletView, name="wallet"),
    path("wallet/balance/", views.WalletBalanceView, name="wallet-balance"),
    path("coin-packs/", views.CoinPackListView, name="coin-pack-list"),
    path("purchase/", views.PurchaseCreateView, name="purchase-create"),
    path("purchase/status/", views.PurchaseStatusView, name="purchase-status"),
    path("payment/callback/", views.PaymentCallbackView, name="payment-callback"),
    path("coin-txns/", views.CoinTxnListView, name="coin-txn-list"),
]
