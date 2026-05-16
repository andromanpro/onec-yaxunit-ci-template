#!/usr/bin/env python3
"""
Helper для общения с Vanessa MCP сервером (client_mcp 0.6.4 на :8080)
напрямую через JSON-RPC, минуя Claude MCP loader (нет двойного рестарта).

Usage:
  py -3.14 vanessa-mcp.py list                # список зарегистрированных tools
  py -3.14 vanessa-mcp.py call <name> [args]  # вызвать tool с JSON args
  py -3.14 vanessa-mcp.py wait                # ждать Vanessa tools (max 90с, min 27)
  py -3.14 vanessa-mcp.py wait 90 1           # ждать только базовый MCP server
  py -3.14 vanessa-mcp.py status              # коротко: жив/мёртв + кол-во tools

Examples:
  py -3.14 vanessa-mcp.py call infobase_info
  py -3.14 vanessa-mcp.py call run_scenario '{"path":"C:/features/login.feature"}'
"""
import sys, json, time, urllib.request, urllib.error

URL = "http://127.0.0.1:8080/mcp"
PROTO = "2025-03-26"
DEFAULT_MIN_TOOLS = 27
# Do not use the system HTTP proxy for the local 1C MCP endpoint.
# The system proxy is still needed for internet access; this helper only talks to 127.0.0.1.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(payload, sid=None, timeout=10):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=h, method="POST")
    r = OPENER.open(req, timeout=timeout)
    body = r.read().decode("utf-8", "replace")
    sid_out = r.headers.get("mcp-session-id") or sid
    for line in body.splitlines():
        if line.startswith("data: "):
            return sid_out, json.loads(line[6:])
    return sid_out, {"raw": body}


def init_session():
    sid, resp = _post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTO, "capabilities": {},
                   "clientInfo": {"name": "vanessa-mcp.py", "version": "1"}}})
    # обязательное notification "initialized"
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "Mcp-Session-Id": sid}
    req = urllib.request.Request(URL, data=json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        headers=h, method="POST")
    try: OPENER.open(req, timeout=5).read()
    except: pass
    return sid, resp


def list_tools(sid=None):
    if sid is None:
        sid, _ = init_session()
    _, t = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)
    return t.get("result", {}).get("tools", [])


def cmd_status():
    try:
        sid, init = init_session()
        tools = list_tools(sid)
        srv = init.get("result", {}).get("serverInfo", {})
        print(f"OK | server={srv.get('name')} {srv.get('version')} | tools={len(tools)}")
        return 0
    except Exception as e:
        print(f"DOWN | {e}")
        return 1


def cmd_wait(max_sec=90, min_tools=DEFAULT_MIN_TOOLS):
    deadline = time.time() + max_sec
    last = ""
    while time.time() < deadline:
        try:
            tools = list_tools()
            tools_count = len(tools)
            if tools_count >= min_tools:
                print(f"READY | tools={tools_count}")
                return 0
            last = f"tools={tools_count}, waiting for >= {min_tools}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(1)
    print(f"TIMEOUT after {max_sec}s | last: {last}")
    return 1


def cmd_list():
    try:
        tools = list_tools()
    except Exception as e:
        print(f"DOWN | {type(e).__name__}: {str(e)[:120]}")
        return 1
    print(f"# tools: {len(tools)}\n")
    for tool in tools:
        name = tool["name"]
        desc = (tool.get("description") or "").replace("\n", " ")[:90]
        print(f"  {name:40s} {desc}")
    return 0


def cmd_call(name, args_json="{}"):
    args = json.loads(args_json) if args_json else {}
    sid, _ = init_session()
    _, r = _post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": name, "arguments": args}}, sid, timeout=300)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if "result" in r else 1


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print(__doc__); return 2
    cmd = sys.argv[1]
    if cmd == "status": return cmd_status()
    if cmd == "wait":
        max_sec = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        min_tools = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_MIN_TOOLS
        return cmd_wait(max_sec, min_tools)
    if cmd == "list":   return cmd_list()
    if cmd == "call":
        if len(sys.argv) < 3:
            print("Usage: call <tool_name> [json_args]"); return 2
        return cmd_call(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}")
    print(f"Unknown command: {cmd}\n{__doc__}"); return 2


if __name__ == "__main__":
    sys.exit(main())
