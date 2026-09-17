"""Interactive terminal interface for SDLC Manager."""

import argparse
import asyncio
import sys
import warnings

# The ADK stamps experimental features with UserWarnings on import and on
# every tool declaration — framework chatter, not something a terminal user
# can act on.
warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\].*")

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from .config import ServerEnv, initialize_environment  # noqa: E402


def _print_banner() -> None:
    print("\n" + "=" * 65)
    print("  🚀 SDLC Manager  -  AI Quality, Git & Infrastructure Orchestrator")
    print("=" * 65)
    print("  Orchestrated Sub-Agents:")
    print("    * 🔀 Git & GitHub Agent       * 🔍 AST & Secret Validation Agent")
    print("    * 📝 AI Code Review Agent     * 🏗️ Terraform Infrastructure Agent")
    print("\n  Type your request in natural language. Type 'exit' to quit.")
    print("=" * 65 + "\n")


def _print_hook_report(result: object) -> None:
    """Show the repository's own check verdicts (lefthook) the moment a git
    tool returns, so a hook run is never invisible in the terminal."""
    if not isinstance(result, dict):
        return
    text = "\n".join(str(result.get(k, "")) for k in ("stdout", "stderr", "message"))
    if "lefthook" not in text:
        return
    import re as _re

    # One verdict line per check — durations and summary lines are noise
    # next to the pass/fail signal.
    verdicts = [
        _re.sub(r"\s*\([\d.]+ seconds?\)\s*$", "", ln.strip())
        for ln in text.splitlines()
        if ln.strip().startswith(("✔️", "🥊"))
    ]
    if verdicts:
        print("  Repository checks:")
        for v in verdicts:
            print(f"    {v}")


async def _run_agent_turn(
    runner: Runner,
    user_id: str,
    session_id: str,
    user_message: str,
    verbose: bool = False,
) -> None:
    content = types.Content(
        role="user",
        parts=[types.Part(text=user_message)],
    )

    print("\n🤖 SDLC Manager:")
    print("-" * 45)

    final_response = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_response += part.text
                if part.function_call and verbose:
                    tool_name = part.function_call.name
                    tool_args = part.function_call.args or {}
                    args_str = ", ".join(f"{k}={v!r}" for k, v in tool_args.items())
                    print(f"  🔧 Calling Tool: {tool_name}({args_str})")
                if part.function_response:
                    resp = part.function_response
                    result = resp.response if hasattr(resp, "response") else {}
                    _print_hook_report(result)
                    if isinstance(result, dict) and result.get("status") in (
                        "pending_approval",
                        "blocked",
                    ):
                        print(f"\n  {result.get('message', '')}")

    if final_response:
        print(final_response)
    print()


async def _interactive_loop(verbose: bool = False) -> None:
    import logging
    import os

    # The App's LoggingPlugin prints raw event traces to stdout; in a
    # terminal the developer wants the answer, not the trace — keep the
    # trace only under -v. Must be set before the agent module builds
    # the App. The ADK context-cache manager also warns on every small
    # request; that is operator noise, not developer signal.
    if not verbose:
        os.environ["SDLC_CONSOLE_TRACE"] = "0"
        logging.getLogger(
            "google_adk.google.adk.models.gemini_context_cache_manager"
        ).setLevel(logging.ERROR)
        logging.getLogger(
            "google.adk.models.gemini_context_cache_manager"
        ).setLevel(logging.ERROR)

    initialize_environment(ServerEnv, print_config=False)
    from .agent import app

    session_service = InMemorySessionService()
    # Run the App, not the bare agent: a bare-agent Runner drops the
    # App's plugins (workspace global instruction, logging).
    runner = Runner(
        app=app,
        session_service=session_service,
    )

    # Stable per-developer identity so memory persists across sessions.
    import getpass

    user_id = f"cli-{getpass.getuser()}"
    session = await session_service.create_session(
        app_name=app.name,
        user_id=user_id,
    )

    _print_banner()

    while True:
        try:
            # input() off the event loop — a blocked loop would stall any
            # background task between turns.
            user_input = (await asyncio.to_thread(input, "👤 You: ")).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n👋 Goodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            print("\n👋 Goodbye!")
            break

        if user_input.lower() == "clear":
            print("\033[2J\033[H", end="")
            _print_banner()
            continue

        await _run_agent_turn(runner, user_id, session.id, user_input, verbose=verbose)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sdlc-manager", description="SDLC Manager terminal assistant"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show the tool calls the agents make",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_interactive_loop(verbose=args.verbose))
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
        sys.exit(0)
    except Exception as e:
        from .config import ConfigurationError

        # A misconfigured environment gets a clean message, not a traceback.
        if isinstance(e, ConfigurationError):
            print(f"\n{e}")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
