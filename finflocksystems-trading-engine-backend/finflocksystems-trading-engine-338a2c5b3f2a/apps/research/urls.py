from django.urls import path

from . import views


urlpatterns = [
    path("dataset-versions/", views.dataset_versions),
    path("universes/", views.universes),
    path("universes/<int:universe_id>/members/", views.universes, {"members": True}),
    path("strategies/", views.strategies),
    path("strategies/<str:research_id>/", views.strategies),
    path("readiness/", views.readiness),
    path("candidate-scores/", views.candidate_scores),
    path("experiments/<int:experiment_id>/", views.experiments),
]
