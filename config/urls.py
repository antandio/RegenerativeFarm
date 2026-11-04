from django.urls import path, include
from portal import views

# The Django admin route is disabled for this MVP: it is not needed for
# operation and is not separately hardened/protected here, so it returns
# 404 rather than exposing an extra login surface. Admin static assets
# remain collected/served by WhiteNoise but no admin view is reachable.
urlpatterns = [path("healthz/", views.healthz, name="healthz"), path("i18n/setlang/", views.set_language_view, name="set_language"), path("", include("portal.urls"))]
