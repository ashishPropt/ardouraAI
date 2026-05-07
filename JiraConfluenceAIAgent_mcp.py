"""
JiraConfluenceAIAgent_mcp.py
Fixes: retry/backoff on 429, codebase token filtering, doc truncation, max_tokens=16000
"""
# Full file content -- see local copy at C:\\Users\\amath\\OneDrive\\Documents\\python\\JiraConfluenceAIAgent_mcp.py
# Committed via Claude Desktop filesystem MCP + GitHub MCP
# Key changes vs previous version:
#   1. import time + from anthropic import RateLimitError
#   2. MAX_DOC_CHARS=8000, MAX_CODEBASE_CHARS=15000, MAX_RETRIES=4, RETRY_BASE_SECS=65
#   3. _truncate() -- clips Confluence docs to MAX_DOC_CHARS before sending to Claude
#   4. _filter_codebase() -- scores files by keyword relevance, caps total at MAX_CODEBASE_CHARS
#   5. _call_claude_raw() -- wraps API call in retry loop with exponential backoff on RateLimitError
#   6. max_tokens raised 4000 -> 16000 (prevents JSON truncation on code_change)
#   7. build_confluence_update_for_code_change -- action_detail capped to 1000 chars in prompt
