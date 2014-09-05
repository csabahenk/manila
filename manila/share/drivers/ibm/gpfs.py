# Copyright 2014 IBM Corp.
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
"""
GPFS Driver for shares.

Config Requirements:
  GPFS file system must have quotas enabled (mmchfs -Q yes).
Notes:
  GPFS independent fileset is used for each share

Todo:
1.  Implement multi-tenancy

Limitation:
1. While using remote GPFS node, with Ganesha NFS, 'gpfs_private_key'
   for remote login to the GPFS node must be specified and there must be
   a passwordless authentication already setup between the Manila and the
   remote GPFS node.

"""

import math
import os
import re
import socket

from oslo.config import cfg
from oslo.utils import units

from manila import exception
from manila.openstack.common import importutils
from manila.openstack.common import log as logging
from manila.share import driver
from manila.share.drivers.ganesha import GaneshaNASHelper
from manila.share.drivers.ganesha import utils as ganeshautils

LOG = logging.getLogger(__name__)

# matches multiple comma separated avpairs on a line.  values with an embedded
# comma must be wrapped in quotation marks
AVPATTERN = re.compile(r'\s*(?P<attr>\w+)\s*=\s*(?P<val>'
                       '(["][a-zA-Z0-9_, ]+["])|(\w+))\s*[,]?')


gpfs_share_opts = [
    cfg.StrOpt('gpfs_share_export_ip',
               default=None,
               help='IP to be added to GPFS export string'),
    cfg.StrOpt('gpfs_mount_point_base',
               default='$state_path/mnt',
               help='Base folder where exported shares are located'),
    cfg.StrOpt('gpfs_nfs_server_type',
               default='KNFS',
               help=('NFS Server type. Valid choices are "KNFS" (kernel NFS) '
                     'or "GNFS" (Ganesha NFS)')),
    cfg.ListOpt('gpfs_nfs_server_list',
                default=None,
                help=('A list of the fully qualified NFS server names that '
                      'make up the OpenStack Manila configuration')),
    cfg.IntOpt('gpfs_ssh_port',
               default=22,
               help='GPFS server ssh port'),
    cfg.StrOpt('gpfs_login',
               default=None,
               help='GPFS server login name'),
    cfg.StrOpt('gpfs_password',
               default=None,
               help='GPFS server login password'),
    cfg.StrOpt('gpfs_private_key',
               default=None,
               help='GPFS server SSH private key for login'),
    cfg.IntOpt('ssh_conn_timeout',
               default=60,
               help='GPFS server SSH connection timeout'),
    cfg.IntOpt('ssh_min_pool_conn',
               default=1,
               help='GPFS server SSH minimum connection pool'),
    cfg.IntOpt('ssh_max_pool_conn',
               default=10,
               help='GPFS server SSH maximum connection pool'),
    cfg.ListOpt('gpfs_share_helpers',
                default=[
                    'KNFS=manila.share.drivers.ibm.gpfs.KNFSHelper',
                    'GNFS=manila.share.drivers.ibm.gpfs.GNFSHelper',
                ],
                help='Specify list of share export helpers.'),
    cfg.StrOpt('knfs_export_options',
               default=('rw,sync,no_root_squash,insecure,no_wdelay,'
                        'no_subtree_check'),
               help=('Options to use when exporting a share using kernel '
                     'NFS server. Note that these defaults can be overridden '
                     'when a share is created by passing metadata with key '
                     'name export_options')),
]


CONF = cfg.CONF
CONF.register_opts(gpfs_share_opts)


class GPFSShareDriver(driver.ExecuteMixin, driver.ShareDriver):
    """Executes commands relating to Shares."""

    def __init__(self, db, *args, **kwargs):
        """Do initialization."""
        super(GPFSShareDriver, self).__init__(*args, **kwargs)
        self.db = db
        self.configuration.append_config_values(gpfs_share_opts)
        self.backend_name = self.configuration.safe_get(
            'share_backend_name') or "IBM Storage System"
        self.sshpool = None
        self.ssh_connections = {}

        host = self.configuration.gpfs_share_export_ip
        localserver_iplist = socket.gethostbyname_ex(socket.gethostname())[2]
        if host in localserver_iplist:  # run locally
            execf = ganeshautils.RootExecutor(self._execute)
        else:
            execf = ganeshautils.SSHExecutor(
              host,
              self.configuration.gpfs_ssh_port,
              self.configuration.ssh_conn_timeout,
              self.configuration.gpfs_login,
              password=self.configuration.gpfs_password,
              privatekey=self.configuration.gpfs_private_key,
              min_size=self.configuration.ssh_min_pool_conn,
              max_size=self.configuration.ssh_max_pool_conn
            )
        self._gpfs_execute = execf

    def _check_gpfs_state(self):
        try:
            out, _ = self._gpfs_execute('mmgetstate', '-Y')
        except exception.ProcessExecutionError:
            msg = (_('Failed to check GPFS state.'))
            LOG.error(msg)
            raise exception.GPFSException(msg)
        lines = out.splitlines()
        state_token = lines[0].split(':').index('state')
        gpfs_state = lines[1].split(':')[state_token]
        if gpfs_state != 'active':
            return False
        return True

    def _is_dir(self, path):
        try:
            output, _ = self._gpfs_execute('stat', '--format=%F',
                                           path, run_as_root=False)
        except exception.ProcessExecutionError:
            msg = (_('%s is not a directory.') % path)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        return output.strip() == 'directory'

    def _is_gpfs_path(self, directory):
        try:
            self._gpfs_execute('mmlsattr', directory)
        except exception.ProcessExecutionError:
            msg = (_('%s is not on GPFS filesystem.') % directory)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        return True

    def _setup_helpers(self):
        """Initializes protocol-specific NAS drivers."""
        conf = self.configuration
        hdic = {}
        for helper_str in conf.gpfs_share_helpers:
            share_proto, _, import_str = helper_str.partition('=')
            hdic[share_proto] = importutils.import_class(import_str)

        if conf.gpfs_nfs_server_type == 'KNFS':
            self._helper = hdic['KNFS'](self._gpfs_execute, conf)
        elif conf.gpfs_nfs_server_type == 'GNFS':
            _GHC = hdic['GNFS']
            execs = [ganeshautils.RootExecutor()] + [
              ganeshautils.SSHExecutor(srv, 22, None, conf.gpfs_login,
                     privatekey=conf.gpfs_private_key) for
                srv in conf.gpfs_nfs_server_list
            ]
            self._helper = ganeshautils.Mux([_GHC(ex, conf) for ex in execs])
        else:
            msg = _('Unknown NFS server type %s.') % conf.gpfs_nfs_server_type
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def _local_path(self, shareobj):
        """Get local path for a share or share snapshot by name."""
        return os.path.join(self.configuration.gpfs_mount_point_base,
                            shareobj['name'])

    def _sizestr(self, size_in_g):
        if int(size_in_g) == 0:
            return '100M'
        return '%sG' % size_in_g

    def _get_gpfs_device(self):
        fspath = self.configuration.gpfs_mount_point_base
        try:
            (out, _) = self._gpfs_execute('df', fspath)
        except exception.ProcessExecutionError:
            msg = (_('Failed to get GPFS device for %s.') % fspath)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        lines = out.splitlines()
        fs = lines[1].split()[0]
        return fs

    def _create_share(self, shareobj, sizestr):
        """Create a linked fileset file in GPFS.

        Note:  GPFS file system must have quotas enabled
        (mmchfs -Q yes).
        """
        sharename = shareobj['name']
        sharepath = self._local_path(shareobj)
        fsdev = self._get_gpfs_device()

        # create fileset for the share, link it to root path and set max size
        try:
            self._gpfs_execute('mmcrfileset', fsdev, sharename,
                               '--inode-space', 'new')
        except exception.ProcessExecutionError:
            msg = (_('Failed to create fileset on %(fsdev)s for '
                     'the share %(sharename)s.') %
                   {"fsdev": fsdev, "sharename": sharename})
            LOG.error(msg)
            raise exception.GPFSException(msg)

        try:
            self._gpfs_execute('mmlinkfileset', fsdev, sharename, '-J',
                               sharepath)
        except exception.ProcessExecutionError:
            msg = (_('Failed to link fileset for the share %s.') % sharename)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        try:
            self._gpfs_execute('mmsetquota', '-j', sharename, '-h',
                               sizestr, fsdev)
        except exception.ProcessExecutionError:
            msg = (_('Failed to set quota for the share %s.') % sharename)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        try:
            self._gpfs_execute('chmod', '777', sharepath)
        except exception.ProcessExecutionError:
            msg = (_('Failed to set permissions for share %s.') % sharename)
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def _delete_share(self, shareobj):
        """Remove container by removing GPFS fileset."""
        sharename = shareobj['name']
        fsdev = self._get_gpfs_device()

        # unlink and delete the share's fileset
        try:
            self._gpfs_execute('mmunlinkfileset', fsdev, sharename, '-f')
        except exception.ProcessExecutionError:
            msg = (_('Failed unlink fileset for share %s.') % sharename)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        try:
            self._gpfs_execute('mmdelfileset', fsdev, sharename, '-f')
        except exception.ProcessExecutionError:
            msg = (_('Failed delete fileset for share %s.') % sharename)
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def _get_available_capacity(self, path):
        """Calculate available space on path."""
        try:
            out, _ = self._gpfs_execute('df', '-P', '-B', '1', path)
        except exception.ProcessExecutionError:
            msg = (_('Failed to check available capacity for %s.') % path)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        out = out.splitlines()[1]
        size = int(out.split()[1])
        available = int(out.split()[3])
        return available, size

    def _create_share_snapshot(self, snapshot):
        """Create a snapshot of the share."""
        sharename = snapshot['share_name']
        snapshotname = snapshot['name']
        fsdev = self._get_gpfs_device()
        LOG.debug("sharename = %s, snapshotname = %s, fsdev = %s" %
                  (sharename, snapshotname, fsdev))

        try:
            self._gpfs_execute('mmcrsnapshot', fsdev, snapshot['name'],
                               '-j', sharename)
        except exception.ProcessExecutionError:
            msg = (_('Failed to create snapshot %s.') % snapshot['name'])
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def _delete_share_snapshot(self, snapshot):
        """Delete a snapshot of the share."""
        sharename = snapshot['share_name']
        fsdev = self._get_gpfs_device()

        try:
            self._gpfs_execute('mmdelsnapshot', fsdev, snapshot['name'],
                               '-j', sharename)
        except exception.ProcessExecutionError:
            msg = (_('Failed to delete snapshot %s.') % snapshot['name'])
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def _create_share_from_snapshot(self, share, snapshot, share_path):
        """Create share from a share snapshot."""
        self._create_share(share, '%sG' % share['size'])
        snapshot_path = self._get_snapshot_path(snapshot)
        snapshot_path = snapshot_path + "/"
        try:
            self._gpfs_execute('rsync', '-rp', snapshot_path, share_path)
        except exception.ProcessExecutionError:
            msg = (_('Failed to create share %(share)s from '
                     'snapshot %(snapshot)s.') %
                   {"share": share['name'], "snapshot": snapshot['name']})
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def create_share(self, ctx, share, share_server=None):
        """Create GPFS directory that will be represented as share."""
        self._create_share(share, '%sG' % share['size'])
        share_path = self._get_share_path(share)
        location = self._get_helper(share).create_export(share_path,
                                                         share)
        return location

    def create_share_from_snapshot(self, ctx, share, snapshot,
                                   share_server=None):
        """Is called to create share from a snapshot."""
        share_path = self._get_share_path(share)
        self._create_share_from_snapshot(share, snapshot, share_path)
        location = self._get_helper(share).create_export(share_path,
                                                         share)
        return location

    def create_snapshot(self, context, snapshot, share_server=None):
        """Creates a snapshot."""
        self._create_share_snapshot(snapshot)

    def delete_share(self, ctx, share, share_server=None):
        """Remove and cleanup share storage."""
        self._delete_share(share)

    def delete_snapshot(self, context, snapshot, share_server=None):
        """Deletes a snapshot."""
        self._delete_share_snapshot(snapshot)

    def ensure_share(self, ctx, share, share_server=None):
        """Ensure that storage are mounted and exported."""
        pass

    def allow_access(self, ctx, share, access, share_server=None):
        """Allow access to the share."""
        location = self._get_share_path(share)
        self._get_helper(share).allow_access(location, share,
                                             access['access_type'],
                                             access['access_to'])

    def deny_access(self, ctx, share, access, share_server=None):
        """Deny access to the share."""
        location = self._get_share_path(share)
        self._get_helper(share).deny_access(location, share,
                                            access['access_type'],
                                            access['access_to'])

    def check_for_setup_error(self):
        """Returns an error if prerequisites aren't met."""
        if not self._check_gpfs_state():
            msg = (_('GPFS is not active'))
            LOG.error(msg)
            raise exception.GPFSException(msg)

        if not self.configuration.gpfs_share_export_ip:
            msg = (_('gpfs_share_export_ip must be specified'))
            raise exception.InvalidParameterValue(err=msg)

        gpfs_base_dir = self.configuration.gpfs_mount_point_base
        if not gpfs_base_dir.startswith('/'):
            msg = (_('%s must be an absolute path.') % gpfs_base_dir)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        if not self._is_dir(gpfs_base_dir):
            msg = (_('%s is not a directory.') % gpfs_base_dir)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        if not self._is_gpfs_path(gpfs_base_dir):
            msg = (_('%s is not on GPFS. Perhaps GPFS not mounted.') %
                   gpfs_base_dir)
            LOG.error(msg)
            raise exception.GPFSException(msg)

        if self.configuration.gpfs_nfs_server_type not in ['KNFS', 'GNFS']:
            msg = (_('Invalid gpfs_nfs_server_type value: %s. '
                     'Valid values are: "KNFS", "GNFS"') %
                   self.configuration.gpfs_nfs_server_type)
            LOG.error(msg)
            raise exception.InvalidParameterValue(err=msg)

        if self.configuration.gpfs_nfs_server_list is None:
            msg = (_('Missing value for gpfs_nfs_server_list.'))
            LOG.error(msg)
            raise exception.InvalidParameterValue(err=msg)

    def do_setup(self, context):
        """Any initialization the volume driver does while starting."""
        super(GPFSShareDriver, self).do_setup(context)
        self._setup_helpers()
        for helper in self._helpers.values():
            helper.init()

    def get_share_stats(self, refresh=False):
        """Get share status.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self._update_share_status()

        return self._stats

    def _update_share_status(self):
        """Retrieve status info from share volume group."""

        LOG.debug("Updating share status")
        data = {}

        data["share_backend_name"] = self.backend_name
        data["vendor_name"] = 'IBM'
        data["driver_version"] = '1.0'
        data["storage_protocol"] = 'NFS'

        data['reserved_percentage'] = \
            self.configuration.reserved_share_percentage
        data['QoS_support'] = False

        free, capacity = self._get_available_capacity(
            self.configuration.gpfs_mount_point_base)

        data['total_capacity_gb'] = math.ceil(capacity / units.Gi)
        data['free_capacity_gb'] = math.ceil(free / units.Gi)

        self._stats = data

    def _get_helper(self, share):
        if share['share_proto'].startswith('NFS'):
            return self._helper
        else:
            msg = (_('Share protocol %s not supported by GPFS driver') %
                   share['share_proto'])
            LOG.error(msg)
            raise exception.InvalidShare(reason=msg)

    def _get_share_path(self, share):
        """Returns share path on storage provider."""
        return os.path.join(self.configuration.gpfs_mount_point_base,
                            share['name'])

    def _get_snapshot_path(self, snapshot):
        """Returns share path on storage provider."""
        snapshot_dir = ".snapshots"
        return os.path.join(self.configuration.gpfs_mount_point_base,
                            snapshot["share_name"], snapshot_dir,
                            snapshot["name"])


class NASHelperBase(object):
    """Interface to work with share."""

    def __init__(self, execute, config_object):
        self.configuration = config_object
        self._execute = execute

    def init(self):
        pass

    def create_export(self, local_path, share, recreate=False):
        """Construct location of new export."""
        return ':'.join([self.configuration.gpfs_share_export_ip, local_path])

    def allow_access(self, local_path, share, access_type, access):
        """Allow access to the host."""
        raise NotImplementedError()

    def deny_access(self, local_path, share, access_type, access,
                    force=False):
        """Deny access to the host."""
        raise NotImplementedError()


class KNFSHelper(NASHelperBase):
    """Wrapper for Kernel NFS Commands."""

    def __init__(self, execute, config_object):
        super(KNFSHelper, self).__init__(execute, config_object)
        self._execute = execute
        try:
            self._execute('exportfs', check_exit_code=True)
        except exception.ProcessExecutionError:
            msg = (_('NFS server not found'))
            LOG.error(msg)
            raise exception.GPFSException(msg)

    def _get_export_options(self, share):
        """Set various export attributes for share."""

        metadata = share.get('share_metadata')
        options = None
        for item in metadata:
            if item['key'] == 'export_options':
                options = item['value']
            else:
                msg = (_('Unknown metadata key %s') % item['key'])
                LOG.error(msg)
                raise exception.InvalidInput(reason=msg)
        if not options:
            options = self.configuration.knfs_export_options

        return options

    def allow_access(self, local_path, share, access_type, access):
        """Allow access to one or more vm instances."""

        # check if present in export
        try:
            out, _ = self._execute('exportfs')
        except exception.ProcessExecutionError:
            msg = (_('Failed to check exports on the systems.'))
            LOG.error(msg)
            raise exception.GPFSException(msg)

        out = re.search(re.escape(local_path) + '[\s\n]*' + re.escape(access),
                        out)
        if out is not None:
            raise exception.ShareAccessExists(access_type=access_type,
                                              access=access)

        export_opts = self._get_export_options(share)

        for server in self.configuration.gpfs_nfs_server_list:
            try:
                self._execute('exportfs', '-o', export_opts,
                              ':'.join([access, local_path]),
                              check_exit_code=True)
            except exception.ProcessExecutionError:
                msg = (_('Failed to allow access for share %s.') %
                       share['name'])
                LOG.error(msg)
                raise exception.GPFSException(msg)

    def deny_access(self, local_path, share, access_type, access,
                    force=False):
        """Remove access for one or more vm instances."""
        for server in self.configuration.gpfs_nfs_server_list:
            try:
                self._execute('exportfs', '-u',
                              ':'.join([access, local_path]),
                              check_exit_code=False)
            except exception.ProcessExecutionError:
                msg = (_('Failed to deny access for share %s.') %
                       share['name'])
                LOG.error(msg)
                raise exception.GPFSException(msg)


class GNFSHelper(GaneshaNASHelper):
    """Wrapper for Ganesha NFS Commands."""

    def _default_config_hook(self):
        dconf = super(GNFSHelper, self)._default_config_hook()
        conf_dir = ganeshautils.path_from(__file__, "conf")
        ganeshautils.patch(dconf, self._load_conf_dir(conf_dir))
        return dconf

    def _fsal_hook(self, *_args):
        return {"Name": "GPFS"}
