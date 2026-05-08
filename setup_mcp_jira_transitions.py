# setup_mcp_jira_transitions.py
# Run AFTER create_jira_approval_workflow.py
# Reads workflow_setup_output.json and patches:
#   .env                         (adds JIRA_TRANSITION_* IDs)
#   mcp_jira_server.py           (adds jira_transition_issue tool)
#   mcp_client.py                (adds transition_issue() to JiraMCP)
#   JiraConfluenceAIAgent_mcp.py (auto-transitions new ACR tickets to Pending Approval)
# See local copy at C:\Users\amath\OneDrive\Documents\python\setup_mcp_jira_transitions.py
