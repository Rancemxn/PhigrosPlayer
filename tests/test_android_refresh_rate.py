import ast
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
tree = ast.parse((root / "src/webcv.py").read_text(encoding="utf-8"))
canvas = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebCanvas")
start = next(node for node in canvas.body if isinstance(node, ast.FunctionDef) and node.name == "geckoview_start")
# Execute the window setup without starting Android, Gecko, or the HTTP server.
setup = start.body[:next(i for i, node in enumerate(start.body) if isinstance(node, ast.FunctionDef))]
code = compile(ast.Module(body=setup, type_ignores=[]), "webcv.py", "exec")


def mode(id, width, height, hz):
    return SimpleNamespace(getModeId=lambda: id, getPhysicalWidth=lambda: width,
                           getPhysicalHeight=lambda: height, getRefreshRate=lambda: hz)


def selected(current, supported):
    attributes = SimpleNamespace(preferredDisplayModeId=0)
    applied = []
    window = SimpleNamespace(getAttributes=lambda: attributes, setAttributes=applied.append)
    display = SimpleNamespace(getMode=lambda: current, getSupportedModes=lambda: supported)
    activity = SimpleNamespace(getWindow=lambda: window, getWindowManager=lambda:
                               SimpleNamespace(getDefaultDisplay=lambda: display))
    exec(code, {"activity": activity})
    assert applied == [attributes]
    return attributes.preferredDisplayModeId


standard = mode(1, 1080, 2376, 60)
assert selected(standard, [mode(4, 1440, 3168, 240), standard,
                           mode(2, 1080, 2376, 120), mode(3, 1080, 2376, 144)]) == 3
assert selected(standard, [standard]) == 1
assert selected(standard, []) == 1
print("Android refresh rate selection checks passed.")
