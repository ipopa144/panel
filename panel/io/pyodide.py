from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import sys

from typing import (
    TYPE_CHECKING, Any, Dict, Tuple,
)

import param
import pyodide

from bokeh import __version__
from bokeh.document import Document
from bokeh.embed.elements import script_for_render_items
from bokeh.embed.util import standalone_docs_json_and_render_items
from bokeh.embed.wrappers import wrap_in_script_tag
from bokeh.io.doc import set_curdoc
from bokeh.protocol.messages.patch_doc import process_document_events
from js import JSON

from ..config import config
from ..util import isurl
from . import resources
from .document import MockSessionContext
from .state import state

if TYPE_CHECKING:
    from bokeh.model import Model

resources.RESOURCE_MODE = 'CDN'
os.environ['BOKEH_RESOURCES'] = 'cdn'


#---------------------------------------------------------------------
# Private API
#---------------------------------------------------------------------

# Patch pandas.read_csv so it works in pyodide
if 'pandas' in sys.modules:
    import pandas

    _read_csv_original = pandas.read_csv

    def _read_csv(*args, **kwargs):
        if args and isurl(args[0]):
            args = (pyodide.http.open_url(args[0]),)+args[1:]
        elif isurl(kwargs.get('filepath_or_buffer')):
            kwargs['filepath_or_buffer'] = pyodide.http.open_url(kwargs['filepath_or_buffer'])
        return _read_csv_original(*args, **kwargs)
    _read_csv.__doc__ = _read_csv_original.__doc__

    pandas.read_csv = _read_csv

def async_execute(func: Any):
    event_loop = asyncio.get_running_loop()
    if event_loop.is_running():
        asyncio.create_task(func())
    else:
        event_loop.run_until_complete(func())
    return

param.parameterized.async_executor = async_execute

def _model_json(model: Model, target: str) -> Tuple[Document, str]:
    """
    Renders a Bokeh Model to JSON representation given a particular
    DOM target and returns the Document and the serialized JSON string.

    Arguments
    ---------
    model: bokeh.model.Model
        The bokeh model to render.
    target: str
        The id of the DOM node to render to.

    Returns
    -------
    document: Document
        The bokeh Document containing the rendered Bokeh Model.
    model_json: str
        The serialized JSON representation of the Bokeh Model.
    """
    doc = Document()
    model.server_doc(doc=doc)
    model = doc.roots[0]
    docs_json, _ = standalone_docs_json_and_render_items(
        [model], suppress_callback_warning=True
    )

    doc_json = list(docs_json.values())[0]
    root_id = doc_json['roots']['root_ids'][0]

    return doc, json.dumps(dict(
        target_id = target,
        root_id   = root_id,
        doc       = doc_json,
        version   = __version__,
    ))

def _doc_json(doc: Document, root_els=None) -> Tuple[str, str, str]:
    """
    Serializes a Bokeh Document into JSON representations of the entire
    Document, the individual render_items and the ids of DOM nodes to
    render each item into.

    Arguments
    ---------
    doc: bokeh.document.Document
        The Bokeh document to serialize to JSON.
    root_els:
        A list of DOM nodes to render each root of the Document into.

    Returns
    -------
    docs_json: str
    render_items: str
    root_ids: str
    """
    docs_json, render_items = standalone_docs_json_and_render_items(
        doc.roots, suppress_callback_warning=True
    )
    render_items_json = [item.to_json() for item in render_items]
    root_ids = [m.id for m in doc.roots]
    if root_els:
        root_data = sorted([(int(el.getAttribute('data-root-id')), el.id) for el in root_els])
        render_items_json[0].update({
            'roots': {model_id: elid for (_, elid), model_id in zip(root_data, root_ids)},
            'root_ids': root_ids
        })
    return json.dumps(docs_json), json.dumps(render_items_json), json.dumps(root_ids)

def _link_docs(pydoc: Document, jsdoc: Any) -> None:
    """
    Links Python and JS documents in Pyodide ensuring taht messages
    are passed between them.

    Arguments
    ---------
    pydoc: bokeh.document.Document
        The Python Bokeh Document instance to sync.
    jsdoc: Javascript Document
        The Javascript Bokeh Document instance to sync.
    """
    def jssync(event):
        if (getattr(event, 'setter_id', None) is not None):
            return
        events = [event]
        json_patch = jsdoc.create_json_patch_string(pyodide.ffi.to_js(events))
        pydoc.apply_json_patch(json.loads(json_patch))

    jsdoc.on_change(pyodide.ffi.create_proxy(jssync), pyodide.ffi.to_js(False))

    def pysync(event):
        json_patch, buffers = process_document_events([event], use_buffers=True)
        buffer_map = {}
        for (ref, buffer) in buffers:
            buffer_map[ref['id']] = pyodide.ffi.to_js(buffer).buffer
        jsdoc.apply_json_patch(JSON.parse(json_patch), pyodide.ffi.to_js(buffer_map), setter_id='js')

    pydoc.on_change(pysync)
    pydoc.callbacks.trigger_json_event(
        {'event_name': 'document_ready', 'event_values': {}
    })

def _link_docs_worker(doc: Document, dispatch_fn: Any, msg_id: str | None = None):
    """
    Links the Python document to a dispatch_fn which can be used to
    sync messages between a WebWorker and the main thread.
    """
    def pysync(event):
        json_patch, buffers = process_document_events([event], use_buffers=True)
        buffer_map = {}
        for (ref, buffer) in buffers:
            buffer_map[ref['id']] = pyodide.to_js(buffer).buffer
        dispatch_fn(json_patch, pyodide.ffi.to_js(buffer_map), msg_id)

    doc.on_change(pysync)
    doc.callbacks.trigger_json_event(
        {'event_name': 'document_ready', 'event_values': {}
    })

async def _link_model(ref: str, doc: Document) -> None:
    """
    Links a rendered Bokeh model on the frontend to a Python Document
    in Python.

    Arguments
    ---------
    ref: str
        The ID of the rendered Bokeh Model.
    doc: bokeh.document.Document
        The Bokeh Document to sync the rendered Model with.
    """
    from js import Bokeh
    rendered = Bokeh.index.object_keys()
    if ref not in rendered:
        await asyncio.sleep(0.1)
        await _link_model(ref, doc)
        return
    views = Bokeh.index.object_values()
    view = views[rendered.indexOf(ref)]
    _link_docs(doc, view.model.document)

def _convert_expr(expr: ast.Expr) -> ast.Expression:
    """
    Converts an ast.Expr to and ast.Expression that can be compiled
    and evaled.
    """
    expr.lineno = 0
    expr.col_offset = 0
    return ast.Expression(expr.value, lineno=0, col_offset = 0)

def _exec_with_return(code: str, global_context: Dict[str, Any] = None) -> Any:
    """
    Executes a code snippet and returns the resulting output of the
    last line.

    Arguments
    ---------
    code: str
      The code to execute
    global_context: Dict[str, Any]
      The globals to inject into the execution context.

    Returns
    -------
    The return value of the executed code.
    """
    global_context = global_context if global_context else globals()
    code_ast = ast.parse(code)

    init_ast = copy.deepcopy(code_ast)
    init_ast.body = code_ast.body[:-1]

    last_ast = copy.deepcopy(code_ast)
    last_ast.body = code_ast.body[-1:]

    exec(compile(init_ast, "<ast>", "exec"), global_context)
    if type(last_ast.body[0]) == ast.Expr:
        return eval(compile(_convert_expr(last_ast.body[0]), "<ast>", "eval"), global_context)
    else:
        exec(compile(last_ast, "<ast>", "exec"), global_context)

#---------------------------------------------------------------------
# Public API
#---------------------------------------------------------------------

def render_script(obj: Any, target: str) -> str:
    """
    Generates a script to render the supplied object to the target.

    Arguments
    ---------
    obj: Viewable
        Object to render into the DOM node
    target: str
        Target ID of the DOM node to render the object into.
    """
    from js import document

    from ..pane import panel as as_panel

    doc = Document()
    as_panel(obj).server_doc(doc, location=False)
    docs_json, [render_item,] = standalone_docs_json_and_render_items(
        doc.roots, suppress_callback_warning=True
    )
    for root in doc.roots:
        render_item.roots._roots[root] = target
    document.getElementById(target).classList.add('bk-root')
    script = script_for_render_items(docs_json, [render_item])
    asyncio.create_task(_link_model(doc.roots[0].ref['id'], doc))
    return wrap_in_script_tag(script)

def serve(*args, **kwargs):
    """
    Stub to replace Tornado based serve function.
    """
    raise RuntimeError('Cannot serve application in pyodide context.')

def init_doc() -> None:
    """
    Creates a Document mocking a server context for embedded Pyodide apps.
    """
    doc = Document()
    set_curdoc(doc)
    doc._session_context = lambda: MockSessionContext(document=doc)
    state.curdoc = doc

async def show(obj: Any, target: str) -> None:
    """
    Renders the object into a DOM node specified by the target.

    Arguments
    ---------
    obj: Viewable
        Object to render into the DOM node
    target: str
        Target ID of the DOM node to render the object into.
    """
    from js import console
    console.log('panel.io.pyodide.show is deprecated in favor of panel.io.pyodide.write')
    await write(target, obj)

async def write(target: str, obj: Any) -> None:
    """
    Renders the object into a DOM node specified by the target.

    Arguments
    ---------
    target: str
        Target ID of the DOM node to render the object into.
    obj: Viewable
        Object to render into the DOM node
    """

    from js import Bokeh

    from ..pane import panel as as_panel

    obj = as_panel(obj)
    pydoc, model_json = _model_json(obj, target)
    views = await Bokeh.embed.embed_item(JSON.parse(model_json))
    jsdoc = views[0].model.document
    _link_docs(pydoc, jsdoc)

def hide_loader() -> None:
    from js import document

    body = document.getElementsByTagName('body')[0]
    body.classList.remove("bk", "pn-loading", config.loading_spinner)

async def write_doc(doc: Document | None = None) -> Tuple[str, str, str]:
    """
    Renders the contents of the Document into an existing template.
    Note that this assumes that the HTML file this function runs in
    was itself generated from the code being run. This function may
    be used in the main browser thread or from within a WebWorker.
    If invoked by a WebWorker the JSON return values can be sent
    to the main thread for rendering.

    Arguments
    ---------
    doc: Document

    Returns
    -------
    docs_json: str
    render_items: str
    root_ids: str
    """
    pydoc: Document = doc or state.curdoc
    if pydoc in state._templates:
        template = state._templates[pydoc]
        template.server_doc(title=template.title, location=True, doc=pydoc)

    # Test whether we have access to DOM
    try:
        from js import Bokeh, document
        root_els = document.getElementsByClassName('bk-root')
        for el in root_els:
            el.innerHTML = ''
    except Exception:
        root_els = None
    docs_json, render_items, root_ids = _doc_json(pydoc, root_els)
    doc._session_context = None

    # If we have DOM access render and sync the document
    if root_els is not None:
        views = await Bokeh.embed.embed_items(JSON.parse(docs_json), JSON.parse(render_items))
        jsdoc = views[0][0].model.document
        _link_docs(pydoc, jsdoc)
        hide_loader()
    return docs_json, render_items, root_ids
