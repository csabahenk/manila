# Copyright (c) 2014 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import errno
import os
import re

from oslo.config import cfg

from manila import exception
from manila.share.drivers.ganesha import ganesharness
from manila.share.drivers.ganesha import utils as ganeshautils


ganesha_share_opts = [
    # XXX if ganesha_config_path is changed, the
    # shcat rule of etc/manila/rootwrap.d/share.filters
    # has to be adjusted accordingly
    cfg.StrOpt('ganesha_config_path',
               default='/etc/ganesha/ganesha.conf',
               help=('Path to ganesha main config file.')),
    cfg.StrOpt('ganesha_db_path',
               default='$state_path/manila-ganesha.db',
               help=('Location of Ganesha database file.')),
    cfg.StrOpt('ganesha_export_template_dir',
               default='/etc/manila/ganesha-export-templ.d',
               help=('Path to ganesha export template.')),
]


CONF = cfg.CONF
CONF.register_opts(ganesha_share_opts)


class NASHelperBase(object):
    """Interface to work with share."""

    def __init__(self, execute, config_object, **kwargs):
        self.configuration = config_object
        self._execute = execute

    def init_helper(self):
        pass

    def allow_access(self, base_path, share, access):
        """Allow access to the host."""
        raise NotImplementedError()

    def deny_access(self, base_path, share, access):
        """Deny access to the host."""
        raise NotImplementedError()

class GaneshaNASHelper(NASHelperBase):
    """Execute commands relating to Shares."""

    def __init__(self, execute, config, **kwargs):
        self._execute = execute
        self.configuration = config
        self.configuration.append_config_values(ganesha_share_opts)

    confrx = re.compile('\.(conf|json)\Z')

    def _load_conf_dir(self, dpath, must_exist=True):
        try:
            dl = os.listdir(dpath)
        except OSError as e:
            if e.errno != errno.ENOENT or must_exist:
                raise
            dl = []
        cfa = filter(self.confrx.search, dl)
        cfa.sort()
        exptemp = {}
        for cf in cfa:
            with open(os.path.join(dpath, cf)) as f:
                ganeshautils.patch(exptemp,
                  ganesharness.parseconf(f.read()))
        return exptemp

    def init_helper(self):
        self.ganhar = ganesharness.GanesHarness(self._execute,
          ganesha_config_path=self.configuration.ganesha_config_path,
          ganesha_db_path=self.configuration.ganesha_db_path)
        sys_exp_tpl = \
          self._load_conf_dir(self.configuration.ganesha_export_template_dir,
                              must_exist=False)
        if sys_exp_tpl:
            self.export_template = sys_exp_tpl
        else:
            self.export_template = self._default_config_hook()

    def _default_config_hook(self):
        """Subclass this to add FSAL specific defaults"""
        return self._load_conf_dir(ganeshautils.path_from(__file__, "conf"))

    def _fsal_hook(self, base_path, share, access):
        """Subclass this to create FSAL block"""
        return {}

    def allow_access(self, base_path, share, access):
        if access['access_type'] != 'ip':
            raise exception.InvalidShareAccess('only ip access type allowed')
        cf = {}
        accid = access['id']
        name = share['name']
        ganeshautils.patch(cf, self.export_template, {
          'EXPORT': {
            'Export_Id': self.ganhar.get_export_id(),
            'Path': os.path.join(base_path, name),
            'Pseudo': os.path.join(base_path, "%s_%s" % (name, accid)),
            'Tag': accid,
            'CLIENT': {
                'Clients': access['access_to']
            },
            'FSAL': self._fsal_hook(base_path, share, access)
          }
        })
        self.ganhar.add_export("%s_%s" % (name, accid), cf)

    def deny_access(self, base_path, share, access):
       self.ganhar.remove_export("%s_%s" % (share['name'], access['id']))
