import re
src = open("src/agents/chat/registry.py", encoding="utf-8").read()
names = re.findall(r'@register_chat_tool\(\s*["\']([a-zA-Z0-9_]+)["\']', src)
print("chat_tools", len(names))
for n in names:
    print(n)
