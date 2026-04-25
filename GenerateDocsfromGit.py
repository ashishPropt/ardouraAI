import os
import base64
import requests
from anthropic import Anthropic

# -----------------------------
# GitHub Helpers
# -----------------------------

def get_github_file(owner, repo, path, token):
    """Fetch a single file from GitHub."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    res = requests.get(url, headers=headers).json()

    if isinstance(res, dict) and res.get("type") == "file":
        return base64.b64decode(res["content"]).decode("utf-8")
    return None


def get_repo_tree(owner, repo, token, branch="main"):
    """Fetch full repo file tree."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    headers = {"Authorization": f"token {token}"}
    res = requests.get(url, headers=headers).json()
    return [item["path"] for item in res.get("tree", []) if item["type"] == "blob"]


def load_repo_code(owner, repo, token):
    """Load all code files from a GitHub repo."""
    print("Fetching repository tree...")
    paths = get_repo_tree(owner, repo, token)

    code_files = {}
    for path in paths:
        if path.endswith((".py", ".js", ".ts", ".java", ".go", ".rb", ".cs", ".yaml", ".yml")):
            content = get_github_file(owner, repo, path, token)
            if content:
                code_files[path] = content

    return code_files


# -----------------------------
# Claude Troubleshooting Guide
# -----------------------------

def generate_troubleshooting_guide(code_files): 
    print(os.getenv("ANTHROPIC_API_KEY"))
    client = Anthropic(api_key='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')

    combined_code = ""
    for path, content in code_files.items():
        combined_code += f"\n\n# FILE: {path}\n{content}\n"

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
    PAGE_TITLE = "Princetondawgs Troubleshooting Guide"

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