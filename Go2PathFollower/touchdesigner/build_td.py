# TouchDesigner のネットワーク「go2」を作る（または足りない部分だけ作り直す）スクリプト。
#
# 使い方: TouchDesigner の Textport（Dialogs > Textport and DATs）に 1 行貼って Enter:
#
#   exec(open('/Users/nibos/Desktop/ZOUNOHANA2026/Go2PathFollower/touchdesigner/build_td.py', encoding='utf-8').read())
#
# 何度実行してもよい（既にあるオペレータとパラメータの値はそのまま。足りないものだけ足す）。
# 作る場所を変えたいときは、先に Textport で GO2_PARENT = '/project1/xxx' を実行しておく。
# フォルダを移したときは GO2_ROOT = '<Go2PathFollower のパス>' を先に実行する。

import importlib
import os
import sys

ROOT = globals().get('GO2_ROOT', '/Users/nibos/Desktop/ZOUNOHANA2026/Go2PathFollower')
TDDIR = os.path.join(ROOT, 'touchdesigner')
if TDDIR not in sys.path:
    sys.path.insert(0, TDDIR)
import go2ctl as _g2  # noqa: E402  パラメータ定義を使う
importlib.reload(_g2)

FRAME_CB = '''# 毎フレーム・起動時・終了時（build_td.py が作成。編集しなくてよい）
def _m():
    return parent().op('go2ctl').module

def onStart():
    _m().on_start(parent())
    return

def onFrameStart(frame):
    _m().on_frame(parent())
    return

def onExit():
    _m().on_exit(parent())
    return
'''

PAR_CB = '''# go2 のカスタムパラメータが変わったとき（build_td.py が作成）
def _m():
    return parent().op('go2ctl').module

def onValueChange(par, prev):
    _m().on_value_change(parent(), par.name)
    return

def onPulse(par):
    _m().on_pulse(parent(), par.name)
    return
'''

KEY_CB = '''# スペースキーで非常停止（TD のウィンドウにフォーカスがあるとき）
def onKey(dat, key, *args, **kwargs):
    parent().op('go2ctl').module.on_key(parent(), key)
    return
'''

TOP_CB = '''def onCook(scriptOp):
    img = parent().op('go2ctl').module.get_image('{name}')
    if img is not None:
        scriptOp.copyNumpyArray(img)
    return
'''


def _aslist(r):
    try:
        return list(r)
    except TypeError:
        return [r]


def _try(desc, fn):
    try:
        fn()
    except Exception as e:  # TD のバージョン差でパラメータ名が違う場合など
        print(f'  [注意] {desc}: {e}')


def build():
    dest = op(globals().get('GO2_PARENT', '/project1')) or op('/')
    comp = dest.op('go2') or dest.create(baseCOMP, 'go2')
    print('go2:', comp.path)

    # ---- カスタムパラメータ
    pages = {}

    def page(name):
        if name not in pages:
            found = [p for p in comp.customPages if p.name == name]
            pages[name] = found[0] if found else comp.appendCustomPage(name)
        return pages[name]

    for pg, name, label, kind, default, lo, hi, key in _g2.PARAMS:
        exists = getattr(comp.par, name + ('x' if kind == 'xy' else ''), None) is not None
        if exists:
            continue
        p = page(pg)
        if kind == 'float':
            pars = _aslist(p.appendFloat(name, label=label))
        elif kind == 'int':
            pars = _aslist(p.appendInt(name, label=label))
        elif kind == 'toggle':
            pars = _aslist(p.appendToggle(name, label=label))
        elif kind in ('str', 'readonly'):
            pars = _aslist(p.appendStr(name, label=label))
        elif kind == 'menu':
            pars = _aslist(p.appendMenu(name, label=label))
            pars[0].menuNames, pars[0].menuLabels = _g2.MENUS[name]
        elif kind == 'pulse':
            pars = _aslist(p.appendPulse(name, label=label))
        elif kind == 'xy':
            pars = _aslist(p.appendXY(name, label=label))
        elif kind == 'top':
            pars = _aslist(p.appendTOP(name, label=label))
        elif kind == 'sop':
            pars = _aslist(p.appendSOP(name, label=label))
        elif kind == 'folder':
            pars = _aslist(p.appendFolder(name, label=label))
        elif kind == 'file':
            pars = _aslist(p.appendFile(name, label=label))
        else:
            continue
        defaults = list(default) if kind == 'xy' else [None if kind in ('top', 'sop') else default]
        for par, d in zip(pars, defaults):
            if d is not None:
                _try(f'{par.name} の既定値', lambda: setattr(par, 'default', d))
                _try(f'{par.name} の値', lambda: setattr(par, 'val', d))
            if lo is not None:
                for attr, v in (('normMin', lo), ('normMax', hi), ('min', lo), ('max', hi),
                                ('clampMin', True), ('clampMax', True)):
                    _try(f'{par.name}.{attr}', lambda: setattr(par, attr, v))
            if kind == 'readonly':
                _try(f'{par.name}.readOnly', lambda: setattr(par, 'readOnly', True))
    if getattr(comp.par, 'Projectdir', None) is not None and comp.par.Projectdir.eval() != ROOT:
        comp.par.Projectdir = ROOT

    # ---- 子オペレータ
    def child(optype, name, x, y):
        o = comp.op(name)
        if o is None:
            o = comp.create(optype, name)
        o.nodeX, o.nodeY = x, y
        return o

    camera = child(videodeviceinTOP, 'camera', -600, 200)
    mono = child(monochromeTOP, 'mono', -400, 200)
    cam = child(nullTOP, 'cam', -200, 200)
    if not mono.inputs:
        mono.inputConnectors[0].connect(camera)
    if not cam.inputs:
        cam.inputConnectors[0].connect(mono)
    if comp.par.Cameratop.eval() is None:
        comp.par.Cameratop = cam.path

    # ルートの形: Circle SOP → Null SOP「route_sop」（例。route_sop の手前を好きな SOP に差し替えてよい）
    shape = comp.op('route_shape')
    if shape is None and comp.op('route_sop') is None:
        shape = child(circleSOP, 'route_shape', -600, 400)
        _try('route_shape.type', lambda: setattr(shape.par, 'type', 'poly'))
        _try('route_shape.radx', lambda: setattr(shape.par, 'radx', 1.5))
        _try('route_shape.rady', lambda: setattr(shape.par, 'rady', 1.5))
        _try('route_shape.divs', lambda: setattr(shape.par, 'divs', 48))
    route_sop = child(nullSOP, 'route_sop', -400, 400)
    if shape is not None and not route_sop.inputs:
        route_sop.inputConnectors[0].connect(shape)
    if comp.par.Routesop.eval() is None:
        comp.par.Routesop = route_sop.path

    ctl = child(textDAT, 'go2ctl', -600, -50)
    ctl.par.file = os.path.join(TDDIR, 'go2ctl.py')
    _try('go2ctl の Sync to File', lambda: setattr(ctl.par, 'syncfile', True))
    if not ctl.text.strip():
        with open(os.path.join(TDDIR, 'go2ctl.py'), encoding='utf-8') as f:
            ctl.text = f.read()

    route = child(tableDAT, 'route', -400, -50)
    if route.numRows == 0 or route[0, 0].val != 'x':
        path = os.path.join(ROOT, 'routes', 'example_route.json')
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        _g2.write_route_table(route, data['points'] if isinstance(data, dict) else data)

    frame = child(executeDAT, 'frame', -200, -50)
    frame.text = FRAME_CB
    for name in ('framestart', 'start', 'exit'):
        _try(f'frame.{name}', lambda: setattr(getattr(frame.par, name), 'val', True))

    pe = child(parameterexecuteDAT, 'pars', 0, -50)
    pe.text = PAR_CB
    _try('pars.op', lambda: setattr(pe.par, 'op', comp.path))
    _try('pars.pars', lambda: setattr(pe.par, 'pars', '*'))
    for name, v in (('custom', True), ('builtin', False), ('valuechange', True), ('onpulse', True)):
        _try(f'pars.{name}', lambda: setattr(getattr(pe.par, name), 'val', v))

    keys = child(keyboardinDAT, 'keys', 200, -50)
    keys_cb = child(textDAT, 'keys_cb', 200, -200)
    keys_cb.text = KEY_CB
    _try('keys.callbacks', lambda: setattr(keys.par, 'callbacks', keys_cb.path))

    for name, x in (('map', 0), ('overlay', 200)):
        top = child(scriptTOP, name, x, 200)
        cb = child(textDAT, name + '_cb', x, 50)
        cb.text = TOP_CB.format(name=name)
        _try(f'{name}.callbacks', lambda: setattr(top.par, 'callbacks', cb.path))
        _try(f'{name}.viewer', lambda: setattr(top, 'viewer', True))

    print('完成。go2 のパラメータ（Run / Route / Connect / Camera / Area / Control / Safety / Status）で操作します。')
    print('ルートの形: go2/route_sop（Null SOP）の手前の SOP を編集すると、その場で map に反映されます。')
    print('カメラ: go2/camera（Video Device In）でデバイスを選んでください。')
    return comp


build()
