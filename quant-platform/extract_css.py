import os

with open("static/index.html", encoding="utf-8") as f:
    lines = f.readlines()

css_lines = lines[31:322]
out_html = lines[:31] + ['<link rel="stylesheet" href="/static/css/main.css">\n'] + lines[323:]

with open("static/css/main.css", "w", encoding="utf-8") as f:
    f.writelines(css_lines)

with open("static/index.html", "w", encoding="utf-8") as f:
    f.writelines(out_html)

print("CSS extracted successfully!")
