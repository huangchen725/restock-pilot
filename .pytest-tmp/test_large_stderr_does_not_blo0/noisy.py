import sys,json
sys.stderr.write("x"*1048576)
sys.stderr.flush()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}),flush=True)
