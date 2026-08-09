"""Analyze the bundled meeting transcript and print structured output."""

from pathlib import Path

from csaf.core import create_runtime


def main() -> None:
    transcript = Path("examples/data/acme-meeting.md").read_text(encoding="utf-8")
    runtime = create_runtime()
    try:
        result = runtime.runner.run(
            "meeting-copilot",
            {
                "customer_id": "acme",
                "meeting_id": "acme-kickoff",
                "transcript": transcript,
                "attendees": ["Alex", "Priya"],
            },
        )
        print(result.output.model_dump_json(indent=2))
    finally:
        runtime.memory.close()


if __name__ == "__main__":
    main()
