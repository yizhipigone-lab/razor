import os

with open("static/index.html", "r", encoding="utf-8") as f:
    lines = f.readlines()

js_start = -1
js_end = -1
for i, l in enumerate(lines):
    if l.strip() == "<script>":
        js_start = i
    if l.strip() == "</script>" and js_start != -1:
        js_end = i

if js_start != -1 and js_end != -1:
    js_lines = lines[js_start+1:js_end]
    with open("static/js/main.js", "w", encoding="utf-8") as f:
        f.writelines(js_lines)
    
    # Replace the inline script with an external reference
    new_lines = lines[:js_start] + ['    <script src="/static/js/main.js"></script>\n'] + lines[js_end+1:]
    with open("static/index.html", "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    print(f"Extracted {js_end - js_start - 1} lines of JS to static/js/main.js")
else:
    print("Could not find single <script> block. Need to refine search.")
