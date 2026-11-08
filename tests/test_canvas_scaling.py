import ast
import json
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
tree = ast.parse((root / "src/webcv.py").read_text(encoding="utf-8"))
canvas = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebCanvas")
# Exercise the real dimension methods without importing Android or opening a window.
methods = {"winfo_screenwidth", "winfo_screenheight", "winfo_legacywindowwidth", "winfo_legacywindowheight"}
canvas.body = [node for node in canvas.body if isinstance(node, ast.FunctionDef) and node.name in methods]
namespace = {"checksys": "Android", "screen_width": 1080, "screen_height": 2376}
exec(compile(ast.Module(body=[canvas], type_ignores=[]), "webcv.py", "exec"), namespace)
window = namespace["WebCanvas"]()
window.run_js_code = {"window.innerWidth;": 360, "window.innerHeight;": 720}.__getitem__
assert (window.winfo_screenwidth() * 3, window.winfo_screenheight() * 3) == (1080, 2160)
namespace["checksys"] = "Windows"
assert (window.winfo_screenwidth(), window.winfo_screenheight()) == (1080, 2376)

html = (root / "src/web_canvas.html").read_text(encoding="utf-8")
position = re.search(r"const fixEventPosition = (.*?);", html, re.S).group(1)
subprocess.run(["node", "-e", """
const assert = require('node:assert/strict');
const window = {innerWidth: 800, innerHeight: 400};
const canvas_ele = {width: 2400, height: 1200};
const canvasPosD = [120, 0];
const dpr = 2;
const fixEventPosition = eval(%s);
assert.deepEqual(fixEventPosition(400, 200), [1080, 600]);
canvas_ele.width = 800;
canvas_ele.height = 400;
canvasPosD[0] = 0;
assert.deepEqual(fixEventPosition(400, 200), [400, 200]);
window.innerWidth = 400;
window.innerHeight = 800;
assert.deepEqual(fixEventPosition(200, 400), [400, 200]);
""" % json.dumps(position)], check=True)
print("Canvas size and pointer scaling checks passed.")
