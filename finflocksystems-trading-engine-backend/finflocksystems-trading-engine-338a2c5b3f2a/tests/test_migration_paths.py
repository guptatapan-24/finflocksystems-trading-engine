import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


pytestmark = pytest.mark.django_db(transaction=True)


def test_activation_lifecycle_migration_only_disables_inactive_warmups():
    executor=MigrationExecutor(connection)
    final_targets=executor.loader.graph.leaf_nodes()
    old_target=[("strategies","0010_paper_live_execution_modes")]
    executor.migrate(old_target)
    old_apps=executor.loader.project_state(old_target).apps

    Account=old_apps.get_model("accounts","BrokerAccount")
    Portfolio=old_apps.get_model("portfolios","TradingPortfolio")
    Instrument=old_apps.get_model("instruments","Instrument")
    Definition=old_apps.get_model("strategies","StrategyDefinition")
    Instance=old_apps.get_model("strategies","StrategyInstance")
    account=Account.objects.create(
        account_id="DU-LIFECYCLE-MIGRATION",net_liquidation=1000,available_cash=1000)
    portfolio=Portfolio.objects.create(name="Lifecycle migration",account=account)
    instrument=Instrument.objects.create(symbol="LCMIG",exchange="SMART")
    definition=Definition.objects.create(
        key="LIFECYCLE_MIGRATION",
        name="Lifecycle migration",
        plugin_path="migration.lifecycle",
        supported_asset_types=["STK"],
        supported_directions=["LONG"],
        supported_timeframes=["1m"],
    )
    disabled=Instance.objects.create(
        name="Disabled warming",
        definition=definition,
        portfolio=portfolio,
        instrument=instrument,
        timeframe="1m",
        enabled=False,
        state="WARMING_UP",
        block_reason="Preserve diagnostic context",
    )
    enabled=Instance.objects.create(
        name="Enabled warming",
        definition=definition,
        portfolio=portfolio,
        instrument=instrument,
        timeframe="1m",
        enabled=True,
        state="WARMING_UP",
    )

    executor=MigrationExecutor(connection)
    executor.migrate([("strategies","0011_activation_lifecycle")])
    new_apps=executor.loader.project_state(
        [("strategies","0011_activation_lifecycle")]).apps
    NewInstance=new_apps.get_model("strategies","StrategyInstance")
    migrated_disabled=NewInstance.objects.get(pk=disabled.pk)
    migrated_enabled=NewInstance.objects.get(pk=enabled.pk)
    assert migrated_disabled.state=="DISABLED"
    assert migrated_disabled.block_reason=="Preserve diagnostic context"
    assert migrated_enabled.state=="WARMING_UP"

    executor=MigrationExecutor(connection)
    executor.migrate(final_targets)


def test_dual_strategy_schema_upgrades_to_instance_only_without_losing_references():
    executor = MigrationExecutor(connection)
    final_targets = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("strategies", "0004_strategyaction"),
        ("oms", "0006_orderintent_idempotency_state"),
        ("allocation", "0007_operation_idempotency_state"),
    ]
    executor.migrate(old_targets)
    old_apps = executor.loader.project_state(old_targets).apps

    Account = old_apps.get_model("accounts", "BrokerAccount")
    Portfolio = old_apps.get_model("portfolios", "TradingPortfolio")
    Instrument = old_apps.get_model("instruments", "Instrument")
    Definition = old_apps.get_model("strategies", "StrategyDefinition")
    LegacyStrategy = old_apps.get_model("strategies", "TradingStrategy")
    Instance = old_apps.get_model("strategies", "StrategyInstance")
    Version = old_apps.get_model("strategies", "StrategyVersion")
    StrategyAllocation = old_apps.get_model("strategies", "StrategyAllocation")
    StrategyRun = old_apps.get_model("strategies", "StrategyRun")
    OrderIntent = old_apps.get_model("oms", "OrderIntent")
    Attribution = old_apps.get_model("allocation", "OrderIntentAttribution")
    Flow = old_apps.get_model("allocation", "PortfolioFlow")
    AllocationRun = old_apps.get_model("allocation", "AllocationRun")
    CapitalSnapshot = old_apps.get_model("allocation", "StrategyCapitalSnapshot")
    AllocationDecision = old_apps.get_model("allocation", "AllocationDecision")

    account = Account.objects.create(account_id="DU-MIGRATION", net_liquidation=1000, available_cash=1000)
    portfolio = Portfolio.objects.create(name="Migration portfolio", account=account)
    instrument = Instrument.objects.create(symbol="MIGRATE", exchange="SMART")
    legacy = LegacyStrategy.objects.create(
        name="Migrated strategy",
        strategy_type="fixed_weight",
        allocated_capital=321,
        kill_switch=True,
    )
    definition, _ = Definition.objects.get_or_create(
        key="FIXED_WEIGHT_REBALANCE",
        defaults={
            "name": "Fixed Weight Rebalance",
            "plugin_path": "apps.strategies.plugins.fixed_weight.FixedWeightRebalance",
        },
    )
    instance = Instance.objects.create(
        name="Migrated strategy",
        definition=definition,
        portfolio=portfolio,
        instrument=instrument,
        timeframe="1d",
        parameters={"direction": "LONG"},
        legacy_strategy=legacy,
    )
    version = Version.objects.create(
        strategy_instance=instance,
        version=1,
        configuration_snapshot={},
        parameter_hash="migration-version",
    )
    allocation = StrategyAllocation.objects.create(strategy=legacy, portfolio=portfolio, weight=1)
    strategy_run = StrategyRun.objects.create(
        strategy=legacy,
        strategy_instance=instance,
        strategy_version=version,
        input_hash="migration-run",
        status="COMPLETED",
    )
    intent = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        strategy=legacy,
        strategy_instance=instance,
        strategy_version=version,
        side="BUY",
        quantity=1,
        idempotency_key="migration-intent",
    )
    attribution = Attribution.objects.create(
        order_intent=intent,
        strategy=legacy,
        strategy_instance=instance,
        strategy_version=version,
        target_delta=1,
        allocated_quantity=1,
    )
    flow = Flow.objects.create(
        portfolio=portfolio,
        flow_type="DEPOSIT",
        amount=10,
        effective_at="2026-01-01T00:00:00Z",
        idempotency_key="migration-flow",
    )
    allocation_run = AllocationRun.objects.create(flow=flow, portfolio_nav_before=1000)
    snapshot = CapitalSnapshot.objects.create(
        allocation_run=allocation_run,
        strategy=legacy,
        capital_before=321,
        target_capital=331,
    )
    decision = AllocationDecision.objects.create(
        run=allocation_run,
        strategy=legacy,
        source="CAPITAL_DEFICIT",
        requested_amount=10,
        approved_amount=10,
    )
    ids = {
        "instance": instance.pk,
        "allocation": allocation.pk,
        "run": strategy_run.pk,
        "intent": intent.pk,
        "attribution": attribution.pk,
        "snapshot": snapshot.pk,
        "decision": decision.pk,
    }

    executor = MigrationExecutor(connection)
    executor.migrate(final_targets)
    new_apps = executor.loader.project_state(final_targets).apps

    NewInstance = new_apps.get_model("strategies", "StrategyInstance")
    migrated = NewInstance.objects.get(pk=ids["instance"])
    assert migrated.allocated_capital == 321
    assert migrated.kill_switch is True
    assert new_apps.get_model("strategies", "StrategyAllocation").objects.get(
        pk=ids["allocation"]
    ).strategy_instance_id == migrated.pk
    assert new_apps.get_model("strategies", "StrategyRun").objects.get(
        pk=ids["run"]
    ).strategy_instance_id == migrated.pk
    assert new_apps.get_model("oms", "OrderIntent").objects.get(
        pk=ids["intent"]
    ).strategy_instance_id == migrated.pk
    assert new_apps.get_model("allocation", "OrderIntentAttribution").objects.get(
        pk=ids["attribution"]
    ).strategy_instance_id == migrated.pk
    assert new_apps.get_model("allocation", "StrategyCapitalSnapshot").objects.get(
        pk=ids["snapshot"]
    ).strategy_snapshot["strategy_name"] == "Migrated strategy"
    assert new_apps.get_model("allocation", "AllocationDecision").objects.get(
        pk=ids["decision"]
    ).strategy_instance_id == migrated.pk
    with pytest.raises(LookupError):
        new_apps.get_model("strategies", "TradingStrategy")


def test_order_origin_migration_backfills_existing_intents():
    executor = MigrationExecutor(connection)
    final_targets = executor.loader.graph.leaf_nodes()
    old_target = [("oms", "0008_order_order_status_updated_idx_and_more")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps

    Account = old_apps.get_model("accounts", "BrokerAccount")
    Portfolio = old_apps.get_model("portfolios", "TradingPortfolio")
    Instrument = old_apps.get_model("instruments", "Instrument")
    OrderIntent = old_apps.get_model("oms", "OrderIntent")
    account = Account.objects.create(account_id="DU-ORIGIN-MIGRATION")
    portfolio = Portfolio.objects.create(name="Origin migration", account=account)
    instrument = Instrument.objects.create(symbol="ORIGIN")
    manual = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        source="MANUAL",
        idempotency_key="manual-origin-migration",
    )
    strategy = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        source="STRATEGY",
        idempotency_key="strategy-origin-migration",
    )
    rebalance = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        source="REBALANCE",
        idempotency_key="rebalance-origin-migration",
    )
    broker_import = OrderIntent.objects.create(
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        source="MANUAL",
        idempotency_key="broker-import:paper:DU-ORIGIN-MIGRATION:1",
    )

    new_target = [("oms", "0009_orderintent_origin")]
    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    NewOrderIntent = executor.loader.project_state(new_target).apps.get_model(
        "oms", "OrderIntent"
    )

    assert NewOrderIntent.objects.get(pk=manual.pk).origin == "MANUAL"
    assert NewOrderIntent.objects.get(pk=strategy.pk).origin == "STRATEGY"
    assert NewOrderIntent.objects.get(pk=rebalance.pk).origin == "REBALANCE"
    assert NewOrderIntent.objects.get(pk=broker_import.pk).origin == "BROKER_IMPORT"
    executor = MigrationExecutor(connection)
    executor.migrate(final_targets)


def test_portfolio_builder_migration_splits_stocks_and_equal_strategy_shares_then_drops_legacy_model():
    executor = MigrationExecutor(connection)
    final_targets = executor.loader.graph.leaf_nodes()
    old_target = [("portfolio_construction", "0002_seed_strategy_construction_profiles")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps

    Account = old_apps.get_model("accounts", "BrokerAccount")
    Portfolio = old_apps.get_model("portfolios", "TradingPortfolio")
    Instrument = old_apps.get_model("instruments", "Instrument")
    Definition = old_apps.get_model("strategies", "StrategyDefinition")
    Plan = old_apps.get_model("portfolio_construction", "PortfolioConstructionPlan")
    Goal = old_apps.get_model("portfolio_construction", "PortfolioGoalAllocation")
    LegacySelection = old_apps.get_model("portfolio_construction", "GoalStrategySelection")

    account = Account.objects.create(account_id="DU-BUILDER-MIGRATION", net_liquidation=1000, available_cash=1000)
    portfolio = Portfolio.objects.create(name="Builder migration portfolio", account=account)
    instrument = Instrument.objects.create(symbol="SPLIT", exchange="SMART")
    plan = Plan.objects.create(portfolio=portfolio)
    goal = Goal.objects.create(
        plan=plan,
        name="Growth",
        allocation_weight=1,
        timeframe_bucket="GROW",
        risk_level=5,
    )
    fixed = Definition.objects.create(
        key="MIGRATION_FIXED", name="Migration Fixed", plugin_path="migration.Fixed",
    )
    sma = Definition.objects.create(
        key="MIGRATION_SMA", name="Migration SMA", plugin_path="migration.SMA",
    )
    LegacySelection.objects.create(
        goal_allocation=goal,
        strategy_definition=fixed,
        instrument=instrument,
        execution_timeframe="1d",
        parameter_overrides={"direction": "LONG"},
        enabled=True,
    )
    LegacySelection.objects.create(
        goal_allocation=goal,
        strategy_definition=sma,
        instrument=instrument,
        execution_timeframe="1d",
        parameter_overrides={"fast_window": 20, "slow_window": 50, "direction": "LONG"},
        enabled=True,
    )

    executor = MigrationExecutor(connection)
    executor.migrate([("portfolio_construction", "0003_remove_portfolioconstructionrun_selection_snapshot_and_more")])
    new_apps = executor.loader.project_state(
        [("portfolio_construction", "0003_remove_portfolioconstructionrun_selection_snapshot_and_more")]
    ).apps
    Stock = new_apps.get_model("portfolio_construction", "GoalInstrumentSelection")
    Assignment = new_apps.get_model("portfolio_construction", "GoalStrategyAssignment")
    stock = Stock.objects.get(goal_allocation_id=goal.pk, instrument_id=instrument.pk)
    assignments = list(Assignment.objects.filter(goal_instrument_selection_id=stock.pk).order_by("id"))
    assert len(assignments) == 2
    assert {str(item.strategy_share) for item in assignments} == {"0.50000000"}
    assert all(item.parameter_hash and len(item.parameter_hash) == 64 for item in assignments)
    with pytest.raises(LookupError):
        new_apps.get_model("portfolio_construction", "GoalStrategySelection")

    executor = MigrationExecutor(connection)
    executor.migrate(final_targets)


def test_paper_live_migrations_quarantine_legacy_runtime_work():
    executor = MigrationExecutor(connection)
    final_targets = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("portfolios", "0004_tradingportfolio_gateway_session"),
        ("strategies", "0009_strategy_lifecycle_states"),
        ("allocation", "0011_portfolio_target_coordination"),
        ("oms", "0009_orderintent_origin"),
        ("execution", "0002_brokercommand"),
        (
            "portfolio_optimization",
            "0004_alter_portfoliooptimizationrun_application_status",
        ),
    ]
    executor.migrate(old_targets)
    old_apps = executor.loader.project_state(old_targets).apps

    Account = old_apps.get_model("accounts", "BrokerAccount")
    Session = old_apps.get_model("broker_gateway", "BrokerGatewaySession")
    Portfolio = old_apps.get_model("portfolios", "TradingPortfolio")
    Instrument = old_apps.get_model("instruments", "Instrument")
    Definition = old_apps.get_model("strategies", "StrategyDefinition")
    Strategy = old_apps.get_model("strategies", "StrategyInstance")
    Policy = old_apps.get_model("allocation", "RebalancePolicy")
    Rebalance = old_apps.get_model("allocation", "RebalanceRun")
    Intent = old_apps.get_model("oms", "OrderIntent")
    Order = old_apps.get_model("oms", "Order")
    Command = old_apps.get_model("execution", "BrokerCommand")
    OptimizationPolicy = old_apps.get_model(
        "portfolio_optimization",
        "PortfolioOptimizationPolicy",
    )

    account = Account.objects.create(account_id="DU-PAPER-LIVE-MIGRATION")
    session = Session.objects.create(
        display_name="Legacy live session",
        username_hint="legacy",
        mode="live",
        child_container_name="legacy-live-session",
        encrypted_gateway_token="test",
        encrypted_novnc_password="test",
    )
    portfolio = Portfolio.objects.create(
        name="Legacy execution portfolio",
        account=account,
        gateway_session=session,
    )
    instrument = Instrument.objects.create(symbol="LEGACYMODE")
    definition, _ = Definition.objects.get_or_create(
        key="FIXED_WEIGHT_REBALANCE",
        defaults={
            "name": "Fixed Weight Rebalance",
            "plugin_path": "apps.strategies.plugins.fixed_weight.FixedWeightRebalance",
        },
    )
    strategy = Strategy.objects.create(
        name="Legacy shadow strategy",
        definition=definition,
        portfolio=portfolio,
        instrument=instrument,
        timeframe="1d",
        execution_mode="SHADOW",
        enabled=True,
        state="LONG",
    )
    policy = Policy.objects.create(portfolio=portfolio, mode="SHADOW")
    rebalance = Rebalance.objects.create(
        portfolio=portfolio,
        policy=policy,
        trigger="MANUAL",
        idempotency_key="legacy-shadow-rebalance",
        mode="SHADOW",
        status="PLANNED",
        phase="SHADOW_COMPLETE",
    )
    intent = Intent.objects.create(
        rebalance=rebalance,
        portfolio=portfolio,
        instrument=instrument,
        side="BUY",
        quantity=1,
        idempotency_key="legacy-shadow-intent",
        mode="SHADOW",
        operation_status="PENDING",
        eligible=True,
    )
    order = Order.objects.create(
        intent=intent,
        internal_id="legacy-shadow-order",
        status="QUEUED",
        quantity=1,
    )
    command = Command.objects.create(
        order=order,
        internal_order_id=order.internal_id,
        gateway_session=session,
        command_type="PLACE",
        idempotency_key="legacy-shadow-command",
        request_payload={"internal_id": order.internal_id},
        request_hash="legacy-shadow-command",
        status="PENDING",
    )
    optimization_policy = OptimizationPolicy.objects.create(
        portfolio=portfolio,
        execution_mode="SHADOW",
    )

    executor = MigrationExecutor(connection)
    executor.migrate(final_targets)
    new_apps = executor.loader.project_state(final_targets).apps

    migrated_strategy = new_apps.get_model(
        "strategies", "StrategyInstance"
    ).objects.get(pk=strategy.pk)
    migrated_rebalance = new_apps.get_model(
        "allocation", "RebalanceRun"
    ).objects.get(pk=rebalance.pk)
    migrated_intent = new_apps.get_model(
        "oms", "OrderIntent"
    ).objects.get(pk=intent.pk)
    migrated_command = new_apps.get_model(
        "execution", "BrokerCommand"
    ).objects.get(pk=command.pk)
    migrated_optimization_policy = new_apps.get_model(
        "portfolio_optimization",
        "PortfolioOptimizationPolicy",
    ).objects.get(pk=optimization_policy.pk)

    assert migrated_strategy.execution_mode == "PAPER"
    assert migrated_strategy.enabled is False
    assert migrated_strategy.state == "PAUSED"
    assert migrated_rebalance.mode == "LIVE"
    assert migrated_rebalance.run_type == "PREVIEW"
    assert migrated_rebalance.phase == "PREVIEW_COMPLETE"
    assert migrated_intent.mode == "LIVE"
    assert migrated_intent.operation_status == "FAILED"
    assert migrated_intent.eligible is False
    assert migrated_intent.retryable is False
    assert migrated_command.mode == "LIVE"
    assert migrated_command.status == "FAILED"
    assert migrated_optimization_policy.execution_mode == "LIVE"


def test_research_paper_validation_migration_preserves_legacy_evidence():
    executor = MigrationExecutor(connection)
    final_targets = executor.loader.graph.leaf_nodes()
    old_targets = [("research", "0004_nullable_recommendation_goal_audit")]
    executor.migrate(old_targets)
    old_apps = executor.loader.project_state(old_targets).apps

    Dataset = old_apps.get_model("research", "ResearchDatasetVersion")
    ResearchStrategy = old_apps.get_model("research", "ResearchStrategyDefinition")
    Implementation = old_apps.get_model(
        "research", "ResearchStrategyImplementation"
    )
    Readiness = old_apps.get_model("research", "ResearchStrategyReadiness")

    dataset = Dataset.objects.create(
        bundle_name="legacy-validation",
        version="1",
        snapshot_date="2026-01-01",
        source_path="migration-test",
        manifest_hash="legacy-validation",
    )
    strategy = ResearchStrategy.objects.create(
        research_id="LEGACY_VALIDATION",
        dataset_version=dataset,
        name="Legacy validation strategy",
        family="migration",
        scope="single_asset",
        role="EXECUTION",
        production_status="VALIDATED",
        configuration_hash="legacy-validation",
    )
    implementation = Implementation.objects.create(
        research_strategy=strategy,
        implementation_path="migration.test.strategy",
        implementation_version="1",
        implementation_hash="legacy-validation",
        role="EXECUTION",
        supported_frequency="1d",
        supported_direction="LONG",
        status="SHADOW_VALIDATED",
        approval_record={"shadow_validated": True, "actor": "migration-test"},
    )
    readiness = Readiness.objects.create(
        research_strategy=strategy,
        as_of_date="2026-01-01",
        blocking_reasons=["SHADOW_VALIDATION_REQUIRED", "OTHER_BLOCKER"],
    )

    executor = MigrationExecutor(connection)
    executor.migrate(final_targets)
    new_apps = executor.loader.project_state(final_targets).apps

    migrated_implementation = new_apps.get_model(
        "research", "ResearchStrategyImplementation"
    ).objects.get(pk=implementation.pk)
    migrated_readiness = new_apps.get_model(
        "research", "ResearchStrategyReadiness"
    ).objects.get(pk=readiness.pk)

    assert migrated_implementation.status == "PAPER_VALIDATED"
    assert migrated_implementation.approval_record == {
        "paper_validated": True,
        "actor": "migration-test",
    }
    assert migrated_readiness.blocking_reasons == [
        "PAPER_VALIDATION_REQUIRED",
        "OTHER_BLOCKER",
    ]
