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
from .asset import Asset, Bundle, solve_dependencies
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
        self.add_shared_asset('reset.css', RESET)
        self.add_shared_asset('flexx-loader.js', LOADER)
        func_names, method_names = get_all_std_names()
        # todo: wrappyscript-std in a module
        std_mod = create_js_module('pyscript-std.js', get_full_std_lib(),
                                   [], func_names + method_names)
        self.add_shared_asset('pyscript-std.js', HEADER + std_mod)
    
    def __repr__(self):
        t = '<AssetStore with %i assets, and %i data>'
        return t % (len(self._assets), len(self._data))
    
    def create_module_assets(self, *args, **kwargs):
        # Backward compatibility
        raise RuntimeError('create_module_assets is deprecated and no '
                           'longer necessary.')
    
    @property
    def modules(self):
        """ The JSModule objects known to the asset store. Each module
        corresponds to a Python module.
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
                module = JSModule(sys.modules[cls.__module__], self._modules)
                new_modules.append(module)
            self._modules[cls.__module__].use_variable(cls.__name__)
        
        bcount = 0
        for mod in new_modules:
            # Get names of bundles to add this module to
            name = mod.name
            bundle_names = []
            bundle_names.append(name)  # bundle of exactly this one module
            if mod.is_package:
                bundle_names.append(name + '-bundle')
            while '.' in name:
                name = name.rsplit('.', 1)[0]
                bundle_names.append(name + '-bundle')
            bcount += len(bundle_names)
            # Add to bundles, create bundle if necesary
            for name in bundle_names:
                for suffix in ['.js', '.css']:
                    bundle_name = name + suffix
                    if bundle_name not in self._assets:
                        self._assets[bundle_name] = Bundle(bundle_name)
                    self._assets[bundle_name].add_module(mod)
        
        t = 'Asset store collected %i new modules, present in %i bundles.'
        logger.info(t % (len(new_modules), bcount))
    
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
    
    def add_shared_asset(self, name, source=None):
        """ Add a JS/CSS asset to share between sessions. It is an error
        to add an asset with a name that is already registered. See
        ``Session.add_asset()`` to set assets per-session.
        
        Parameters:
            name (str): the asset name, e.g. 'foo.js' or 'bar.css'. Can contain
                slashes to emulate a file system. e.g. 'spam/foo.js'. If this
                is a uri, then this is a "remote" asset (the client loads
                the asset from the uri), and source should not be given.
            source (str): the source code for this asset.
                If this is a uri, the served loads the source from there (the
                client loads the asset from the Flexx server as usual).
        
        Note: a uri is a string starting with 'http://', 'https://' or 'file://'.
        The ``export()`` method provides control over how remote assets are
        handled.
        """
        asset = Asset(name, source)
        if asset.name in self._assets:
            raise ValueError('add_shared_asset() %r is already set.' % asset.name)
        self._assets[asset.name] = asset
    
    # todo: remove?
    def get_asset_for_class(self, cls):
        """ Get the asset that provides the given Python class, or None
        """
        for asset in self._assets.values():
            if cls in asset.sources:
                return asset
        return None
    # todo: remove?
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
    
    def add_asset(self, name, source=None):
        """ Use the given JS/CSS asset in this session. It is safe to
        call this method with an already registered asset. See
        ``app.assets.add_shared_asset()`` to define shared assets.
        
        If no source is given and the given name corresponds to a shared asset,
        that shared asset is used in this session.
        
        Parameters:
            name (str): the asset name, e.g. 'foo.js' or 'bar.css'. Can contain
                slashes to emulate a file system. e.g. 'spam/foo.js'. If this
                is a uri, then this is a "remote" asset (the client loads
                the asset from the uri), and source should not be given.
            source (str): the source code for this asset.
                If this is a uri, the served loads the source from there (the
                client loads the asset from the Flexx server as usual).
        
        Note: a uri is a string starting with 'http://', 'https://' or 'file://'.
        The ``export()`` method provides control over how remote assets are
        handled.
        """
        if source is None:
            uri_starts = 'http://', 'https://', 'file://'
            if name.startswith(uri_starts):
                asset = Asset(name)
            else:
                asset = self._store.get_asset(asset)
                if asset is None:
                    raise ValueError('Session.add_asset() got unknown asset name.')
        else:
            asset = Asset(name, source)
        self._register_asset(asset)
    
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
            # self._collect_dependencies(asset, True)
            self._inject_asset_dynamically(asset)
        elif asset is self._store.get_asset(asset.name):
            self._assets[asset.name] = None  # None means that asset is global
            # self._collect_dependencies(asset)
        else:
            self._assets[asset.name] = asset
            # self._collect_dependencies(asset)
    
    # def _collect_dependencies(self, asset, warn=False):
    #     """ Register dependencies of the given asset.
    #     """
    #     for dep in asset.deps:
    #         if dep not in self._assets:
    #             if dep in self._store._assets:
    #                 self._register_asset(self._store._assets[dep])
    #             elif warn:
    #                 logger.warn('Asset %r has unfulfilled dependency %r' %
    #                             (asset.name, dep))
    
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
            # todo: convert a class to a module and then an asset
            1/0
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
            # todo: to modules and then to bundle
            1/0
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
        js_assets.insert(0, Asset('embed/flexx-init.js', t))
        
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
        self.add_asset('flexx-export.js', '\n'.join(lines))
        
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
