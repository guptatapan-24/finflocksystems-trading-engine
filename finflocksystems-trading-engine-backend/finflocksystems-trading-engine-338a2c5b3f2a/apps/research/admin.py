from django.contrib import admin

from . import models


for model in (
    models.ResearchDatasetVersion,
    models.BacktestProtocolVersion,
    models.CompatibilityRuleSet,
    models.GICSTaxonomyNode,
    models.InstrumentClassification,
    models.ResearchUniverse,
    models.ResearchUniverseMember,
    models.InstrumentEligibilitySnapshot,
    models.ResearchDataCoverageSummary,
    models.ResearchStrategyDefinition,
    models.ResearchFeatureDefinition,
    models.ResearchStrategyFeatureRequirement,
    models.ResearchStrategyImplementation,
    models.ResearchStrategyReadiness,
    models.ResearchDailyBar,
    models.ResearchIntradayBar,
    models.ResearchCorporateAction,
    models.ResearchFundamentalFact,
    models.ResearchAnalystFact,
    models.ResearchEvent,
    models.ResearchExperiment,
    models.ResearchTrial,
    models.ResearchCandidateScore,
    models.ResearchRoleScore,
    models.InstrumentFeatureSnapshot,
    models.CrossSectionalFeatureSnapshot,
    models.MarketRegimeSnapshot,
    models.EventFeatureSnapshot,
    models.GoalRecommendationPolicy,
    models.GoalRecommendationRun,
    models.GoalRecommendationSleeve,
    models.GoalRecommendationAcceptance,
    models.RecommendationCacheSnapshot,
    models.RecommendationBatchRun,
    models.RecommendationBatchGoalResult,
):
    admin.site.register(model)
