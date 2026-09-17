import json
with open('/mnt/c/Users/tianxiang/sida-work/shots/audit_report.json') as f:
    d = json.load(f)
for p in d['pages']:
    issues = []
    if p.get('error'): issues.append(f"ERROR: {p['error'][:80]}")
    if p.get('failedReqs'): issues.append(f"failedReqs: {len(p['failedReqs'])}")
    if p.get('consoleErrors'): issues.append(f"consoleErrors: {len(p['consoleErrors'])}")
    if issues:
        print(f"\n{p['page']}: {', '.join(issues)}")
        for f in p.get('failedReqs', [])[:5]:
            print(f"  FAIL: {f[:150]}")
        for c in p.get('consoleErrors', [])[:5]:
            print(f"  ERR: {c[:150]}")
