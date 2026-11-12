import json
import time

from django.core.management.base import BaseCommand, CommandError

from apps.intelligence import conf
from apps.intelligence.llm import LlmError, get_client

SCHEMA = {"type": "object", "required": ["verdict", "n"], "additionalProperties": False,
          "properties": {"verdict": {"type": "string"}, "n": {"type": "integer"}}}
TOOLS = [{"type": "function", "function": {
    "name": "get_episode_report", "description": "Fetch a trade episode report",
    "parameters": {"type": "object", "properties": {"episode_id": {"type": "integer"}}, "required": ["episode_id"]}}}]


class Command(BaseCommand):
    """Live smoke test of the configured LLM provider (the R15 gate). Uses synthetic prompts only: no trading data,
    nothing is written to the database, and the key is never printed."""

    help = "Call the configured LLM provider with tiny synthetic prompts: JSON, tool call, stream, cost, errors."

    def add_arguments(self, parser):
        parser.add_argument("--model", default="", help="Defaults to INTELLIGENCE_MODEL_REVIEW")

    def handle(self, *args, **options):
        model = options["model"] or conf.model_for("review")
        if not model:
            raise CommandError("No model: pass --model or set INTELLIGENCE_MODEL_REVIEW")
        try:
            client = get_client()
        except LlmError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"provider={client.provider} model={model}")
        failures = []

        def step(name, fn):
            started = time.monotonic()
            try:
                detail = fn()
                self.stdout.write(self.style.SUCCESS(f"OK    {name} ({int((time.monotonic() - started) * 1000)} ms) {detail}"))
            except Exception as exc:  # report every failure, keep going
                failures.append(name)
                self.stdout.write(self.style.ERROR(f"FAIL  {name}: {exc}"))

        def json_call():
            result = client.generate_json("You are a test. Be brief.", "Return verdict 'good' and n=3.", SCHEMA, model)
            if not isinstance(result.parsed, dict) or result.parsed.get("n") != 3:
                raise LlmError(f"unexpected answer {result.parsed!r}")
            return f"parsed={json.dumps(result.parsed)} tokens in/out/think={result.usage.input_tokens}/" \
                   f"{result.usage.output_tokens}/{result.usage.thinking_tokens} cost={result.cost_usd}"

        def tool_call():
            result = client.chat([{"role": "user", "content": "What is the report for episode 42? Use the tool."}],
                                 model, tools=TOOLS)
            if not result.tool_calls or result.tool_calls[0]["arguments"].get("episode_id") != 42:
                raise LlmError(f"no tool call returned: {result.content!r}")
            return f"call={result.tool_calls[0]['name']}({result.tool_calls[0]['arguments']}) cost={result.cost_usd}"

        def stream_call():
            text, done = "", None
            for event in client.stream([{"role": "user", "content": "Count from 1 to 3."}], model):
                if event["type"] == "text":
                    text += event["text"]
                elif event["type"] == "done":
                    done = event
            if not text.strip() or done is None:
                raise LlmError("stream produced no text or no done event")
            return f"text={text.strip()[:40]!r} tokens in/out={done['usage'].input_tokens}/{done['usage'].output_tokens}"

        step("generate_json", json_call)
        step("tool calling", tool_call)
        step("streaming", stream_call)
        if failures:
            raise CommandError(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        self.stdout.write(self.style.SUCCESS("All provider checks passed"))
