from __future__ import absolute_import, division, print_function

from collections import OrderedDict
import copy
import hashlib
import json
import os
from os.path import isfile, join
import re
import sys
import time

from .conda_interface import iteritems, PY3, text_type
from .conda_interface import md5_file
from .conda_interface import non_x86_linux_machines
from .conda_interface import MatchSpec
from .conda_interface import specs_from_url
from .conda_interface import envs_dirs
from .conda_interface import string_types

from conda_build import exceptions, filt, utils
from conda_build.features import feature_list
from conda_build.config import Config, get_or_merge_config
from conda_build.utils import (ensure_list, find_recipe, expand_globs, get_installed_packages,
                               HashableDict, trim_empty_keys, filter_files)
from conda_build.license_family import ensure_valid_license_family
from conda_build.variants import get_default_variants
from conda_build.exceptions import DependencyNeedsBuildingError

try:
    import yaml

    # try to import C loader
    try:
        from yaml import CBaseLoader as BaseLoader
    except ImportError:
        from yaml import BaseLoader
except ImportError:
    sys.exit('Error: could not import yaml (required to read meta.yaml '
             'files of conda recipes)')

on_win = (sys.platform == 'win32')

# arches that don't follow exact names in the subdir need to be mapped here
ARCH_MAP = {'32': 'x86',
            '64': 'x86_64'}


def ns_cfg(config):
    # Remember to update the docs of any of this changes
    plat = config.build_subdir
    d = dict(
        linux=plat.startswith('linux-'),
        linux32=bool(plat == 'linux-32'),
        linux64=bool(plat == 'linux-64'),
        arm=plat.startswith('linux-arm'),
        osx=plat.startswith('osx-'),
        unix=plat.startswith(('linux-', 'osx-')),
        win=plat.startswith('win-'),
        win32=bool(plat == 'win-32'),
        win64=bool(plat == 'win-64'),
        x86=plat.endswith(('-32', '-64')),
        x86_64=plat.endswith('-64'),
        os=os,
        environ=os.environ,
        nomkl=bool(int(os.environ.get('FEATURE_NOMKL', False)))
    )

    py = config.variant.get('python', get_default_variants()[0]['python'])
    py = int("".join(py.split('.')[:2]))
    d.update(dict(py=py,
                    py3k=bool(30 <= py < 40),
                    py2k=bool(20 <= py < 30),
                    py26=bool(py == 26),
                    py27=bool(py == 27),
                    py33=bool(py == 33),
                    py34=bool(py == 34),
                    py35=bool(py == 35),
                    py36=bool(py == 36),))

    np = config.variant.get('numpy', get_default_variants()[0]['numpy'])
    d['np'] = int("".join(np.split('.')[:2]))

    pl = config.variant.get('perl', get_default_variants()[0]['perl'])
    d['pl'] = pl

    lua = config.variant.get('lua', get_default_variants()[0]['lua'])
    d['lua'] = lua
    d['luajit'] = bool(lua[0] == "2")

    for machine in non_x86_linux_machines:
        d[machine] = bool(plat == 'linux-%s' % machine)

    for feature, value in feature_list:
        d[feature] = value
    d.update(os.environ)
    return d


# Selectors must be either:
# - at end of the line
# - embedded (anywhere) within a comment
#
# Notes:
# - [([^\[\]]+)\] means "find a pair of brackets containing any
#                 NON-bracket chars, and capture the contents"
# - (?(2).*)$ means "allow trailing characters iff group 2 (#.*) was found."
sel_pat = re.compile(r'(.+?)\s*(#.*)?\[([^\[\]]+)\](?(2).*)$')


# this function extracts the variable name from a NameError exception, it has the form of:
# "NameError: name 'var' is not defined", where var is the variable that is not defined. This gets
#    returned
def parseNameNotFound(error):
    m = re.search('\'(.+?)\'', str(error))
    if len(m.groups()) == 1:
        return m.group(1)
    else:
        return ""


# We evaluate the selector and return True (keep this line) or False (drop this line)
# If we encounter a NameError (unknown variable in selector), then we replace it by False and
#     re-run the evaluation
def eval_selector(selector_string, namespace):
    try:
        # TODO: is there a way to do this without eval?  Eval allows arbitrary
        #    code execution.
        return eval(selector_string, namespace, {})
    except NameError as e:
        missing_var = parseNameNotFound(e)
        print("Warning: Treating unknown selector \'" + missing_var + "\' as if it was False.")
        next_string = selector_string.replace(missing_var, "False")
        return eval_selector(next_string, namespace)


def select_lines(data, namespace):
    lines = []

    for i, line in enumerate(data.splitlines()):
        line = line.rstrip()

        trailing_quote = ""
        if line and line[-1] in ("'", '"'):
            trailing_quote = line[-1]

        if line.lstrip().startswith('#'):
            # Don't bother with comment only lines
            continue
        m = sel_pat.match(line)
        if m:
            cond = m.group(3)
            try:
                if eval_selector(cond, namespace):
                    lines.append(m.group(1) + trailing_quote)
            except:
                sys.exit('''\
Error: Invalid selector in meta.yaml line %d:
%s
''' % (i + 1, line))
        else:
            lines.append(line)
    return '\n'.join(lines) + '\n'


def yamlize(data):
    try:
        return yaml.load(data, Loader=BaseLoader)
    except yaml.error.YAMLError as e:
        if '{{' in data:
            try:
                import jinja2
                jinja2  # Avoid pyflakes failure: 'jinja2' imported but unused
            except ImportError:
                raise exceptions.UnableToParseMissingJinja2(original=e)
        raise exceptions.UnableToParse(original=e)


def ensure_valid_fields(meta):
    try:
        pin_depends = meta['build']['pin_depends']
    except KeyError:
        pin_depends = ''
    if pin_depends not in ('', 'record', 'strict'):
        raise RuntimeError("build/pin_depends cannot be '%s'" % pin_depends)


def _trim_None_strings(meta_dict):
    log = utils.get_logger(__name__)
    for key, value in meta_dict.items():
        if hasattr(value, 'keys'):
            meta_dict[key] = _trim_None_strings(value)
        elif value and hasattr(value, '__iter__') or isinstance(value, string_types):
            if isinstance(value, string_types):
                meta_dict[key] = None if 'None' in value else value
            else:
                # support lists of dicts (homogeneous)
                keep = []
                if hasattr(next(iter(value)), 'keys'):
                    for d in value:
                        trimmed_dict = _trim_None_strings(d)
                        if trimmed_dict:
                            keep.append(trimmed_dict)
                # support lists of strings (homogeneous)
                else:
                    keep = [i for i in value if 'None' not in i]
                meta_dict[key] = keep
        else:
            log.debug("found unrecognized data type in dictionary: {0}, type: {1}".format(value,
                                                                                    type(value)))
    trim_empty_keys(meta_dict)
    return meta_dict


def ensure_valid_noarch_value(meta):
    try:
        build_noarch = meta['build']['noarch']
    except KeyError:
        return
    if build_noarch.lower() == 'none':
        raise exceptions.CondaBuildException("Invalid value for noarch: %s" % build_noarch)


def parse(data, config, path=None):
    data = select_lines(data, ns_cfg(config))
    res = yamlize(data)
    # ensure the result is a dict
    if res is None:
        res = {}
    for field in FIELDS:
        if field not in res:
            continue
        # ensure that empty fields are dicts (otherwise selectors can cause invalid fields)
        if not res[field]:
            res[field] = {}
        # source field may be either a dictionary, or a list of dictionaries
        if field == 'source':
            if not (isinstance(res[field], dict) or (hasattr(res[field], '__iter__') and not
                        isinstance(res[field], string_types))):
                raise RuntimeError("The %s field should be a dict or list of dicts, not "
                                   "%s in file %s." % (field, res[field].__class__.__name__, path))
        else:
            if not isinstance(res[field], dict):
                raise RuntimeError("The %s field should be a dict, not %s in file %s." %
                                (field, res[field].__class__.__name__, path))

    ensure_valid_fields(res)
    ensure_valid_license_family(res)
    ensure_valid_noarch_value(res)
    return sanitize(res)


trues = {'y', 'on', 'true', 'yes'}
falses = {'n', 'no', 'false', 'off'}

default_structs = {
    'build/entry_points': list,
    'build/features': list,
    'source/patches': list,
    'build/script': list,
    'build/script_env': list,
    'build/run_exports': list,
    'build/track_features': list,
    'requirements/build': list,
    'requirements/host': list,
    'requirements/run': list,
    'requirements/conflicts': list,
    'requirements/run_constrained': list,
    'test/requires': list,
    'test/files': list,
    'test/source_files': list,
    'test/commands': list,
    'test/imports': list,
    'package/version': text_type,
    'build/string': text_type,
    'build/pin_depends': text_type,
    'source/svn_rev': text_type,
    'source/git_tag': text_type,
    'source/git_branch': text_type,
    'source/md5': text_type,
    'source/git_rev': text_type,
    'source/path': text_type,
    'source/git_url': text_type,
    'build/osx_is_app': bool,
    'build/preserve_egg_dir': bool,
    'build/binary_relocation': bool,
    'build/noarch': text_type,
    'build/noarch_python': bool,
    'build/detect_binary_files_with_prefix': bool,
    'build/skip': bool,
    'build/skip_compile_pyc': list,
    'build/preferred_env': text_type,
    'build/preferred_env_executable_paths': list,
    'app/own_environment': bool
}


def sanitize(meta):
    """
    Sanitize the meta-data to remove aliases/handle deprecation

    """
    sanitize_funs = {'source': [_git_clean]}
    for section, funs in sanitize_funs.items():
        if section in meta:
            for func in funs:
                section_data = meta[section]
                # section is a dictionary
                if hasattr(section_data, 'keys'):
                    section_data = func(section_data)
                # section is a list of dictionaries
                else:
                    section_data = [func(_d) for _d in section_data]
                meta[section] = section_data
    _trim_None_strings(meta)
    return meta


def _git_clean(source_meta):
    """
    Reduce the redundancy in git specification by removing git_tag and
    git_branch.

    If one is specified, copy to git_rev.

    If more than one field is used to specified, exit
    and complain.
    """

    git_rev_tags_old = ('git_branch', 'git_tag')
    git_rev = 'git_rev'

    git_rev_tags = (git_rev,) + git_rev_tags_old

    has_rev_tags = tuple(bool(source_meta.get(tag, text_type())) for
                          tag in git_rev_tags)
    if sum(has_rev_tags) > 1:
        msg = "Error: multiple git_revs:"
        msg += ', '.join("{}".format(key) for key, has in
                         zip(git_rev_tags, has_rev_tags) if has)
        sys.exit(msg)

    # make a copy of the input so we have no side-effects
    ret_meta = source_meta.copy()
    # loop over the old versions
    for key, has in zip(git_rev_tags[1:], has_rev_tags[1:]):
        # update if needed
        if has:
            ret_meta[git_rev_tags[0]] = ret_meta[key]
        # and remove
        ret_meta.pop(key, None)

    return ret_meta


# If you update this please update the example in
# conda-docs/docs/source/build.rst
FIELDS = {
    'package': ['name', 'version'],
    'source': ['fn', 'url', 'md5', 'sha1', 'sha256', 'path',
               'git_url', 'git_tag', 'git_branch', 'git_rev', 'git_depth',
               'hg_url', 'hg_tag',
               'svn_url', 'svn_rev', 'svn_ignore_externals',
               'patches'
               ],
    'build': ['number', 'string', 'entry_points', 'osx_is_app',
              'features', 'track_features', 'preserve_egg_dir',
              'no_link', 'binary_relocation', 'script', 'noarch', 'noarch_python',
              'has_prefix_files', 'binary_has_prefix_files', 'ignore_prefix_files',
              'detect_binary_files_with_prefix', 'skip_compile_pyc', 'rpaths',
              'script_env', 'always_include_files', 'skip', 'msvc_compiler',
              'pin_depends', 'include_recipe',  # pin_depends is experimental still
              'preferred_env', 'preferred_env_executable_paths', 'run_exports',
              ],
    'requirements': ['build', 'host', 'run', 'conflicts', 'run_constrained'],
    'app': ['entry', 'icon', 'summary', 'type', 'cli_opts',
            'own_environment'],
    'test': ['requires', 'commands', 'files', 'imports', 'source_files'],
    'about': ['home', 'dev_url', 'doc_url', 'license_url',  # these are URLs
              'license', 'summary', 'description', 'license_family',  # text
              'license_file', 'readme',  # paths in source tree
              ],
}


def check_bad_chrs(s, field):
    bad_chrs = '=@#$%^&*:;"\'\\|<>?/ '
    if field in ('package/version', 'build/string'):
        bad_chrs += '-'
    if field != 'package/version':
        bad_chrs += '!'
    for c in bad_chrs:
        if c in s:
            sys.exit("Error: bad character '%s' in %s: %s" % (c, field, s))


def build_string_from_metadata(metadata):
    if metadata.meta.get('build', {}).get('string'):
        build_str = metadata.get_value('build/string')
    else:
        res = []
        build_pkg_names = [ms.name for ms in metadata.ms_depends('build')]
        # TODO: this is the bit that puts in strings like py27np111 in the filename.  It would be
        #    nice to get rid of this, since the hash supercedes that functionally, but not clear
        #    whether anyone's tools depend on this file naming right now.
        for s, names, places in (('np', 'numpy', 2), ('py', 'python', 2), ('pl', 'perl', 2),
                                 ('lua', 'lua', 2), ('r', ('r', 'r-base'), 3)):
            for ms in metadata.ms_depends('run'):
                for name in ensure_list(names):
                    if ms.name == name and name in build_pkg_names:
                        # only append numpy when it is actually pinned
                        if name == 'numpy' and (not hasattr(ms, 'version') or not ms.version):
                            continue
                        if metadata.noarch == name or (metadata.get_value('build/noarch_python') and
                                                    name == 'python'):
                            res.append(s)
                        else:
                            pkg_names = list(ensure_list(names))
                            pkg_names.extend([_n.replace('-', '_')
                                              for _n in ensure_list(names) if '-' in _n])
                            for _n in pkg_names:
                                _n = _n.replace('-', '_')
                                variant_version = metadata.config.variant.get(_n, "")
                                if variant_version:
                                    break
                            res.append(''.join([s] + variant_version.split('.')[:places]))

        features = ensure_list(metadata.get_value('build/features', []))
        if res:
            res.append('_')
        if features:
            res.extend(('_'.join(features), '_'))
        res.append('{0}'.format(metadata.build_number() if metadata.build_number() else 0))
        build_str = "".join(res)
    return build_str


# This really belongs in conda, and it is int conda.cli.common,
#   but we don't presently have an API there.
def _get_env_path(env_name_or_path):
    if not os.path.isdir(env_name_or_path):
        for envs_dir in list(envs_dirs) + [os.getcwd()]:
            path = os.path.join(envs_dir, env_name_or_path)
            if os.path.isdir(path):
                env_name_or_path = path
                break
    bootstrap_metadir = os.path.join(env_name_or_path, 'conda-meta')
    if not os.path.isdir(bootstrap_metadir):
        print("Bootstrap environment '%s' not found" % env_name_or_path)
        sys.exit(1)
    return env_name_or_path


def _get_dependencies_from_environment(env_name_or_path):
    path = _get_env_path(env_name_or_path)
    # construct build requirements that replicate the given bootstrap environment
    # and concatenate them to the build requirements from the recipe
    bootstrap_metadata = get_installed_packages(path)
    bootstrap_requirements = []
    for package, data in bootstrap_metadata.items():
        bootstrap_requirements.append("%s %s %s" % (package, data['version'], data['build']))
    return {'requirements': {'build': bootstrap_requirements}}


def toposort(output_metadata_map, phase):
    '''This function is used to work out the order to run the install scripts
       for split packages based on any interdependencies. The result is just
       a re-ordering of outputs such that we can run them in that order and
       reset the initial set of files in the install prefix after each. This
       will naturally lead to non-overlapping files in each package and also
       the correct files being present during the install and test procedures,
       provided they are run in this order.'''
    from .conda_interface import _toposort
    # We only care about the conda packages built by this recipe. Non-conda
    # packages get sorted to the end.
    these_packages = [output_d['name'] for output_d in output_metadata_map
                      if output_d.get('type', 'conda') == 'conda']
    topodict = dict()
    order = dict()
    endorder = set()
    for idx, (output_d, output_m) in enumerate(output_metadata_map.items()):
        if output_d.get('type', 'conda') == 'conda':
            name = output_d['name']
            order[name] = idx
            topodict[name] = set()
            for run_dep in output_m.get_value('requirements/{}'.format(phase), []):
                run_dep = run_dep.split(' ')[0]
                if run_dep in these_packages:
                    topodict[name].update((run_dep,))
        else:
            endorder.add(idx)
    topo_order = list(_toposort(topodict))
    keys = [k for pkgname in topo_order for k in output_metadata_map.keys()
            if 'name' in k and k['name'] == pkgname]
    # not sure that this is working...  not everything has 'name', and not sure how this pans out
    #    may end up excluding packages without the 'name' field
    keys.extend([k for pkgname in endorder for k in output_metadata_map.keys()
                 if 'name' in k and k['name'] == pkgname])
    result = OrderedDict()
    for key in keys:
        result[key] = output_metadata_map[key]
    return result


def output_dict_from_top_level_meta(m):
    requirements = m.meta.get('requirements', {})
    output_d = {'name': m.name(), 'requirements': requirements,
                'noarch_python': m.get_value('build/noarch_python'),
                'noarch': m.get_value('build/noarch'),
                'type': 'conda',
                }

    run_exports = m.meta.get('build', {}).get('run_exports')
    if run_exports:
        output_d['run_exports'] = run_exports
    return output_d


def get_output_dicts_from_metadata(metadata):
    outputs = metadata.get_section('outputs')

    if not outputs:
        outputs = [output_dict_from_top_level_meta(metadata)]
    else:
        # make a metapackage for the top-level package if the top-level requirements
        #     mention a subpackage,
        # but only if a matching output name is not explicitly provided
        if metadata.uses_subpackage and not any(metadata.name() == out.get('name', '')
                                            for out in outputs):
            outputs.append(output_dict_from_top_level_meta(metadata))
    for out in outputs:
        if (metadata.name() == out.get('name', '') and not (out.get('files') or
                                                        out.get('script'))):
            out['requirements'] = metadata.meta.get('requirements', {})
            out['noarch_python'] = out.get('noarch_python',
                                            metadata.get_value('build/noarch_python'))
            out['noarch'] = out.get('noarch', metadata.get_value('build/noarch'))
    return outputs


def finalize_outputs_pass(base_metadata, iteration, render_order=None, outputs=None,
                          permit_unsatisfiable_variants=True):
    from .render import finalize_metadata
    outputs = OrderedDict()
    for output_d, metadata in render_order.values():
        try:
            log = utils.get_logger(__name__)
            log.info("Attempting to finalize metadata for {}".format(metadata.name()))
            # this is weird, but I think necessary. We should reparse
            #    the top-level recipe to get all of our dependencies
            #    fixed up.
            if not hasattr(base_metadata, 'other_outputs'):
                base_metadata.other_outputs = OrderedDict()
            base_metadata.other_outputs.update(outputs)
            om = base_metadata.copy()
            om.config.variant = metadata.config.variant
            if not om.final:
                om.parse_until_resolved()
            # get the new output_d from the reparsed top-level metadata, so that we have any
            #    exact subpackage version/hash info
            recipe_outputs = get_output_dicts_from_metadata(om)
            output_d = get_updated_output_dict_from_reparsed_metadata(output_d,
                                                                        recipe_outputs)
            metadata = om.get_output_metadata(output_d)
            fm = finalize_metadata(metadata)
            if not output_d.get('type') or output_d.get('type') == 'conda':
                outputs[(fm.name(), HashableDict(fm.config.variant))] = (output_d, fm)
        except DependencyNeedsBuildingError as e:
            if not permit_unsatisfiable_variants:
                raise
            else:
                log = utils.get_logger(__name__)
                log.warn("Could not finalize metadata due to missing dependencies: "
                            "{}".format(e.packages))
                outputs[(metadata.name(), HashableDict(metadata.config.variant))] = (
                    output_d, metadata)
    return outputs


def get_updated_output_dict_from_reparsed_metadata(original_dict, new_outputs):
    output_d = original_dict
    if 'name' in original_dict:
        output_ds = [out for out in new_outputs if 'name' in out and
                    out['name'] == original_dict['name']]
        assert len(output_ds) == 1
        output_d = output_ds[0]
    return output_d


class MetaData(object):
    def __init__(self, path, config=None, variant=None):

        self.undefined_jinja_vars = []
        # decouple this config from whatever was fed in.  People must change config by
        #    accessing and changing this attribute.
        self.config = get_or_merge_config(config, variant=variant).copy()

        if isfile(path):
            self.meta_path = path
            self.path = os.path.dirname(path)
        else:
            self.meta_path = find_recipe(path)
            self.path = os.path.dirname(self.meta_path)
        self.requirements_path = join(self.path, 'requirements.txt')

        # Start with bare-minimum contents so we can call environ.get_dict() with impunity
        # We'll immediately replace these contents in parse_again()
        self.meta = dict()

        # This is the 'first pass' parse of meta.yaml, so not all variables are defined yet
        # (e.g. GIT_FULL_HASH, etc. are undefined)
        # Therefore, undefined jinja variables are permitted here
        # In the second pass, we'll be more strict. See build.build()
        # Primarily for debugging.  Ensure that metadata is not altered after "finalizing"
        self.parse_again(permit_undefined_jinja=True, allow_no_other_outputs=True)
        if 'host' in self.get_section('requirements'):
            self.config.has_separate_host_prefix = True
        self.config.disable_pip = self.disable_pip

    @property
    def final(self):
        return self.get_value('extra/final')

    @final.setter
    def final(self, boolean):
        extra = self.meta.get('extra', {})
        extra['final'] = boolean
        self.meta['extra'] = extra

    @property
    def disable_pip(self):
        return self.config.disable_pip or ('build' in self.meta and
                                           'disable_pip' in self.meta['build'])

    @disable_pip.setter
    def disable_pip(self, value):
        self.config.disable_pip = value
        build = self.meta.get('build', {})
        build['disable_pip'] = value
        self.meta['build'] = build

    def append_metadata_sections(self, sections_file_or_dict, merge, raise_on_clobber=False):
        """Append to or replace subsections to meta.yaml

        This is used to alter input recipes, so that a given requirement or
        setting is applied without manually altering the input recipe. It is
        intended for vendors who want to extend existing recipes without
        necessarily removing information. pass merge=False to replace sections.
        """
        if hasattr(sections_file_or_dict, 'keys'):
            build_config = sections_file_or_dict
        else:
            with open(sections_file_or_dict) as configfile:
                build_config = parse(configfile.read(), config=self.config)
        utils.merge_or_update_dict(self.meta, build_config, self.path, merge=merge,
                                   raise_on_clobber=raise_on_clobber)

    def parse_again(self, permit_undefined_jinja=False, allow_no_other_outputs=False,
                    bypass_env_check=False):
        """Redo parsing for key-value pairs that are not initialized in the
        first pass.

        config: a conda-build Config object.  If None, the config object passed at creation
                time is used.

        permit_undefined_jinja: If True, *any* use of undefined jinja variables will
                                evaluate to an emtpy string, without emitting an error.
        """
        assert not self.final, "modifying metadata after finalization"

        log = utils.get_logger(__name__)
        log.addFilter(filt)

        if isfile(self.requirements_path) and not self.get_value('requirements/run'):
            self.meta.setdefault('requirements', {})
            run_requirements = specs_from_url(self.requirements_path)
            self.meta['requirements']['run'] = run_requirements

        os.environ["CONDA_BUILD_STATE"] = "RENDER"
        append_sections_file = None
        clobber_sections_file = None
        try:
            # we sometimes create metadata from dictionaries, in which case we'll have no path
            if self.meta_path:
                self.meta = parse(self._get_contents(permit_undefined_jinja,
                                                     allow_no_other_outputs=allow_no_other_outputs,
                                                     bypass_env_check=bypass_env_check),
                                  config=self.config,
                                  path=self.meta_path)

                if (isfile(self.requirements_path) and
                        not self.meta.get('requirements', {}).get('run', [])):
                    self.meta.setdefault('requirements', {})
                    run_requirements = specs_from_url(self.requirements_path)
                    self.meta['requirements']['run'] = run_requirements

                append_sections_file = os.path.join(self.path, 'recipe_append.yaml')
                clobber_sections_file = os.path.join(self.path, 'recipe_clobber.yaml')

            append_sections_file = self.config.append_sections_file or append_sections_file
            if append_sections_file and not os.path.isfile(append_sections_file):
                log.debug('input append sections file did not exist: %s', append_sections_file)
                append_sections_file = None
            clobber_sections_file = self.config.clobber_sections_file or clobber_sections_file
            if clobber_sections_file and not os.path.isfile(clobber_sections_file):
                log.debug('input clobber sections file did not exist: %s', clobber_sections_file)
                clobber_sections_file = None

            if append_sections_file:
                self.append_metadata_sections(append_sections_file, merge=True)
            if clobber_sections_file:
                self.append_metadata_sections(clobber_sections_file, merge=False)
            if self.config.bootstrap:
                dependencies = _get_dependencies_from_environment(self.config.bootstrap)
                self.append_metadata_sections(dependencies, merge=True)
        except:
            raise
        finally:
            del os.environ["CONDA_BUILD_STATE"]
            pass
        self.validate_features()
        self.ensure_no_pip_requirements()

    def ensure_no_pip_requirements(self):
        keys = 'requirements/build', 'requirements/run', 'test/requires'
        for key in keys:
            if any(hasattr(item, 'keys') for item in self.get_value(key)):
                raise ValueError("Dictionaries are not supported as values in requirements sections"
                                 ".  Note that pip requirements as used in conda-env "
                                 "environment.yml files are not supported by conda-build.")
        self.append_requirements()

    def append_requirements(self):
        """For dynamic determination of build or run reqs, based on configuration"""
        reqs = self.meta.get('requirements', {})
        run_reqs = reqs.get('run', [])
        if bool(self.get_value('build/osx_is_app', False)) and self.config.platform == 'osx':
            if 'python.app' not in run_reqs:
                run_reqs.append('python.app')
        self.meta['requirements'] = reqs

    def parse_until_resolved(self, allow_no_other_outputs=False, bypass_env_check=False):
        """variant contains key-value mapping for additional functions and values
        for jinja2 variables"""
        # undefined_jinja_vars is refreshed by self.parse again
        undefined_jinja_vars = ()
        # store the "final" state that we think we're in.  reloading the meta.yaml file
        #   can reset it (to True)
        final = self.final
        # always parse again at least once.
        self.parse_again(permit_undefined_jinja=True, allow_no_other_outputs=allow_no_other_outputs,
                         bypass_env_check=bypass_env_check)
        self.final = final

        while set(undefined_jinja_vars) != set(self.undefined_jinja_vars):
            undefined_jinja_vars = self.undefined_jinja_vars
            self.parse_again(permit_undefined_jinja=True,
                             allow_no_other_outputs=allow_no_other_outputs,
                             bypass_env_check=bypass_env_check)
            self.final = final
        if undefined_jinja_vars:
            sys.exit("Undefined Jinja2 variables remain ({}).  Please enable "
                     "source downloading and try again.".format(self.undefined_jinja_vars))

        # always parse again at the end, too.
        self.parse_again(permit_undefined_jinja=False,
                         allow_no_other_outputs=allow_no_other_outputs,
                         bypass_env_check=bypass_env_check)
        self.final = final

    @classmethod
    def fromstring(cls, metadata, config=None, variant=None):
        m = super(MetaData, cls).__new__(cls)
        if not config:
            config = Config()
        m.meta = parse(metadata, config=config, path='', variant=variant)
        m.config = config
        m.parse_again(permit_undefined_jinja=True)
        return m

    @classmethod
    def fromdict(cls, metadata, config=None, variant=None):
        """
        Create a MetaData object from metadata dict directly.
        """
        m = super(MetaData, cls).__new__(cls)
        m.path = ''
        m.meta_path = ''
        m.requirements_path = ''
        m.meta = sanitize(metadata)

        if not config:
            config = Config(variant=variant)

        m.config = config
        m.undefined_jinja_vars = []
        m.final = False

        return m

    def get_section(self, section):
        return self.meta.get(section, {})

    def get_value(self, field, default=None, autotype=True):
        """
        Get a value from a meta.yaml.
        :param field: Field to return
        :param default: Default object to return if field doesn't exist
        :param autotype: If True, return the default type of field if one exists.
        False will return the default object.
        :return:
        """
        section, key = field.split('/')

        # get correct default
        if autotype and default is None and field in default_structs:
            default = default_structs[field]()

        value = self.get_section(section).get(key, default)

        # handle yaml 1.1 boolean values
        if isinstance(value, text_type):
            if value.lower() in trues:
                value = True
            elif value.lower() in falses:
                value = False

        return value

    def check_fields(self):
        for section, submeta in iteritems(self.meta):
            # anything goes in the extra section
            if section == 'extra':
                continue
            if section not in FIELDS:
                raise ValueError("unknown section: %s" % section)
            for key in submeta:
                if key not in FIELDS[section]:
                    raise ValueError("in section %r: unknown key %r" %
                             (section, key))
        return True

    def name(self):
        res = self.get_value('package/name')
        if not res:
            sys.exit('Error: package/name missing in: %r' % self.meta_path)
        res = text_type(res)
        if res != res.lower():
            sys.exit('Error: package/name must be lowercase, got: %r' % res)
        check_bad_chrs(res, 'package/name')
        return res

    def version(self):
        res = str(self.get_value('package/version'))
        if res is None:
            sys.exit("Error: package/version missing in: %r" % self.meta_path)
        check_bad_chrs(res, 'package/version')
        if self.final and res.startswith('.'):
            raise ValueError("Fully-rendered version can't start with period -  got %s", res)
        return res

    def build_number(self):
        number = self.get_value('build/number')
        # build number can come back as None if no setting (or jinja intermediate)
        try:
            build_int = int(number)
        except (ValueError, TypeError):
            build_int = ""
        return build_int

    def ms_depends(self, typ='run'):
        res = []
        names = ('python', 'numpy', 'perl', 'lua')
        name_ver_list = [(name, self.config.variant[name])
                         for name in names
                         if self.config.variant.get(name)]
        if self.config.variant.get('r_base'):
            # r is kept for legacy installations, r-base deprecates it.
            name_ver_list.extend([('r', self.config.variant['r_base']),
                                  ('r-base', self.config.variant['r_base']),
                                  ])
        for spec in self.get_value('requirements/' + typ, []):
            try:
                ms = MatchSpec(spec)
            except AssertionError:
                raise RuntimeError("Invalid package specification: %r" % spec)
            except (AttributeError, ValueError):
                raise RuntimeError("Received dictionary as spec.  Note that pip requirements are "
                                   "not supported in conda-build meta.yaml.")
            if ms.name == self.name():
                raise RuntimeError("%s cannot depend on itself" % self.name())
            for name, ver in name_ver_list:
                if ms.name == name:
                    if self.noarch:
                        continue

            for c in '=!@#$%^&*:;"\'\\|<>?/':
                if c in ms.name:
                    sys.exit("Error: bad character '%s' in package name "
                             "dependency '%s'" % (c, ms.name))
            parts = spec.split()
            if len(parts) >= 2:
                if parts[1] in {'>', '>=', '=', '==', '!=', '<', '<='}:
                    msg = ("Error: bad character '%s' in package version "
                           "dependency '%s'" % (parts[1], ms.name))
                    if len(parts) >= 3:
                        msg += "\nPerhaps you meant '%s %s%s'" % (ms.name,
                            parts[1], parts[2])
                    sys.exit(msg)
            res.append(ms)
        return res

    def _get_hash_contents(self):
        sections = ['requirements', 'build']
        # make a copy of values, so that no sorting occurs in place
        composite = HashableDict({section: copy.copy(self.get_section(section))
                                  for section in sections})

        if self.get_section('source'):
            src = self.get_section('source')
            if hasattr(src, 'keys'):
                composite['source'] = HashableDict(src.copy())
            else:
                composite['source'] = [HashableDict(s.copy()) for s in src]

        # filter build requirements for ones that should not be in the hash
        requirements = composite.get('requirements', {})
        build_reqs = requirements.get('build', [])
        excludes = self.config.variant.get('ignore_version', [])
        if excludes:
            exclude_pattern = re.compile('|'.join('{}[\s$]?.*'.format(exc) for exc in excludes))
            build_reqs = [req for req in build_reqs if not exclude_pattern.match(req)]
        requirements['build'] = build_reqs
        composite['requirements'] = requirements

        # remove the build number from the hash, so that we can bump it without changing the hash
        if 'number' in composite['build']:
            del composite['build']['number']
        # remove the build string, so that hashes don't affect themselves
        for key in ('string', 'noarch', 'noarch_python'):
            if key in composite['build']:
                del composite['build'][key]
        if not composite['build']:
            del composite['build']

        for key in 'build', 'run':
            if key in composite['requirements'] and not composite['requirements'].get(key):
                del composite['requirements'][key]

        file_paths = []
        if self.path and self.config.include_recipe and self.include_recipe():
            recorded_input_files = os.path.join(self.path, '..', 'hash_input_files')
            if os.path.exists(recorded_input_files):
                with open(recorded_input_files) as f:
                    file_paths = f.read().splitlines()
            else:
                files = utils.rec_glob(self.path, "*")
                file_paths = sorted([f.replace(self.path + os.sep, '') for f in files])
                # exclude meta.yaml and , because the json dictionary captures their content
                # never include run_test - these can be renamed from subpackages, or the top-level
                #    and if they're part of the top-level only, there will be missing files in the
                #    subpackage
                file_paths = [f for f in file_paths if not (f == 'meta.yaml' or
                                                            f.startswith('run_test'))]
                file_paths = filter_files(file_paths, self.path)
        trim_empty_keys(composite)
        return composite, sorted(file_paths)

    def _hash_dependencies(self):
        """With arbitrary pinning, we can't depend on the build string as done in
        build_string_from_metadata - there's just too much info.  Instead, we keep that as-is, to
        not be disruptive, but we add this extra hash, which is just a way of distinguishing files
        on disk.  The actual determination of dependencies is done in the repository metadata."""
        # save only the first HASH_LENGTH characters - should be more than enough, since these only
        #    need to be unique within one version
        # plus one is for the h - zero pad on the front, trim to match HASH_LENGTH
        recipe_input, file_paths = self._get_hash_contents()
        hash_ = hashlib.sha1(json.dumps(recipe_input, sort_keys=True).encode())
        for recipe_file in file_paths:
            with open(os.path.join(self.path, recipe_file), 'rb') as f:
                hash_.update(f.read())
        hash_ = 'h{0}'.format(hash_.hexdigest())[:self.config.hash_length + 1]
        return hash_

    def build_id(self):
        out = self.get_value('build/string')
        if out:
            check_bad_chrs(out, 'build/string')
        else:
            out = build_string_from_metadata(self)
        if self.config.filename_hashing:
            if not re.findall('h[0-9a-f]{%s}' % self.config.hash_length, out):
                ret = out.rsplit('_', 1)
                try:
                    int(ret[0])
                    out = self._hash_dependencies() + '_' + str(ret[0])
                except ValueError:
                    out = ret[0] + self._hash_dependencies()
                if len(ret) > 1:
                    out = '_'.join([out] + ret[1:])
            else:
                out = re.sub('h[0-9a-f]{%s}' % self.config.hash_length, self._hash_dependencies(),
                             out)
        return out

    def dist(self):
        return '%s-%s-%s' % (self.name(), self.version(), self.build_id())

    def pkg_fn(self):
        return "%s.tar.bz2" % self.dist()

    def is_app(self):
        return bool(self.get_value('app/entry'))

    def app_meta(self):
        d = {'type': 'app'}
        if self.get_value('app/icon'):
            d['icon'] = '%s.png' % md5_file(join(
                self.path, self.get_value('app/icon')))

        for field, key in [('app/entry', 'app_entry'),
                           ('app/type', 'app_type'),
                           ('app/cli_opts', 'app_cli_opts'),
                           ('app/summary', 'summary'),
                           ('app/own_environment', 'app_own_environment')]:
            value = self.get_value(field)
            if value:
                d[key] = value
        return d

    def info_index(self):
        arch = self.config.host_arch or self.config.arch
        d = dict(
            name=self.name(),
            version=self.version(),
            build=self.build_id(),
            build_number=self.build_number() if self.build_number() else 0,
            platform=self.config.platform if self.config.platform != 'noarch' else None,
            arch=ARCH_MAP.get(arch, arch),
            subdir=self.config.host_subdir,
            depends=sorted(' '.join(ms.spec.split())
                             for ms in self.ms_depends()),
            timestamp=int(time.time() * 1000),
        )
        for key in ('license', 'license_family'):
            value = self.get_value('about/' + key)
            if value:
                d[key] = value

        preferred_env = self.get_value('build/preferred_env')
        if preferred_env:
            d['preferred_env'] = preferred_env

        # conda 4.4+ optional dependencies
        constrains = self.get_value('requirements/run_constrained')
        if constrains:
            d['constrains'] = constrains

        if self.get_value('build/features'):
            d['features'] = ' '.join(self.get_value('build/features'))
        if self.get_value('build/track_features'):
            d['track_features'] = ' '.join(self.get_value('build/track_features'))
        if self.noarch:
            d['platform'] = d['arch'] = None
            d['subdir'] = 'noarch'
            # These are new-style noarch settings.  the self.noarch setting can be True in 2 ways:
            #    if noarch: True or if noarch_python: True.  This is disambiguation.
            build_noarch = self.get_value('build/noarch')
            if build_noarch:
                d['noarch'] = build_noarch
        if self.is_app():
            d.update(self.app_meta())
        return d

    def has_prefix_files(self):
        ret = ensure_list(self.get_value('build/has_prefix_files', []))
        if not isinstance(ret, list):
            raise RuntimeError('build/has_prefix_files should be a list of paths')
        if sys.platform == 'win32':
            if any('\\' in i for i in ret):
                raise RuntimeError("build/has_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix)

    def ignore_prefix_files(self):
        ret = self.get_value('build/ignore_prefix_files', False)
        if type(ret) not in (list, bool):
            raise RuntimeError('build/ignore_prefix_files should be boolean or a list of paths '
                               '(optionally globs)')
        if sys.platform == 'win32':
            if type(ret) is list and any('\\' in i for i in ret):
                raise RuntimeError("build/ignore_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix) if type(ret) is list else ret

    def always_include_files(self):
        files = ensure_list(self.get_value('build/always_include_files', []))
        if any('\\' in i for i in files):
            raise RuntimeError("build/always_include_files paths must use / "
                                "as the path delimiter on Windows")
        if on_win:
            files = [f.replace("/", "\\") for f in files]

        return expand_globs(files, self.config.build_prefix)

    def binary_relocation(self):
        ret = self.get_value('build/binary_relocation', True)
        if type(ret) not in (list, bool):
            raise RuntimeError('build/ignore_prefix_files should be boolean or a list of paths '
                               '(optionally globs)')
        if sys.platform == 'win32':
            if type(ret) is list and any('\\' in i for i in ret):
                raise RuntimeError("build/ignore_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix) if type(ret) is list else ret

    def include_recipe(self):
        return self.get_value('build/include_recipe', True)

    def binary_has_prefix_files(self):
        ret = ensure_list(self.get_value('build/binary_has_prefix_files', []))
        if not isinstance(ret, list):
            raise RuntimeError('build/binary_has_prefix_files should be a list of paths')
        if sys.platform == 'win32':
            if any('\\' in i for i in ret):
                raise RuntimeError("build/binary_has_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix)

    def skip(self):
        return self.get_value('build/skip', False)

    def _get_contents(self, permit_undefined_jinja, allow_no_other_outputs=False,
                      bypass_env_check=False):
        '''
        Get the contents of our [meta.yaml|conda.yaml] file.
        If jinja is installed, then the template.render function is called
        before standard conda macro processors.

        permit_undefined_jinja: If True, *any* use of undefined jinja variables will
                                evaluate to an emtpy string, without emitting an error.
        '''
        try:
            import jinja2
        except ImportError:
            print("There was an error importing jinja2.", file=sys.stderr)
            print("Please run `conda install jinja2` to enable jinja template support", file=sys.stderr)  # noqa
            with open(self.meta_path) as fd:
                return fd.read()

        from conda_build.jinja_context import context_processor, UndefinedNeverFail, FilteredLoader

        path, filename = os.path.split(self.meta_path)
        loaders = [  # search relative to '<conda_root>/Lib/site-packages/conda_build/templates'
                   jinja2.PackageLoader('conda_build'),
                   # search relative to RECIPE_DIR
                   jinja2.FileSystemLoader(path)
                   ]

        # search relative to current conda environment directory
        conda_env_path = os.environ.get('CONDA_DEFAULT_ENV')  # path to current conda environment
        if conda_env_path and os.path.isdir(conda_env_path):
            conda_env_path = os.path.abspath(conda_env_path)
            conda_env_path = conda_env_path.replace('\\', '/')  # need unix-style path
            env_loader = jinja2.FileSystemLoader(conda_env_path)
            loaders.append(jinja2.PrefixLoader({'$CONDA_DEFAULT_ENV': env_loader}))

        undefined_type = jinja2.StrictUndefined
        if permit_undefined_jinja:
            # The UndefinedNeverFail class keeps a global list of all undefined names
            # Clear any leftover names from the last parse.
            UndefinedNeverFail.all_undefined_names = []
            undefined_type = UndefinedNeverFail

        loader = FilteredLoader(jinja2.ChoiceLoader(loaders), config=self.config)
        env = jinja2.Environment(loader=loader, undefined=undefined_type)

        env.globals.update(ns_cfg(self.config))
        env.globals.update(context_processor(self, path, config=self.config,
                                             permit_undefined_jinja=permit_undefined_jinja,
                                             allow_no_other_outputs=allow_no_other_outputs,
                                             bypass_env_check=bypass_env_check))

        # Future goal here.  Not supporting jinja2 on replaced sections right now.

        # we write a temporary file, so that we can dynamically replace sections in the meta.yaml
        #     file on disk.  These replaced sections also need to have jinja2 filling in templates.
        # The really hard part here is that we need to operate on plain text, because we need to
        #     keep selectors and all that.

        try:
            template = env.get_or_select_template(filename)
            rendered = template.render(environment=env)

            if permit_undefined_jinja:
                self.undefined_jinja_vars = UndefinedNeverFail.all_undefined_names
            else:
                self.undefined_jinja_vars = []

        except jinja2.TemplateError as ex:
            if "'None' has not attribute" in str(ex):
                ex = "Failed to run jinja context function"
            sys.exit("Error: Failed to render jinja template in {}:\n{}"
                     .format(self.meta_path, str(ex)))
        return rendered

    def __unicode__(self):
        '''
        String representation of the MetaData.
        '''
        return text_type(self.__dict__)

    def __str__(self):
        if PY3:
            return self.__unicode__()
        else:
            return self.__unicode__().encode('utf-8')

    def __repr__(self):
        '''
        String representation of the MetaData.
        '''
        return self.__str__()

    @property
    def uses_setup_py_in_meta(self):
        meta_text = ""
        if self.meta_path:
            with open(self.meta_path) as f:
                meta_text = f.read()
        return "load_setup_py_data" in meta_text or "load_setuptools" in meta_text

    @property
    def uses_regex_in_meta(self):
        meta_text = ""
        if self.meta_path:
            with open(self.meta_path) as f:
                meta_text = f.read()
        return "load_file_regex" in meta_text

    @property
    def needs_source_for_render(self):
        return self.uses_vcs_in_meta or self.uses_setup_py_in_meta or self.uses_regex_in_meta

    @property
    def uses_jinja(self):
        if not self.meta_path:
            return False
        with open(self.meta_path) as f:
            metayaml = f.read()
            matches = re.findall(r"{{.*}}", metayaml)
        return len(matches) > 0

    @property
    def uses_vcs_in_meta(self):
        """returns name of vcs used if recipe contains metadata associated with version control systems.
        If this metadata is present, a download/copy will be forced in parse_or_try_download.
        """
        vcs = None
        vcs_types = ["git", "svn", "hg"]
        # We would get here if we use Jinja2 templating, but specify source with path.
        if self.meta_path:
            with open(self.meta_path) as f:
                metayaml = f.read()
                for _vcs in vcs_types:
                    matches = re.findall(r"{}_[^\.\s\'\"]+".format(_vcs.upper()), metayaml)
                    if len(matches) > 0 and _vcs != self.meta['package']['name']:
                        if _vcs == "hg":
                            _vcs = "mercurial"
                        vcs = _vcs
                        break
        return vcs

    @property
    def uses_vcs_in_build(self):
        build_script = "bld.bat" if on_win else "build.sh"
        build_script = os.path.join(os.path.dirname(self.meta_path), build_script)
        for recipe_file in (build_script, self.meta_path):
            if os.path.isfile(recipe_file):
                vcs_types = ["git", "svn", "hg"]
                with open(recipe_file) as f:
                    build_script = f.read()
                    for vcs in vcs_types:
                        # commands are assumed to have 3 parts:
                        #   1. the vcs command, optionally with an exe extension
                        #   2. a subcommand - for example, "clone"
                        #   3. a target url or other argument
                        matches = re.findall(r"{}(?:\.exe)?(?:\s+\w+\s+[\w\/\.:@]+)".format(vcs),
                                            build_script, flags=re.IGNORECASE)
                        if len(matches) > 0 and vcs != self.meta['package']['name']:
                            if vcs == "hg":
                                vcs = "mercurial"
                            return vcs
        return None

    def extract_requirements_text(self):
        text = ""
        if self.meta_path:
            with open(self.meta_path) as f:
                recipe_text = f.read()
            if PY3 and hasattr(recipe_text, 'decode'):
                recipe_text = recipe_text.decode()
            match = re.search(r'(^requirements:.*?)(^test:|^extra:|^about:|^outputs:|\Z)',
                              recipe_text, flags=re.MULTILINE | re.DOTALL)
            text = match.group(1) if match else ""
        return text

    @property
    def uses_subpackage(self):
        outputs = self.get_section('outputs')
        in_reqs = False
        for out in outputs:
            if 'name' in out:
                name_re = re.compile(r"^{}(\s|\Z|$)".format(out['name']))
                in_reqs = any(name_re.match(req) for req in self.get_value('requirements/run'))
        subpackage_pin = False
        if not in_reqs and self.meta_path:
                data = self.extract_requirements_text()
                if data:
                    subpackage_pin = re.search("{{\s*pin_subpackage\(.*\)\s*}}", data)
        return in_reqs or bool(subpackage_pin)

    def validate_features(self):
        if any('-' in feature for feature in ensure_list(self.get_value('build/features'))):
            raise ValueError("- is a disallowed character in features.  Please change this "
                             "character in your recipe.")

    def copy(self):
        new = copy.copy(self)
        new.config = self.config.copy()
        new.meta = copy.deepcopy(self.meta)
        return new

    @property
    def noarch(self):
        return self.get_value('build/noarch')

    @noarch.setter
    def noarch(self, value):
        build = self.meta.get('build', {})
        build['noarch'] = value
        self.meta['build'] = build
        if not self.noarch_python and not value:
            self.config.reset_platform()
        elif value:
            self.config.host_platform = 'noarch'

    @property
    def noarch_python(self):
        return self.get_value('build/noarch_python')

    @noarch_python.setter
    def noarch_python(self, value):
        build = self.meta.get('build', {})
        build['noarch_python'] = value
        self.meta['build'] = build
        if not self.noarch and not value:
            self.config.reset_platform()
        elif value:
            self.config.host_platform = 'noarch'

    def reconcile_metadata_with_output_dict(self, output_metadata, output_dict):
        output_metadata.meta['package']['name'] = output_dict.get('name', self.name())

        # make sure that subpackages do not duplicate tests from top-level recipe
        test = output_metadata.meta.get('test', {})
        if output_dict.get('name') != self.name():
            if 'commands' in test:
                del test['commands']
            if 'imports' in test:
                del test['imports']

        # make sure that subpackages do not duplicate top-level entry-points
        build = output_metadata.meta.get('build', {})
        if 'entry_points' in output_dict:
            build['entry_points'] = output_dict['entry_points']
        elif 'entry_points' in build:
            del build['entry_points']
        output_metadata.meta['build'] = build

        # reset these so that reparsing does not reset the metadata name
        output_metadata.path = ""
        output_metadata.meta_path = ""

    def get_output_metadata(self, output):
        self.config.target_subdir = output.get('target', self.config.host_subdir)
        if self.name() == output.get('name'):
            output_metadata = self
        else:
            output_metadata = self.copy()
            self.reconcile_metadata_with_output_dict(output_metadata, output)

        if 'type' in output and output['type'] != 'conda':
            name = output.get('name', self.name()) + '_' + output['type']
            output_metadata.meta['package']['name'] = name

        output_reqs = output.get('requirements', {})
        if hasattr(output_reqs, 'keys'):
            build_reqs = output_reqs.get('build', [])
            run_reqs = output_reqs.get('run', [])
            constrain_reqs = output_reqs.get('run_constrained', [])
        else:
            output_reqs = ensure_list(output_reqs)
            build_reqs = output_reqs
            run_reqs = output_reqs
            constrain_reqs = []

        if 'name' in output:
            # since we are copying reqs from the top-level package, which
            #   can depend on subpackages, make sure that we filter out
            #   subpackages so that they don't depend on themselves
            subpackage_pattern = re.compile(r'(?:^{}(?:\s|$|\Z))'.format(output['name']))
            build_reqs = [req for req in build_reqs if not subpackage_pattern.match(req)]
            run_reqs = [req for req in run_reqs if not subpackage_pattern.match(req)]

        if 'about' in output:
            output_metadata.meta['about'] = output['about']

        requirements = {'build': build_reqs, 'run': run_reqs}
        if constrain_reqs:
            requirements['run_constrained'] = constrain_reqs
        output_metadata.meta['requirements'] = requirements
        output_metadata.meta['package']['version'] = output.get('version') or self.version()
        extra = self.meta.get('extra', {})
        if self.name() == output.get('name') and 'requirements' not in output:
            output['requirements'] = requirements
        output_metadata.meta['extra'] = extra
        output_metadata.final = False
        if self.name() != output_metadata.name():
            extra = self.meta.get('extra', {})
            extra['parent_recipe'] = {'path': self.path, 'name': self.name(),
                                    'version': self.version()}
            output_metadata.meta['extra'] = extra
        output_metadata.noarch = output.get('noarch', False)
        output_metadata.noarch_python = output.get('noarch_python', False)
        # primarily for tests - make sure that we keep the platform consistent (setting noarch
        #      would reset it)
        if (not (output_metadata.noarch or output_metadata.noarch_python) and
                self.config.platform != output_metadata.config.platform):
            output_metadata.config.platform = self.config.platform
        if 'run_exports' in output and output['run_exports']:
            build = output_metadata.meta.get('build', {})
            build['run_exports'] = output['run_exports']
            output_metadata.meta['build'] = build

        # ensure that packaging scripts are copied over into the workdir
        if 'script' in output:
            utils.copy_into(os.path.join(self.path, output['script']), self.config.work_dir)

        # same thing, for test scripts
        test_script = output.get('test', {}).get('script')
        if test_script:
            utils.copy_into(os.path.join(self.path, test_script),
                            os.path.join(self.config.work_dir, test_script))

        return output_metadata

    def get_output_metadata_set(self, permit_undefined_jinja=False,
                                permit_unsatisfiable_variants=True):
        out_metadata_map = {}

        for variant in (self.config.variants if hasattr(self.config, 'variants')
                        else [self.config.variant]):
            om = self.copy()
            om.final = False
            om.config.variant = variant
            om.parse_until_resolved(allow_no_other_outputs=True, bypass_env_check=True)
            outputs = get_output_dicts_from_metadata(om)

            try:
                for out in outputs:
                    out_metadata_map[HashableDict(out)] = om.get_output_metadata(out)
            except SystemExit:
                if not permit_undefined_jinja:
                    raise
                out_metadata_map = {}

        # format here is {output_dict: metadata_object}
        render_order = toposort(out_metadata_map, phase='build')

        conda_packages = OrderedDict()
        non_conda_packages = []
        for output_d, m in render_order.items():
            if not output_d.get('type') or output_d['type'] == 'conda':
                conda_packages[m.name(), HashableDict(m.config.variant)] = (output_d, m)
            else:
                # for wheels and other non-conda packages, just append them at the end.
                #    no deduplication with hashes currently.
                # hard part about including any part of output_d
                #    outside of this func is that it is harder to
                #    obtain an exact match elsewhere
                non_conda_packages.append((output_d, m))

        # early stages don't need to do the finalization.  Skip it until the later stages
        #     when we need it.
        if not permit_undefined_jinja:
            # The loop above gives us enough info to determine the build order.  After that,
            #    we can "finalize" the metadata (fill in version pins) for the build metadata.
            #    This does not, however, account for circular dependencies
            #    where a runtime exact dependency pin on a downstream build
            #    subpackage in an upstream subpackage changes the upstream's
            #    hash, which then changes the downstream package's hash, which
            #    then recurses infinitely

            # In general, given upstream a and downstream b, b can depend on a
            # exactly, but a can only have version constraints on b at run or
            # run_exports, not exact=True

            # 3 passes here:
            #    1. fill in fully-resolved build-time dependencies
            #    2. fill in fully-resolved run-time dependencies.  Note that circular dependencies
            #       are allowed, but you can't have exact=True for circular run-time dependencies
            #    3. finally, everything should be filled in and done.
            for i in range(3):
                conda_packages = finalize_outputs_pass(self, i, conda_packages,
                                        permit_unsatisfiable_variants=permit_unsatisfiable_variants)
                self.other_outputs = conda_packages
        return list(conda_packages.values()) + non_conda_packages
