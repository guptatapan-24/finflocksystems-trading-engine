from django.db import models


class DatasetStatus(models.TextChoices):
    STAGED = "STAGED"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class TaxonomyLevel(models.TextChoices):
    SECTOR = "SECTOR"
    INDUSTRY_GROUP = "INDUSTRY_GROUP"
    INDUSTRY = "INDUSTRY"
    SUB_INDUSTRY = "SUB_INDUSTRY"


class StrategyRole(models.TextChoices):
    SELECTOR = "SELECTOR"
    EXECUTION = "EXECUTION"
    ALLOCATOR = "ALLOCATOR"
    OVERLAY = "OVERLAY"
    EVENT = "EVENT"
    PAIR_BASKET = "PAIR_BASKET"
    INCOME = "INCOME"
    RESEARCH_ONLY = "RESEARCH_ONLY"


class MappingStatus(models.TextChoices):
    METADATA_ONLY = "METADATA_ONLY"
    INSTRUMENT_MAPPED = "INSTRUMENT_MAPPED"
    PROVIDER_VERIFIED = "PROVIDER_VERIFIED"
    RESEARCH_DATA_READY = "RESEARCH_DATA_READY"
    BROKER_QUALIFIED = "BROKER_QUALIFIED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class ReadinessStatus(models.TextChoices):
    DECLARED = "DECLARED"
    IMPLEMENTED = "IMPLEMENTED"
    VALIDATED = "VALIDATED"
    BLOCKED = "BLOCKED"


class ImplementationStatus(models.TextChoices):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    BACKTESTED = "BACKTESTED"
    SCORED = "SCORED"
    APPROVED_FOR_RECOMMENDATION = "APPROVED_FOR_RECOMMENDATION"
    PAPER_VALIDATED = "PAPER_VALIDATED"
    BUILDER_READY = "BUILDER_READY"
    # Kept for compatibility with deployments promoted under the legacy lifecycle.
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class WorkStatus(models.TextChoices):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
