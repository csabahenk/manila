# vim: tabstop=4 shiftwidth=4 softtabstop=4

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
from mock import Mock
from mock import patch
import os
import string
import subprocess
from copy import copy

from manila import context
from manila.db.sqlalchemy import models
from manila import exception

from manila.openstack.common import importutils
from manila.openstack.common import log as logging
from manila.share import configuration as config
from manila.share.drivers import ganesha
from manila import test
from manila.tests.db import fakes as db_fakes
from manila.tests import fake_utils

from oslo.config import cfg

# Structure of this test file
# ---------------------------
#
# This file is an experiment in TDD
# (https://en.wikipedia.org/wiki/Test-driven_development).
#
# It is intended to serve as a tech spec for the to-be-written ganesha driver.
# Therefore it should be evaluated not just on a technical base
# (like coverage) but also as a means to exchange ideas between humans.
#
# I provide some pointers here to help its use as such; in particular,
# regarding the proposed code layout for the driver (which is most likely
# different from what one would come up with during a direct implementation
# of desired functionality).
#
# Driver interface methods are broken down to helper methods
# (names of which start with underscore). So far nothing new (although
# note that for tests of `_helper_method` I used the name format
# `test__helper_method_whatsoever` instead of the more common
# `test_helper_method_whatsoever`, to make it easier on the eye); the
# interesting thing is that helper methods are of two kind:
#
# - quasi-pure
# - ones that deal with external resources ("resource functions" in the sequel)
#
# What resource functions do I think is easy to understand -- typical ones
# interact with db, or filesystem. Two remarks on them:
# - the resource functions are intended to be as close to the (technically not
#   specified) ideal of atomiticy as possible. They just do one particular
#   action, like read or write a file, without any event logic.
# - (at least in this initial effort) they are mostly black boxes. We don't
#   specify what external resource to use (db or filesystem), we treat thay as
#   implemention detail, we just specify what side effect they ought to have
#   (typically, get/put/delete some piece of data).
#
# Quasi-pure ones are on the orthogonal axis of resource functions: they have
# logic but as small side effect on their own as it's possible -- that is, they
# either encapsulate side effects in calls to resource functions, or do side
# effects that are known of and handled at test framework level -- like command
# execution, for which the test framework provides generic mocks.
#
# The two type of helper functions are not separated by syntactic convention
# (both just follow the underscore-name pattern). What tells them apart is
# coverage. By and large resource functions are the ones for which we don't
# provide test cases (as they are black boxes), we just cite them in as mocks
# within the test cases of the other functions (driver interface or quasi-pure
# helper) which are calling them.  What coverage aims to expose is the event
# logic of driver interface and quasi-pure helper functions.
#
# A notable exception is _read_ganesha_templates() which is a resource function
# (for reading in the templates used for ganesha config generation) but is not
# treated as black box rather we specify what files to read in. This
# eccentricity is marked by mocking a system call (open(2)).

CONF = cfg.CONF


ganeshaconf_base_template = """
FSAL
{
  GLUSTER {
    FSAL_Shared_Library = "/usr/local/lib64/ganesha/libfsalgluster.so";
    LogFile = "/var/log/nfs-ganesha.log";
  }
}
FileSystem
{
  Umask = 0000;
  Link_support = TRUE;
  Symlink_support = TRUE;
  CanSetTime = TRUE;
  xattr_access_rights = 0600;
}

NFS_Core_Param
{
    Nb_Worker = 8;
    NFS_Port = 2049;
}
NFS_IP_Name
{
    Index_Size = 17;
    Expiration_Time = 3600;
}
_9P
{
    DebugLevel = "NIV_FULL_DEBUG";
    LogFile    = "/tmp/nfs-ganesha.log";
}

"""

ganeshaconf_gluster_export_template = """
EXPORT
{
  Export_Id = ${export_id};
  Path = "${export_path}";
  FS_Specific = \
    "volume=${gluster_volume},hostname=${gluster_hostname},${gluster_volpath}";
  FSAL = "GLUSTER";
  Root_Access = "*";
  RW_Access = "${export_access}";
  Pseudo = "${export_pseudo}";
  Anonymous_root_uid = -2;
  NFS_Protocols = "3,4";
  Transport_Protocols = "TCP";
  SecType = "sys";
  MaxRead = 131072;
  MaxWrite = 131072;
  PrefRead = 32768;
  PrefWrite = 32768;
  Filesystem_id = 192.168;
  Tag = "gfs";
}
"""

cfg_default = {
    'ganesha_mount_point_base': '/mnt/ganesharoot',
    'ganesha_fsal': 'gluster',
    'ganesha_gluster_volume': 'testvol',
    'ganesha_gluster_hostname': '127.0.0.1',
    'ganesha_template_path': '/etc/manila/ganesha.${ganesha_fsal}.template',
    'ganesha_base_config_path': '/etc/manila/ganesha.base.conf',
    'ganesha_pid_file': '/var/run/ganesha.pid'
}

ganesha_exports = {'resource': 'testvol',
                   'shares': [('d1', '10.0.0.1'), ('d2', '10.0.0.2')]}

sharedict = {
    'id': 'fakeid',
    'name': 'fakename',
    'size': 1,
    'share_proto': 'NFS',
    # 'export_location': ???,
}


def fake_share(**kwargs):
    share.update(kwargs)
    return db_fakes.FakeModel(sharedict)


class GaneshaShareDriverTestCase(test.TestCase):
    """Tests GaneshaShareDriver."""

    def setUp(self):
        super(GaneshaShareDriverTestCase, self).setUp()
        fake_utils.stub_out_utils_execute(self.stubs)
        self._execute = fake_utils.fake_execute
        self._context = context.get_admin_context()

        for k, v in cfg_default.items():
            CONF.set_default(k, v)

        self.fake_conf = config.Configuration(None)
        self._db = Mock()
        self._driver = ganesha.GaneshaShareDriver(
                        self._db, execute=self._execute,
                        configuration=self.fake_conf)
        self.share = fake_share()

    def tearDown(self):
        super(GaneshaShareDriverTestCase, self).tearDown()
        fake_utils.fake_execute_set_repliers([])
        fake_utils.fake_execute_clear_log()

    def test__read_ganesha_templates(self):
        """Test if ganesha config input is found properly"""

        __builtin__.open = Mock()
        self._driver._read_ganesha_templates()
        open_args_list = ('/etc/manila/ganesha.base.conf',
                          '/etc/manila/ganesha.gluster.template')
        self.assertEqual(__builtin__.open.call_args_list,
                         [((x,), {}) for x in open_args_list])

    def test__make_ganesha_conf(self):
        """Test the making of ganesha conf from base config,
           export templates and export data"""

        # overwrite export id calculation routine with static value
        self._driver._get_export_id = Mock(return_value=77)
        ret = self._driver.make_ganesha_conf(
                  ganeshaconf_base_template,
                  ganeshaconf_gluster_export_template,
                  **ganesha_exports)
        # normalize output by stripping spaces
        ret = ret.replace(' ', '')

        # manual calculation of expected output;
        # it would look as follows (modulo whitespace):
        #
        # FSAL
        # {
        #   GLUSTER {
        #     FSAL_Shared_Library = "/usr/local/lib64/ganesha/libfsalgluster.so";
        #     LogFile = "/var/log/nfs-ganesha.log";
        #   }
        # }
        # FileSystem
        # {
        #   Umask = 0000;
        #   Link_support = TRUE;
        #   Symlink_support = TRUE;
        #   CanSetTime = TRUE;
        #   xattr_access_rights = 0600;
        # }
        #
        # NFS_Core_Param
        # {
        #     Nb_Worker = 8;
        #     NFS_Port = 2049;
        # }
        # NFS_IP_Name
        # {
        #     Index_Size = 17;
        #     Expiration_Time = 3600;
        # }
        # _9P
        # {
        #     DebugLevel = "NIV_FULL_DEBUG";
        #     LogFile    = "/tmp/nfs-ganesha.log";
        # }
        #
        #
        # EXPORT
        # {
        #   Export_Id = 1;
        #   Path = "/testvol";
        #   FS_Specific =     "volume=testvol,hostname=127.0.0.1,";
        #   FSAL = "GLUSTER";
        #   Root_Access = "*";
        #   RW_Access = "127.0.0.1";
        #   Pseudo = "/testvol";
        #   Anonymous_root_uid = -2;
        #   NFS_Protocols = "3,4";
        #   Transport_Protocols = "TCP";
        #   SecType = "sys";
        #   MaxRead = 131072;
        #   MaxWrite = 131072;
        #   PrefRead = 32768;
        #   PrefWrite = 32768;
        #   Filesystem_id = 192.168;
        #   Tag = "gfs";
        # }
        #
        # EXPORT
        # {
        #   Export_Id = 77;
        #   Path = "/testvol/d1";
        #   FS_Specific =     "volume=testvol,hostname=127.0.0.1,volpath=/d1";
        #   FSAL = "GLUSTER";
        #   Root_Access = "*";
        #   RW_Access = "10.0.0.1";
        #   Pseudo = "/testvol/d1";
        #   Anonymous_root_uid = -2;
        #   NFS_Protocols = "3,4";
        #   Transport_Protocols = "TCP";
        #   SecType = "sys";
        #   MaxRead = 131072;
        #   MaxWrite = 131072;
        #   PrefRead = 32768;
        #   PrefWrite = 32768;
        #   Filesystem_id = 192.168;
        #   Tag = "gfs";
        # }
        #
        # EXPORT
        # {
        #   Export_Id = 77;
        #   Path = "/testvol/d2";
        #   FS_Specific =     "volume=testvol,hostname=127.0.0.1,volpath=/d2";
        #   FSAL = "GLUSTER";
        #   Root_Access = "*";
        #   RW_Access = "10.0.0.2";
        #   Pseudo = "/testvol/d2";
        #   Anonymous_root_uid = -2;
        #   NFS_Protocols = "3,4";
        #   Transport_Protocols = "TCP";
        #   SecType = "sys";
        #   MaxRead = 131072;
        #   MaxWrite = 131072;
        #   PrefRead = 32768;
        #   PrefWrite = 32768;
        #   Filesystem_id = 192.168;
        #   Tag = "gfs";
        # }
        exdict_base = {'gluster_volume': 'testvol',
                       'gluster_hostname': '127.0.0.1'}
        svc_exp_dict, exp1_dict, exp2_dict =\
            (copy(exdict_base) for i in (1, 2, 3))
        svc_exp_dict.update(export_id=1, export_path='/testvol',
                            gluster_volpath='', export_access='127.0.0.1',
                            export_pseudo='/testvol')
        share_exp = ganesha_exports['shares']
        for ed, d in zip([exp1_dict, exp2_dict], share_exp):
            ed.update(export_id=77, export_path='/testvol/' + d[0],
                      gluster_volpath='volpath=/' + d[0],
                      export_access=d[1],
                      export_pseudo='/testvol/' + d[0])
        extemp = string.Template(ganeshaconf_gluster_export_template)
        confparts = [ganeshaconf_base_template] +\
                    [extemp.substitute(d) for d in
                     (svc_exp_dict, exp1_dict, exp2_dict)]
        expected_ret = ''.join(confparts).replace(' ', '')

        self.assertEqual(ret, expected_ret)

    def test__update_ganesha_conf_confchange(self):
        """Test _update_ganesha_conf when configuration has changed"""

        self._driver._get_exports = Mock(return_value=ganesha_exports)
        self._driver._make_ganesha_conf = Mock(return_value='newconf')
        self._driver._read_ganesha_conffile = Mock(return_value='oldconf')
        self._driver._write_ganesha_conffile = Mock()

        ret = self._driver._update_ganesha_conf()

        self._driver_._get_exports.assert_once_called_with()
        self._driver._make_ganesha_conf.call_count.assert_once_called_with(
            ganeshaconf_base_template,            # taken from attribute value
            ganeshaconf_gluster_export_template,  # taken from attribute value
            **ganesha_exports)                    # taken from _get_exports()
        self._driver._read_ganesha_conffile.assert_once_called_with()
        self.assertEqual(self._driver._write_ganesha_conffile.call_count, 1)
        self.assertEqual(ret, False)

    def test__update_ganesha_conf_nochange(self):
        """Test _update_ganesha_conf when configuration remained the same"""

        self._driver._get_exports = Mock(return_value=ganesha_exports)
        self._driver._make_ganesha_conf = Mock(return_value='oldconf')
        self._driver._read_ganesha_conffile = Mock(return_value='oldconf')
        self._driver._write_ganesha_conffile = Mock()

        ret = self._driver._update_ganesha_conf()

        self._driver._get_exports.assert_once_called_with()
        self._driver._make_ganesha_conf.call_count.assert_once_called_with(
            ganeshaconf_base_template,            # taken from attribute value
            ganeshaconf_gluster_export_template,  # taken from attribute value
            **ganesha_exports)                    # taken from _get_exports()
        self._driver._read_ganesha_conffile.assert_once_called_with()
        self.assertEqual(self._driver._write_ganesha_conffile.call_count, 0)
        self.assertEqual(ret, True)

    def test__update_ganesha_nochange(self):
        """Test Ganesha check/restart routine when nothing changed"""

        # XXX flake8 frowns on this matrix layout but for now
        # we keep it like that for visibility
        helper_call_table =\
        (('_make_ganesha_conf',     {},                    1),
         ('_update_ganesha_conf',   {'return_value':True}, 1),
         ('_check_ganesha_running', {'return_value':True}, 1),
         ('_restart_ganesha',       {},                    0),
         ('_ensure_service_mount',  {},                    1))
        for h, k, _ in helper_call_table:
            setattr(self._driver, h, Mock(**k))

        self._driver._update_ganesha()

        for h, _, c in helper_call_table:
            self.assertEqual(getattr(self._driver, h).call_count, c)

    def test__update_ganesha_exportchange(self):
        """Test Ganesha check/restart routine when config (exports) changed"""

        helper_call_table =\
        (('_make_ganesha_conf',     {},                     1),
         ('_update_ganesha_conf',   {'return_value':False}, 1),
         ('_check_ganesha_running', {},                     0),
         ('_restart_ganesha',       {},                     1),
         ('_ensure_service_mount',  {},                     1))
        for h, k, _ in helper_call_table:
            setattr(self._driver, h, Mock(**k))

        self._driver._update_ganesha()

        for h, _, c in helper_call_table:
            self.assertEqual(getattr(self._driver, h).call_count, c)

    def test__update_ganesha_ganeshadown(self):
        """Test Ganesha check/restart routine when ganesha is not running"""

        helper_call_table =\
        (('_make_ganesha_conf',     {},                     1),
         ('_update_ganesha_conf',   {'return_value':True},  1),
         ('_check_ganesha_running', {'return_value':False}, 1),
         ('_restart_ganesha',       {},                     1),
         ('_ensure_service_mount',  {},                     1))
        for h, k, _ in helper_call_table:
            setattr(self._driver, h, Mock(**k))

        self._driver._update_ganesha()

        for h, _, c in helper_call_table:
            self.assertEqual(getattr(self._driver, h).call_count, c)

    def test_do_setup(self):
        self._driver._read_ganesha_templates =\
            Mock(return_value=
                 (ganeshaconf_base_template,
                  ganeshaconf_gluster_export_template))
        self._driver._update_ganesha = Mock

        self._driver.do_setup(self._context)

        self._driver._read_ganesha_templates._assert_called_once_with()
        self._driver._update_ganesha._assert_called_once_with()
        self.assertEqual(getattr(self._driver, 'ganeshaconf_base_template',
                                 None),
                         ganeshaconf_base_template)
        self.assertEqual(getattr(self._driver, 'ganeshaconf_export_template',
                                 None),
                         ganeshaconf_gluster_export_template)

    def test__ensure_service_mount_already_mounted(self):
        """Test if ensure_service_mount does nothing if we have the service
           mount in place"""

        self._driver._check_service_mount = Mock(True)
        expected_exec = []

        self._driver._ensure_service_mount()

        self.assertEqual(fake_utils.fake_execute, expected_exec)

    def test__ensure_service_mount_not_yet_mounted(self):
        """Test if ensure_service_mount executes ganesha with proper
           arguments"""

        self._driver._check_service_mount = Mock(False)
        mnttemp = 'mount localhost:/${ganesha_gluster_volume} ' \
                  '${ganesha_mount_point_base}'
        expected_exec = [String.Template(mnttemp).substitute(cfg_default)]

        self._driver._ensure_service_mount()

        self.assertEqual(fake_utils.fake_execute_get_log, expected_exec)

    def test__restart_ganesha(self):
        """Test ganesha execution"""

        # regexeps that are to be used against executed command,
        # representing:
        expected_exec_rxs = [re.compile(x) for x in
            ('^\S*ganesha\.nfsd ',  # name of executable
             ' -d( |$)',            # daemonized mode
             '-c *\S+( |$)',        # conf file specification
             '-p *\S+( | $)')]      # pid file specification

        self._driver._restart_ganesha(self)

        execlog = fake_utils.fake_execute_get_log()
        self.assertTrue(execlog)
        for rx in expected_exec_rxs:
            self.assertTrue(rx.search(execlog[-1]))

    def test_create_share(self):
        expected_exec = ['mkdir /mnt/ganesharoot/fakename']
        expected_ret = '127.0.0.1:/testvol/fakename'

        ret = self._driver.create_share(self._context, self.share)
        self.assertEqual(fake_utils.fake_execute_get_log(), expected_exec)
        self.assertEqual(ret, expected_ret)

    def test_delete_share(self):
        expected_exec = ['rm -rf /mnt/ganesharoot/fakename']

        self._driver.delete_share(self._context, self.share)
        self.assertEqual(fake_utils.fake_execute_get_log(), expected_exec)

    ## allow/deny access and helpers

    def test__update_export_adding_new(self):
        self._driver._get_exports = Mock(return_value=ganesha_exports)
        self._driver._add_export = Mock()
        self._driver._remove_export = Mock()

        ret = self._driver._update_export(('fakename', '0.0.0.0'), op='+')

        self._driver._get_exports.assert_once_called_with()
        self._driver._add_export.assert_once_called_with(
            ('fakename', '0.0.0.0'))
        self.assertEqual(self._driver._remove_export.call_count, 0)
        self.assertEqual(ret, False)

    def test__update_export_adding_already_existing(self):
        self._driver._get_exports = Mock(return_value=ganesha_exports)
        self._driver._add_export = Mock()
        self._driver._remove_export = Mock()

        ret = self._driver._update_export(('d2', '10.0.0.2'), op='+')

        self._driver._get_exports.assert_once_called_with()
        self.assertEqual(self._driver._add_export.call_count, 0)
        self.assertEqual(self._driver._remove_export.call_count, 0)
        self.assertEqual(ret, True)

    def test__update_export_removing_non_existant(self):
        self._driver._get_exports = Mock(return_value=ganesha_exports)
        self._driver._add_export = Mock()
        self._driver._remove_export = Mock()

        ret = self._driver._update_export(('fakename', '0.0.0.0'), op='-')

        self._driver._get_exports.assert_once_called_with()
        self.assertEqual(self._driver._add_export.call_count, 0)
        self.assertEqual(self._driver._remove_export.call_count, 0)
        self.assertEqual(ret, True)

    def test__update_export_removing_existant(self):
        self._driver._get_exports = Mock(return_value=ganesha_exports)
        self._driver._add_export = Mock()
        self._driver._remove_export = Mock()

        ret = self._driver._update_export(('d2', '10.0.0.2'), op='-')

        self._driver._get_exports.assert_once_called_with()
        self.assertEqual(self._driver._add_export.call_count, 0)
        self._driver._remove_export.assert_once_called_with(('d2', '10.0.0.2'))
        self.assertEqual(ret, False)

    def test_allow_access_with_share_having_noaccess(self):
        self._driver._update_export = Mock(return_value=False)
        self._driver._update_ganesha = Mock()

        access = {'access_type': 'ip', 'access_to': '0.0.0.0'}
        self._driver._allow_access(self._context, self.share, access)

        self._driver._update_export.assert_once_called_with(
            ('fakename', '0.0.0.0'), op='+')
        self.assertEqual(self._driver._update_ganesha.call_count, 1)

    def test_allow_access_with_share_having_access(self):
        self._driver._update_export = Mock(return_value=True)
        self._driver._update_ganesha = Mock()

        access = {'access_type': 'ip', 'access_to': '0.0.0.0'}
        self._driver._deny_access(self._context, self.share, access)

        self._driver._update_export.assert_once_called_with(
            ('fakename', '0.0.0.0'), op='+')
        self.assertEqual(self._driver._update_ganesha.call_count, 0)

    def test_deny_access_with_share_having_noaccess(self):
        self._driver._update_export = Mock(return_value=True)
        self._driver._update_ganesha = Mock()

        access = {'access_type': 'ip', 'access_to': '0.0.0.0'}
        self._driver._deny_access(self._context, self.share, access)

        self._driver._update_export.assert_once_called_with(
            ('fakename', '0.0.0.0'), op='-')
        self.assertEqual(self._driver._update_ganesha.call_count, 0)

    def test_deny_access_with_share_having_access(self):
        self._driver._update_export = Mock(return_value=False)
        self._driver._update_ganesha = Mock()

        access = {'access_type': 'ip', 'access_to': '0.0.0.0'}
        self._driver._allow_access(self._context, self.share, access)

        self._driver._update_export.assert_once_called_with(
            ('fakename', '0.0.0.0'), op='-')
        self.assertEqual(self._driver._update_ganesha.call_count, 1)
