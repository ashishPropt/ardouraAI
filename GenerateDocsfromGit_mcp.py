"""
GenerateDocsfromGit_mcp.py
==========================
Generate comprehensive documentation from a GitHub repo and publish to Confluence.
MCP Edition — all GitHub and Confluence calls go through MCP servers.

Credentials via environment (or .env file):
    ANTHROPIC_API_KEY
    GITHUB_TOKEN
    GITHUB_OWNER               (default: ashishPropt)
    GITHUB_REPO                (default: Princetondawgs)
    GITHUB_BRANCH              (default: main)
    ATLASSIAN_BASE
    ATLASSIAN_EMAIL
    ATLASSIAN_API_TOKEN
    CONFLUENCE_SPACE_KEY
    CONFLUENCE_PAGE_TITLE      (default: "Application Documentation")
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from mcp_client import GitHubMCP, ConfluenceMCP

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
GITHUB_OWNER         = os.environ.get("GITHUB_OWNER",  "ashishPropt")
GITHUB_REPO          = os.environ.get("GITHUB_REPO",   "Princetondawgs")
GITHUB_BRANCH        = os.environ.get("GITHUB_BRANCH", "main")
CONFLUENCE_SPACE_KEY = os.environ.get("CONFLUENCE_SPACE_KEY", "")
CONFLUENCE_TITLE     = os.environ.get("CONFLUENCE_PAGE_TITLE", "Application Documentation")

github     = GitHubMCP()
confluence = ConfluenceMCP()
claude     = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Documentation generation ──────────────────────────────────────────────────

def generate_documentation(code_files: dict[str, str]) -> str:
    """Send all source files to Claude and return structured documentation."""
    combined = ""
    for path, content in code_files.items():
        combined += f"\n\n# FILE: {path}\n{content}\n"

    prompt = f"""
You are an expert software architect and senior documentation engineer.

Analyze the following repository code and produce **comprehensive, structured documentation**.

Your documentation must include:

1. **Executive Summary** — what the application is, what problem it solves, who uses it.

2. **High-Level Architecture** — major components, how they interact, data flow (text-based
   diagrams), external dependencies.

3. **Detailed Module Breakdown** — for each file: purpose, key functions/classes,
   inputs/outputs, how it fits the overall system.

4. **Application Workflow** — step-by-step execution, request/response lifecycle,
   background jobs or event triggers.

5. **Configuration & Environment** — required env vars, secrets, deployment assumptions,
   build/run instructions.

6. **Data Structures** — important models, schemas, objects; how data is validated and
   transformed.

7. **APIs (if any)** — endpoints, parameters, response formats, error handling.

8. **Dependencies** — libraries and frameworks used, why they are needed,
   version-specific considerations.

9. **Security Considerations** — authentication/authorization, sensitive data handling,
   potential risks.

10. **Troubleshooting & Common Issues** — known failure points, misconfigurations,
    how to debug typical problems.

11. **Future Improvements** — code quality issues, architectural suggestions,
    missing documentation, refactoring opportunities.

Write as if the audience includes:
- new developers onboarding
- SREs supporting the application
- architects reviewing the system

Repository Code:
{combined}
"""

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'═'*60}")
    print(f"  Documentation Generator  (MCP Edition)")
    print(f"{'═'*60}")

    # 1. Load codebase via GitHub MCP
    print(f"\n[Step 1] Loading code from {GITHUB_OWNER}/{GITHUB_REPO} via GitHub MCP …")
    code_files = github.load_codebase(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)
    print(f"  Loaded {len(code_files)} source files")

    if not code_files:
        print("  No source files found. Exiting.")
        return

    # 2. Generate docs with Claude
    print("\n[Step 2] Generating documentation with Claude …")
    documentation = generate_documentation(code_files)
    print(f"  Generated {len(documentation):,} chars of documentation")

    # 3. Publish to Confluence via MCP
    print(f"\n[Step 3] Publishing '{CONFLUENCE_TITLE}' to Confluence space '{CONFLUENCE_SPACE_KEY}' …")
    if not CONFLUENCE_SPACE_KEY:
        print("  CONFLUENCE_SPACE_KEY not set — skipping Confluence publish.")
        print("\n  Documentation preview (first 500 chars):")
        print(documentation[:500])
    else:
        result = confluence.create_or_update(
            space_key=CONFLUENCE_SPACE_KEY,
            title=CONFLUENCE_TITLE,
            content=documentation,
        )
        print(f"  Confluence page {result['action']} "
              f"(id: {result['page_id']}, status: {result['status_code']})")

    print(f"\n{'═'*60}")
    print("  Documentation run complete.")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
