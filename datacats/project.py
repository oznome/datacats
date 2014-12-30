from os.path import abspath, split as path_split, expanduser, isdir, exists
from os import makedirs, getcwd
import subprocess
import shutil
from string import uppercase, lowercase, digits
from random import SystemRandom
from ConfigParser import SafeConfigParser, Error as ConfigParserError

from datacats.validate import valid_name
from datacats.docker import (web_command, run_container, remove_container,
    inspect_container, is_boot2docker, data_only_container, docker_host)

class ProjectError(Exception):
    def __init__(self, message, format_args=()):
        self.message = message
        self.format_args = format_args
        super(ProjectError, self).__init__(message, format_args)

    def __str__(self):
        return self.message.format(*self.format_args)


class Project(object):
    """
    DataCats project settings object

    Create with Project.new(path) or Project.load(path)
    """
    def __init__(self, name, target, datadir, ckan_version):
        self.name = name
        self.target = target
        self.datadir = datadir
        self.ckan_version = ckan_version

    def save(self):
        """
        Save project settings into project directory
        """
        cp = SafeConfigParser()
        cp.add_section('datacats')
        cp.set('datacats', 'name', self.name)
        cp.set('datacats', 'ckan_version', self.ckan_version)
        cp.add_section('passwords')
        for n in sorted(self.passwords):
            cp.set('passwords', n.lower(), self.passwords[n])
        cp.write(open(self.target + '/.datacats-project', 'w'))

    @classmethod
    def new(cls, path, ckan_version):
        """
        Return a Project object with settings for a new project.
        No directories or containers are created by this call.

        :params path: location for new project directory, may be relative

        Raises ProjectError if directories or project with same
        name already exits.
        """
        workdir, name = path_split(abspath(expanduser(path)))

        if not valid_name(name):
            raise ProjectError('Please choose a project name starting with a '
                'letter and including only lowercase letters and digits')
        if not isdir(workdir):
            raise ProjectError('Parent directory for project does not exist')

        datadir = expanduser('~/.datacats/' + name)
        target = workdir + '/' + name

        if isdir(datadir):
            raise ProjectError('Project data directory {0} already exists',
                (datadir,))
        if isdir(target):
            raise ProjectError('Project directory already exists')

        project = cls(name, target, datadir, ckan_version)
        project._generate_passwords()
        return project

    @classmethod
    def load(cls, project_name=None):
        """
        Return a Project object based on an existing project.

        :params project_name: exising project name or None to look in
            current or parent directories for project

        Raises ProjectError if project can't be found or if there is an
        error parsing the project information.
        """
        if project_name is None:
            wd = abspath(getcwd())
            while not exists(wd + '/.datacats-project'):
                oldwd = wd
                wd, ignore = path_split(wd)
                if wd == oldwd:
                    raise ProjectError(
                        'Project not found in current directory')
        else:
            assert 0, 'TBD'

        cp = SafeConfigParser()
        try:
            cp.read([wd + '/.datacats-project'])
        except ConfigParserError:
            raise ProjectError('Error reading project information')

        name = cp.get('datacats', 'name')
        datadir = expanduser('~/.datacats/' + name)
        ckan_version = cp.get('datacats', 'ckan_version')
        passwords = {}
        for n in cp.options('passwords'):
            passwords[n.upper()] = cp.get('passwords', n)

        project = cls(name, wd, datadir, ckan_version)
        project.passwords = passwords
        return project

    def create_directories(self):
        """
        Call once for new projects to create the initial project directories.
        """
        makedirs(self.datadir, mode=0o700)
        makedirs(self.datadir + '/venv')
        makedirs(self.datadir + '/search')
        if not is_boot2docker():
            makedirs(self.datadir + '/data')
        makedirs(self.datadir + '/files')
        makedirs(self.target + '/conf')
        makedirs(self.target + '/src')

    def _preload_image(self):
        """
        Return the preloaded ckan src and venv image name
        """
        return 'datacats/web:preload_{0}'.format(self.ckan_version)

    def create_virtualenv(self):
        """
        Populate venv directory from preloaded image
        """
        web_command(
            command='/bin/cp -a /usr/lib/ckan/. /usr/lib/ckan_target/.',
            rw={self.datadir + '/venv': '/usr/lib/ckan_target'},
            image=self._preload_image())

    def create_source(self):
        """
        Populate src/ckan directory from preloaded image and copy
        who.ini and schema.xml info conf directory
        """
        web_command(
            command='/bin/cp -a /project/src/. /project/src_target/.',
            rw={self.target + '/src': '/project/src_target'},
            image=self._preload_image())
        shutil.copy(
            self.target + '/src/ckan/ckan/config/who.ini',
            self.target + '/conf')
        shutil.copy(
            self.target + '/src/ckan/ckan/config/solr/schema.xml',
            self.target + '/conf')

    def start_data_and_search(self):
        """
        run the postgres and solr containers
        """
        # complicated because postgres needs hard links to
        # work on its data volume. see issue #5
        if is_boot2docker():
            data_only_container('datacats_dataonly_' + self.name,
                ['/var/lib/postgresql/data'])
            rw = {}
            volumes_from='datacats_dataonly_' + self.name
        else:
            rw = {self.datadir + '/data': '/var/lib/postgresql/data'}
            volumes_from=None

        # users are created when data dir is blank so we must pass
        # all the user passwords as environment vars
        run_container(
            name='datacats_data_' + self.name,
            image='datacats/data',
            environment=self.passwords,
            rw=rw,
            volumes_from=volumes_from)
        run_container(
            name='datacats_search_' + self.name,
            image='datacats/search',
            rw={self.datadir + '/search': '/var/lib/solr'},
            ro={self.target + '/conf/schema.xml': '/etc/solr/conf/schema.xml'})

    def stop_data_and_search(self):
        """
        stop and remove postgres and solr containers
        """
        remove_container('datacats_data_' + self.name)
        remove_container('datacats_search_' + self.name)

    def fix_storage_permissions(self):
        """
        Set the owner of all apache storage files to www-data container user
        """
        web_command(
            command='/bin/chown -R www-data: /var/www/storage',
            rw={self.datadir + '/files': '/var/www/storage'})

    def create_ckan_ini(self):
        """
        Use make-config to generate an initial ckan.ini file
        """
        web_command(
            command='/usr/lib/ckan/bin/paster make-config'
                ' ckan /etc/ckan/default/ckan.ini',
            ro={self.datadir + '/venv': '/usr/lib/ckan',
                self.target + '/src': '/project/src'},
            rw={self.target + '/conf': '/etc/ckan/default'})

    def update_ckan_ini(self):
        """
        Use config-tool to update ckan.ini with our project settings
        """
        p = self.passwords
        command = [
            '/usr/lib/ckan/bin/paster', '--plugin=ckan', 'config-tool',
            '/etc/ckan/default/ckan.ini', '-e',
            'sqlalchemy.url = postgresql://ckan:'
                '{CKAN_PASSWORD}@db:5432/ckan'.format(**p),
            'ckan.datastore.read_url = postgresql://ckan_datastore_readonly:'
                '{DATASTORE_RO_PASSWORD}@db:5432/ckan_datastore'.format(**p),
            'ckan.datastore.write_url = postgresql://ckan_datastore_readwrite:'
                '{DATASTORE_RW_PASSWORD}@db:5432/ckan_datastore'.format(**p),
            'solr_url = http://solr:8080/solr',
            'ckan.storage_path = /var/www/storage',
            ]
        web_command(
            command=command,
            ro={self.datadir + '/venv': '/usr/lib/ckan',
                self.target + '/src': '/project/src'},
            rw={self.target + '/conf': '/etc/ckan/default'})

    def fix_project_permissions(self):
        """
        Reset owner of project files to the host user so they can edit,
        move and delete them freely.
        """
        web_command(
            command='/bin/chown -R --reference=/etc/ckan/default'
                ' /usr/lib/ckan /project/src /etc/ckan/default',
            rw={self.datadir + '/venv': '/usr/lib/ckan',
                self.target + '/src': '/project/src',
                self.target + '/conf': '/etc/ckan/default'})

    def ckan_db_init(self):
        """
        Run db init to create all ckan tables
        """
        web_command(
            command='/usr/lib/ckan/bin/paster --plugin=ckan db init'
                ' -c /etc/ckan/default/ckan.ini',
            ro={self.datadir + '/venv': '/usr/lib/ckan',
                self.target + '/src': '/project/src',
                self.target + '/conf': '/etc/ckan/default'},
            links={'datacats_search_' + self.name: 'solr',
                'datacats_data_' + self.name: 'db'})

    def _generate_passwords(self):
        """
        Generate new DB passwords and store them in self.passwords
        """
        self.passwords = {
            'POSTGRES_PASSWORD': generate_db_password(),
            'CKAN_PASSWORD': generate_db_password(),
            'DATASTORE_RO_PASSWORD': generate_db_password(),
            'DATASTORE_RW_PASSWORD': generate_db_password(),
            }

    def start_web(self):
        """
        Start the apache server or paster serve
        """
        port_bindings = {80: None} if is_boot2docker() else {80: ('127.0.0.1',)}
        run_container(
            name='datacats_web_' + self.name,
            image='datacats/web',
            rw={self.datadir + '/files': '/var/www/storage'},
            ro={self.datadir + '/venv': '/usr/lib/ckan',
                self.target + '/src': '/project/src',
                self.target + '/conf': '/etc/ckan/default'},
            links={'datacats_search_' + self.name: 'solr',
                'datacats_data_' + self.name: 'db'},
            port_bindings=port_bindings,
            )

    def stop_web(self):
        """
        Stop and remove the web container
        """
        remove_container('datacats_web_' + self.name, force=True)

    def web_address(self):
        """
        Return the url of the web server or None if not running
        """

        info = inspect_container('datacats_web_' + self.name)
        if info is None:
            return None
        port = info['NetworkSettings']['Ports']['80/tcp'][0]['HostPort']
        return 'http://{0}:{1}/'.format(docker_host(), port)

    def interactive_set_admin_password(self):
        """
        launch docker client interactively to set the admin password
        """
        # FIXME: consider switching this to dockerpty
        # using subprocess for docker client's interactive session
        subprocess.call([
            '/usr/bin/docker', 'run', '--rm', '-it',
            '-v', self.datadir + '/venv:/usr/lib/ckan:ro',
            '-v', self.target + '/src:/project/src:ro',
            '-v', self.target + '/conf:/etc/ckan/default:ro',
            '--link', 'datacats_search_' + self.name + ':solr',
            '--link', 'datacats_data_' + self.name + ':db',
            'datacats/web', '/usr/lib/ckan/bin/paster', '--plugin=ckan',
            'sysadmin', 'add', 'admin', '-c' '/etc/ckan/default/ckan.ini'])


def generate_db_password():
    """
    Return a 16-character alphanumeric random string generated by the
    operating system's secure pseudo random number generator
    """
    chars = uppercase + lowercase + digits
    return ''.join(SystemRandom().choice(chars) for x in xrange(16))
