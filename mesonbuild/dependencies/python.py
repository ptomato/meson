# Copyright 2022 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import functools, json, os
from pathlib import Path
import typing as T

from .. import mesonlib, mlog
from .base import process_method_kw, DependencyMethods, DependencyTypeName, ExternalDependency, SystemDependency
from .framework import ExtraFrameworkDependency
from .pkgconfig import PkgConfigDependency
from ..environment import detect_cpu_family
from ..programs import ExternalProgram

if T.TYPE_CHECKING:
    from typing_extensions import TypedDict

    from .factory import DependencyGenerator
    from ..environment import Environment
    from ..mesonlib import MachineChoice

    class PythonIntrospectionDict(TypedDict):

        install_paths: T.Dict[str, str]
        is_pypy: bool
        is_venv: bool
        link_libpython: bool
        sysconfig_paths: T.Dict[str, str]
        paths: T.Dict[str, str]
        platform: str
        suffix: str
        variables: T.Dict[str, str]
        version: str

    _Base = ExternalDependency
else:
    _Base = object


class BasicPythonExternalProgram(ExternalProgram):
    def __init__(self, name: str, command: T.Optional[T.List[str]] = None,
                 ext_prog: T.Optional[ExternalProgram] = None):
        if ext_prog is None:
            super().__init__(name, command=command, silent=True)
        else:
            self.name = name
            self.command = ext_prog.command
            self.path = ext_prog.path

        # We want strong key values, so we always populate this with bogus data.
        # Otherwise to make the type checkers happy we'd have to do .get() for
        # everycall, even though we know that the introspection data will be
        # complete
        self.info: 'PythonIntrospectionDict' = {
            'install_paths': {},
            'is_pypy': False,
            'is_venv': False,
            'link_libpython': False,
            'sysconfig_paths': {},
            'paths': {},
            'platform': 'sentinal',
            'suffix': 'sentinel',
            'variables': {},
            'version': '0.0',
        }
        self.pure: bool = True

    def _check_version(self, version: str) -> bool:
        if self.name == 'python2':
            return mesonlib.version_compare(version, '< 3.0')
        elif self.name == 'python3':
            return mesonlib.version_compare(version, '>= 3.0')
        return True

    def sanity(self) -> bool:
        # Sanity check, we expect to have something that at least quacks in tune

        import importlib.resources

        with importlib.resources.path('mesonbuild.scripts', 'python_info.py') as f:
            cmd = self.get_command() + [str(f)]
            p, stdout, stderr = mesonlib.Popen_safe(cmd)

        try:
            info = json.loads(stdout)
        except json.JSONDecodeError:
            info = None
            mlog.debug('Could not introspect Python (%s): exit code %d' % (str(p.args), p.returncode))
            mlog.debug('Program stdout:\n')
            mlog.debug(stdout)
            mlog.debug('Program stderr:\n')
            mlog.debug(stderr)

        if info is not None and self._check_version(info['version']):
            self.info = T.cast('PythonIntrospectionDict', info)
            return True
        else:
            return False


class _PythonDependencyBase(_Base):

    def __init__(self, python_holder: 'BasicPythonExternalProgram', embed: bool):
        self.embed = embed
        self.version: str = python_holder.info['version']
        self.platform = python_holder.info['platform']
        self.variables = python_holder.info['variables']
        self.paths = python_holder.info['paths']
        self.is_pypy = python_holder.info['is_pypy']
        self.link_libpython = python_holder.info['link_libpython']
        self.info: T.Optional[T.Dict[str, str]] = None
        if mesonlib.version_compare(self.version, '>= 3.0'):
            self.major_version = 3
        else:
            self.major_version = 2


class PythonPkgConfigDependency(PkgConfigDependency, _PythonDependencyBase):

    def __init__(self, name: str, environment: 'Environment',
                 kwargs: T.Dict[str, T.Any], installation: 'BasicPythonExternalProgram',
                 libpc: bool = False):
        if libpc:
            mlog.debug(f'Searching for {name!r} via pkgconfig lookup in LIBPC')
        else:
            mlog.debug(f'Searching for {name!r} via fallback pkgconfig lookup in default paths')

        PkgConfigDependency.__init__(self, name, environment, kwargs)
        _PythonDependencyBase.__init__(self, installation, kwargs.get('embed', False))

        if libpc and not self.is_found:
            mlog.debug(f'"python-{self.version}" could not be found in LIBPC, this is likely due to a relocated python installation')

        # The "-embed" version of python.pc was introduced in 3.8, and distutils
        # extension linking was changed to be considered a non embed usage. Before
        # then, this dependency always uses the embed=True file because that is the
        # only one that exists,
        #
        # On macOS and some Linux distros (Debian) distutils doesn't link extensions
        # against libpython, even on 3.7 and below. We call into distutils and
        # mirror its behavior. See https://github.com/mesonbuild/meson/issues/4117
        if not self.embed and not self.link_libpython and mesonlib.version_compare(self.version, '< 3.8'):
            self.link_args = []


class PythonFrameworkDependency(ExtraFrameworkDependency, _PythonDependencyBase):

    def __init__(self, name: str, environment: 'Environment',
                 kwargs: T.Dict[str, T.Any], installation: 'BasicPythonExternalProgram'):
        ExtraFrameworkDependency.__init__(self, name, environment, kwargs)
        _PythonDependencyBase.__init__(self, installation, kwargs.get('embed', False))


class PythonSystemDependency(SystemDependency, _PythonDependencyBase):

    def __init__(self, name: str, environment: 'Environment',
                 kwargs: T.Dict[str, T.Any], installation: 'BasicPythonExternalProgram'):
        SystemDependency.__init__(self, name, environment, kwargs)
        _PythonDependencyBase.__init__(self, installation, kwargs.get('embed', False))

        if mesonlib.is_windows():
            self.find_libpy_windows(environment)
        else:
            self.find_libpy(environment)

    def find_libpy(self, environment: 'Environment') -> None:
        if self.is_pypy:
            if self.major_version == 3:
                libname = 'pypy3-c'
            else:
                libname = 'pypy-c'
            libdir = os.path.join(self.variables.get('base'), 'bin')
            libdirs = [libdir]
        else:
            libname = f'python{self.version}'
            if 'DEBUG_EXT' in self.variables:
                libname += self.variables['DEBUG_EXT']
            if 'ABIFLAGS' in self.variables:
                libname += self.variables['ABIFLAGS']
            libdirs = []

        largs = self.clib_compiler.find_library(libname, environment, libdirs)
        if largs is not None:
            self.link_args = largs

        self.is_found = largs is not None or not self.link_libpython

        inc_paths = mesonlib.OrderedSet([
            self.variables.get('INCLUDEPY'),
            self.paths.get('include'),
            self.paths.get('platinclude')])

        self.compile_args += ['-I' + path for path in inc_paths if path]

    def get_windows_python_arch(self) -> T.Optional[str]:
        if self.platform == 'mingw':
            pycc = self.variables.get('CC')
            if pycc.startswith('x86_64'):
                return '64'
            elif pycc.startswith(('i686', 'i386')):
                return '32'
            else:
                mlog.log(f'MinGW Python built with unknown CC {pycc!r}, please file a bug')
                return None
        elif self.platform == 'win32':
            return '32'
        elif self.platform in {'win64', 'win-amd64'}:
            return '64'
        mlog.log(f'Unknown Windows Python platform {self.platform!r}')
        return None

    def get_windows_link_args(self) -> T.Optional[T.List[str]]:
        if self.platform.startswith('win'):
            vernum = self.variables.get('py_version_nodot')
            verdot = self.variables.get('py_version_short')
            imp_lower = self.variables.get('implementation_lower', 'python')
            if self.static:
                libpath = Path('libs') / f'libpython{vernum}.a'
            else:
                comp = self.get_compiler()
                if comp.id == "gcc":
                    if imp_lower == 'pypy' and verdot == '3.8':
                        # The naming changed between 3.8 and 3.9
                        libpath = Path('libpypy3-c.dll')
                    elif imp_lower == 'pypy':
                        libpath = Path(f'libpypy{verdot}-c.dll')
                    else:
                        libpath = Path(f'python{vernum}.dll')
                else:
                    libpath = Path('libs') / f'python{vernum}.lib'
            # base_prefix to allow for virtualenvs.
            lib = Path(self.variables.get('base_prefix')) / libpath
        elif self.platform == 'mingw':
            if self.static:
                libname = self.variables.get('LIBRARY')
            else:
                libname = self.variables.get('LDLIBRARY')
            lib = Path(self.variables.get('LIBDIR')) / libname
        else:
            raise mesonlib.MesonBugException(
                'On a Windows path, but the OS doesn\'t appear to be Windows or MinGW.')
        if not lib.exists():
            mlog.log('Could not find Python3 library {!r}'.format(str(lib)))
            return None
        return [str(lib)]

    def find_libpy_windows(self, env: 'Environment') -> None:
        '''
        Find python3 libraries on Windows and also verify that the arch matches
        what we are building for.
        '''
        pyarch = self.get_windows_python_arch()
        if pyarch is None:
            self.is_found = False
            return
        arch = detect_cpu_family(env.coredata.compilers.host)
        if arch == 'x86':
            arch = '32'
        elif arch == 'x86_64':
            arch = '64'
        else:
            # We can't cross-compile Python 3 dependencies on Windows yet
            mlog.log(f'Unknown architecture {arch!r} for',
                     mlog.bold(self.name))
            self.is_found = False
            return
        # Pyarch ends in '32' or '64'
        if arch != pyarch:
            mlog.log('Need', mlog.bold(self.name), f'for {arch}-bit, but found {pyarch}-bit')
            self.is_found = False
            return
        # This can fail if the library is not found
        largs = self.get_windows_link_args()
        if largs is None:
            self.is_found = False
            return
        self.link_args = largs
        # Compile args
        inc_paths = mesonlib.OrderedSet([
            self.variables.get('INCLUDEPY'),
            self.paths.get('include'),
            self.paths.get('platinclude')])

        self.compile_args += ['-I' + path for path in inc_paths if path]

        # https://sourceforge.net/p/mingw-w64/mailman/message/30504611/
        if pyarch == '64' and self.major_version == 2:
            self.compile_args += ['-DMS_WIN64']

        self.is_found = True

    @staticmethod
    def log_tried() -> str:
        return 'sysconfig'

def python_factory(env: 'Environment', for_machine: 'MachineChoice',
                   kwargs: T.Dict[str, T.Any],
                   installation: T.Optional['BasicPythonExternalProgram'] = None) -> T.List['DependencyGenerator']:
    # We can't use the factory_methods decorator here, as we need to pass the
    # extra installation argument
    methods = process_method_kw({DependencyMethods.PKGCONFIG, DependencyMethods.SYSTEM}, kwargs)
    embed = kwargs.get('embed', False)
    candidates: T.List['DependencyGenerator'] = []
    from_installation = installation is not None
    # When not invoked through the python module, default installation.
    if installation is None:
        installation = BasicPythonExternalProgram('python3', mesonlib.python_command)
        installation.sanity()
    pkg_version = installation.info['variables'].get('LDVERSION') or installation.info['version']

    if DependencyMethods.PKGCONFIG in methods:
        if from_installation:
            pkg_libdir = installation.info['variables'].get('LIBPC')
            pkg_embed = '-embed' if embed and mesonlib.version_compare(installation.info['version'], '>=3.8') else ''
            pkg_name = f'python-{pkg_version}{pkg_embed}'

            # If python-X.Y.pc exists in LIBPC, we will try to use it
            def wrap_in_pythons_pc_dir(name: str, env: 'Environment', kwargs: T.Dict[str, T.Any],
                                       installation: 'BasicPythonExternalProgram') -> 'ExternalDependency':
                if not pkg_libdir:
                    # there is no LIBPC, so we can't search in it
                    empty = ExternalDependency(DependencyTypeName('pkgconfig'), env, {})
                    empty.name = 'python'
                    return empty

                old_pkg_libdir = os.environ.pop('PKG_CONFIG_LIBDIR', None)
                old_pkg_path = os.environ.pop('PKG_CONFIG_PATH', None)
                os.environ['PKG_CONFIG_LIBDIR'] = pkg_libdir
                try:
                    return PythonPkgConfigDependency(name, env, kwargs, installation, True)
                finally:
                    def set_env(name: str, value: str) -> None:
                        if value is not None:
                            os.environ[name] = value
                        elif name in os.environ:
                            del os.environ[name]
                    set_env('PKG_CONFIG_LIBDIR', old_pkg_libdir)
                    set_env('PKG_CONFIG_PATH', old_pkg_path)

            candidates.append(functools.partial(wrap_in_pythons_pc_dir, pkg_name, env, kwargs, installation))
            # We only need to check both, if a python install has a LIBPC. It might point to the wrong location,
            # e.g. relocated / cross compilation, but the presence of LIBPC indicates we should definitely look for something.
            if pkg_libdir is not None:
                candidates.append(functools.partial(PythonPkgConfigDependency, pkg_name, env, kwargs, installation))
        else:
            candidates.append(functools.partial(PkgConfigDependency, 'python3', env, kwargs))

    if DependencyMethods.SYSTEM in methods:
        candidates.append(functools.partial(PythonSystemDependency, 'python', env, kwargs, installation))

    if DependencyMethods.EXTRAFRAMEWORK in methods:
        nkwargs = kwargs.copy()
        if mesonlib.version_compare(pkg_version, '>= 3'):
            # There is a python in /System/Library/Frameworks, but that's python 2.x,
            # Python 3 will always be in /Library
            nkwargs['paths'] = ['/Library/Frameworks']
        candidates.append(functools.partial(PythonFrameworkDependency, 'Python', env, nkwargs, installation))

    return candidates