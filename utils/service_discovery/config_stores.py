# std
import logging
import simplejson as json
from os import path

# 3p
from urllib3.exceptions import TimeoutError

# project
from utils.checkfiles import get_check_class, get_auto_conf


log = logging.getLogger(__name__)

SD_TEMPLATE_DIR = '/datadog/check_configs'

AUTO_CONF_IMAGES = {
    # image_name: check_name
    'redis': 'redisdb',
    'nginx': 'nginx',
    'consul': 'consul',
    'elasticsearch': 'elastic',
}


class KeyNotFound(Exception):
    pass


class ConfigStore(object):
    """Singleton for config stores"""
    _instance = None
    previous_config_index = None

    def __new__(cls, *args, **kwargs):
        from utils.service_discovery.etcd_config_store import EtcdStore
        from utils.service_discovery.consul_config_store import ConsulStore
        if cls._instance is None:
            agentConfig = kwargs.get('agentConfig', {})
            if agentConfig.get('sd_config_backend') == 'etcd':
                cls._instance = object.__new__(EtcdStore, agentConfig)
            elif agentConfig.get('sd_config_backend') == 'consul':
                cls._instance = object.__new__(ConsulStore, agentConfig)
            elif agentConfig.get('sd_config_backend') is None:
                cls._instance = object.__new__(StubStore, agentConfig)
        return cls._instance

    def __init__(self, agentConfig):
        self.client = None
        self.agentConfig = agentConfig
        self.settings = self._extract_settings(agentConfig)
        self.client = self.get_client()
        self.sd_template_dir = agentConfig.get('sd_template_dir')

    @classmethod
    def _drop(cls):
        """Drop the config store instance"""
        cls._instance = None

    def _extract_settings(self, config):
        raise NotImplementedError()

    def get_client(self, reset=False):
        raise NotImplementedError()

    def client_read(self, path, **kwargs):
        raise NotImplementedError()

    def _get_auto_config(self, image_name):
        for key in AUTO_CONF_IMAGES:
            if key == image_name:
                check_name = AUTO_CONF_IMAGES[key]
                check = get_check_class(self.agentConfig, check_name)
                if check is None:
                    log.info("Could not find an auto configuration template for %s."
                             " Leaving it unconfigured." % image_name)
                    return None
                auto_conf = get_auto_conf(self.agentConfig, check_name)
                init_config, instances = auto_conf.get('init_config'), auto_conf.get('instances')

                # stringify the dict to be consistent with what comes from the config stores
                init_config_tpl = json.dumps(init_config) if init_config else '{}'
                instance_tpl = json.dumps(instances[0]) if instances and len(instances) > 0 else '{}'

                return (check_name, init_config_tpl, instance_tpl)
        return None

    def get_check_tpl(self, image, **kwargs):
        """Retrieve template config strings from the ConfigStore."""
        # this flag is used when no valid configuration store was provided
        if kwargs.get('auto_conf') is True:
            auto_config = self._get_auto_config(image)
            if auto_config is not None:
                check_name, init_config_tpl, instance_tpl = auto_config
            else:
                log.debug('No auto config was found for image %s, leaving it alone.' % image)
                return None
        else:
            try:
                # Try to read from the user-supplied config
                check_name = self.client_read(path.join(self.sd_template_dir, image, 'check_name').lstrip('/'))
                init_config_tpl = self.client_read(path.join(self.sd_template_dir, image, 'init_config').lstrip('/'))
                instance_tpl = self.client_read(path.join(self.sd_template_dir, image, 'instance').lstrip('/'))
            except (KeyNotFound, TimeoutError):
                # If it failed, try to read from auto-config templates
                log.info("Could not find directory {0} in the config store, "
                         "trying to auto-configure the check...".format(image))
                auto_config = self._get_auto_config(image)
                if auto_config is not None:
                    check_name, init_config_tpl, instance_tpl = auto_config
                else:
                    log.debug('No auto config was found for image %s, leaving it alone.' % image)
                    return None
            except Exception:
                log.warning(
                    'Fetching the value for {0} in the config store failed, '
                    'this check will not be configured by the service discovery.'.format(image))
                return None
        template = (check_name, init_config_tpl, instance_tpl)
        return template

    def crawl_config_template(self):
        """Return whether or not configuration templates have changed since the previous crawl"""
        config_index = self.client_read(self.sd_template_dir.lstrip('/'), recursive=True, watch=True)
        # Initialize the config index reference
        if self.previous_config_index is None:
            self.previous_config_index = config_index
            return False
        # Config has been modified since last crawl
        if config_index != self.previous_config_index:
            log.info('Detected an update in config template, reloading check configs...')
            self.previous_config_index = config_index
            return True
        return False

    @staticmethod
    def extract_sd_config(config):
        """Extract configuration about service discovery for the agent"""
        sd_config = {}
        if config.has_option('Main', 'sd_config_backend'):
            sd_config['sd_config_backend'] = config.get('Main', 'sd_config_backend')
        else:
            sd_config['sd_config_backend'] = None
        if config.has_option('Main', 'sd_template_dir'):
            sd_config['sd_template_dir'] = config.get(
                'Main', 'sd_template_dir')
        else:
            sd_config['sd_template_dir'] = SD_TEMPLATE_DIR
        if config.has_option('Main', 'sd_backend_host'):
            sd_config['sd_backend_host'] = config.get(
                'Main', 'sd_backend_host')
        if config.has_option('Main', 'sd_backend_port'):
            sd_config['sd_backend_port'] = config.get(
                'Main', 'sd_backend_port')
        return sd_config


class StubStore(ConfigStore):
    """Used when no valid config store was found. Allow to use auto_config."""
    def _extract_settings(self, config):
        pass

    def get_client(self):
        pass

    def crawl_config_template(self):
        # There is no user provided templates in auto_config mode
        return False
