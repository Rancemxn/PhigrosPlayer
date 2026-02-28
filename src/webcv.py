from __future__ import annotations

import fix_workpath as _
import imageload_hook as _
import init_logging as _
import threading
import typing
import os
import io
import time
import socket
import sys
import logging
from os.path import abspath
from random import randint
from PIL import Image

from checksys import checksys

import graplib_webview

disengage_webview = "--disengage-webview" in sys.argv

if checksys != 'Android': import webview
if checksys == "Windows":
    from ctypes import windll
    screen_width = windll.user32.GetSystemMetrics(0)
    screen_height = windll.user32.GetSystemMetrics(1)
if checksys == "Android":
    from jnius import autoclass # type: ignore
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    metrics = PythonActivity.mActivity.getResources().getDisplayMetrics()
    screen_width = metrics.widthPixels
    screen_height = metrics.heightPixels

host = socket.gethostbyname(socket.gethostname()) if "--nolocalhost" in sys.argv else "127.0.0.1"
logging.debug(f"server host: {host}")

if checksys == 'Android':
    from jnius import autoclass, cast, java_method, PythonJavaClass # type: ignore
    from android.runnable import run_on_ui_thread # type: ignore

    from android.permissions import request_permissions, Permission # type: ignore
    request_permissions([Permission.WRITE_EXTERNAL_STORAGE])

    
    os.environ['MOZ_DEBUG_CHILD_PROCESS'] = '1'
    GeckoRuntimeSettings = autoclass('org.mozilla.geckoview.GeckoRuntimeSettings')
    Builder = autoclass('org.mozilla.geckoview.GeckoRuntimeSettings$Builder')  # 使用 $ 符号
    GeckoView = autoclass('org.mozilla.geckoview.GeckoView')
    GeckoRuntime = autoclass('org.mozilla.geckoview.GeckoRuntime')
    GeckoSession = autoclass('org.mozilla.geckoview.GeckoSession')
    activity = autoclass('org.kivy.android.PythonActivity').mActivity
    LayoutParams = autoclass('android.view.ViewGroup$LayoutParams')

class JsApi:
    def __init__(self) -> None:
        self.things: dict[str, typing.Any] = {}

    def get_thing(self, name: str):
        return self.things[name]
    
    def set_thing(self, name: str, value: typing.Any):
        self.things[name] = value
    
    def get_attr(self, name: str):
        return getattr(self, name)
    
    def set_attr(self, name: str, value: typing.Any):
        setattr(self, name, value)
    
    def call_attr(self, name: str, *args, **kwargs):
        try: func = getattr(self, name)
        except AttributeError:
            return logging.warning(f"JsApi: No such attribute '{name}'")
        return func(*args, **kwargs)
    
    @staticmethod
    def _socket_bridge_error(code: str, err: dict):
        raise Exception(f"SocketBridge: {err}")

class PILResourcePacker:
    def __init__(self, cv: WebCanvas):
        self.cv = cv
        self.imgs: list[tuple[str, Image.Image|bytes]] = []
        self._imgopted: dict[str, threading.Event] = {}
    
    def reg_img(self, img: Image.Image|bytes, name: str):
        self.imgs.append((name, img))
        
    def pack(self):
        logging.info('Packing images...')
        datas = []
        dataindexs = []
        datacount = 0
        cache = {}
        for name, img in self.imgs:
            if isinstance(img, Image.Image):
                if hasattr(img, "byteData"):
                    data = img.byteData
                else:
                    if id(img) in cache:
                        data = cache[id(img)]
                    else:
                        btio = io.BytesIO()
                        img.save(btio, "png") # toooooooooooooo slow
                        data = btio.getvalue()
                        cache[id(img)] = data
            else:
                data = img
                
            datas.append(data)
            dataindexs.append([name, [datacount, len(data)]])
            datacount += len(data)
        
        logging.info('Packing Done')
        return b"".join(datas), dataindexs

    def load(self, data: bytes, indexs: list[list[str, list[int, int]]]):
        rid = f"pilrespacker_{randint(0, 2 << 31)}"
        self.cv.reg_res(data, rid)
        
        imnames = self.cv.wait_jspromise(f"loadrespackage(URL.createObjectURL(new Blob([new Uint8Array({list(data)})], {{type: 'application/octet-stream'}})), {indexs});")
        self.cv.wait_loadimgs(self.cv.get_imgcomplete_jseval(imnames))
        self.cv.unreg_res(rid)
        
        self.cv.run_js_code(f"""[{",".join(map(self.cv.get_img_jsvarname, imnames))}].forEach(im => URL.revokeObjectURL(im.src));""")
        
        def optimize():
            codes = []
            codes.append(f"cachecv = document.createElement('canvas');")
            codes.append(f"cachecv.width = cachecv.height = 1;")
            codes.append(f"cachectx = cachecv.getContext('2d');")
            for im in imnames:
                codes.append(f"cachectx.drawImage({self.cv.get_img_jsvarname(im)}, 0, 0);")
            codes.append(f"delete cachecv; delete cachectx;")
            self.cv.run_js_code("".join(codes))
            
            for im in imnames:
                self._imgopted[im].set()
        
        self._imgopted.update({im: threading.Event() for im in imnames})
        threading.Thread(target=optimize, daemon=True).start()
    
    def getnames(self):
        return [name for name, _ in self.imgs]

    def unload(self, names: list[str]):
        for name in names:
            self._imgopted[name].wait()
            self._imgopted.pop(name)
            
        self.cv.run_js_code(f"""{";".join(map(lambda x: f"delete {self.cv.get_img_jsvarname(x)}", names))};""")

class WebCanvas:
    def __init__(
        self,
        width: int, height: int,
        x: int, y: int,
        debug: bool = False,
        title: str = "WebCanvas",
        resizable: bool = True,
        frameless: bool = False,
        html_path: str = "web_canvas.html",
        renderdemand: bool = False,
        renderasync: bool = False,
        hidden: bool = False,
        jslog: bool = False,
        jslog_path: str = "web_canvas.jslog.txt",
    ):
        self.jsapi = JsApi()
        self._destroyed = threading.Event()
        self._regims: dict[str, Image.Image] = {}
        self._regres: dict[str, bytes] = {}
        self._is_loadimg: dict[str, bool] = {}
        self._jscodes: list[str] = []
        self._jscode_orders: dict[int, list[tuple[str, bool]]] = {}
        
        self._rdevent = threading.Event()
        self._raevent = threading.Event()
        self.renderdemand = renderdemand
        self.renderasync = renderasync
        
        self.jslog = jslog
        self.jslog_path = jslog_path
        self.jslog_f = open(jslog_path, "w", encoding="utf-8") if self.jslog else None
        
        html_path = abspath(html_path)
        if checksys != 'Android':
            self.web = webview.create_window(
                title = title,
                url = html_path,
                resizable = resizable,
                js_api = self.jsapi,
                frameless = frameless,
                hidden = hidden
            ) if not disengage_webview else None
            self.evaljs = lambda x, *args, **kwargs: (self.web.evaluate_js(x) if not disengage_webview else None)
        self.init = lambda func: (self._init(width, height, x, y), func())
        self.start = lambda: webview.start(debug=debug) if not disengage_webview else time.sleep(60 * 60 * 24 * 7 * 4 * 12 * 80)
        self.start = self.geckoview_start if checksys == 'Android' else self.start

    @run_on_ui_thread
    def geckoview_start(self):
        
        with open('org.qaqfei.phigrosplayer.phigrosplayer-geckoview-config.yaml', 'w', encoding='utf-8') as f:
            f.write("""
env:
  MOZ_LOG: "Acceleration:5,GLContext:5,Compositor:5,WebRender:5,GPU:5"

args:
  - --ignore-gpu-blocklist

prefs:
  gfx.work-around-driver-bugs: false
  gfx.canvas.accelerated: true
  gfx.webgpu.ignore-blocklist: true
  gfx.canvas.accelerated.force-enabled: true
  gfx.canvas.accelerated.allow-in-parent: true
  gfx.webrender.all: true
  gfx.webrender.debug.profiler: true
  gfx.webrender.fallback.software: false
  gfx.webrender.software: false
  layers.acceleration.disabled: false
  layers.acceleration.force-enabled: true
  layers.gpu-process.allow-software: false
  layers.gpu-process.enabled: true
  layers.gpu-process.force-enabled: true
  security.sandbox.gpu.level: 0
""")
        logging.info('Initializing Geckoview')
        builder = Builder()
        builder.configFilePath(os.path.abspath('org.qaqfei.phigrosplayer.phigrosplayer-geckoview-config.yaml'))
        builder.aboutConfigEnabled(True)
        builder = cast(GeckoRuntimeSettings, builder.build())
        self.runtime = GeckoRuntime.create(activity, builder)
        self.settings = self.runtime.getSettings()
        self.settings.setRemoteDebuggingEnabled(True)
        self.settings.setConsoleOutputEnabled(True)
        self.settings.setJavaScriptEnabled(True)
        #self.settings.setParallelMarkingEnabled(True)
        
        self.webview = GeckoView(activity)
        
        self.session = GeckoSession()
        self.session.open(self.runtime)
        self.webview.setSession(self.session)
        self.session.loadUri(os.path.abspath('web_canvas.html'))
        
        match_parent = LayoutParams.MATCH_PARENT
        params = LayoutParams(match_parent, match_parent)
        activity.setContentView(self.webview, params)
        
    
    def _init(self, width: int, height: int, x: int, y: int):
        if not disengage_webview:
            self.web_hwnd = 0
            if checksys == 'Windows':
                self.web.resize(width, height)
                self.web.move(x, y)
                title = self.web.title
                temp_title = self.web.title + " " * randint(0, 4096)
                self.web.set_title(temp_title)
                while not self.web_hwnd:
                    self.web_hwnd = windll.user32.FindWindowW(None, temp_title)
                    time.sleep(0.01)
                self.web.set_title(title)
                self.web.events.closed += self._destroyed.set
        else:
            self.web_hwnd = -1
        self.jsapi.set_attr("_rdcallback", self._rdevent.set)
        self._raevent.set()
        logging.info('Webview start')
        graplib_webview.root = self
    
    def title(self, title: str) -> str:
        if checksys == 'Android': return
        self.web.set_title(title) if not disengage_webview else None
    
    def winfo_screenwidth(self) -> int: return screen_width

    def winfo_screenheight(self) -> int: return screen_height

    def winfo_hwnd(self) -> int: return self.web_hwnd
    def winfo_legacywindowwidth(self) -> int: return self.run_js_code("window.innerWidth;")
    def winfo_legacywindowheight(self) -> int: return self.run_js_code("window.innerHeight;")

    def destroy(self):
        if checksys == 'Android': return
        self.web.destroy() if not disengage_webview else None
    
    def resize(self, width: int, height: int):
        if checksys == 'Android': return
        self.web.resize(width, height) if not disengage_webview else None
    
    def move(self, x: int, y:int):
        if checksys == 'Android': return
        self.web.move(x, y) if not disengage_webview else None
    
    def fullscreen(self):
        if checksys == 'Android': return
        self.web.toggle_fullscreen() if not disengage_webview else None
    
    def run_js_code(self, code: str, add_code_array: bool = False, order: int|None = None, needresult: bool = True):
        if self.jslog and not code.endswith(";"): code += ";"
        
        if order is None:
            return self._jscodes.append(code) if add_code_array else self.evaljs(code, needresult)
        
        if order not in self._jscode_orders:
            self._jscode_orders[order] = []
        self._jscode_orders[order].append((code, add_code_array))
    
    def _rjwc(self, codes: list[str]):
        codes.append("ctx.flush();") 
        self.run_js_code(f"{codes}.forEach(r2eval);")
        
        if self.renderdemand:
            self._rdevent.wait()
            self._rdevent.clear()
        
        if self.renderasync:
            self._raevent.set()
    
    def run_jscode_orders(self):
        if self._jscode_orders:
            for _, i in sorted(self._jscode_orders.items(), key=lambda x: x[0]):
                for c, w in i: 
                    if w: self._jscodes.append(c)
                    else: self.run_js_code(c)
            self._jscode_orders.clear()
        
    def run_js_wait_code(self):
        if self._jscode_orders: self.run_jscode_orders() # not to create a new pyframe
        codes = self._jscodes.copy()
        self._jscodes.clear()
        
        if self.jslog:
            self.jslog_f.write("\n// JSCODE - FRAME - START //\n")
            self.jslog_f.writelines(codes)
            self.jslog_f.write("\n// JSCODE - FRAME - END //\n")
        
        if not self.renderasync:
            return self._rjwc(codes)
        else:
            self._raevent.wait()
            self._raevent.clear()
            threading.Thread(target=self._rjwc, args=(codes, ), daemon=True).start()
    
    def string2cstring(self, code: str): return code.replace("\\", "\\\\").replace("'", "\\'").replace("\"", "\\\"").replace("`", "\\`").replace("\n", "\\n")
    def string2sctring_hqm(self, code: str): return f"'{self.string2cstring(code)}'"
    
    def get_img_jsvarname(self, imname: str):
        return f"{imname}_img"
    
    def reg_img(self, im: Image.Image, name: str) -> None:
        self._regims[name] = im
        self._is_loadimg[name] = False
    
    def reg_res(self, res_data: bytes, name: str) -> None:
        self._regres[name] = res_data
    
    def unreg_res(self, name: str) -> None:
        self._regres.pop(name)
    
    def get_imgcomplete_jseval(self, ns: list[str]) -> str:
        return f"""[{",".join([f"{self.get_img_jsvarname(item)}.complete" for item in ns])}]"""
    
    def wait_loadimgs(self, complete_code: str) -> None:
        while not all(self.run_js_code(complete_code)):
            time.sleep(0.01)
    
    def reg_event(self, name: str, callback: typing.Callable) -> None:
        if disengage_webview or checksys == 'Android': return
        setattr(self.web.events, name, getattr(self.web.events, name) + callback)
    
    def wait_for_close(self) -> None:
        while not self._destroyed.wait(0.1):
            pass
        sys.exit(0)

    def wait_jspromise(self, code: str) -> None:
        eid = f"wait_jspromise_{randint(0, 2 << 31)}"
        ete = threading.Event()
        ecbname = f"{eid}_callback"
        result = None

        def _callback(jsresult):
            nonlocal result
            result = jsresult
            ete.set()
            
        self.jsapi.set_attr(ecbname, _callback)
        self.run_js_code(f"eval({self.string2sctring_hqm(code)}).then((result) => pywebview.api.call_attr('{ecbname}', result));", needresult=False)
        ete.wait()
        delattr(self.jsapi, ecbname)
        return result