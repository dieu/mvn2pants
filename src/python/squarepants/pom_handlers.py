#!/usr/bin/python
#
# This module contains XML parsing handlers and other helper classes.
# Many of these objects have factory constructors in PomUtils which
# will cache a singleton instance.
#

from abc import ABCMeta, abstractmethod
from collections import defaultdict

import logging
import os
import re
import sys
import xml.sax

from generation_utils import GenerationUtils
from target_template import Target


logger = logging.getLogger(__name__)

def reset_caches():
  LocalTargets.reset()
  CachedDependencyInfos.reset()
  DependencyManagementFinder.reset()
  PomProvidesTarget.reset()
  GenericPomInfo.reset()


class MalformattedPOMException(Exception):
  """Thrown when a pom.xml is malformatted."""

  def __init__(self, *vargs):
    super(MalformattedPOMException, self).__init__(self._assemble_error_message(*vargs))

  @classmethod
  def _assemble_error_message(cls, pom_file_path, error):
    original_message = str(error)

    details = ''
    match = re.match(r'.*?pom[.]xml:(\d+):(\d+):.*', original_message)
    if match:
      # Attempt to print out the offending part of the pom.xml by line and column number.
      try:
        with open(pom_file_path, 'r') as f:
          line_no = int(match.group(1))-1
          column_no = int(match.group(2))-2
          lines = f.readlines()
          details = '\n{prev_line}{error_line}{column_text}^\n{next_line}'.format(
            prev_line=lines[line_no-1] if line_no > 0 else '',
            error_line=lines[line_no],
            next_line=lines[line_no+1] if line_no < len(lines)-1 else '',
            column_text=' '*max(0, column_no),
          )
      except:
        pass

    return 'Malformatted pom.xml {}:\n{}{}'.format(pom_file_path, error, details)


class PomContentHandler(xml.sax.ContentHandler):
  """Base class for a simple sax parser to read a maven pom.xml file

    Tracks the current path as an array in self.path
    Tracks property defs in the array self.properties
  """

  invocations = 0

  def __init__(self):
    xml.sax.ContentHandler.__init__(self)
    # path of tags leading up to this element
    self.path = []
    # text content of the current node being parsed (can be retrieved in endElement)
    self.content = ""
    self.contentStack=[]

    self.properties = {}
    self.artifactId = ""
    self.groupId = ""
    # dict containing elements defined in <project><parent> element
    self.parent = {}


  def startElement(self, name, attrs):
    """invoke this at the beginning of subclass call to startElement()

    :param string name: name of the current element.
    :param list<string> attrs: xml attributes of the current element.
    """
    self.contentStack.append(self.content)
    self.content = ""
    self.path.append(name)

  def characters(self, content):
    """invoked with the element content as a string"""
    self.content += content.encode('ascii','ignore')

  def endElement(self, name):
    """invoke this at the end of subclass call to endElement()

    :param string name: name of the current element that is being closed.
    """

    if self.path == ["project", "groupId"]:
      self.groupId = self.content.strip()
    elif self.path == ["project", "artifactId"]:
      self.artifactId = self.content.strip()

    # Parse properties of the form: <project><properties><foo>fooValue</foo></properties></project>
    elif self.pathStartsWith(["project", "properties"]):
      self.properties[name] = self.content.strip()
    if self.pathStartsWith(["project", "parent"]):
      self.parent[name] = self.content.strip()
    self.path.pop(len(self.path) - 1)
    self.content = self.contentStack.pop()

  def endDocument(self):
    """invoked at the end of the XML document. """
    xml.sax.ContentHandler.endDocument(self)
    PomContentHandler.invocations += 1

  def pathStartsWith(self, path_prefix):
    """Convenience routine to see if the path to the current element matches the path passed.

    :param list path_prefix: prefix to compare to the current path.
    """
    if (len(self.path) >= len(path_prefix)) and self.path[0:len(path_prefix)] == path_prefix:
      return True
    return False

  def pathEndsWith(self, path_suffix):
    """Convenience routine to see if the path to the current element matches the path passed.

    :param list path_suffix: suffix to compare to the current path.
    """
    if (len(self.path) >= len(path_suffix)) and self.path[-len(path_suffix):] == path_suffix:
      return True
    return False

  def properties(self):
    """returns a hash of known property name, value pairs"""
    return self.properties

  @staticmethod
  def num_invocations():
    """Static method used for performance analysis"""
    return PomContentHandler.invocations


class TopPomContentHandler(PomContentHandler):
  """SAX parser to read top level Maven pom.xml file.

      Just parses out the 'module' definitions.  Don't instantiate this yourself, use the
      cached factory method PomUtil.top_pom_content_handler().
  """
  def __init__(self):
    PomContentHandler.__init__(self)
    self.modules = []

  def endElement(self, name):
    if self.path == ["project", "modules", "module"]:
      self.modules.append(self.content.strip())

    PomContentHandler.endElement(self, name)

  def modules(self):
    return self.modules;


class _DMFPomContentHandler(PomContentHandler):
  """Dependency Management Finder Content Handler

  Used to parse <dependencyManagement> tags.
  """
  def __init__(self):
    PomContentHandler.__init__(self)
    # Array containing hash of { groupId => "", artifactId => "", version => "" exclusions => [exclusions]}
    self.dependency_management = []

    # Temporary storage for data parsed from sub-elements
    self._dependency = {}
    self._dependency_excludes = []
    self._dependency_exclude = {}

  def endElement(self, name):
    if self.pathStartsWith(["project", "dependencyManagement", "dependencies", "dependency", "exclusions", "exclusion"]):
      if len(self.path) == 7:
        self._dependency_exclude[self.path[-1]] = self.content.strip()
      elif (len(self.path) == 6):
        self._dependency_excludes.append(self._dependency_exclude)
        self._dependency_exclude = {}
    elif self.pathStartsWith(["project", "dependencyManagement", "dependencies", "dependency"]):
      # Parse 'dependencies' under the 'dependencyManagement' tag
      if len(self.path) == 5:
        self._dependency[self.path[-1]] = self.content.strip()
      elif len(self.path) == 4:
        # end of <dependency> definition. Save it.

        # override the 'exclusions' field with the array we built up
        self._dependency['exclusions'] = self._dependency_excludes
        self._dependency_excludes = []
        self.dependency_management.append(self._dependency)
        self._dependency = {}

    PomContentHandler.endElement(self, name)

  def dependencyManagement(self):
    return self.dependency_management


class _DFPomContentHandler(PomContentHandler):
  """Dependency Finder Content Handler

  Used to parse <dependency> tags.
  """

  def __init__(self):
    PomContentHandler.__init__(self)
    # Array containing hash of { groupId => "", artifactId => "", version => "" }
    self.dependencies = []
    self.dependency = {}
    self.dependency_excludes = []
    self.dependency_exclude = {}


  def endElement(self, name):
    # Add the parent pom as one of the dependencies
    if self.pathStartsWith(["project", "parent"]):
      if len(self.path) == 3:
        self.dependency[self.path[-1]] = self.content.strip()
      if len(self.path) == 2:
        self.dependencies.append(self.dependency)
        self.dependency = {}

    if self.pathStartsWith(["project", "dependencies", "dependency", "exclusions", "exclusion"]):
      if len(self.path) == 6:
        self.dependency_exclude[self.path[-1]] = self.content.strip()
      elif (len(self.path) == 5):
        self.dependency_excludes.append(self.dependency_exclude)
        self.dependency_exclude = {}
    elif self.pathStartsWith(["project",  "dependencies", "dependency"]):
      if len(self.path) == 4:
        # Parse members of '<dependency>'
        self.dependency[self.path[-1]] = self.content.strip()
      elif len(self.path) == 3:
        # end of <dependency> definition. Save it.

        # override the 'exclusions' field with the array we built up
        self.dependency['exclusions'] = self.dependency_excludes
        self.dependency_excludes = []
        self.dependencies.append(self.dependency)
        self.dependency = {}

    PomContentHandler.endElement(self, name)


class PomHandlerManager(PomContentHandler):

  def __init__(self):
    PomContentHandler.__init__(self)
    info_types = [
      WireInfo,
      SpecialPropertiesInfo,
      SignedJarInfo,
    ]
    self.infos = { info_type: info_type() for info_type in info_types }
    self.handlers = [info.create_content_handler(self) for info in self.infos.values()]

  def endElement(self, name):
    for handler in self.handlers:
      handler.endElement(name)

    PomContentHandler.endElement(self, name)


class GenericPomHandler(object):
  def __init__(self, parent):
    self.parent = parent

  def pathStartsWith(self, path_prefix):
    return self.parent.pathStartsWith(path_prefix)

  def pathEndsWith(self, path_suffix):
    return self.parent.pathEndsWith(path_suffix)

  @property
  def path(self):
    return self.parent.path

  @property
  def content(self):
    return self.parent.content


class GenericPomInfo(object):
  __metaclass__ = ABCMeta
  # Dict which maps class names to dicts which map pom files to infos.
  # I.e., { type -> { pom_file -> pom_info } }.
  _ALL_CACHES = defaultdict(dict)

  class MissingInfoError(Exception):
    """Raised when a GenericPomInfo subclass instance is requested, but never created.

    This probably means that it needs to be added to the info_types list in PomHandlerManager's
    constructor.
    """

  @abstractmethod
  def create_content_handler(self, parent):
    """Creates a generic pom handler instance for this info."""

  @classmethod
  def get_cache(cls):
    return cls._ALL_CACHES[cls]

  @classmethod
  def reset(cls):
    cls._ALL_CACHES.clear()

  @classmethod
  def from_pom(cls, source_file_name, rootdir=None):
    key = (source_file_name, rootdir)
    if key in cls.get_cache():
      return cls.get_cache()[key]
    pom_handler = PomHandlerManager()
    full_source_path = source_file_name
    if rootdir:
      full_source_path = os.path.join(rootdir, full_source_path)
    try:
      with open(full_source_path) as source:
        xml.sax.parse(source, pom_handler)
    except IOError:
      return None
    except xml.sax.SAXParseException as e:
      raise MalformattedPOMException(source_file_name, e)

    for info_type, info in pom_handler.infos.items():
      cls._ALL_CACHES[info_type][key] = info
    if key not in cls.get_cache():
      raise cls.MissingInfoError('PomHandler has no {}!'.format(cls.__name__))
    return cls.get_cache()[key]


class WireInfo(GenericPomInfo):
  """Holds info for wire generation."""

  def __init__(self):
    self.protos = []
    self.roots = []
    self.service_writer = None
    self.no_options = None
    self.enum_options = []
    self.registry_class = None
    self.artifacts = defaultdict(dict)
    self.proto_source_directory = None

  def create_content_handler(self, parent):
    return WirePomHandler(parent, self)


class WirePomHandler(GenericPomHandler):
  """Finds relevant data for wire generation."""

  def __init__(self, parent, info):
    super(WirePomHandler, self).__init__(parent)
    self.info = info
    self.plugin_groupId = None
    self.unpack_groupId = None
    self.unpack_artifactId = None

  def endElement(self, name):
    if self.pathStartsWith(['project', 'build', 'plugins', 'plugin', 'executions', 'execution',
                            'configuration', 'artifactItems', 'artifactItem']):
      if self.pathEndsWith(['groupId']):
        self.unpack_groupId = self.content.strip()
      elif self.pathEndsWith(['artifactId']):
        self.unpack_artifactId = self.content.strip()
      elif self.pathEndsWith(['outputDirectory']):
        self.info.artifacts[self._unpack_artifact]['output_directory'] = self.content.strip()
      elif self.pathEndsWith(['includes']):
        self.info.artifacts[self._unpack_artifact]['includes'] = self.content.strip()

    if self.path == ['project', 'build', 'plugins', 'plugin', 'groupId',]:
      self.plugin_groupId = self.content.strip()

    if self.plugin_groupId == 'com.squareup.wire':
      if self.path == ['project', 'build', 'plugins', 'plugin', 'configuration', 'protoFiles', 'protoFile']:
        self.info.protos.append(self.content.strip())
      elif self.path == ['project', 'build', 'plugins', 'plugin', 'configuration', 'roots', 'root']:
        self.info.roots.append(self.content.strip())
      elif self.pathEndsWith(['plugin', 'configuration', 'serviceWriter',]):
        self.info.service_writer = self.content.strip()
      elif self.pathEndsWith(['plugin', 'configuration','noOptions',]):
        if self.content.strip() == 'true':
          self.info.no_options = True
        else:
          self.info.no_options = None
      elif self.pathEndsWith(['plugin', 'configuration','enumOptions','enumOption',]):
        self.info.enum_options.append(self.content.strip())
      elif self.pathEndsWith(['plugin', 'configuration','registryClass',]):
        self.info.registry_class = self.content.strip()
      elif self.pathEndsWith(['plugin', 'configuration', 'protoSourceDirectory',]):
        self.info.proto_source_directory = self.content.strip()

  @property
  def _unpack_artifact(self):
    return (self.unpack_groupId, self.unpack_artifactId)


class SpecialPropertiesInfo(GenericPomInfo):
  """Holds information for special properties, like those only set for particular OS's."""
  def __init__(self):
    self.properties = {}

  def create_content_handler(self, parent):
    return SpecialPropertiesHandler(parent, self)


class SpecialPropertiesHandler(GenericPomHandler):

  def __init__(self, parent, info):
    super(SpecialPropertiesHandler, self).__init__(parent)
    self.info = info
    self._profile_active = False

  def _same_platform(self, a, b):
    a, b = a.lower().strip(), b.lower().strip()
    if a == b: # They are trivially the same platform.
      return True
    # Check to see if we fall into some known synonyms.
    platforms = [
      {'darwin', 'mac os x', 'posix'},
      {'win32', 'windows'},
    ]
    return any(a in group and b in group for group in platforms)

  def endElement(self, name):
    if not self.pathStartsWith(['project', 'profiles', 'profile']):
      return
    if name.strip() == 'profile':
      self._profile_active = False
      return
    if self.path == ['project', 'profiles', 'profile', 'activation', 'os', 'name']:
      self._profile_active = self._same_platform(sys.platform, self.content.strip())
      return
    if self._profile_active:
      if self.path[-2] == 'properties':
        self.info.properties[name.strip()] = self.content.strip()


class SignedJarInfo(GenericPomInfo):
  """Holds information about signed_jars."""

  def __init__(self):
    self.signed_jars = []
    self.excludes = []
    self.manifest_entries = {}
    self.strip_version = False

  def create_content_handler(self, parent):
    return SignedJarHandler(parent, self)

class SignedJarHandler(GenericPomHandler):

  def __init__(self, parent, info):
    super(SignedJarHandler, self).__init__(parent)
    self.info = info
    self._execution_id = None
    self._execution_phase = None
    self._execution_goals = set()
    self._execution_output_dir = None

  def endElement(self, name):
    if self.pathStartsWith(['project', 'build', 'plugins', 'plugin', 'executions']):
      if self._execution_phase == 'package':
        if 'shade' in self._execution_goals:
          if self.path[-2] == 'manifestEntries' and name:
            self.info.manifest_entries[name.strip()] = self.content.strip()
          if self.path[-3] == 'artifactSet' and name == 'exclude':
            self.info.excludes.append(tuple(self.content.strip().split(':')))
        elif 'copy-dependencies' in self._execution_goals:
          if name == 'outputDirectory':
            self._execution_output_dir = self.content.strip()
          elif name == 'includeArtifactIds':
            artifact_ids = self.content.strip().split(',')
            if self._execution_output_dir and '/lib-signed' in self._execution_output_dir:
              self.info.signed_jars.extend(artifact_ids)
          elif name == 'stripVersion':
            self.info.strip_version = self.content.strip().lower() == 'true'
      if name == 'id':
        self._execution_id = self.content.strip()
      elif name == 'phase':
        self._execution_phase = self.content.strip()
      elif name == 'goal':
        self._execution_goals.add(self.content.strip())
      elif name == 'execution':
        # Execution ended, clear variables.
        self._execution_id = None
        self._execution_phase = None
        self._execution_goals = set()
        self._execution_output_dir = None


class DependencyInfo(object):
  """Process a module pom.xml file looking for dependencies."""

  def __init__(self, source_file_name, rootdir=None):
    self._artifactId = None
    self._groupId = None
    self._properties = {}
    self._dependencies = []
    self._parent = None
    self._source_file_name = source_file_name
    self._rootdir = rootdir
    # Parse the pom file.
    self._parse(source_file_name, rootdir)

  def _parse(self, source_file_name, rootdir):
    pomHandler = _DFPomContentHandler()
    if rootdir:
      full_source_path = os.path.join(rootdir, source_file_name)
    else:
      full_source_path = source_file_name

    if os.path.basename(full_source_path) != 'pom.xml':
      full_source_path = os.path.join(full_source_path, 'pom.xml')

    try:
      with open(full_source_path) as source:
        xml.sax.parse(source, pomHandler)
    except IOError:
      # assume this file has been removed for a good reason and just continue normally
      return
    except xml.sax.SAXParseException as e:
      raise MalformattedPOMException(source_file_name, e)

    self._artifactId = pomHandler.artifactId
    self._groupId = pomHandler.groupId
    self._parent = pomHandler.parent

    # Since dependencies are just dicts, we keep track of keys separately.  Maybe in the future
    # it would be good to create a Dependency data structure and return a set or ordered dictionary
    # of those instances instead.
    dep_keys = set()
    for dep in pomHandler.dependencies:
      if 'groupId' in dep and 'artifactId' in dep:
        dep_keys.add('{0} {1}'.format(dep['groupId'], dep['artifactId']))
        self._dependencies.append(dep)

    parent_df = self.parent
    if parent_df:
      for dep in parent_df.dependencies:
        key = '{0} {1}'.format(dep['groupId'], dep['artifactId'])
        # dependencies declared in parent poms can be overridden
        if key not in dep_keys:
          self._dependencies.append(dep)
      self._properties.update(parent_df.properties)

    self._properties.update(pomHandler.properties)
    for key, value in self._properties.items():
      self._properties[key] = GenerationUtils.symbol_substitution(self._properties, value)
    self._dependencies = GenerationUtils.symbol_substitution_on_dicts(self._properties,
                                                                      self._dependencies)

  @property
  def source_file_name(self):
    return self._source_file_name

  @property
  def root_directory(self):
    return self._rootdir

  @property
  def parent(self):
    if 'groupId' in self._parent \
        and 'artifactId' in self._parent \
        and 'relativePath' in self._parent:
      relative_parent_pom = self._parent['relativePath']
      if os.path.basename(relative_parent_pom) != 'pom.xml':
        relative_parent_pom = os.path.join(relative_parent_pom, 'pom.xml')
      parent_path = os.path.normpath(os.path.join(os.path.dirname(self.source_file_name),
                                                  relative_parent_pom))
      parent_df = CachedDependencyInfos().get(parent_path, rootdir=self.root_directory)
      return parent_df
    return None

  @property
  def artifactId(self):
    """The contents of the <artifactId> tag for the project in the pom.xml file."""
    return self._artifactId

  @property
  def groupId(self):
    """The contents of the <groupId> tag for the project in the pom.xml file."""
    return self._groupId

  @property
  def properties(self):
    """A dictionary of the contents of the <properties> tag from the pom.xml file."""
    return self._properties

  @property
  def dependencies(self):
    """An array of dictionaries with contents of <project><dependencies><dependency> tags."""
    return self._dependencies


class CachedDependencyInfos(object):
  """Keeps cached instances of DependencyInfos so we only have to process them once."""
  cached_dfs = {}

  @classmethod
  def reset(cls):
    """Reset cache for unit testing."""
    CachedDependencyInfos.cached_dfs = {}

  @classmethod
  def get(cls, source_file_name, rootdir=None):
    """Returns a cached instance of DependencyInfo or creates a new one if needed."""
    if rootdir:
      key = os.path.join(rootdir, source_file_name)
    else:
      key = source_file_name

    if key not in cls.cached_dfs:
      cls.cached_dfs[key] = DependencyInfo(source_file_name, rootdir=rootdir)
    return cls.cached_dfs[key]


class DependencyManagementFinder():
  """ Searches a pom file for <dependencyManagement> tags.

  Do not instantiate.  Use the factory method PomUtils.dependency_managment_finder()
  """
  _cache = {}

  def __init__(self, rootdir=None):
    """:param string rootdir: root directory of the repo to analyze"""
    self._rootdir = rootdir

  @classmethod
  def reset(cls):
    cls._cache = {}

  def find_dependencies(self, source_file_name):
    """Process a pom.xml file containing the <dependencyManagement> tag.
       Returns an array of dictionaries containing the children of the <dependency> tag.
    """
    if source_file_name in DependencyManagementFinder._cache:
      return DependencyManagementFinder._cache[source_file_name]
    if self._rootdir:
      source_file_name = os.path.join(self._rootdir, source_file_name)
    pomHandler = _DMFPomContentHandler()
    with open(source_file_name) as source:
      xml.sax.parse(source, pomHandler)
    source.close()
    return GenerationUtils.symbol_substitution_on_dicts(pomHandler.properties,
                                                        pomHandler.dependency_management)



class PomProvidesTarget():
  # indexed by <artifactId> mapped to a list of pom file names
  artifacts_in_modules = {}
  # indexed by <groupId>.<artifactId> mapped to a list of pom file names.
  targets_in_modules = {}

  def __init__(self, top_pom_content_handler):
    """Creates a singleton for the lookups so we don't parse the XML over and over (takes about 500ms)"""

    self._top_pom_content_handler = top_pom_content_handler
    if not PomProvidesTarget.artifacts_in_modules:
      self.init_artifacts_in_modules()

  @classmethod
  def reset(cls):
    cls.artifacts_in_modules = {}
    cls.targets_in_modules = {}

  def add_to_dict(self, dictionary, key, value):
    if not dictionary.has_key(key):
      dictionary[key] = []
    dictionary[key].append(value)

  def init_artifacts_in_modules(self):
    modules = self._top_pom_content_handler.modules
    logger.debug("modules are {modules}".format(modules=modules))
    for module in modules:
      pom = module + "/pom.xml"
      finder = CachedDependencyInfos.get(pom)
      self.add_to_dict(PomProvidesTarget.artifacts_in_modules, finder.artifactId, pom)
      self.add_to_dict(PomProvidesTarget.targets_in_modules,
                       "{groupId}.{artifactId}".format(groupId=finder.groupId,
                                                       artifactId=finder.artifactId),
                       pom)

  def find_artifact(self, query):
    poms = []
    if PomProvidesTarget.artifacts_in_modules.has_key(query):
      poms = PomProvidesTarget.artifacts_in_modules[query]
    return poms

  def find_target(self, query):
    """looks for groupId.artifactId. There should be only one.
    :param string query: target name to find in the list of targets
    :return: list of pom.xml paths that contain the specified target name
    """
    poms = []
    if PomProvidesTarget.targets_in_modules.has_key(query):
      poms = PomProvidesTarget.targets_in_modules[query]
    return poms

  def targets(self):
    return PomProvidesTarget.targets_in_modules.keys()


class DepsFromPom():
  """ Given a module's pom.xml file, pull out the list of dependencies formatted for using a pants BUILD file"""

  def __init__(self, pom_provides_target, rootdir=None, exclude_project_targets=None):
    """:param string rootdir: root directory of the repo to analyze"""
    self.exclude_project_targets = exclude_project_targets or []
    self.target = ""
    self.artifact_id = ""
    self.group_id = ""
    self.properties = {}
    self._source_file_name = ""
    self._rootdir=rootdir
    self._pom_provides_target = pom_provides_target

  def get(self, source_file_name, raw_deps=False):
    """:param source_file_name: relative path of pom.xml file to analyze
    :return: tuple of (list of library deps, list of test refs)
    """
    df = CachedDependencyInfos.get(source_file_name, rootdir=self._rootdir)
    deps = sorted(df.dependencies)
    self.target = "{groupId}.{artifactId}".format(groupId=df.groupId, artifactId=df.artifactId)
    self.group_id = df.groupId
    self.artifact_id = df.artifactId
    self.properties = df.properties

    self._source_file_name = source_file_name
    lib_deps, test_deps = [], []
    for dep in deps:
      if 'scope' in dep and dep['scope'] == 'test':
        test_deps.append(dep)
      else:
        lib_deps.append(dep)

    if raw_deps:
      return lib_deps, test_deps

    lib_pants_refs = self.build_pants_refs(lib_deps)
    test_pants_refs = self.build_pants_refs(test_deps)
    return lib_pants_refs, test_pants_refs

  def get_closest_match(self, project_root, target_prefix):
    """Looks for a target close to the one specified in the list of known targets.  If one maven
    project depends on another, there is usually a .../java:lib target, but not always.  This
    method substitutes in a .../proto:proto target if a .../java:lib does not exist.
    :param project_root: the project directory where the pom.xml for the project is found
    :param target_prefix: prefix of the target we're looking for.
    :return: a target that matches the highest precedent target for that project.
    """
    # When targets is empty, we assume we could not build the list and thus just don't do anything.
    target_prefix = os.path.join(project_root, target_prefix)
    targets = LocalTargets.get(project_root)

    # order is important in the list below. if java:lib exists, it will depend on the others.
    # proto:proto and wire_proto:wire_proto will depend on resources if they exist.
    for suffix in ['java:lib', 'proto:proto', 'wire_proto:wire_proto', 'resources:resources']:
      tmp = target_prefix + suffix
      if tmp in targets:
        return tmp
    return None

  def build_pants_refs(self, deps):
    from pom_utils import PomUtils
    pants_refs = []
    for dep in deps:
      dep_target = "{groupId}.{artifactId}".format(groupId=dep['groupId'],
                                                   artifactId=dep['artifactId'])
      if PomUtils.is_local_dep(dep_target):
        # find the POM that contains this artifact
        poms = self._pom_provides_target.find_target(dep_target)
        if len(poms) > 0:
          project_root = os.path.dirname(poms[0])
        else:
          project_root = dep['artifactId']
        if project_root in self.exclude_project_targets:
          continue
        if dep.has_key('type') and dep['type'] == 'test-jar':
          target_prefix = 'src/test/'
        else:
          target_prefix = "src/main/"
        target_name = self.get_closest_match(project_root, target_prefix)
        if target_name:
          pants_refs.append("'{0}'".format(target_name))

    # Print 3rdparty dependencies after the local deps
    for dep in deps:
      dep_target = "{groupId}.{artifactId}".format(groupId=dep['groupId'],
                                                   artifactId=dep['artifactId'])
      if PomUtils.is_third_party_dep(dep_target):
        logger.debug("dep_target {target} is not local".format(target=dep_target))
        pants_refs.append("'3rdparty:{target}'".format(target=dep_target))

    # Print the external deps last
    for dep in deps:
      dep_target = "{groupId}.{artifactId}".format(groupId=dep['groupId'],
                                                   artifactId=dep['artifactId'])
      if PomUtils.is_external_dep(dep_target):
        if not dep.has_key('version'):
          raise Exception(
            "Expected artifact {artifactId} group {groupId} in pom {pom_file} to have a version."
            .format(artifactId=dep['artifactId'],
                    groupId=dep['groupId'],
                    pom_file=self._source_file_name))
        jar_excludes = ""
        if dep.has_key('exclusions'):
          for jar_exclude in dep['exclusions']:
            jar_excludes += ".exclude(org='{groupId}', name='{artifactId}')".format(
              groupId=jar_exclude['groupId'], artifactId=jar_exclude['artifactId'])
        classifier = dep.get('classifier') # Important to use 'get', so we default to None.
        if dep.get('type') == 'test-jar':
          # In our repo, this is special, *or* ivy doesn't translate this correctly.
          # Transform this into a classifier named 'tests' instead
          classifier = 'tests'
          type_ = None
        else:
          type_ = dep.get('type')

        pants_refs.append(Target.jar.format(
          org=dep['groupId'],
          name=dep['artifactId'],
          rev=dep['version'],
          classifier=classifier,
          type_=type_,
        ) + jar_excludes)
    return pants_refs

  def get_property(self, name):
    if self.properties.has_key(name):
      return self.properties[name]
    return ""


class LocalTargets:
  """A util class that looks at a maven project root and uses its structure to determine
    local dependencies.  Results are cached.
  """
  _cache = {}
  _types = {
    'src/main/java': ['lib'],
    'src/main/proto': ['proto'],
    'src/main/resources': ['resources'],
    'src/test/java': ['lib','test'],
    'src/test/proto' : ['proto'],
    'src/test/resources' : ['resources'],
    'src/main/wire_proto': ['wire_proto'],
    }

  @classmethod
  def reset(cls):
    cls._cache = {}

  @classmethod
  def get(cls, project_root):
    """:param project_root: path to the root of the project where the pom.xml is located
    :return: a set of all targets based on the presence of directories."""

    if cls._cache.has_key(project_root):
      return cls._cache[project_root]

    def is_candidate_dir(source_dir):
      if not os.path.isdir(source_dir):
        return False
      files = os.listdir(source_dir)
      if files:
        return True
      return False

    result = set();
    result.add("{project_root}:lib".format(project_root=project_root))
    for path in cls._types.keys():
      if is_candidate_dir(os.path.join(project_root, path)):
        for target in cls._types[path]:
          result.add("{path}:{target}".format(path=os.path.join(project_root, path), target=target))
    # HACK for external protos - the src/main/proto directory may not exist yet and will likely
    # be empty
    if project_root.startswith("external-protos"):
      result.add("{project_root}/src/main/proto:proto".format(project_root=project_root))
    cls._cache[project_root] = result
    return result

