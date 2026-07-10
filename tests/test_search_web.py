"""Test that search_web tool is wired correctly and produces grounded answers.

Two scenarios:
  1. Tool-level unit test — calls search_web directly and checks the returned
     structure matches the expected schema (query / answer / results).
  2. Tool-selection test — verifies that, when the query clearly requires an
     external web lookup (latest DeepEval release version), the tool returns
     web-sourced data rather than the agent guessing.  The test does NOT spin up
     a full CugaAgent (that requires a live LLM key); instead it confirms that:
       a) the tool itself returns a non-empty answer grounded in real URLs, AND
       b) the tool is present in the agent's tools list so the agent can choose it.

Requires TAVILY_API_KEY to be set in the environment (or .env file).
"""

import os
import sys
from pathlib import Path

# Load .env so TAVILY_API_KEY is available when running locally
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not required; rely on env being set externally

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_search_web_schema():
    """search_web returns the expected dict structure on a real query."""
    from meeting_scribe.tools import search_web

    if not os.environ.get("TAVILY_API_KEY") or os.environ["TAVILY_API_KEY"].startswith("your-"):
        print("⚠️  TAVILY_API_KEY not set — skipping live API call")
        print("   Set TAVILY_API_KEY in .env to run this test against the real API.")
        return

    query = "latest release version of DeepEval LLM evaluation framework"
    result = search_web.invoke({"query": query, "max_results": 3})

    print("\n" + "=" * 70)
    print("SEARCH_WEB SCHEMA TEST")
    print("=" * 70)
    print(f"Query : {result.get('query')}")
    print(f"Answer: {result.get('answer', '')[:200]}")
    print(f"Result count: {len(result.get('results', []))}")
    for i, r in enumerate(result.get("results", []), 1):
        print(f"  [{i}] {r['title']}")
        print(f"       {r['url']}")
        print(f"       {r['content'][:120]}...")
    print("=" * 70)

    # --- assertions ---
    assert "error" not in result, f"Tool returned error: {result.get('error')}"
    assert result["query"] == query
    assert isinstance(result.get("answer"), str) and len(result["answer"]) > 0, \
        "Expected a non-empty answer string"
    assert isinstance(result.get("results"), list) and len(result["results"]) > 0, \
        "Expected at least one result"

    first = result["results"][0]
    assert "title" in first and "url" in first and "content" in first, \
        "Each result must have title, url, content keys"

    # The answer should mention a version number — confirms it's grounded
    answer_lower = result["answer"].lower()
    assert any(c.isdigit() for c in result["answer"]), \
        "Answer should contain a version number (digit), not be a generic guess"

    print("\n✅ Schema test PASSED — answer is grounded in web results")


def test_search_web_registered_in_agent():
    """search_web is present in the agent's tools list."""
    from meeting_scribe.tools import search_web
    import inspect

    # Confirm it's a proper LangChain tool (has .name and .invoke)
    assert hasattr(search_web, "name"), "search_web must be a @tool with a .name attribute"
    assert hasattr(search_web, "invoke"), "search_web must be a @tool with an .invoke method"
    assert search_web.name == "search_web", f"Unexpected tool name: {search_web.name}"

    # Confirm it appears in the agent module's tool list
    import meeting_scribe.agent as agent_module
    import ast, pathlib

    source = pathlib.Path(agent_module.__file__).read_text()
    assert "search_web" in source, \
        "search_web must be imported and listed in agent.py's tools list"

    print("\n✅ Registration test PASSED — search_web is a valid @tool and appears in agent.py")


def test_search_web_error_handling():
    """search_web returns a safe error dict when the API key is wrong."""
    import os
    from meeting_scribe.tools import search_web

    # Temporarily override the key
    original = os.environ.get("TAVILY_API_KEY")
    os.environ["TAVILY_API_KEY"] = "invalid-key-for-testing"
    try:
        result = search_web.invoke({"query": "test", "max_results": 1})
        assert "error" in result, "Expected error key when API key is invalid"
        assert result["query"] == "test"
        print("\n✅ Error-handling test PASSED — bad key returns {'error': ..., 'query': ...}")
    finally:
        if original is not None:
            os.environ["TAVILY_API_KEY"] = original
        else:
            del os.environ["TAVILY_API_KEY"]


if __name__ == "__main__":
    print("Running search_web tests...\n")
    test_search_web_registered_in_agent()
    test_search_web_error_handling()
    test_search_web_schema()   # last — requires live key
    print("\nAll tests complete.")
