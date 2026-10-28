import argparse
import json
from pathlib import Path
import sqlite3
import sys
from .agent import run_agent
from .domain import Request
from .model import DeepSeekModel, MockModel, load_env
from .storage import Store


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="素材审核与排期草稿 Agent（不发布）")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", choices=["mock", "deepseek"], default="mock")
    parser.add_argument("--data", type=Path, default=Path("data/fixtures.json"))
    parser.add_argument("--db", type=Path, default=Path("runtime/drafts.sqlite3"))
    parser.add_argument("--output", type=Path, help="JSON result including actual tool trace")
    parser.add_argument("--max-steps", type=int, default=32)
    args = parser.parse_args(argv)
    store = None
    try:
        load_env()
        request = Request.parse(json.loads(args.input.read_text(encoding="utf-8-sig")))
        fixtures = json.loads(args.data.read_text(encoding="utf-8-sig"))
        if not 1 <= args.max_steps <= 200:
            raise ValueError("max_steps must be 1..200")
        model = MockModel() if args.model == "mock" else DeepSeekModel()
        args.db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(args.db)
        result = run_agent(request, fixtures, store, model, args.max_steps)
        code = 0 if result["status"] in {"completed", "needs_attention"} else 2
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return code
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        # No traceback or raw exception text containing credentials/provider input.
        print(json.dumps({"status": "error", "error": "configuration_input_or_storage_error",
                          "hint": "Check input schema, data path, writable DB and model environment.",
                          "published": False}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
