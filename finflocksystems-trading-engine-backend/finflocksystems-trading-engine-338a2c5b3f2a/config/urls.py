from django.conf import settings
from django.urls import include, path
from apps.core import views
from apps.event_bus import views as streaming_views
from apps.allocation import views as allocation_views
from apps.rebalancing import views as rebalancing_views
from apps.position_sizing import views as sizing_views
from apps.strategies import views as strategy_views
from apps.core import analytics as analytics_views
from apps.market_data import views as market_data_views
from apps.portfolio_optimization import views as optimization_views
from apps.portfolio_construction import views as construction_views
from apps.research import views as research_views
from apps.broker_gateway import views as broker_session_views
from apps.execution import views as execution_views

api_patterns = [
    path("system/", views.system), path("auth/session/",views.auth_session), path("gateway/", views.gateway), path("accounts/", views.accounts),
    path("instruments/", views.instruments), path("portfolios/", views.portfolios), path("positions/", views.positions),
    path("rebalances/", views.rebalances),
    path("orders/", views.orders), path("orders/manual-quote/", views.manual_order_quote), path("orders/intents/<int:intent_id>/status/", views.manual_order_intent_status), path("orders/intents/<int:intent_id>/confirmation/", views.manual_order_confirmation), path("orders/<str:internal_id>/detail/", views.orders, {"action":"detail"}), path("orders/<str:internal_id>/", views.orders), path("orders/<str:internal_id>/cancel/", views.orders, {"action":"cancel"}), path("executions/", views.executions), path("reconciliation/", views.reconciliation),
    path("risk/", views.risk), path("audit/", views.audit),
]
new_api = [
    path("options/strategy-instances/", strategy_views.instances, {"options_only": True}),
    path("options/strategy-instances/<int:instance_id>/", strategy_views.instances, {"options_only": True}),
    path("broker-sessions/",broker_session_views.sessions),
    path("broker-sessions/<uuid:session_id>/",broker_session_views.sessions),
    path("broker-sessions/<uuid:session_id>/reconnect/",broker_session_views.sessions,{"action":"reconnect"}),
    path("broker-sessions/<uuid:session_id>/credentials/",broker_session_views.sessions,{"action":"credentials"}),
    path("broker-sessions/<uuid:session_id>/accounts/",broker_session_views.sessions,{"action":"accounts"}),
    path("dashboard/summary/",analytics_views.dashboard_summary),path("portfolios/series/",analytics_views.portfolio_series),
    path("strategy-risk-profiles/",strategy_views.risk_profile_catalogue),
    path("strategy-definitions/",strategy_views.definitions),path("strategy-definitions/<str:key>/",strategy_views.definitions),
    path("strategy-instances/",strategy_views.instances),path("strategy-instances/<int:instance_id>/",strategy_views.instances),
    path("strategy-instances/<int:instance_id>/enable/",strategy_views.action,{"action_name":"enable"}),
    path("strategy-instances/<int:instance_id>/pause/",strategy_views.action,{"action_name":"pause"}),
    path("strategy-instances/<int:instance_id>/flatten/",strategy_views.action,{"action_name":"flatten"}),
    path("strategy-instances/<int:instance_id>/state/",strategy_views.related,{"resource":"state"}),
    path("strategy-instances/<int:instance_id>/signals/",strategy_views.related,{"resource":"signals"}),
    path("strategy-instances/<int:instance_id>/runs/",strategy_views.related,{"resource":"runs"}),
    path("strategy-instances/<int:instance_id>/targets/",strategy_views.related,{"resource":"targets"}),
    path("strategy-instances/<int:instance_id>/execution-timeline/",strategy_views.related,{"resource":"execution-timeline"}),
    path("strategy-instances/<int:instance_id>/ledger/",strategy_views.related,{"resource":"ledger"}),
    path("strategy-instances/<int:instance_id>/pnl/",strategy_views.related,{"resource":"pnl"}),
    path("strategy-instances/<int:instance_id>/chart/",strategy_views.chart),
    path("strategy-policies/",strategy_views.policies),path("instruments/search/",strategy_views.search_instruments),path("instruments/resolve/",strategy_views.resolve),
    path("instruments/options/chain/",strategy_views.instrument_option_chain),path("instruments/options/resolve/",strategy_views.resolve_option),
    path("intelligence/", include("apps.intelligence.urls")),
    path("streaming/health/",streaming_views.health),path("streaming/topics/",streaming_views.topics),
    path("execution/readiness/",execution_views.readiness),
    path("execution/diagnostics/",execution_views.diagnostics),
    path("execution/flink-diagnostics/",execution_views.flink_diagnostics),
    path("streaming/consumer-lag/",streaming_views.consumer_lag),path("streaming/dead-letter/",streaming_views.dead_letter),
    path("streaming/replay/",streaming_views.replay),path("streaming/replay/<int:replay_id>/",streaming_views.replay_status),
    path("allocations/policies/",allocation_views.policies),path("allocations/flows/",allocation_views.flows),
    path("allocations/runs/",allocation_views.runs),path("allocations/runs/<int:run_id>/",allocation_views.runs),
    path("rebalancing/policies/",rebalancing_views.policies),path("rebalancing/preview/",rebalancing_views.execute,{"preview":True}),
    path("rebalancing/run/",rebalancing_views.execute,{"preview":False}),path("rebalancing/runs/",rebalancing_views.runs),
    path("rebalancing/runs/<int:run_id>/",rebalancing_views.runs),path("position-sizing/preview/",sizing_views.preview),
    path("position-sizing/decisions/<int:decision_id>/",sizing_views.decision),
    path("data-providers/finnhub/",market_data_views.status),
    path("data-providers/nfo-ingestion/",market_data_views.ingestion_status),
    path("data-providers/finnhub/configure/",market_data_views.configure),
    path("data-providers/finnhub/test/",market_data_views.test),
    path("data-providers/finnhub/mappings/",market_data_views.mappings),
    path("data-providers/finnhub/mappings/<int:instrument_id>/",market_data_views.mappings),
    path("portfolio-universe/",optimization_views.universes),
    path("portfolio-optimization/policies/",optimization_views.policies),
    path("portfolio-optimization/preview/",optimization_views.execute,{"preview":True}),
    path("portfolio-optimization/run/",optimization_views.execute,{"preview":False}),
    path("portfolio-optimization/runs/",optimization_views.runs),
    path("portfolio-optimization/runs/<int:run_id>/",optimization_views.runs),
    path("portfolio-construction/plans/",construction_views.plans),
    path("portfolio-construction/plans/<int:plan_id>/",construction_views.plans),
    path("portfolio-construction/plans/<int:plan_id>/goals/",construction_views.plan_goals),
    path("portfolio-construction/plans/<int:plan_id>/recommendations/",construction_views.plan_recommendations),
    path("portfolio-construction/recommendation-batches/<int:batch_id>/",construction_views.recommendation_batches),
    path("portfolio-construction/readiness/",construction_views.builder_readiness),
    path("portfolio-construction/goals/<int:goal_id>/",construction_views.goal_detail),
    path("portfolio-construction/goals/<int:goal_id>/eligible-strategies/",construction_views.goal_eligible_strategies),
    path("portfolio-construction/goals/<int:goal_id>/instruments/",construction_views.goal_instruments),
    path("portfolio-construction/instruments/<int:goal_instrument_id>/",construction_views.instrument_detail),
    path("portfolio-construction/instruments/<int:goal_instrument_id>/assignments/",construction_views.instrument_assignments),
    path("portfolio-construction/assignments/<int:assignment_id>/",construction_views.assignment_detail),
    path("portfolio-construction/preview/",construction_views.preview),
    path("portfolio-construction/runs/",construction_views.runs),
    path("portfolio-construction/runs/<int:run_id>/",construction_views.runs),
    path("portfolio-construction/runs/<int:run_id>/apply/",construction_views.apply),
    path("research/dataset-versions/",research_views.dataset_versions),
    path("research/universes/",research_views.universes),
    path("research/universes/<int:universe_id>/members/",research_views.universes,{"members":True}),
    path("research/strategies/",research_views.strategies),
    path("research/strategies/<str:research_id>/",research_views.strategies),
    path("research/readiness/",research_views.readiness),
    path("research/candidate-scores/",research_views.candidate_scores),
    path("research/experiments/<int:experiment_id>/",research_views.experiments),
]

api_urlpatterns = [*api_patterns, *new_api]
application_urlpatterns = [
    path("", views.backend_root),
    path("healthz", views.health),
    path("readyz", views.readiness),
    path("dashboard", views.dashboard_alias),
    path("metrics", streaming_views.prometheus_metrics),
    path("api/v1/", include(api_urlpatterns)),
]

urlpatterns = [*application_urlpatterns]
if settings.APP_BASE_PATH:
    prefix = settings.APP_BASE_PATH.strip("/")
    urlpatterns += [
        path(prefix, views.backend_root),
        path(f"{prefix}/", include(application_urlpatterns)),
    ]
