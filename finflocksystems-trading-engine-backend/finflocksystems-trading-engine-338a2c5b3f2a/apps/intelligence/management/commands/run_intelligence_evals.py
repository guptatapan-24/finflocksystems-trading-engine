import json

from django.core.management.base import BaseCommand, CommandError

from apps.intelligence import conf
from apps.intelligence.llm import get_client
from apps.intelligence.models import PromptTemplate
from apps.intelligence.services import evals, prompts


class Command(BaseCommand):
    """Score a prompt template on the golden episodes and, with --activate, replace the active one only if it wins."""

    help = "Evaluate a trade_review prompt version on the golden set (costs real tokens with the qch provider)."

    def add_arguments(self, parser):
        parser.add_argument("--prompt-version", type=int, required=True)
        parser.add_argument("--model", default="", help="Defaults to INTELLIGENCE_MODEL_REVIEW")
        parser.add_argument("--activate", action="store_true", help="Activate the version if it beats the active one")
        parser.add_argument("--json", dest="json_path", default="", help="Write the full result to this file")

    def handle(self, *args, **options):
        prompts.get_active_template()  # makes sure the code-defined versions exist
        template = PromptTemplate.objects.filter(name=prompts.TEMPLATE_NAME, version=options["prompt_version"]).first()
        if template is None:
            raise CommandError(f"No {prompts.TEMPLATE_NAME} template with version {options['prompt_version']}")
        model = options["model"] or conf.model_for("review")
        if not model:
            raise CommandError("No model: pass --model or set INTELLIGENCE_MODEL_REVIEW")
        client = get_client()
        if options["activate"]:
            outcome = evals.promote(template, model, client)
            result, activated = outcome["candidate"], outcome["activated"]
            self.stdout.write(f"Active template compared: {outcome['active']['summary'] if outcome['active'] else 'none'}")
        else:
            result, activated, outcome = evals.evaluate_prompt(template, model, client, run_label="manual"), False, {"candidate": None}
        self.stdout.write(json.dumps(result["summary"], indent=2, sort_keys=True))
        if options["activate"]:
            self.stdout.write("ACTIVATED" if activated else "Not activated: the candidate did not beat the active template")
        if options["json_path"]:
            with open(options["json_path"], "w", encoding="utf-8") as handle:
                json.dump(outcome if options["activate"] else result, handle, indent=2, sort_keys=True, default=str)
