"""Minimal test: verify streaming keeps connection alive past the ~90s idle timeout.

Usage:
    python scripts/test_streaming.py

Tests:
  1. Streaming request with gpt-5.4 + xhigh reasoning (should survive >90s)
  2. Verifies llm_overrides correctly override .env defaults
"""
import os
import sys
import time

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openai import OpenAI

PROBLEM_STATEMENT = (
    "Let $p$ be a prime number greater than $3$. For each $k \\in \\{1,\\dots,p-1\\}$, "
    "let $I(k) \\in \\{1,2,\\dots,p-1\\}$ be such that $k \\cdot I(k) \\equiv 1 \\pmod{p}$. "
    "Prove that the number of integers $k \\in \\{1,\\dots,p-2\\}$ such that $I(k+1) < I(k)$ "
    "is greater than $p/4-1$."
)


def test_streaming_survives_90s():
    """Send a streaming request with xhigh reasoning to gpt-5.4.

    If the old ~90s connection drop bug is fixed, this should complete
    even when reasoning takes several minutes.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Try loading from .env
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("OPENAI_API_KEY="):
                    api_key = line.strip().split("=", 1)[1]

    if not api_key:
        print("ERROR: No OPENAI_API_KEY found")
        sys.exit(1)

    client = OpenAI(api_key=api_key, timeout=600.0, max_retries=0)

    prompt = (
        "You are a mathematics proof assistant. Analyze this theorem and outline "
        "a proof strategy with key lemmas needed. Be thorough.\n\n"
        f"Theorem: {PROBLEM_STATEMENT}"
    )

    kwargs = {
        "model": "gpt-5.4",
        "reasoning": {"effort": "xhigh"},
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    }

    print(f"Sending STREAMING request to gpt-5.4 with reasoning=xhigh...")
    print(f"(The old bug would kill this at ~90s)")
    print(f"Start time: {time.strftime('%H:%M:%S')}")
    print()

    t0 = time.time()
    last_report = t0

    stream_fn = getattr(client.responses, "stream", None)
    if stream_fn is None:
        print("ERROR: client.responses.stream not available — openai package too old?")
        sys.exit(1)

    try:
        with stream_fn(**kwargs) as stream:
            # Print periodic status while waiting
            for event in stream:
                now = time.time()
                elapsed = now - t0
                if now - last_report >= 15:
                    print(f"  ... still alive at {elapsed:.0f}s (event: {type(event).__name__})")
                    last_report = now

            response = stream.get_final_response()
    except Exception as e:
        elapsed = time.time() - t0
        print(f"\nFAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
        if elapsed > 85 and elapsed < 95:
            print(">>> This looks like the ~90s idle timeout bug! Streaming may not be working.")
        sys.exit(1)

    elapsed = time.time() - t0

    # Extract output text
    output_text = ""
    for item in response.output:
        if hasattr(item, "content") and item.content:
            for block in item.content:
                if hasattr(block, "text"):
                    output_text += block.text

    print(f"\nSUCCESS after {elapsed:.1f}s")
    print(f"Model: {response.model}")
    if hasattr(response, "usage") and response.usage:
        u = response.usage
        print(f"Usage: input={u.input_tokens}, output={u.output_tokens}")
        if hasattr(u, "output_tokens_details") and u.output_tokens_details:
            print(f"  reasoning_tokens={u.output_tokens_details.reasoning_tokens}")
    print(f"Output preview: {output_text[:300]}...")

    if elapsed > 90:
        print(f"\n>>> Request took {elapsed:.0f}s — survived past the 90s timeout! Streaming fix works.")
    else:
        print(f"\n>>> Request completed in {elapsed:.0f}s (under 90s, so this doesn't fully prove the fix)")
        print("    But streaming path executed successfully.")


def test_llm_overrides():
    """Verify that per-problem llm_overrides correctly override .env defaults."""
    from nl_engine.domain.config import LlmConfig, AgentLlmConfig
    from nl_engine.services.agents import AgentService

    # The .env has OPENAI_REASONING_EFFORT=low
    # A problem config with agent2.thinking_level=xhigh should override it

    llm_overrides = {
        "agent2": {
            "model": "gpt-5.4",
            "thinking_level": "xhigh",
            "verbosity": "medium",
            "timeout_seconds": 60000,
        }
    }

    agent = AgentService(llm_overrides=llm_overrides)
    model, effort, verbosity, timeout = agent._resolve_agent_model_and_effort(
        agent_key="agent2",
        default_model="gpt-5-mini",  # default from settings
    )

    print(f"\n--- LLM Override Test ---")
    print(f"  .env OPENAI_REASONING_EFFORT = {agent.settings.openai_reasoning_effort}")
    print(f"  Override thinking_level = xhigh")
    print(f"  Resolved: model={model}, effort={effort}, verbosity={verbosity}, timeout={timeout}")

    if effort != "xhigh":
        print(f"  FAIL: Expected effort='xhigh', got '{effort}'")
        print(f"  >>> The .env value is overriding per-problem config!")
        return False
    else:
        print(f"  PASS: Per-problem override correctly applied")
        return True


if __name__ == "__main__":
    # Test 2 first (free, no API call)
    override_ok = test_llm_overrides()

    if not override_ok:
        print("\nFix the override issue before testing streaming.")
        sys.exit(1)

    print()

    # Test 1 (costs money — one gpt-5.4 xhigh call)
    if "--skip-api" in sys.argv:
        print("Skipping API test (--skip-api)")
    else:
        test_streaming_survives_90s()
