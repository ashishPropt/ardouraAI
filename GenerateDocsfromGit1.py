import os
import base64
import requests
from anthropic import Anthropic

# -----------------------------
# GitHub Helpers
# -----------------------------

def get_github_file(owner, repo, path, token):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    res = requests.get(url, headers=headers).json()

    if isinstance(res, dict) and res.get("type") == "file":
        return base64.b64decode(res["content"]).decode("utf-8")
    return None


def get_repo_tree(owner, repo, token, branch="main"):
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    print(url)
    headers = {"Authorization": f"token {token}"}
    res = requests.get(url, headers=headers).json()
    print("Tree API response:", res)

    return [item["path"] for item in res.get("tree", []) if item["type"] == "blob"]


def load_repo_code(owner, repo, token):
    print("Fetching repository tree...")
    paths = get_repo_tree(owner, repo, token)
    print(paths)
    code_files = {}
    for path in paths:
        if path.endswith((".py", ".js", ".ts", ".java", ".go", ".rb", ".cs", ".yaml", ".yml","sql")):
            content = get_github_file(owner, repo, path, token)
            print(content)
            if content:
                code_files[path] = content

    return code_files


# -----------------------------
# Claude Troubleshooting Guide
# -----------------------------

def generate_troubleshooting_guide(code_files):
    client = Anthropic(api_key='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')

    combined_code = ""
    for path, content in code_files.items():
        combined_code += f"\n\n# FILE: {path}\n{content}\n"
    print(combined_code)
    prompt = f"""
You are an expert software architect and senior documentation engineer.

Analyze the following repository code and produce **comprehensive, structured documentation** that explains exactly what this application does.

Your documentation must include:

1. **Executive Summary**
   - What the application is
   - What problem it solves
   - Who uses it

2. **High-Level Architecture**
   - Major components
   - How they interact
   - Data flow diagrams (text-based)
   - External dependencies (APIs, databases, services)

3. **Detailed Module Breakdown**
   For each file or module:
   - Purpose
   - Key functions/classes
   - Inputs and outputs
   - How it fits into the overall system

4. **Application Workflow**
   - Step-by-step explanation of how the application runs
   - Request/response lifecycle (if applicable)
   - Background jobs, schedulers, or event triggers

5. **Configuration & Environment**
   - Required environment variables
   - Secrets
   - Deployment assumptions
   - Build/run instructions

6. **Data Structures**
   - Important models, schemas, or objects
   - How data is validated and transformed

7. **APIs (if any)**
   - Endpoints
   - Parameters
   - Response formats
   - Error handling

8. **Dependencies**
   - Libraries and frameworks used
   - Why they are needed
   - Any version-specific considerations

9. **Security Considerations**
   - Authentication/authorization
   - Sensitive data handling
   - Potential risks

10. **Troubleshooting & Common Issues**
    - Known failure points
    - Misconfigurations
    - How to debug typical problems

11. **Future Improvements**
    - Code quality issues
    - Architectural suggestions
    - Missing documentation
    - Opportunities for refactoring

Make the documentation extremely clear, structured, and written as if it will be used by:
- new developers onboarding to the project
- SREs supporting the application
- architects reviewing the system

Repository Code:
{combined_code}
"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


# -----------------------------
# Confluence Integration
# -----------------------------

def get_page_id(base_url, space_key, title, auth):
    url = f"{base_url}/rest/api/content"
    params = {"title": title, "spaceKey": space_key}
    res = requests.get(url, params=params, auth=auth).json()

    if res.get("results"):
        return res["results"][0]["id"]
    return None


def create_or_update_confluence_page(base_url, space_key, title, content, auth):
    page_id = get_page_id(base_url, space_key, title, auth)

    if page_id:
        # Update existing page
        url = f"{base_url}/rest/api/content/{page_id}"
        current = requests.get(url, auth=auth).json()
        version = current["version"]["number"] + 1

        data = {
            "id": page_id,
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "version": {"number": version},
            "body": {
                "storage": {
                    "value": content.replace("\n", "<br/>"),
                    "representation": "storage"
                }
            }
        }

        res = requests.put(url, json=data, auth=auth)
        return res.status_code, "updated"

    else:
        # Create new page
        url = f"{base_url}/rest/api/content"
        data = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": content.replace("\n", "<br/>"),
                    "representation": "storage"
                }
            }
        }

        res = requests.post(url, json=data, auth=auth)
        return res.status_code, "created"


# -----------------------------
# Main
# -----------------------------

if __name__ == "__main__":
    GITHUB_TOKEN = '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'
    OWNER = "ashishPropt"
    REPO = "Princetondawgs"

    print("Loading code from GitHub...")
    code_files = load_repo_code(OWNER, REPO, GITHUB_TOKEN)
    print("Generating Troubleshooting Guide with Claude...")
    guide = generate_troubleshooting_guide(code_files)
    #guide="Test guide"
    # Confluence configuration
    CONFLUENCE_BASE_URL = 'https://proptxchange.atlassian.net/wiki'
    CONFLUENCE_EMAIL = 'ashish@proptxchange.com'
    CONFLUENCE_API_TOKEN = 'ATATT3xFfGF0uJY9cuMSM6d0bU_8KMUm2BSnw2PPvOJ5WXGRBfpKCrgw480FXxkvDBHDvReHjIyCr66XeqdiUoIcqrsGbFvID7WvCMsZTxcn4M5xh5XlX-EdmCTnEPsDlgX1i-ccStCXxFB-_Do5DtPuNcRXvscs6RRpDki6O9mAUesZrMnHB4Q=8F34BC98'
    SPACE_KEY = "~7120207f8be028cbb84bc294e4ddd29895033d"  # example
    PAGE_TITLE = "Princetondawgs Documentation"

    auth = (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)

    print("Publishing to Confluence...")
    status, action = create_or_update_confluence_page(
        CONFLUENCE_BASE_URL,
        SPACE_KEY,
        PAGE_TITLE,
        guide,
        auth
    )

    print(f"Confluence page {action} with status {status}.")