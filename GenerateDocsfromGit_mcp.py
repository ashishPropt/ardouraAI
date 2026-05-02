"""
GenerateDocsfromGit_mcp.py
==========================
Generate comprehensive documentation AND a troubleshooting guide from a GitHub
repo and publish both to Confluence — MCP Edition.

Matches the logic of GenerateDocsfromGit1.py (full structured docs) and
GenerateDocsfromGit.py (troubleshooting guide), merged into one MCP-aware script.

All credentials come from environment variables (or .env file).
See .env for the full list of required variables.

Usage:
  python GenerateDocsfromGit_mcp.py
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# Load .env (safe — does not override existing env vars)
load_dotenv(Path(__file__).parent / ".env")

from mcp_client import GitHubMCP, ConfluenceMCP

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY          = os.environ["ANTHROPIC_API_KEY"]
GITHUB_OWNER               = os.environ.get("GITHUB_OWNER",  "ashishPropt")
GITHUB_REPO                = os.environ.get("GITHUB_REPO",   "Princetondawgs")
GITHUB_BRANCH              = os.environ.get("GITHUB_BRANCH", "main")
CONFLUENCE_SPACE_KEY       = os.environ.get("CONFLUENCE_SPACE_KEY", "")
CONFLUENCE_DOC_TITLE       = os.environ.get("CONFLUENCE_DOC_PAGE_TITLE",
                                             "Princetondawgs Documentation")
CONFLUENCE_TS_TITLE        = os.environ.get("CONFLUENCE_TROUBLESHOOT_PAGE_TITLE",
                                             "Princetondawgs Troubleshooting Guide")

github     = GitHubMCP()
confluence = ConfluenceMCP()
claude     = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Documentation generation ──────────────────────────────────────────────────

def generate_documentation(code_files: dict[str, str]) -> str:
    """
    Send all source files to Claude and return comprehensive structured docs.
    Matches the full prompt used in GenerateDocsfromGit1.py.
    """
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

Write as if the audience includes new developers, SREs, and architects.

Repository Code:
{combined}
"""

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def generate_troubleshooting_guide(code_files: dict[str, str]) -> str:
    """
    Send all source files to Claude and return a focused troubleshooting guide.
    Matches the prompt used in GenerateDocsfromGit.py.
    """
    combined = ""
    for path, content in code_files.items():
        combined += f"\n\n# FILE: {path}\n{content}\n"

    prompt = f"""
You are an expert software reliability engineer.

Analyze the following repository code and produce a **detailed Troubleshooting Guide** that includes:

- Common failure points
- Misconfigurations
- Runtime errors
- Dependency issues
- API misuse
- Logging gaps
- Recommended fixes
- Preventative best practices

Repository Code:
{combined}
"""

    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Confluence publish helper ─────────────────────────────────────────────────

def publish(title: str, content: str) -> None:
    if not CONFLUENCE_SPACE_KEY:
        print(f"  [WARNING] CONFLUENCE_SPACE_KEY not set — skipping publish of '{title}'.")
        print(f"  Preview (first 300 chars):\n  {content[:300]}")
        return
    result = confluence.create_or_update(
        space_key=CONFLUENCE_SPACE_KEY,
        title=title,
        content=content,
    )
    print(f"  Confluence page '{title}' {result['action']} "
          f"(id: {result['page_id']}, status: {result['status_code']})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'═'*60}")
    print(f"  Documentation Generator  (MCP Edition)")
    print(f"  Repo : {GITHUB_OWNER}/{GITHUB_REPO}  branch: {GITHUB_BRANCH}")
    print(f"{'═'*60}")

    # 1. Load codebase via GitHub MCP
    print(f"\n[Step 1] Loading code from {GITHUB_OWNER}/{GITHUB_REPO} via GitHub MCP …")
    code_files = github.load_codebase(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)
    print(f"  Loaded {len(code_files)} source files")

    if not code_files:
        print("  No source files found. Exiting.")
        return

    # 2. Generate comprehensive documentation
    print("\n[Step 2] Generating full documentation with Claude …")
    documentation = generate_documentation(code_files)
    print(f"  Generated {len(documentation):,} chars")

    # 3. Generate troubleshooting guide
    print("\n[Step 3] Generating troubleshooting guide with Claude …")
    guide = generate_troubleshooting_guide(code_files)
    print(f"  Generated {len(guide):,} chars")

    # 4. Publish both pages to Confluence via MCP
    print(f"\n[Step 4] Publishing to Confluence space '{CONFLUENCE_SPACE_KEY}' …")
    print(f"  Publishing '{CONFLUENCE_DOC_TITLE}' …")
    publish(CONFLUENCE_DOC_TITLE, documentation)

    print(f"  Publishing '{CONFLUENCE_TS_TITLE}' …")
    publish(CONFLUENCE_TS_TITLE, guide)

    print(f"\n{'═'*60}")
    print("  Documentation run complete.")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
