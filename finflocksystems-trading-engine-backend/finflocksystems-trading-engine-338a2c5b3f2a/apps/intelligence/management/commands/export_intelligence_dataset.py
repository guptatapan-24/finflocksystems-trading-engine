import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from apps.intelligence import models as m
from apps.intelligence.services.metrics import METRICS_VERSION

TABLES = [
    ("episodes", m.TradeEpisode), ("episode_legs", m.TradeEpisodeLeg), ("episode_metrics", m.EpisodeMetrics),
    ("reviews", m.TradeReview), ("mistake_findings", m.MistakeFinding), ("suggestions", m.Suggestion),
    ("review_feedback", m.ReviewFeedback), ("lessons", m.Lesson), ("lesson_evidence", m.LessonEvidence),
    ("performance_stats", m.PerformanceStat), ("calibration_stats", m.CalibrationStat),
    ("learning_events", m.LearningEvent), ("suggestion_outcomes", m.SuggestionOutcome),
    ("run_config_snapshots", m.RunConfigSnapshot), ("prompt_templates", m.PromptTemplate),
    ("operator_events", m.OperatorEvent), ("report_artifacts", m.ReportArtifact), ("session_records", m.SessionRecord),
    ("context_items", m.ContextItem), ("episode_context", m.EpisodeContext), ("daily_briefs", m.DailyBrief), ("context_sources", m.ContextSource), ("change_points", m.ChangePointState),
]


class Command(BaseCommand):
    """Back-up and analysis export of everything the intelligence layer stored: one JSONL file per table plus a
    manifest with row counts and checksums. This is a dataset for backup and offline analysis; nothing in this project
    trains or fine-tunes a model (R14). Evidence packs and raw model responses are excluded unless requested."""

    help = "Export intelligence data as JSONL + manifest.json (use before any database reset)."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="Directory to write into (created if missing)")
        parser.add_argument("--include-evidence", action="store_true", help="Also export frozen evidence packs and LLM run logs")

    def handle(self, *args, **options):
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        tables = list(TABLES)
        if options["include_evidence"]:
            tables += [("evidence_snapshots", m.EpisodeEvidenceSnapshot), ("llm_run_log", m.LlmRunLog)]
        manifest = {"created_at": timezone.now(), "metrics_version": METRICS_VERSION, "files": {},
                    "note": "Backup and analysis dataset. No credentials, broker account ids or contract ids are stored in these tables."}
        for name, model in tables:
            path = out / f"{name}.jsonl"
            digest = hashlib.sha256()
            count = 0
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in model.objects.all().order_by("pk").values().iterator():
                    line = json.dumps(row, cls=DjangoJSONEncoder, sort_keys=True) + "\n"
                    handle.write(line)
                    digest.update(line.encode())
                    count += 1
            manifest["files"][path.name] = {"rows": count, "sha256": digest.hexdigest()}
        try:
            (out / "manifest.json").write_text(json.dumps(manifest, cls=DjangoJSONEncoder, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            raise CommandError(str(exc)) from exc
        total = sum(f["rows"] for f in manifest["files"].values())
        self.stdout.write(f"Exported {total} rows in {len(manifest['files'])} files to {out}")
