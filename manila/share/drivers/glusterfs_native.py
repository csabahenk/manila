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

""" GlusterFS native protocol (glusterfs) driver for shares.

Manila share is a GlusterFS volume. Unlike the generic driver, this
does not use service VM approach. Instances directly talk with the
GlusterFS backend storage pool. Instance use the 'glusterfs' protocol
to mount the GlusterFS share. Access to the share is allowed via
SSL Certificates. Only the instance which has the SSL trust established
with the GlusterFS backend can mount and hence use the share.

Supports working with multiple glusterfs volumes.
"""

import errno
import pipes
import re
import shutil
import string
import tempfile
import xml.etree.cElementTree as etree

from oslo_config import cfg
from oslo_log import log
import six

from manila import exception
from manila.i18n import _
from manila.i18n import _LE
from manila.i18n import _LI
from manila.share import driver
from manila.share.drivers import glusterfs
from manila import utils

LOG = log.getLogger(__name__)

glusterfs_native_manila_share_opts = [
    cfg.ListOpt('glusterfs_servers',
                default=[],
                help='List of GlusterFS servers that can be used to create '
                     'shares. Each GlusterFS server should be of the form '
                     '[remoteuser@]<volserver>, and they are assumed to '
                     'belong to distinct Gluster clusters.'),
    cfg.StrOpt('glusterfs_native_server_password',
               default=None,
               secret=True,
               help='Remote GlusterFS server node\'s login password. '
                    'This is not required if '
                    '\'glusterfs_native_path_to_private_key\' is '
                    'configured.'),
    cfg.StrOpt('glusterfs_native_path_to_private_key',
               default=None,
               help='Path of Manila host\'s private SSH key file.'),
    cfg.StrOpt('glusterfs_volume_pattern',
               default=None,
               help='Regular expression template used to filter '
                    'GlusterFS volumes for share creation. '
                    'The regex template can contain the ${size} '
                    'parameter which matches a number (sequence of '
                    'digits) and the value shall be intepreted as '
                    'size of the volume in GB. Examples: '
                    '"manila-share-volume-\d+$", '
                    '"manila-share-volume-${size}G-\d+$"; '
                    'with matching volume names, respectively: '
                    '"manila-share-volume-12", "manila-share-volume-3G-13". '
                    'In latter example, the number that matches "${size}", '
                    'that is, 3, is an indication that the size of volume '
                    'is 3G.')
]

CONF = cfg.CONF
CONF.register_opts(glusterfs_native_manila_share_opts)

ACCESS_TYPE_CERT = 'cert'
AUTH_SSL_ALLOW = 'auth.ssl-allow'
CLIENT_SSL = 'client.ssl'
NFS_EXPORT_VOL = 'nfs.export-volumes'
SERVER_SSL = 'server.ssl'
PATTERN_DICT = {'size': {'pattern': '(?P<size>\d+)', 'trans': int}}


class GlusterfsNativeShareDriver(driver.ExecuteMixin, driver.ShareDriver):
    """GlusterFS native protocol (glusterfs) share driver.

    Executes commands relating to Shares.
    Supports working with multiple glusterfs volumes.

    API version history:

        1.0 - Initial version.
        1.1 - Support for working with multiple gluster volumes.
    """

    def __init__(self, db, *args, **kwargs):
        super(GlusterfsNativeShareDriver, self).__init__(
            False, *args, **kwargs)
        self.db = db
        self._helpers = None
        self.gluster_used_vols_dict = {}
        self.configuration.append_config_values(
            glusterfs_native_manila_share_opts)
        self.gluster_nosnap_vols_dict = {}
        self.backend_name = self.configuration.safe_get(
            'share_backend_name') or 'GlusterFS-Native'
        self.volume_pattern = self._compile_volume_pattern()
        self.volume_pattern_keys = self.volume_pattern.groupindex.keys()
        glusterfs_servers = {}
        for srvaddr in self.configuration.glusterfs_servers:
            glusterfs_servers[srvaddr] = self._glustermanager(
                srvaddr, has_volume=False)
        self.glusterfs_servers = glusterfs_servers

    def _compile_volume_pattern(self):
        """Compile a RegexObject from the config specified regex template.

        (cfg.glusterfs_volume_pattern)
        """

        subdict = {}
        for key, val in six.iteritems(PATTERN_DICT):
            subdict[key] = val['pattern']
        volume_pattern = string.Template(
            self.configuration.glusterfs_volume_pattern).substitute(subdict)
        return re.compile(volume_pattern)

    def do_setup(self, context):
        """Setup the GlusterFS volumes."""
        super(GlusterfsNativeShareDriver, self).do_setup(context)

        # We don't use a service mount as its not necessary for us.
        # Do some sanity checks.
        gluster_volumes0 = set(self._fetch_gluster_volumes())
        if not gluster_volumes0:
            # No suitable volumes are found on the Gluster end.
            # Raise exception.
            msg = (_("Gluster backend does not provide any volume "
                     "matching pattern %s"
                     ) % self.configuration.glusterfs_volume_pattern)
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        LOG.info(_LI("Found %d Gluster volumes allocated for Manila."
                     ) % len(gluster_volumes0))

        try:
            self._execute('mount.glusterfs', check_exit_code=False)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                msg = (_("mount.glusterfs is not installed."))
                LOG.error(msg)
                raise exception.GlusterfsException(msg)
            else:
                msg = (_("Error running mount.glusterfs."))
                LOG.error(msg)
                raise

        # Update gluster_used_vols_dict by walking through the DB.
        self._update_gluster_vols_dict(context)
        unused_vols = gluster_volumes0 - set(self.gluster_used_vols_dict)
        if not unused_vols:
            # No volumes available for use as share. Warn user.
            msg = (_("No unused gluster volumes available for use as share! "
                     "Create share won't be supported unless existing shares "
                     "are deleted or add one or more gluster volumes to work "
                     "with in the glusterfs_servers configuration parameter."))
            LOG.warn(msg)
        else:
            LOG.info(_LI("Number of gluster volumes in use:  "
                         "%(inuse-numvols)s. Number of gluster volumes "
                         "available for use as share: %(unused-numvols)s"),
                     {'inuse-numvols': len(self.gluster_used_vols_dict),
                     'unused-numvols': len(unused_vols)})

    def _glustermanager(self, gluster_address, has_volume=True):
        """Create GlusterManager object for gluster_address."""

        return glusterfs.GlusterManager(
            gluster_address, self._execute,
            self.configuration.glusterfs_native_path_to_private_key,
            self.configuration.glusterfs_native_server_password,
            has_volume=has_volume)

    def _fetch_gluster_volumes(self):
        """Do a 'gluster volume list | grep <volume pattern>'.

        Aggregate the results from all servers.
        Extract the named groups from the matching volume names
        using the specs given in PATTERN_DICT.
        Return a dict with keys of the form <server>:/<volname>
        and values being dicts that map names of named groups
        to their extracted value.
        """

        volumes_dict = {}
        for gsrv, gluster_mgr in six.iteritems(self.glusterfs_servers):
            try:
                out, err = gluster_mgr.gluster_call('volume', 'list')
            except exception.ProcessExecutionError as exc:
                msgdict = {'err': exc.stderr, 'hostinfo': ''}
                if gluster_mgr.remote_user:
                    msgdict['hostinfo'] = ' on host %s' % gluster_mgr.host
                LOG.error(_LE("Error retrieving volume list%(hostinfo)s: "
                              "%(err)s") % msgdict)
                raise exception.GlusterfsException(
                    'gluster volume list failed')
            for vol in out.split("\n"):
                patmatch = self.volume_pattern.match(vol)
                if not patmatch:
                    continue
                pattern_dict = {}
                for key in self.volume_pattern_keys:
                    keymatch = patmatch.group(key)
                    if keymatch is None:
                        pattern_dict[key] = None
                    else:
                        trans = PATTERN_DICT[key].get('trans', lambda x: x)
                        pattern_dict[key] = trans(keymatch)
                volumes_dict[gsrv + ':/' + vol] = pattern_dict
        return volumes_dict

    @utils.synchronized("glusterfs_native", external=False)
    def _update_gluster_vols_dict(self, context):
        """Update dict of gluster vols that are used/unused."""

        shares = self.db.share_get_all(context)

        for s in shares:
            vol = s['export_location']
            gluster_mgr = self._glustermanager(vol)
            self.gluster_used_vols_dict[vol] = gluster_mgr

    def _setup_gluster_vol(self, vol):
        # Enable gluster volumes for SSL access only.

        for option, value in six.iteritems(
            {NFS_EXPORT_VOL: 'off', CLIENT_SSL: 'on', SERVER_SSL: 'on'}
        ):
            gluster_mgr = self._glustermanager(vol)
            try:
                gluster_mgr.gluster_call(
                    'volume', 'set', gluster_mgr.volume,
                    option, 'value')
            except exception.ProcessExecutionError as exc:
                msg = (_("Error in gluster volume set during volume setup. "
                         "volume: %(volname)s, option: %(option)s, "
                         "value: %(value)s, error: %(error)s") %
                       {'volname': gluster_mgr.volume,
                        'option': option, 'value': value, 'error': exc.stderr})
                LOG.error(msg)
                raise exception.GlusterfsException(msg)

            # TODO(deepakcs) Remove this once ssl options can be
            # set dynamically.
            self._restart_gluster_vol(gluster_mgr)

    @staticmethod
    def _restart_gluster_vol(gluster_mgr):
        try:
            # XXX Why '--mode=script' ?
            gluster_mgr.gluster_call(
                'volume', 'stop', gluster_mgr.volume, '--mode=script')
        except exception.ProcessExecutionError as exc:
            msg = (_("Error stopping gluster volume. "
                     "Volume: %(volname)s, Error: %(error)s"),
                   {'volname': gluster_mgr.volume, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        try:
            gluster_mgr.gluster_call(
                'volume', 'start', gluster_mgr.volume)
        except exception.ProcessExecutionError as exc:
            msg = (_("Error starting gluster volume. "
                     "Volume: %(volname)s, Error: %(error)s"),
                   {'volname': gluster_mgr.volume, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

    @utils.synchronized("glusterfs_native", external=False)
    def _pop_gluster_vol(self, size=None):
        """Pick an unbound volume.

        Do a _fetch_gluster_volumes() first to get the complete
        list of usable volumes.
        Keep only the unbound ones (ones that are not yet used to
        back a share).
        If size is given, try to pick one which has a size specification
        (according to the 'size' named group of the volume pattern),
        and its size is greater-than-or-equal to the given size.
        Return the volume chosen (in <host>:/<volname> format).
        """

        voldict = self._fetch_gluster_volumes()
        # calculate the set of unused volumes
        set1, set2 = (
            set(d) for d in (voldict, self.gluster_used_vols_dict)
        )
        unused_vols = set1 - set2
        # if both caller has specified size and 'size' occurs as
        # a parameter in the volume pattern...
        if size and 'size' in self.volume_pattern_keys:
            # ... then create a list that stores those of the unused volumes
            # (along with their sizes) which are indicated to have a suitable
            # size; and another list for the ones for which no size is given.
            sized_unused_vols, unsized_unused_vols = [], []
            for vol in unused_vols:
                volsize = voldict[vol]['size']
                if volsize:
                    if volsize >= size:
                        sized_unused_vols.append([volsize, vol])
                else:
                    unsized_unused_vols.append(vol)
        else:
            # ... else just use a stub for the "sized" list
            # and put all unused ones to the "unsized" list
            sized_unused_vols = []
            unsized_unused_vols = unused_vols

        if sized_unused_vols:
            sized_unused_vols.sort()
            vol = sized_unused_vols[0][1]
        elif unsized_unused_vols:
            vol = unsized_unused_vols[0]
        else:
            msg = (_("Couldn't find a free gluster volume to use."))
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        self._setup_gluster_vol(vol)
        self.gluster_used_vols_dict[vol] = self._glustermanager[vol]
        return vol

    @utils.synchronized("glusterfs_native", external=False)
    def _push_gluster_vol(self, exp_locn):
        try:
            self.gluster_used_vols_dict.pop(exp_locn)
        except KeyError:
            msg = (_("Couldn't find the share in used list."))
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

    def _do_mount(self, gluster_export, mntdir):

        cmd = ['mount', '-t', 'glusterfs', gluster_export, mntdir]
        try:
            self._execute(*cmd, run_as_root=True)
        except exception.ProcessExecutionError as exc:
            msg = (_("Unable to mount gluster volume. "
                     "gluster_export: %(export)s, Error: %(error)s"),
                   {'export': gluster_export, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

    def _do_umount(self, mntdir):

        cmd = ['umount', mntdir]
        try:
            self._execute(*cmd, run_as_root=True)
        except exception.ProcessExecutionError as exc:
            msg = (_("Unable to unmount gluster volume. "
                     "mount_dir: %(mntdir)s, Error: %(error)s"),
                   {'mntdir': mntdir, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

    def _wipe_gluster_vol(self, gluster_mgr):

        # Reset the SSL options.
        try:
            gluster_mgr.gluster_call(
                'volume', 'set', gluster_mgr.volume,
                CLIENT_SSL, 'off')
        except exception.ProcessExecutionError as exc:
            msg = (_("Error in gluster volume set during _wipe_gluster_vol. "
                     "Volume: %(volname)s, Option: %(option)s, "
                     "Error: %(error)s"),
                   {'volname': gluster_mgr.volume,
                    'option': CLIENT_SSL, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        try:
            gluster_mgr.gluster_call(
                'volume', 'set', gluster_mgr.volume,
                SERVER_SSL, 'off')
        except exception.ProcessExecutionError as exc:
            msg = (_("Error in gluster volume set during _wipe_gluster_vol. "
                     "Volume: %(volname)s, Option: %(option)s, "
                     "Error: %(error)s"),
                   {'volname': gluster_mgr.volume,
                    'option': SERVER_SSL, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        self._restart_gluster_vol(gluster_mgr)

        # Create a temporary mount.
        gluster_export = gluster_mgr.export
        tmpdir = tempfile.mkdtemp()
        try:
            self._do_mount(gluster_export, tmpdir)
        except exception.GlusterfsException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

        # Delete only the contents, not the directory.
        cmd = ['find', pipes.quote(tmpdir), '-mindepth', '1', '-delete']
        try:
            self._execute(*cmd, run_as_root=True)
        except exception.ProcessExecutionError as exc:
            msg = (_("Error trying to wipe gluster volume. "
                     "gluster_export: %(export)s, Error: %(error)s"),
                   {'export': gluster_export, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)
        finally:
            # Unmount.
            self._do_umount(tmpdir)
            shutil.rmtree(tmpdir, ignore_errors=True)

        # Set the SSL options.
        try:
            gluster_mgr.gluster_call(
                'volume', 'set', gluster_mgr.volume,
                CLIENT_SSL, 'on')
        except exception.ProcessExecutionError as exc:
            msg = (_("Error in gluster volume set during _wipe_gluster_vol. "
                     "Volume: %(volname)s, Option: %(option)s, "
                     "Error: %(error)s"),
                   {'volname': gluster_mgr.volume,
                    'option': CLIENT_SSL, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        try:
            gluster_mgr.gluster_call(
                'volume', 'set', gluster_mgr.volume,
                SERVER_SSL, 'on')
        except exception.ProcessExecutionError as exc:
            msg = (_("Error in gluster volume set during _wipe_gluster_vol. "
                     "Volume: %(volname)s, Option: %(option)s, "
                     "Error: %(error)s"),
                   {'volname': gluster_mgr.volume,
                    'option': SERVER_SSL, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        self._restart_gluster_vol(gluster_mgr)

    def get_network_allocations_number(self):
        return 0

    def create_share(self, context, share, share_server=None):
        """Create a share using GlusterFS volume.

        1 Manila share = 1 GlusterFS volume. Pick an unused
        GlusterFS volume for use as a share.
        """
        try:
            export_location = self._pop_gluster_vol(share['size'])
        except exception.GlusterfsException:
            msg = (_("Error creating share %(share_id)s"),
                   {'share_id': share['id']})
            LOG.error(msg)
            raise

        # TODO(deepakcs): Enable quota and set it to the share size.

        # For native protocol, the export_location should be of the form:
        # server:/volname
        LOG.info(_LI("export_location sent back from create_share: %s"),
                 (export_location,))
        return export_location

    def delete_share(self, context, share, share_server=None):
        """Delete a share on the GlusterFS volume.

        1 Manila share = 1 GlusterFS volume. Put the gluster
        volume back in the available list.
        """
        exp_locn = share.get('export_location', None)
        try:
            # Get the gluster address associated with the export.
            gmgr = self.gluster_used_vols_dict[exp_locn]
        except KeyError:
            msg = (_("Invalid request. Ignoring delete_share request for "
                     "share %(share_id)s"), {'share_id': share['id']},)
            LOG.warn(msg)
            return

        try:
            self._wipe_gluster_vol(gmgr)
            self._push_gluster_vol(exp_locn)
        except exception.GlusterfsException:
            msg = (_("Error during delete_share request for "
                     "share %(share_id)s"), {'share_id': share['id']},)
            LOG.error(msg)
            raise

        # TODO(deepakcs): Disable quota.

    def create_snapshot(self, context, snapshot, share_server=None):
        """Creates a snapshot."""
        # FIXME: need to access db to retrieve share data
        vol = self.db.share_get(context,
                                snapshot['share_id'])['export_location']
        if vol in self.gluster_nosnap_vols_dict:
            opret, operrno = -1, 0
            operrstr = self.gluster_nosnap_vols_dict[vol]
        else:
            gluster_mgr = self.gluster_used_vols_dict[vol]
            args = ('--xml', 'snapshot', 'create', snapshot['id'],
                    gluster_mgr.volume)
            try:
                out, err = gluster_mgr.gluster_call(*args)
            except exception.ProcessExecutionError as exc:
                LOG.error(_LE("Error retrieving volume info: %s"), exc.stderr)
                raise exception.GlusterfsException("gluster %s failed" %
                                                   ' '.join(args))

            if not out:
                raise exception.GlusterfsException(
                    'gluster volume info %s: no data received' %
                    gluster_mgr.volume
                )

            outxml = etree.fromstring(out)
            opret = int(outxml.find('opRet').text)
            operrno = int(outxml.find('opErrno').text)
            operrstr = outxml.find('opErrstr').text

        if opret == -1 and operrno == 0:
            self.gluster_nosnap_vols_dict[vol] = operrstr
            msg = _("Share %(share_id)s does not support snapshots: "
                    "%(errstr)s.") % {'share_id': snapshot['share_id'],
                                      'errstr': operrstr}
            LOG.error(msg)
            raise exception.ShareSnapshotNotSupported(msg)
        elif operrno:
            raise exception.GlusterfsException(
                _("Creating snapshot for share %(share_id)s failed "
                  "with %(errno)d: %(errstr)s") % {
                      'share_id': snapshot['share_id'],
                      'errno': operrno,
                      'errstr': operrstr})

    def delete_snapshot(self, context, snapshot, share_server=None):
        """Deletes a snapshot."""
        # FIXME: need to access db to retrieve share data
        vol = self.db.share_get(context,
                                snapshot['share_id'])['export_location']
        gluster_mgr = self.gluster_used_vols_dict[vol]
        args = ('--xml', 'snapshot', 'delete', snapshot['id'])
        try:
            out, err = gluster_mgr.gluster_call(*args)
        except exception.ProcessExecutionError as exc:
            LOG.error(_LE("Error retrieving volume info: %s"), exc.stderr)
            raise exception.GlusterfsException("gluster %s failed" %
                                               ' '.join(args))

        if not out:
            raise exception.GlusterfsException(
                'gluster volume info %s: no data received' %
                gluster_mgr.volume
            )

        outxml = etree.fromstring(out)
        opret = int(outxml.find('opRet').text)
        operrno = int(outxml.find('opErrno').text)
        operrstr = outxml.find('opErrstr').text

        if opret:
            raise exception.GlusterfsException(
                _("Deleting snapshot %(snap_id)s of share %(share_id)s failed "
                  "with %(errno)d: %(errstr)s") % {
                      'snap_id': snapshot['id'],
                      'share_id': snapshot['share_id'],
                      'errno': operrno,
                      'errstr': operrstr})

    def allow_access(self, context, share, access, share_server=None):
        """Allow access to a share using certs.

        Add the SSL CN (Common Name) that's allowed to access the server.
        """

        if access['access_type'] != ACCESS_TYPE_CERT:
            raise exception.InvalidShareAccess(_("Only 'cert' access type "
                                                 "allowed"))
        exp_locn = share.get('export_location', None)
        gluster_mgr = self.gluster_used_vols_dict.get(exp_locn)

        try:
            gluster_mgr.gluster_call(
                'volume', 'set', gluster_mgr.volume,
                AUTH_SSL_ALLOW, access['access_to'])
        except exception.ProcessExecutionError as exc:
            msg = (_("Error in gluster volume set during allow access. "
                     "Volume: %(volname)s, Option: %(option)s, "
                     "access_to: %(access_to)s, Error: %(error)s"),
                   {'volname': gluster_mgr.volume,
                    'option': AUTH_SSL_ALLOW,
                    'access_to': access['access_to'], 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        # TODO(deepakcs) Remove this once ssl options can be
        # set dynamically.
        self._restart_gluster_vol(gluster_mgr)

    def deny_access(self, context, share, access, share_server=None):
        """Deny access to a share that's using cert based auth.

        Remove the SSL CN (Common Name) that's allowed to access the server.
        """

        if access['access_type'] != ACCESS_TYPE_CERT:
            raise exception.InvalidShareAccess(_("Only 'cert' access type "
                                                 "allowed for access "
                                                 "removal."))
        exp_locn = share.get('export_location', None)
        gluster_mgr = self.gluster_used_vols_dict.get(exp_locn)

        try:
            gluster_mgr.gluster_call(
                'volume', 'reset', gluster_mgr.volume,
                AUTH_SSL_ALLOW)
        except exception.ProcessExecutionError as exc:
            msg = (_("Error in gluster volume reset during deny access. "
                     "Volume: %(volname)s, Option: %(option)s, "
                     "Error: %(error)s"),
                   {'volname': gluster_mgr.volume,
                    'option': AUTH_SSL_ALLOW, 'error': exc.stderr})
            LOG.error(msg)
            raise exception.GlusterfsException(msg)

        # TODO(deepakcs) Remove this once ssl options can be
        # set dynamically.
        self._restart_gluster_vol(gluster_mgr)

    def _update_share_stats(self):
        """Send stats info for the GlusterFS volume."""

        data = dict(
            share_backend_name=self.backend_name,
            vendor_name='Red Hat',
            driver_version='1.1',
            storage_protocol='glusterfs',
            reserved_percentage=self.configuration.reserved_share_percentage)

        # We don't use a service mount to get stats data.
        # Instead we use glusterfs quota feature and use that to limit
        # the share to its expected share['size'].

        # TODO(deepakcs): Change below once glusterfs supports volume
        # specific stats via the gluster cli.
        data['total_capacity_gb'] = 'infinite'
        data['free_capacity_gb'] = 'infinite'

        super(GlusterfsNativeShareDriver, self)._update_share_stats(data)

    def ensure_share(self, context, share, share_server=None):
        """Invoked to ensure that share is exported."""
        pass
