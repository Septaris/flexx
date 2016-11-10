"""
Flexx asset and data management system. The purpose of these classes
is to provide the assets (JavaScript and CSS files) and data (images,
etc.) needed by the applications.

Flexx makes a dinstinction between shared assets and session-specific
assets. Most source code for e.g. the widgets is served as a shared asset,
but app-specific classes and session-specific data can be served
per-session (and is deleted when the session is closed).

"""

import os
import sys
import json
import time
import types
import random
import shutil
import hashlib
from urllib.request import urlopen
from collections import OrderedDict

from .model import Model, get_model_classes
from .modules import JSModule, HEADER
from ..pyscript import (py2js, Parser,
                        create_js_module, get_all_std_names, get_full_std_lib)
from . import logger


INDEX = """<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Flexx UI</title>
</head>

<body id='body'>

ASSET-HOOK

</body>
</html>
"""

# todo: make this work with out-of-order assets too

# This is our loader for AMD modules. It invokes the modules immediately,
# since we want Flexx to be ready to use so we can execute commands via the
# websocket. It also allows redefining modules so that one can interactively
# (re)define module classes. The loader is itself wrapped in a IIFE to
# create a private namespace. The modules must follow this pattern:
# define(name, dep_strings, function (name1, name2) {...});

LOADER = """
/*Flexx module loader. Licensed by BSD-2-clause.*/

(function(){

if (typeof window === 'undefined' && typeof module == 'object') {
    global.window = global; // https://github.com/nodejs/node/pull/1838
    window.is_node = true;
}
if (typeof flexx == 'undefined') {
    window.flexx = {};
}

var modules = {};
function define (name, deps, factory) {
    if (arguments.length == 1) {
        factory = name;
        deps = [];
        name = null;
    }
    if (arguments.length == 2) {
        factory = deps;
        deps = name;
        name = null;
    }
    // Get dependencies - in current implementation, these must be loaded
    var dep_vals = [];
    for (var i=0; i<deps.length; i++) {
        if (modules[deps[i]] === undefined) {
            throw Error('Unknown dependency: ' + deps[i]);
        }
        dep_vals.push(modules[deps[i]]);
    }
    // Load the module and store it if is not anonymous
    var mod = factory.apply(null, dep_vals);
    if (name) {
        modules[name] = mod;
    }
}
define.amd = true;
define.flexx = true;

function require (name) {
    return modules[name];
}

// Expose this
window.flexx.define = define;
window.flexx.require = require;
window.flexx._modules = modules;

})();
"""

RESET = """
/*! normalize.css v3.0.3 | MIT License | github.com/necolas/normalize.css */
html
{font-family:sans-serif;-ms-text-size-adjust:100%;-webkit-text-size-adjust:100%}
body{margin:0}
article,aside,details,figcaption,figure,footer,header,hgroup,main,menu,nav,
section,summary{display:block}
audio,canvas,progress,video{display:inline-block;vertical-align:baseline}
audio:not([controls]){display:none;height:0}
[hidden],template{display:none}
a{background-color:transparent}
a:active,a:hover{outline:0}
abbr[title]{border-bottom:1px dotted}
b,strong{font-weight:bold}
dfn{font-style:italic}
h1{font-size:2em;margin:.67em 0}
mark{background:#ff0;color:#000}
small{font-size:80%}
sub,sup{font-size:75%;line-height:0;position:relative;vertical-align:baseline}
sup{top:-0.5em}
sub{bottom:-0.25em}
img{border:0}
svg:not(:root){overflow:hidden}
figure{margin:1em 40px}
hr{box-sizing:content-box;height:0}
pre{overflow:auto}
code,kbd,pre,samp{font-family:monospace,monospace;font-size:1em}
button,input,optgroup,select,textarea{color:inherit;font:inherit;margin:0}
button{overflow:visible}
button,select{text-transform:none}
button,html input[type="button"],input[type="reset"],input[type="submit"]
{-webkit-appearance:button;cursor:pointer}
button[disabled],html input[disabled]{cursor:default}
button::-moz-focus-inner,input::-moz-focus-inner{border:0;padding:0}
input{line-height:normal}
input[type="checkbox"],input[type="radio"]{box-sizing:border-box;padding:0}
input[type="number"]::-webkit-inner-spin-button,
input[type="number"]::-webkit-outer-spin-button{height:auto}
input[type="search"]{-webkit-appearance:textfield;box-sizing:content-box}
input[type="search"]::-webkit-search-cancel-button,
input[type="search"]::-webkit-search-decoration{-webkit-appearance:none}
fieldset{border:1px solid #c0c0c0;margin:0 2px;padding:.35em .625em .75em}
legend{border:0;padding:0}
textarea{overflow:auto}
optgroup{font-weight:bold}
table{border-collapse:collapse;border-spacing:0}
td,th{padding:0}
"""

reprs = json.dumps


def modname_startswith(x, y):
    return (x + '.').startswith(y + '.')


# Use the system PRNG for session id generation (if possible)
# NOTE: secure random string generation implementation is adapted
#       from the Django project. 

def get_random_string(length=24, allowed_chars=None):
    """ Produce a securely generated random string.
    
    With a length of 12 with the a-z, A-Z, 0-9 character set returns
    a 71-bit value. log_2((26+26+10)^12) =~ 71 bits
    """
    allowed_chars = allowed_chars or ('abcdefghijklmnopqrstuvwxyz' +
                                      'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
    try:
        srandom = random.SystemRandom()
    except NotImplementedError:  # pragma: no cover
        srandom = random
        logger.warn('Falling back to less secure Mersenne Twister random string.')
        bogus = "%s%s%s" % (random.getstate(), time.time(), 'sdkhfbsdkfbsdbhf')
        random.seed(hashlib.sha256(bogus.encode()).digest())

    return ''.join(srandom.choice(allowed_chars) for i in range(length))


def export_assets_and_data(assets, data, dirname, app_id, clear=False):
    """ Export the given assets (list of Asset objects) and data (list of
    (name, value) tuples to a file system structure.
    """
    # Normalize and check - we create the dir if its inside an existing dir
    dirname = os.path.abspath(os.path.expanduser(dirname))
    if clear and os.path.isdir(dirname):
        shutil.rmtree(dirname)
    if not os.path.isdir(dirname):
        if os.path.isdir(os.path.dirname(dirname)):
            os.mkdir(dirname)
        else:
            raise ValueError('dirname %r for export is not a directory.' % dirname)
    
    # Export all assets
    for asset in assets:
        filename = os.path.join(dirname, '_assets', app_id, asset.name)
        dname = os.path.dirname(filename)
        if not os.path.isdir(dname):
            os.makedirs(dname)
        with open(filename, 'wb') as f:
            f.write(asset.to_string().encode())
    
    # Export all data
    for fname, d in data:
        filename = os.path.join(dirname, '_data', app_id, fname)
        dname = os.path.dirname(filename)
        if not os.path.isdir(dname):
            os.makedirs(dname)
        with open(filename, 'wb') as f:
            f.write(d)


# todo: minification ...


# todo: naming


class Asset:
    """ Class to represent an asset (JS or CSS) to be included on the
    page, defined from one more more sources, and which can have
    dependencies on other assets.
    
    Parameters:
        name (str): the asset name, e.g. 'foo.js' or 'bar.css'. Can contain
            slashes to emulate a file system. e.g. 'spam/foo.js'.
        sources (str, list): the sources to generate this asset from.
            Can be strings with source code, string uri's, ``Model``
            subclasses, and (for JS) any PyScript compatible class or function.
        deps (list): names of assets that this asset depends on, used to
            resolve the load order. For module assets one can use
            `'foo.js as foo'` to define the name by which the dependency can be
            accessed inside the module.
        exports (list, str, optional): Should not be given for CSS. If given
            for JS (and not None) the asset is wrapped in an AMD module that
            exports the given name/names. Note that providing an empty list
            is interpreted as "make a module without exported names".
    
    **Remote assets**
    
    If a source is provided as a URI (starting with 'http://', 'https://' or
    'file://') Flexx will (down)load the code to include it in the final asset.
    If only name is provided and it is a URI, it is considered a remote asset,
    i.e. the client will load the asset from elsewhere. Note that
    ``app.export()`` provides control over how remote assets are handled.
    """
    
    def __init__(self, name=None, sources=None, deps=None, exports=None):
        
        # Handle name
        if name is None:
            raise TypeError('Assets name must be given (str).')
        if not isinstance(name, str):
            raise TypeError('Asset name must be str.')
        if not name.lower().endswith(('.js', '.css')):
            raise ValueError('Asset is only for .js and .css assets.')
        self._name = name
        isjs = name.lower().endswith('.js')
        
        # Handle sources
        if sources is None:
            sources = []  # checked below when we know if this is a remote
        if not isinstance(sources, (tuple, list)):
            sources = [sources]
        for source in sources:
            if not (isinstance(source, str) or
                    isinstance(source, types.ModuleType) or
                    (isinstance(source, type) and issubclass(source, Model)) or
                    (isjs and isinstance(ob, pyscript_types))):
                raise TypeError('Asset %r cannot convert source %r to %s.' %
                                (name, source, name.split('.')[-1].upper()))
        self._sources = list(sources)
        
        # Remote source?
        self._remote = None
        uri_starts = 'http://', 'https://', 'file://'
        if name.startswith(uri_starts):
            self._remote = name
            self._name = name.replace('\\', '/').split('/')[-1]
            if len(self._sources):
                raise TypeError('A remote asset cannot have sources: %s' % name)
        elif len(self._sources) == 0:
            raise TypeError('An asset cannot be without sources '
                            '(unless its a remote asset).')
        
        # Handle deps
        if deps is None and self._remote:
            deps = []
        if deps is None:
            raise TypeError('Assets deps must be given, '
                             'use empty list if it has no dependencies.')
        if not isinstance(deps, (tuple, list)):
            raise TypeError('Asset deps must be a tuple/list.')
        if not all(isinstance(dep, str) for dep in deps):
            raise TypeError('Asset deps\' elements must be str.')
        # Handler "as" in deps
        self._deps = [d.split(' as ')[0] for d in deps]
        self._imports = [d for d in deps if ' as ' in d]
        
        # Handle exports
        self._need_pyscript_std = False
        if not isjs:
            if exports is not None:
                raise TypeError('Assets exports must *not* be given for CSS assets.')
            exports = None
        if exports is None:
            self._exports = None
        elif isinstance(exports, str):
            self._exports = str(exports)
        elif isinstance(exports, (tuple, list)):
            if not all(isinstance(export, str) for export in exports):
                raise TypeError('Asset exports\' elements must be str.')
            self._exports = list(exports)
        else:
            raise TypeError('Asset exports must be a None or list.')
        
        # Cache -> total code is generated just once
        self._cache = None
        if not self._remote:
            self.to_string()  # Generate code now
    
    def __repr__(self):
        return '<%s %r at 0x%0x>' % (self.__class__.__name__, self._name, id(self))
        
    @property
    def name(self):
        """ The (file) name of this asset.
        """
        return self._name
    
    @property
    def remote(self):
        """ If the asset is remote (client will load it from elsewhere), then 
        this is the corresponding URI. Otherwise this is None.
        """
        return self._remote
    
    @property
    def is_module(self):
        """ Whether this asset is wrapped inside an AMD JS module.
        """
        return self._exports is not None
    
    @property
    def deps(self):
        """ The list of dependencies for this JS/CSS asset.
        """
        return tuple(self._deps)
    
    @property
    def sources(self):
        """ The list of sources. Each source can be:
        
        * A string of JS/CSS code.
        * A filename or URI (start with "file://", "http://" or "https://"):
          the code is (down)loaded on the server.
        * A subclass of ``Model``: the corresponding JS/CSS is extracted.
        * Any other Python function ot class: JS is generated via PyScript.
        * A Python module is converted to JS as a whole.
        """
        return tuple(self._sources)
    
    @property
    def exports(self):
        """ None if the asset is not a module asset. If it is, ``exports``
        is a str or list of names that this JS module should export. Is
        auto-populated with the names of classes provided in the code list.
        """
        if isinstance(self._exports, list):
            return tuple(self._exports)
        return self._exports  # str or None

    def to_html(self, path='{}', link=2):
        """ Get HTML element tag to include in the document.
        
        Parameters:
            path (str): the path of this asset, in which '{}' can be used as
                a placeholder for the asset name.
            link (int): whether to link to this asset. If 0, the asset is
                embedded. If 1, the asset is linked (and served by our server
                as a separate file). If 2 (default) remote assets remain remote.
        """
        path = path.replace('{}', self.name)
        
        if self.name.lower().endswith('.js'):
            if not link:
                return "<script id='%s'>%s</script>" % (self.name, self.to_string())
            elif link >= 2 and self._remote:
                return "<script src='%s' id='%s'></script>" % (self._remote, self.name)
            else:
                return "<script src='%s' id='%s'></script>" % (path, self.name)
        elif self.name.lower().endswith('.css'):
            if not link:
                return "<style id='%s'>%s</style>" % (self.name, self.to_string())
            elif link >= 2 and self._remote:
                t = "<link rel='stylesheet' type='text/css' href='%s' id='%s' />"
                return t % (self._remote, self.name)
            else:
                t = "<link rel='stylesheet' type='text/css' href='%s' id='%s' />"
                return t % (path, self.name)
        else:  # pragma: no cover
            raise NameError('Assets must be .js or .css')
    
    def to_string(self):
        """ Get the string code provided by this asset. This is what
        gets served to the client.
        """
        if self._cache is None:
            if self.is_module:
                # Create JS module, but also take care of inserting PyScript std.
                lib = 'pyscript-std.js'
                code, names = self._get_code_and_names()
                if names and isinstance(self._exports, list):
                    self._exports.extend(names)
                if self._need_pyscript_std and lib not in self._imports:
                    self._imports.append(lib)
                if lib in self._imports:
                    self._imports[self._imports.index(lib)] = lib + ' as _py'
                    func_names, method_names = get_all_std_names()
                    pre1 = ', '.join(['%s = _py.%s' % (n, n) for n in func_names])
                    pre2 = ', '.join(['%s = _py.%s' % (n, n) for n in method_names])
                    code.insert(0, 'var %s;\nvar %s;' % (pre1, pre2))
                self._cache = create_js_module(self.name, '\n\n'.join(code), 
                                            self._imports, self._exports, 'amd-flexx')
            elif self.remote:
                self._cache = self._handle_uri(self.remote)
            else:
                code, names = self._get_code_and_names()
                self._cache = '\n\n'.join(code)
            if self._need_pyscript_std or self.name.startswith('pyscript-std'):
                self._cache = HEADER + '\n\n' + self._cache  # Add license header
        return self._cache
    
    def _get_code_and_names(self):
        """ Get the code and public class/function names of the sources.
        """
        code = []
        names = []
        for ob in self.sources:
            c, names2 = self._convert_to_code(ob)
            if c:
                code.append(c)
                names.extend([n for n in names2 if n and not n.startswith('_')])
        if not (len(code) == 1 and '\n' not in code[0]):
            code.append('')
        return code, names
    
    def _convert_to_code(self, ob):
        """ Convert object to JS/CSS.
        """
        isjs = self.name.lower().endswith('.js')
        names = []
        
        if isinstance(ob, str):
            c = self._handle_uri(ob)
        elif isinstance(ob, type) and issubclass(ob, Model):
            names.append(ob.__name__)
            c = ob.JS.CODE if isjs else ob.CSS
            self._need_pyscript_std = True
        elif isinstance(ob, types.ModuleType):
            fname = ob.__file__
            py = open(fname, 'rb').read().decode()
            try:
                parser = Parser(py, fname, inline_stdlib=False, docstrings=False)
                c = parser.dump()
            except Exception as err:
                raise ValueError('Asset %r cannot convert %r to JS:\n%s' %
                                 (self.name, ob, str(err)))
            names.extend(list(parser.vars))  # todo: only defined vars
            self._need_pyscript_std = True
        elif isjs and isinstance(ob, (type, types.FunctionType)):
            try:
                c = py2js(ob, inline_stdlib=False, docstrings=False)
                self._need_pyscript_std = True
            except Exception as err:
                raise ValueError('Asset %r cannot convert %r to JS:\n%s' %
                                 (self.name, ob, str(err)))
            names.append(ob.__name__)
        else:  # pragma: no cover - this should not happen
            raise ValueError('Asset %r cannot convert object %r to %s.' %
                             (self.name, ob, self.name.split('.')[-1].upper()))
        return c.strip(), names
    
    def _handle_uri(self, s):
        if s.startswith(('http://', 'https://')):
            return urlopen(s, timeout=5.0).read().decode()
        elif s.startswith('file://'):
            fname = s.split('//', 1)[1]
            if sys.platform.startswith('win'):
                fname = fname.lstrip('/')
            return open(fname, 'rb').read().decode()
        else:
            return s


class AssetStore:
    """
    Provider of shared assets (CSS, JavaScript) and data (images, etc.).
    The global asset store object can be found at ``flexx.app.assets``.
    Assets and data in the asset store can be used by all sessions.
    Each session object also keeps track of assets and data. Using
    ``session.add_asset(str_name)`` makes a session use a shared asset.
    """
    
    def __init__(self):
        self._modules = dict()
        self._assets = OrderedDict()
        self._data = {}
        self.add_shared_asset(Asset('reset.css', RESET, []))
        self.add_shared_asset(Asset('flexx-loader.js', LOADER, [], None))
        func_names, method_names = get_all_std_names()
        self.add_shared_asset(Asset('pyscript-std.js', get_full_std_lib(), [],
                                    func_names + method_names))
    
    def __repr__(self):
        names1 = ', '.join([repr(name) for name in self._assets])
        names2 = ', '.join([repr(name) for name in self._data])
        return '<AssetStore with assets: %s, and data %s>' % (names1, names2)
    
    def create_module_assets(self, *args, **kwargs):
        # Backward compatibility
        raise RuntimeError('create_module_assets is deprecated. Use '
                           'get_module_classes() plus add_shared_asset() instead.')
    
    
    @property
    def modules(self):
        """ The JS modules known to the asset store. Each module corresponds to
        a Python module.
        """
        return self._modules
    
    def collect_modules(self):
        """ Collect JS modules corresponding to Python modules that define
        Model classes. It is safe (and pretty fast) to call this more than once
        since only missing modules are added.
        """
        # todo: where to test/skip __main__??
        new_modules = []
        for cls in Model.CLASSES:
            if cls.__module__ not in self._modules:
                module = Module(sys.modules[cls.__module__])
                self._modules[cls.__module__] = module
                new_modules.append(module)
        logger.info('Asset store collected %i modules.' % len(new_modules))
        
        # Make asset for the modules
        # todo: optionally bundle the assets
        for mod in self._modules.values():
            if( mod.name + '.js') not in self._assets:
                self.add_shared_asset(name=mod.name + '.js', sources=mod.get_js(), deps=[])
                self.add_shared_asset(name=mod.name + '.css', sources=mod.get_css(), deps=[])
    
    def get_asset(self, name):
        """ Get the asset instance corresponding to the given name or None
        if it not known.
        """
        if not name.lower().endswith(('.js', '.css')):
            raise ValueError('Asset names always end in .js or .css')
        return self._assets.get(name, None)
    
    def get_data(self, name):
        """ Get the data (as bytes) corresponding to the given name or None
        if it not known.
        """
        return self._data.get(name, None)
    
    def get_asset_names(self):
        """ Get a list of all asset names.
        """
        return list(self._assets.keys())
    
    def get_data_names(self):
        """ Get a list of all data names.
        """
        return list(self._data.keys())
    
    def add_shared_data(self, name, data):
        """ Add data to serve to the client (e.g. images), which is shared
        between sessions. It is an error to add data with a name that
        is already registered. Returns the link at which the data can
        be retrieved. See ``Session.add_data()`` to set data per-session.
        
        Parameters:
            name (str): the name of the data, e.g. 'icon.png'. 
            data (bytes): the data blob. Can also be a uri to the blob
                (string starting with "file://", "http://" or "https://").
          the code is (down)loaded on the server.
        """
        if not isinstance(name, str):
            raise TypeError('add_shared_data() name must be a str.')
        if name in self._data:
            raise ValueError('add_shared_data() got existing name %r.' % name)
        if isinstance(data, str):
            if data.startswith('file://'):
                data = open(data.split('//', 1)[1], 'rb').read()
            elif data.startswith(('http://', 'https://')):
                data = urlopen(data, timeout=5.0).read()
        if not isinstance(data, bytes):
            raise TypeError('add_shared_data() data must be bytes.')
        self._data[name] = data
        return '_data/shared/%s' % name  # relative path so it works /w export
    
    def add_shared_asset(self, asset=None, **kwargs):
        """ Add a JS/CSS asset to share between sessions. It is an error
        to add an asset with a name that is already registered. See
        ``Session.add_asset()`` to set assets per-session.
        
        The asset should be given either as an asset instance, or as keyword
        arguments to create a new ``Asset``.
        See :class:`Asset class <flexx.app.Asset>` for details.
        """
        if kwargs and asset is not None:
            raise TypeError('add_shared_asset() needs either asset or kwargs.')
        elif asset is not None:
            if not isinstance(asset, Asset):
                raise TypeError('add_shared_asset() asset arg must be an Asset.')
            if asset.name in self._assets:
                raise ValueError('add_shared_asset() %r is already set.' % asset.name)
            self._assets[asset.name] = asset
        elif kwargs:
            self.add_shared_asset(Asset(**kwargs))
        else:
            raise TypeError('add_shared_asset() needs asset or kwargs.')
    
    def get_asset_for_class(self, cls):
        """ Get the asset that provides the given Python class, or None
        """
        for asset in self._assets.values():
            if cls in asset.sources:
                return asset
        return None
    
    def get_module_classes(self, module_name):
        """ Get the Model classes corrsesponding to the given module name
        and that are not already provided by an asset.
        """
        classes = list()
        for cls in get_model_classes():
            if modname_startswith(cls.__module__, module_name):
                if self.get_asset_for_class(cls) is None:
                    classes.append(cls)
        return classes
    
    def export(self, dirname, clear=False):
        """ Write all shared assets and data to the given directory.
        
        Parameters:
            dirname (str): the directory to export to. The toplevel
                directory is created if necessary.
            clear (bool): if given and True, the directory is first cleared.
        """
        assets = [self.get_asset(name) for name in self.get_asset_names()]
        data = [(name, self.get_data(name)) for name in self.get_data_names()]
        export_assets_and_data(assets, data, dirname, 'shared', clear)
        logger.info('Exported shared assets and data to %r.' % dirname)


# Our singleton asset store
assets = AssetStore()


class SessionAssets:
    """ Provider for assets of a specific session. Inherited by Session.
    
    Assets included on the document consist of the page assets
    registered on the session, plus the (shared) page assets that these
    depend on.
    """
    
    def __init__(self, store=None):  # Allow custom store for testing
        self._store = store if (store is not None) else assets
        assert isinstance(self._store, AssetStore)
        
        self._id = get_random_string()
        self._app_name = ''
        
        # Keep track of all assets for this session. Assets that are provided
        # by the asset store have a value of None.
        self._modules = OrderedDict()
        self._assets = OrderedDict()
        # Data for this session (in addition to the data provided by the store)
        self._data = {}
        # Whether the page has been served already
        self._served = False
        # Cache what classes we know (for performance)
        self._known_classes = set()
        # Model classes that are not in an asset/module
        self._extra_model_classes = []
    
    @property
    def id(self):
        """ The unique identifier of this session.
        """
        return self._id
    
    def get_asset_names(self):
        """ Get a list of names of the assets used by this session, in
        the order that they were added.
        """
        return list(self._assets.keys())  # Note: order matters
    
    def get_data_names(self):
        """ Get a list of names of the data provided by this session, in
        the order that they were added.
        """
        return list(self._data.keys())  # Note: order matters
    
    def _inject_asset_dynamically(self, asset):
        """ Load an asset in a running session.
        This method assumes that this is a Session class.
        """
        logger.debug('Dynamically loading asset %r' % asset.name)
        
        # In notebook?
        from .session import manager  # noqa - avoid circular import
        is_interactive = self is manager.get_default_session()  # e.g. in notebook
        in_notebook = is_interactive and getattr(self, 'init_notebook_done', False)
        
        if in_notebook:
            # Load using IPython constructs
            from IPython.display import display, HTML
            if asset.name.lower().endswith('.js'):
                display(HTML("<script>%s</script>" % asset.to_string()))
            else:
                display(HTML("<style>%s</style>" % asset.to_string()))
        else:
            # Load using Flexx construct (using Session._send_command())
            suffix = asset.name.split('.')[-1].upper()
            self._send_command('DEFINE-%s %s' % (suffix, asset.to_string()))
    
    def get_asset(self, name):
        """ Get the asset corresponding to the given name. This can be
        an asset local to the session, or a global asset that this session
        is using. Returns None if asset by that name is unknown.
        """
        if not name.lower().endswith(('.js', '.css')):
            raise ValueError('Asset names always end in .js or .css')
        asset = self._assets.get(name, None)
        if asset is None:
            asset = self._store.get_asset(name)
        return asset
    
    def get_data(self, name):
        """ Get the data corresponding to the given name. This can be
        data local to the session, or global data. Returns None if data
        by that name is unknown.
        """
        data = self._data.get(name, None)
        if data is None:
            data = self._store.get_data(name)
        return data
    
    def add_asset(self, asset=None, **kwargs):  # -> asset must already exist
        """ Use the given JS/CSS asset in this session. It is safe to
        call this method with an already registered asset. See
        ``app.assets.add_shared_asset()`` to define shared assets.
        
        The asset should be given either as an asset instance, the name of
        an asset in the asset store, or as keyword arguments to create
        a new ``Asset``. See :class:`Asset class <flexx.app.Asset>` for details.
        """
        if kwargs and asset is not None:
            raise TypeError('Session.add_asset() needs either asset or kwargs.')
        elif asset is not None:
            # Get the actual asset instance
            if isinstance(asset, str):
                asset = self._store.get_asset(asset)
                if asset is None:
                    raise ValueError('Session.add_asset() got unknown asset name.')
            elif not isinstance(asset, Asset):
                raise TypeError('Session.add_asset() needs str, Asset or kwargs.')
            self._register_asset(asset)
        elif kwargs:
            self.add_asset(Asset(**kwargs))
        else:
            raise TypeError('Session.add_asset() needs asset or kwargs.')
    
    def _register_asset(self, asset):
        """ Register an asset and also try to resolve dependencies.
        """
        # Early exit?
        if asset.name in self._assets:
            cur_asset = self._assets[asset.name]
            if asset.remote:  # Remote assets can be overridden
                self._assets[asset.name] = asset
            elif not (cur_asset is None or cur_asset is asset):
                raise ValueError('Cannot register asset under an existing asset name.')
            return
        # Register / load the asset. Also try to resolve dependencies.
        # In dynamic case we load dependency first. In other cases we
        # do not as to avoid recursion with circular deps.
        if self._served:
            self._collect_dependencies(asset, True)
            self._inject_asset_dynamically(asset)
        elif asset is self._store.get_asset(asset.name):
            self._assets[asset.name] = None  # None means that asset is global
            self._collect_dependencies(asset)
        else:
            self._assets[asset.name] = asset
            self._collect_dependencies(asset)
    
    def _collect_dependencies(self, asset, warn=False):
        """ Register dependencies of the given asset.
        """
        for dep in asset.deps:
            if dep not in self._assets:
                if dep in self._store._assets:
                    self._register_asset(self._store._assets[dep])
                elif warn:
                    logger.warn('Asset %r has unfulfilled dependency %r' %
                                (asset.name, dep))
    
    def add_data(self, name, data):  # todo: add option to clear data after its loaded?
        """ Add data to serve to the client (e.g. images), specific to this
        session. Returns the link at which the data can be retrieved.
        See ``app.assets.add_shared_data()`` to provide shared data.
        
        Parameters:
            name (str): the name of the data, e.g. 'icon.png'. If data has
                already been set on this name, it is overwritten.
            data (bytes): the data blob. Can also be a uri to the blob
                (string starting with "file://", "http://" or "https://").
        """
        if not isinstance(name, str):
            raise TypeError('Session.add_data() name must be a str.')
        if name in self._data:
            raise ValueError('Session.add_data() got existing name %r.' % name)
        if isinstance(data, str):
            if data.startswith('file://'):
                data = open(data.split('//', 1)[1], 'rb').read()
            elif data.startswith(('http://', 'https://')):
                data = urlopen(data, timeout=5.0).read()
        if not isinstance(data, bytes):
            raise TypeError('Session.add_data() data must be a bytes.')
        self._data[name] = data
        return '_data/%s/%s' % (self.id, name)  # relative path so it works /w export
    
    def register_model_class(self, cls):
        """ Ensure that the client knows the given class. A class can
        already be defined via a module asset, or we can add it to a
        pending list if the page has not been served yet. Otherwise it
        needs to be defined dynamically.
        """
        if not (isinstance(cls, type) and issubclass(cls, Model)):
            raise ValueError('Not a Model class')
        
        # Early exit if we know the class already
        if cls in self._known_classes:
            return
        
        # Make sure the base classes are registered first
        for cls2 in cls.mro()[1:]:
            if not issubclass(cls2, Model):  # True if cls2 is *the* Model class
                break
            if cls2 not in self._known_classes:
                self.register_model_class(cls2)
        
        # We might need to collect
        # todo: check __main__?
        mod_name = cls.__module__
        if mod_name not in self._store.modules:
            self._store.collect_modules()
        
        # Make sure that no two models have the same name, or we get problems
        # that are difficult to debug. Unless classes are defined in the notebook.
        same_name = [c for c in self._known_classes if c.__name__ == cls.__name__]
        if same_name:
            from .session import manager  # noqa - avoid circular import
            same_name.append(cls)
            is_interactive = self is manager.get_default_session()  # e.g. in notebook
            is_dynamic_cls = all([c.__module__ == '__main__' for c in same_name])  # todo: correct?
            if not (is_interactive and is_dynamic_cls):
                raise RuntimeError('Cannot have multiple Model classes with the same '
                                   'name unless using interactive session and the '
                                   'classes are dynamically defined: %r' % same_name)
        
        logger.debug('Registering Model class %r' % cls.__name__)
        self._known_classes.add(cls)  # todo: rename to used_classes
        
        
        js_module = self._store.modules.get(mod_name, None)
        
        if js_module is not None:
            # cls is present in a module, add corresponding asset (overwrite ok)
            if mod_name not in self._modules:
                if self._served:
                    # todo: convert a module to an asset
                    self._register_asset(asset)
                else:
                    self._modules[mod_name] = js_module  # todo: None or js_module?
        elif not self._served:
            # Remember cls, will be served in session-specific asset
            self._extra_model_classes.append(cls)
        else:
            # Define class dynamically via a single-class asset
            # todo: convert a class to an asset
            for asset in [Asset(cls.__name__ + '.js', [cls], [], []), 
                          Asset(cls.__name__ + '.css', [cls], [])]:
                if asset.to_string().strip():
                    self._register_asset(asset)
        # 
        # ##
        # 
        # # Make sure the base classes are registered first
        # for cls2 in cls.mro()[1:]:
        #     if not issubclass(cls2, Model):  # True if cls2 is *the* Model class
        #         break
        #     if cls2 not in self._known_classes:
        #         self.register_model_class(cls2)
        # 
        # # Make sure that no two models have the same name, or we get problems
        # # that are difficult to debug. Unless classes are defined in the notebook.
        # same_name = [c for c in self._known_classes if c.__name__ == cls.__name__]
        # if same_name:
        #     from .session import manager  # noqa - avoid circular import
        #     same_name.append(cls)
        #     is_interactive = self is manager.get_default_session()  # e.g. in notebook
        #     is_dynamic_cls = not any([self._store.get_asset_for_class(c)
        #                               for c in same_name])
        #     if not (is_interactive and is_dynamic_cls):
        #         raise RuntimeError('Cannot have multiple Model classes with the same '
        #                            'name unless using interactive session and the '
        #                            'classes are dynamically defined: %r' % same_name)
        # 
        # logger.debug('Registering Model class %r' % cls.__name__)
        # self._known_classes.add(cls)
        # 
        # # Check if cls is covered by our assets
        # asset_js = self._store.get_asset_for_class(cls)
        # asset_css = None
        # if asset_js:
        #     asset_css = self._store.get_asset(asset_js.name[:-2] + 'css')
        # 
        # if asset_js:
        #     # cls is present in a module, add corresponding asset (overwrite ok)
        #     for asset in [asset_js, asset_css]:
        #         if asset and asset.name not in self._assets:
        #             if self._served:
        #                 self._register_asset(asset)
        #             else:
        #                 self._assets[asset.name] = None
        # elif not self._served:
        #     # Remember cls, will be served in the index
        #     self._extra_model_classes.append(cls)
        # else:
        #     # Define class dynamically via a single-class asset
        #     for asset in [Asset(cls.__name__ + '.js', [cls], [], []), 
        #                   Asset(cls.__name__ + '.css', [cls], [])]:
        #         if asset.to_string().strip():
        #             self._register_asset(asset)
    
    def get_assets_in_order(self, css_reset=False):
        """ Get two lists containing the JS assets and CSS assets,
        respectively. The assets contain all assets in use and their
        dependencies. The order is based on the dependency resolution
        and the order in which assets were registered via
        ``add_asset()``. Special assets are added, such as the CSS reset,
        the JS loader, and CSS and JS for classes not defined in a module.
        
        When this function gets called, it is assumed that the assets have
        been served and that future asset loads should be done dynamically.
        """
        
        def sort_modules(modules):
            module_names = [mod.name for mod in modules]
            for index in range(len(module_names)):
                seen_names = []
                while True:
                    # Get module name on this position, check if its new
                    name = module_names[index]
                    if name in seen_names:
                        raise RuntimeError('Detected circular dependency in modules!')
                    seen_names.append(name)
                    # Move deps in front of us if necessary
                    for dep in self._modules[name].deps:
                        if dep not in self._modules:  # todo: self._store.modules?
                            logger.warn('Module %r has unfulfilled dependency %r' %
                                        (name, dep))
                        else:
                            j = module_names.index(dep)
                            if j > index:
                                module_names.insert(index, module_names.pop(j))
                                break  # do this index again; the dep we just moved
                    else:
                        break  # no changes, move to next index
            return [self._modules[name] for name in module_names]
        
        # Put modules in correct load order
        modules = sorted(self._modules.values(), key=lambda x:x.name)
        modules = sort_modules(modules)
        
        # Create assets from modules and their dependencies
        # todo: what about pyscript module deps?
        js_assets = []
        css_assets = []
        for mod in modules:
            for asset in mod.asset_deps:
                if asset.name.lower().endswith('.js'):
                    if asset not in js_assets:
                        js_assets.append(asset)
                else:
                    if asset not in css_assets:
                        css_assets.append(asset)
            js_assets.append(self._store.get_asset(mod.name + '.js'))
            css_assets.append(self._store.get_asset(mod.name + '.css'))
        
        # Append code for extra classes
        if self._extra_model_classes and 'extra-classes.js' not in self._assets:
            self.add_asset(Asset('extra-classes.js', self._extra_model_classes,
                                 deps=[], exports=[]))
            self.add_asset(Asset('extra-classes.css', self._extra_model_classes, []))
        
        # Add assets specific to this session
        for asset in self._assets:
            if asset.name.lower().endswith('.js'):
                js_assets.append(asset)
            else:
                css_assets.append(asset)
        
        # # Do a round of collecting deps
        # for name in self.get_asset_names():
        #     self._collect_dependencies(self.get_asset(name))
        # 
        # # Collect initial assets for this session, per JS/CSS
        # js_assets, css_assets = OrderedDict(), OrderedDict()
        # for name in self._assets.keys():
        #     asset = self.get_asset(name)
        #     if asset.name.lower().endswith('.js'):
        #         js_assets[asset.name] = asset
        #     else:
        #         css_assets[asset.name] = asset
        # 
        # # Flatten the trees into flat lists
        # js_assets2 = list(js_assets.keys())
        # flatten_tree(js_assets2, js_assets)
        # css_assets2 = list(css_assets.keys())
        # flatten_tree(css_assets2, css_assets)
        # js_assets = [js_assets[name] for name in js_assets2]
        # css_assets = [css_assets[name] for name in css_assets2]
        
        # Prepend reset.css
        if css_reset:
            css_assets.insert(0, self.get_asset('reset.css'))
        
        # Prepend loader
        js_assets.insert(0, self.get_asset('flexx-loader.js'))
        t = 'var flexx = {app_name: "%s", session_id: "%s"};' % (self._app_name,
                                                                 self.id)
        js_assets.insert(0, Asset('embed/flexx-init.js', t, []))
        
        # Mark this session as served; all future asset loads are dynamic
        self._served = True
        
        # todo: fix incorrect order; loader should be able to handle it for JS
        #import random
        #random.shuffle(js_assets)
        
        return js_assets, css_assets
    
    def get_page(self, link=2):
        """ Get the string for the HTML page to render this session's app.
        """
        js_assets, css_assets = self.get_assets_in_order(True)
        for asset in js_assets + css_assets:
            if asset.remote and asset.remote.startswith('file://'):
                raise RuntimeError('Can only use remote assets with "file://" '
                                   'when exporting.')
        return self._get_page(js_assets, css_assets, link, False)
    
    def get_page_for_export(self, commands, link=0):
        """ Get the string for an exported HTML page (to run without a server).
        """
        # Create lines to init app
        lines = []
        lines.append('flexx.is_exported = true;\n')
        lines.append('flexx.runExportedApp = function () {')
        lines.extend(['    flexx.command(%s);' % reprs(c) for c in commands])
        lines.append('};\n')
        # Create a session asset for it
        self.add_asset(Asset('flexx-export.js', '\n'.join(lines), []))
        
        # Compose
        js_assets, css_assets = self.get_assets_in_order(True)
        return self._get_page(js_assets, css_assets, link, True)
    
    def _get_page(self, js_assets, css_assets, link, export):
        """ Compose index page.
        """
        pre_path = '_assets' if export else '/flexx/assets'
        
        codes = []
        for assets in [css_assets, js_assets]:
            for asset in assets:
                if not link:
                    html = asset.to_html('{}', link)
                else:
                    if asset.name.startswith('embed/'):
                        html = asset.to_html('', 0)
                    elif self._store.get_asset(asset.name) is not asset:
                        html = asset.to_html(pre_path + '/%s/{}' % self.id, link)
                    else:
                        html = asset.to_html(pre_path + '/shared/{}', link)
                codes.append(html)
            codes.append('')  # whitespace between css and js assets
        
        src = INDEX
        if not link:
            asset_names = [a.name for a in css_assets + js_assets]
            toc = '<!-- Contents:\n\n- ' + '\n- '.join(asset_names) + '\n\n-->'
            codes.insert(0, toc)
            src = src.replace('ASSET-HOOK', '\n\n\n'.join(codes))
        else:
            src = src.replace('ASSET-HOOK', '\n'.join(codes))
        
        return src
    
    def _export(self, dirname, clear=False):
        """ Export all assets and data specific to this session.
        Private method, used by app.export().
        """
        # Note that self.id will have been set to the app name.
        assets = [self._assets[name] for name in self.get_asset_names()]
        assets = [asset for asset in assets if asset is not None]
        data = [(name, self.get_data(name)) for name in self.get_data_names()]
        export_assets_and_data(assets, data, dirname, self.id, clear)
        logger.info('Exported assets and data for %r to %r.' % (self.id, dirname))
