# Copyright (c) 2014 IBM Corp.
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

"""Unit tests for the IBM GPFS driver module."""

import os

import mock
from oslo.config import cfg

from manila import compute
from manila import context
from manila import exception
from manila.openstack.common import lockutils
from manila.share.configuration import Configuration
from manila.share.drivers.ibm import gpfs
from manila import test
from manila.tests.db import fakes as db_fakes
from manila.tests import fake_compute
from manila.tests import fake_service_instance
from manila.tests import fake_utils
from manila.tests import fake_volume
from manila import volume


CONF = cfg.CONF


def fake_share(**kwargs):
    share = {
        'id': 'fakeid',
        'name': 'fakename',
        'size': 1,
        'share_proto': 'NFS',
        'export_location': '127.0.0.1:/mnt/nfs/share-1',
    }
    share.update(kwargs)
    return db_fakes.FakeModel(share)


def fake_snapshot(**kwargs):
    snapshot = {
        'id': 'fakesnapshotid',
        'share_name': 'fakename',
        'share_id': 'fakeid',
        'name': 'fakesnapshotname',
        'share_size': 1,
        'share_proto': 'NFS',
        'export_location': '127.0.0.1:/mnt/nfs/volume-00002',
    }
    snapshot.update(kwargs)
    return db_fakes.FakeModel(snapshot)


def fake_access(**kwargs):
    access = {
        'id': 'fakeaccid',
        'access_type': 'ip',
        'access_to': '10.0.0.2',
        'state': 'active',
    }
    access.update(kwargs)
    return db_fakes.FakeModel(access)


class GPFSShareDriverTestCase(test.TestCase):
    """Tests GPFSShareDriver."""

    def setUp(self):
        super(GPFSShareDriverTestCase, self).setUp()
        self._context = context.get_admin_context()
        self._gpfs_execute = mock.Mock(return_value=('', ''))

        self._helper_knfs = mock.Mock()
        self.fake_conf = Configuration(None)
        self._db = mock.Mock()
        self._driver = gpfs.GPFSShareDriver(self._db,
                                                  execute=self._gpfs_execute,
                                                  configuration=self.fake_conf)
        self.fakedev = "/dev/gpfs0"
        self.fakefspath = "/gpfs0"
        self.fakesharepath = "/gpfs0/share-fakeid"
        self.fakesnapshotpath = "/gpfs0/.snapshots/snapshot-fakesnapshotid"
        self._driver._ssh_exec = mock.Mock(return_value=('', ''))
        self.stubs.Set(gpfs.os.path, 'exists', mock.Mock(return_value=True))
        self._driver._helpers = {
            'KNFS': self._helper_knfs
        }
        self.share = fake_share()
        self.server = {
            'backend_details': {
                'ip': '1.2.3.4',
                'instance_id': 'fake'
            }
        }
        self.access = fake_access()
        self.snapshot = fake_snapshot()

    def test_do_setup(self):
        self.stubs.Set(self._driver, '_setup_helpers', mock.Mock())
        self._driver.do_setup(self._context)
        self._driver._setup_helpers.assert_called_once()

    def test_setup_helpers(self):
        self._driver._helpers = {}
        CONF.set_default('gpfs_share_helpers', ['KNFS=fakeknfs'])
        self.stubs.Set(gpfs.importutils, 'import_class',
                       mock.Mock(return_value=self._helper_knfs))
        self._driver._setup_helpers()
        gpfs.importutils.import_class.assert_has_calls([
            mock.call('fakeknfs')
        ])
        self.assertEqual(len(self._driver._helpers), 1)

    def test_create_share(self):
        self._helper_knfs.create_export.return_value = 'fakelocation'
        methods = ('_create_share', '_get_share_path')
        for method in methods:
            self.stubs.Set(self._driver, method, mock.Mock())
        result = self._driver.create_share(self._context, self.share,
                                           share_server=self.server)
        for method in methods:
            getattr(self._driver, method).assert_called_once()
        self.assertEqual(result, 'fakelocation')

    def test_create_share_from_snapshot(self):
        self._helper_knfs.create_export.return_value = 'fakelocation'
        methods = ('_get_share_path', '_create_share_from_snapshot')
        for method in methods:
            self.stubs.Set(self._driver, method, mock.Mock())
        result = self._driver.create_share_from_snapshot(self._context,
                                            self.share,
                                            self.snapshot,
                                            share_server=None)
        for method in methods:
            getattr(self._driver, method).assert_called_once()
        self.assertEqual(result, 'fakelocation')

    def test_create_snapshot(self):
        self._driver._create_share_snapshot = mock.Mock()
        self._driver.create_snapshot(self._context, self.snapshot,
                                        share_server=None)
        self._driver._create_share_snapshot.assert_called_once()

    def test_delete_share(self):
        self._helper_knfs.remove_export = mock.Mock()
        methods = ('_get_share_path', '_delete_share')
        for method in methods:
            self.stubs.Set(self._driver, method, mock.Mock())
        self._driver.delete_share(self._context, self.share,
                                           share_server=None)
        for method in methods:
            getattr(self._driver, method).assert_called_once()

        self._helper_knfs.remove_export.assert_called_once()

    def test_delete_snapshot(self):
        self._driver._delete_share_snapshot = mock.Mock()
        self._driver.delete_snapshot(self._context, self.snapshot,
                                        share_server=None)
        self._driver._delete_share_snapshot.assert_called_once()

    def test_allow_access(self):
        self._driver._get_share_path = mock.Mock()
        self._helper_knfs.allow_access = mock.Mock()
        self._driver.allow_access(self._context, self.share,
                                        self.access, share_server=None)
        self._helper_knfs.allow_access.assert_called_once()

    def test_deny_access(self):
        self._driver._get_share_path = mock.Mock()
        self._helper_knfs.deny_access = mock.Mock()
        self._driver.deny_access(self._context, self.share,
                                        self.access, share_server=None)
        self._helper_knfs.deny_access.assert_called_once()

    def test__check_gpfs_state(self):
        fakeout = "mmgetstate::state:\nmmgetstate::active:"
        self._driver._gpfs_execute = mock.Mock(return_value=(fakeout, ''))
        result = self._driver._check_gpfs_state()
        self._driver._gpfs_execute.assert_called_once()
        self.assertEqual(result, True)

    def test__is_dir(self):
        fakeoutput = "directory"
        self._driver._gpfs_execute = mock.Mock(return_value=(fakeoutput, ''))
        result = self._driver._is_dir(self.fakefspath)
        self._driver._gpfs_execute.assert_called_once()
        self.assertEqual(result, True)

    def test__is_gpfs_path(self):
        self._driver._gpfs_execute = mock.Mock(return_value=0)
        result = self._driver._is_gpfs_path(self.fakefspath)
        self._driver._gpfs_execute.assert_called_with('mmlsattr',
                                self.fakefspath,
                                run_as_root=True)
        self.assertEqual(result, True)

    def test__get_gpfs_device(self):
        fakeout = "Filesystem\n/dev/gpfs0"
        self._driver._gpfs_execute = mock.Mock(return_value=(fakeout, ''))
        result = self._driver._get_gpfs_device()
        self._driver._gpfs_execute.assert_called_once()
        self.assertEqual(result, "/dev/gpfs0")

    def test__create_share(self):
        self._driver._gpfs_execute = mock.Mock(return_value=True)
        self._driver._local_path = mock.Mock(return_value=self.fakesharepath)
        self._driver._get_gpfs_device = mock.Mock(return_value=self.fakedev)
        self._driver._create_share(self.share, self.share['size'])
        self._driver._gpfs_execute.assert_called_with('chmod',
                                '777',
                                self.fakesharepath,
                                run_as_root=True)

    def test__delete_share(self):
        self._driver._gpfs_execute = mock.Mock(return_value=True)
        self._driver._get_gpfs_device = mock.Mock(return_value=self.fakedev)
        self._driver._delete_share(self.share)
        self._driver._gpfs_execute.assert_called_with('mmdelfileset',
                                self.fakedev, self.share['name'],
                                '-f', run_as_root=True)

    def test__create_share_snapshot(self):
        self._driver._gpfs_execute = mock.Mock(return_value=True)
        self._driver._get_gpfs_device = mock.Mock(return_value=self.fakedev)
        self._driver._create_share_snapshot(self.snapshot)
        self._driver._gpfs_execute.assert_called_with('mmcrsnapshot',
                                self.fakedev, self.snapshot['name'],
                                '-j', self.snapshot['share_name'],
                                run_as_root=True)

    def test__create_share_from_snapshot(self):
        self._driver._gpfs_execute = mock.Mock(return_value=True)
        self._driver._create_share = mock.Mock(return_value=True)
        self._driver._get_snapshot_path = mock.Mock(
                                return_value=self.fakesnapshotpath)
        self._driver._create_share_from_snapshot(self.share, self.snapshot,
                                self.fakesharepath)
        self._driver._gpfs_execute.assert_called_with('rsync', '-rp',
                                self.fakesnapshotpath + '/',
                                self.fakesharepath,
                                run_as_root=True)
