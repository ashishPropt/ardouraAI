"""
mcp_client.py -- MCP client wrappers.
Classes: GitHubMCP, JiraMCP, MySQLMCP, ConfluenceMCP, ObservabilityMCP
ObservabilityMCP wraps mcp_observability_server.py and gracefully handles
unset API keys for Dynatrace, Datadog, Grafana, and Prometheus.
"""
import json, subprocess, sys, os
from typing import Any

class _MCPClient:
    def __init__(self, server_script):
        self._script = server_script
    @staticmethod
    def _frame(obj): return (json.dumps(obj)+"\n").encode("utf-8")
    def call(self, tool_name, arguments):
        init_req = {"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"mcp_client.py","version":"1.0"}}}
        init_notify = {"jsonrpc":"2.0","method":"notifications/initialized"}
        tool_req = {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":tool_name,"arguments":arguments}}
        stdin_bytes = self._frame(init_req)+self._frame(init_notify)+self._frame(tool_req)
        proc = subprocess.run([sys.executable,self._script],input=stdin_bytes,capture_output=True,env={**os.environ},timeout=120)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line: continue
            try: frame = json.loads(line)
            except: continue
            if frame.get("id")==1:
                if "error" in frame: raise RuntimeError(f"MCP tool '{tool_name}' error: {frame['error']}")
                content_list = frame.get("result",{}).get("content",[])
                if content_list:
                    text = content_list[0].get("text","")
                    try: return json.loads(text)
                    except: return text
        stderr = proc.stderr.decode("utf-8",errors="replace")
        raise RuntimeError(f"MCP call to '{tool_name}' failed.\nstderr: {stderr[:400]}")

_HERE = os.path.dirname(os.path.abspath(__file__))

class GitHubMCP:
    def __init__(self): self._client = _MCPClient(os.path.join(_HERE,"mcp_github_server.py"))
    def get_repo_tree(self,owner,repo,branch="main"): return self._client.call("github_get_repo_tree",{"owner":owner,"repo":repo,"branch":branch})
    def get_file(self,owner,repo,path): return self._client.call("github_get_file",{"owner":owner,"repo":repo,"path":path})
    def commit_file(self,owner,repo,path,content,commit_message,branch="main"): return self._client.call("github_commit_file",{"owner":owner,"repo":repo,"path":path,"content":content,"commit_message":commit_message,"branch":branch})
    def load_codebase(self,owner,repo,branch="main"):
        result = self._client.call("github_load_codebase",{"owner":owner,"repo":repo,"branch":branch})
        if isinstance(result,str): raise RuntimeError(f"github_load_codebase returned string: {result[:300]}")
        for e in result.get("errors",[]): print(f"  [AppGitHub-MCP] {e}")
        return result.get("files",{})

class JiraMCP:
    def __init__(self): self._client = _MCPClient(os.path.join(_HERE,"mcp_jira_server.py"))
    def search_issues(self,jql,max_results=50): return self._client.call("jira_search_issues",{"jql":jql,"max_results":max_results})
    def create_ticket(self,project_key,summary,description,issue_type="Task",priority="Medium",labels=None): return self._client.call("jira_create_ticket",{"project_key":project_key,"summary":summary,"description":description,"issue_type":issue_type,"priority":priority,"labels":labels or []})
    def get_issue(self,issue_key): return self._client.call("jira_get_issue",{"issue_key":issue_key})
    def fetch_open_issues(self,project_key=None,max_results=50):
        jql = "status != Done AND status != Closed ORDER BY created DESC"
        if project_key: jql = f"project = {project_key} AND {jql}"
        return self.search_issues(jql,max_results)

class MySQLMCP:
    def __init__(self): self._client = _MCPClient(os.path.join(_HERE,"mcp_mysql_server.py"))
    def execute_query(self,sql): return self._client.call("mysql_execute_query",{"sql":sql})
    def execute_update(self,sql,dry_run=False): return self._client.call("mysql_execute_update",{"sql":sql,"dry_run":dry_run})
    def get_schema(self): return self._client.call("mysql_get_schema",{})
    def begin_snapshot(self,tables,where_clauses=None): return self._client.call("mysql_begin_snapshot",{"tables":tables,"where_clauses":where_clauses or {}})

class ConfluenceMCP:
    def __init__(self): self._client = _MCPClient(os.path.join(_HERE,"mcp_confluence_server.py"))
    def get_page(self,page_id):
        result = self._client.call("confluence_get_page",{"page_id":page_id})
        return result.get("text","")
    def get_page_by_title(self,space_key,title):
        page_id = self.find_page_id(space_key,title)
        return self.get_page(page_id) if page_id else None
    def find_page_id(self,space_key,title):
        result = self._client.call("confluence_find_page_id",{"space_key":space_key,"title":title})
        return result.get("page_id")
    def create_or_update(self,space_key,title,content): return self._client.call("confluence_create_or_update",{"space_key":space_key,"title":title,"content":content})

class ObservabilityMCP:
    """
    Wrapper around mcp_observability_server.py.
    All methods return safe dicts -- never raises if a tool is not configured.
    """
    def __init__(self): self._client = _MCPClient(os.path.join(_HERE,"mcp_observability_server.py"))
    def get_dynatrace_problems(self,problem_selector="status(\"OPEN\")",page_size=20): return self._client.call("obs_get_dynatrace_problems",{"problem_selector":problem_selector,"page_size":page_size})
    def get_dynatrace_events(self,event_type="",page_size=20): return self._client.call("obs_get_dynatrace_events",{"event_type":event_type,"page_size":page_size})
    def get_datadog_monitors(self,states="Alert,Warn,No Data"): return self._client.call("obs_get_datadog_monitors",{"states":states})
    def get_datadog_events(self,start=None,end=None,priority="normal"):
        args={"priority":priority}
        if start: args["start"]=start
        if end: args["end"]=end
        return self._client.call("obs_get_datadog_events",args)
    def get_grafana_alerts(self,state="firing"): return self._client.call("obs_get_grafana_alerts",{"state":state})
    def prometheus_query(self,query): return self._client.call("obs_prometheus_query",{"query":query})
    def prometheus_alerts(self,all_alerts=False): return self._client.call("obs_prometheus_alerts",{"all":all_alerts})
    def status(self): return self._client.call("obs_status",{})
    def get_all_alerts(self):
        unified = []
        try:
            dt = self.get_dynatrace_problems()
            if not dt.get("error"):
                for p in dt.get("problems",[]):
                    unified.append({"source":"dynatrace","id":p.get("id"),"title":p.get("title","Dynatrace Problem"),"severity":p.get("severity","UNKNOWN"),"status":p.get("status","OPEN"),"description":f"Impact: {p.get('impact')} | Root cause: {p.get('root_cause')} | Affected: {', '.join(p.get('affected',[]))}","raw":p})
        except Exception as e: print(f"  [ObsMCP] Dynatrace: {e}")
        try:
            dd = self.get_datadog_monitors()
            if not dd.get("error"):
                for a in dd.get("alerts",[]):
                    unified.append({"source":"datadog","id":str(a.get("monitor_id")),"title":a.get("monitor_name","Datadog Alert"),"severity":a.get("status","UNKNOWN"),"status":a.get("status","TRIGGERED"),"description":f"Group: {a.get('group')}","raw":a})
        except Exception as e: print(f"  [ObsMCP] Datadog: {e}")
        try:
            gf = self.get_grafana_alerts()
            if not gf.get("error"):
                for a in gf.get("alerts",[]):
                    unified.append({"source":"grafana","id":a.get("fingerprint"),"title":a.get("name","Grafana Alert"),"severity":a.get("severity","UNKNOWN"),"status":a.get("status","firing"),"description":a.get("description") or a.get("summary",""),"raw":a})
        except Exception as e: print(f"  [ObsMCP] Grafana: {e}")
        try:
            pm = self.prometheus_alerts()
            if not pm.get("error"):
                for a in pm.get("alerts",[]):
                    unified.append({"source":"prometheus","id":a.get("labels",{}).get("alertname"),"title":a.get("name","Prometheus Alert"),"severity":a.get("severity","UNKNOWN"),"status":a.get("state","firing"),"description":a.get("summary",""),"raw":a})
        except Exception as e: print(f"  [ObsMCP] Prometheus: {e}")
        return unified
