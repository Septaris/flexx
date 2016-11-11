import os
import sys
import types
from urllib.request import urlopen

from . import logger


def solve_dependencies(things, warn_missing=False):
    """ Given a list of things, which each have a ``name`` and ``deps``
    attribute, sort the things to meet dependencies. In-place.
    """
    assert isinstance(things, list)
    names = [thing.name for thing in things]
    thingmap = dict([(n, t) for n, t in zip(names, things)])
    
    for index in range(len(names)):
        seen_names = set()
        while True:
            # Get thing name on this position, check if its new
            name = names[index]
            if name in seen_names:
                raise RuntimeError('Detected circular dependency!')
            seen_names.add(name)
            # Move deps in front of us if necessary
            for dep in thingmap[name].deps:
                if dep not in names:
                    if warn_missing:
                        logger.warn('%r has missing dependency %r' % (name, dep))
                else:
                    j = names.index(dep)
                    if j > index:
                        names.insert(index, names.pop(j))
                        things.insert(index, things.pop(j))
                        break  # do this index again; the dep we just moved
            else:
                break  # no changes, move to next index

# todo: minification ...

class Asset:
    """ Class to represent an asset (JS or CSS) to be included on the page.
    
    Parameters:
        name (str): the asset name, e.g. 'foo.js' or 'bar.css'. Can contain
            slashes to emulate a file system. e.g. 'spam/foo.js'. If this
            is a uri, then this is a "remote" asset (the client loads
            the asset from the uri).
        source (str): the source code for this asset.
            If this is a uri, the served loads the source from there (the
            client loads the asset from the Flexx server as usual).
    
    Note: a uri is a string starting with 'http://', 'https://' or 'file://'.
    The ``app.export()`` method provides control over how remote assets are
    handled.
    
    """
    
    def __init__(self, name, source=None):
        
        # Handle name
        if name is None:
            raise TypeError('Assets name must be given (str).')
        if not isinstance(name, str):
            raise TypeError('Asset name must be str.')
        if not name.lower().endswith(('.js', '.css')):
            raise ValueError('Asset name must end in .js or .css.')
        self._name = name
        
        # Handle source
        if source is None:
            pass
        elif not isinstance(source, str):
            raise TypeError('Asset source must be str.')
        self._source = source
        
        # Remote source?
        self._remote = None
        uri_starts = 'http://', 'https://', 'file://'
        if name.startswith(uri_starts):
            self._remote = name
            self._name = name.replace('\\', '/').split('/')[-1]
            if self._source is not None:
                raise TypeError('Remote assets cannot have a source: %s' % name)
        if source is None:
            raise TypeError('Assets must have a source (except remote assets).')
    
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
    def source(self):
        """ The string source for this asset, or None if its a remote asset.
        """
        return self._source
    
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
            elif link >= 2 and self.remote:
                return "<script src='%s' id='%s'></script>" % (self.remote, self.name)
            else:
                return "<script src='%s' id='%s'></script>" % (path, self.name)
        elif self.name.lower().endswith('.css'):
            if not link:
                return "<style id='%s'>%s</style>" % (self.name, self.to_string())
            elif link >= 2 and self.remote:
                t = "<link rel='stylesheet' type='text/css' href='%s' id='%s' />"
                return t % (self.remote, self.name)
            else:
                t = "<link rel='stylesheet' type='text/css' href='%s' id='%s' />"
                return t % (path, self.name)
        else:  # pragma: no cover
            raise NameError('Assets must be .js or .css')
    
    def to_string(self):
        """ Get the string code for this asset. Even for remote assets.
        """
        if self.remote:
            if not hasattr(self, '_remote_source_cache'):
                uri = self.remote
                if uri.startswith(('http://', 'https://')):
                    source = urlopen(uri, timeout=5.0).read().decode()
                elif uri.startswith('file://'):
                    fname = uri.split('//', 1)[1]
                    if sys.platform.startswith('win'):
                        fname = fname.lstrip('/')
                    source = open(fname, 'rb').read().decode()
                else:
                    assert False  # should not happen
                self._remote_source_cache = source
            return self._remote_source_cache
        else:
            return self._source


class Bundle(Asset):
    """ A bundle is an asset that represents a collection of JSModule objects.
    """
    
    def __init__(self, name):
        super().__init__(name, '')
        self._module_name = name.rsplit('.', 1)[0].split('-')[0]
        self._modules = []
        self._deps = set()
        self._need_sort = False
    
    def __repr__(self):
        return '<%s %r with %i modules at 0x%0x>' % (self.__class__.__name__,
                                                     self._name,
                                                     len(self._modules),
                                                     id(self))
    
    def add_module(self, m):
        """ Add a module to the bundle. """
        
        # Check if module belongs here
        if not m.name.startswith(self._module_name):
            raise ValueError('Module %s does not belong in bundle %s.' %
                             (m.name, self.name))
        
        # Add module
        self._modules.append(m)
        self._need_sort = True
        
        # Add deps for this module
        for dep in m.deps:
            while '.' in dep:
                self._deps.add(dep)
                dep = dep.rsplit('.', 1)[0]
            self._deps.add(dep)
        
        # Clear deps that are represented by this bundle
        for dep in list(self._deps):
            if dep.startswith(self._module_name):
                self._deps.discard(dep)
            elif self._module_name.startswith(dep + '.'):
                self._deps.discard(dep)
    
    # todo: module_names?
    @property
    def module_name(self):
        """ The name of the virtual module that this bundle represents.
        E.g. A bundle "foo.bar-bundle.js" represents "foo.bar.xx" and
        "foo.bar.yy" and has module_name "foo.bar".
        """
        return self._module_name
    
    @property
    def modules(self):
        """ The list of modules, sorted by dependencies.
        """
        if self._need_sort:
            self._modules.sort(key=lambda m: m.name)
            solve_dependencies(self._modules)
        return tuple(self._modules)
    
    @property
    def deps(self):
        """ The set of dependencies for this bundle, expressed in module names.
        """
        return self._deps
    
    def to_string(self):
        # Concatenate module strings
        isjs = self.name.lower().endswith('.js')
        toc = []
        source = []
        for m in self.modules:
            s = m.get_js() if isjs else m.get_css()
            toc.append('- ' + m.name)
            source.append('/* ' + (' %s ' % m.name).center(70, '=') + '*/')
            source.append(s)
        source.insert(0, '/* Bundle contents:\n' + '\n'.join(toc) + '\n*/\n')
        return '\n\n'.join(source)
